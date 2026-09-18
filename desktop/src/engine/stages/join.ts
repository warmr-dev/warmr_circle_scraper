import type { StageContext, StageResult } from '../context'
import {
  attemptJoin,
  EgoBridgeError,
  openTaskSpace,
  resolveEgoBin,
  TERMINAL_JOIN_STATUSES,
  type EgoJoinResult
} from '../join/ego'
import {
  closeTasks,
  connectSession,
  joinCandidates,
  openTask,
  persistJoinOutcome,
  recordJoinAttempt,
  visitsToday
} from '../db/content'
import { logActivity } from '../db/repo'
import { hostFromUrl, randomBetween, sleep } from '../util'
import { circleGovernor } from '../circle/governor'

export const JOIN_ACCOUNT = 'main'
const SPACE_NAME = 'warmr auto-join (main)'

const HANDOFF_TITLES: Record<string, string> = {
  needs_login: 'Нужно войти в аккаунт Circle',
  challenge_stop: 'Проверка «вы человек?» — пройдите её сами',
  application_form_detected: 'Анкета при вступлении — ответьте сами',
  unclear: 'Непонятная страница — посмотрите сами'
}

/** Pick the host the cookies belong to: the page's real final host, not the candidate URL. */
export function cookieHost(result: EgoJoinResult, fallbackUrl: string): string | null {
  const fromCookie = result.cookies?.map((c) => (c.domain || '').replace(/^\./, '').toLowerCase()).find((d) => d && d !== 'circle.so')
  return fromCookie || hostFromUrl(fallbackUrl)
}

/**
 * Stage 3: join ICP-fit communities in the user's own ego lite browser.
 * Pacing (WORKLOG 2026-09-16): ~94 visits/day from one account made 3 hosts
 * demand /two_fa, so every page opened counts toward the daily cap, the cap
 * lives in the database (shared by every machine), and attempts are 30-90 s
 * apart. Anything that needs a human becomes a task; nothing is bypassed.
 */
export async function runJoin(ctx: StageContext, opts: { communityId?: number } = {}): Promise<StageResult> {
  const { sql, settings } = ctx
  const counts: Record<string, number> = {}
  if (process.platform !== 'darwin') {
    throw new Error('Автоджойн работает через ego lite, а он есть только для macOS. На Windows используйте «Вступить вручную» в карточке сообщества.')
  }
  const bin = await resolveEgoBin(ctx.local.egoPath)
  if (!bin) throw new Error('ego-browser не найден. Установите ego lite (lite.ego.app) и пройдите первый запуск.')
  const cooling = circleGovernor.coolingDownUntil
  if (cooling) {
    return { summary: `пауза: Circle ограничил запросы с этого компьютера до ${cooling.toLocaleTimeString('ru-RU')}`, counts }
  }

  const cap = settings.join.maxVisitsPerDay
  let used = await visitsToday(sql, JOIN_ACCOUNT)
  if (used >= cap) {
    return { summary: `дневной лимит уже исчерпан (${used}/${cap})`, counts: { visitsToday: used } }
  }
  const limit = opts.communityId ? 1 : Math.min(settings.join.batchSize, cap - used)
  const candidates = await joinCandidates(sql, {
    limit,
    includePaid: settings.join.includePaid,
    approvedOnly: settings.join.approvedOnly,
    communityId: opts.communityId
  })
  if (!candidates.length) {
    if (opts.communityId) return { summary: 'уже состоим в этом сообществе или вступление уже пробовали', counts }
    return {
      summary: settings.join.approvedOnly
        ? 'очередь пуста: нет сообществ, одобренных LLM или вами'
        : 'очередь на вступление пуста',
      counts
    }
  }

  const email = ctx.secrets.circleEmail || null
  const password = ctx.secrets.circlePassword || null
  let spaceId = ctx.local.egoSpaceId
  if (spaceId == null) {
    spaceId = await openTaskSpace(bin, SPACE_NAME)
    await ctx.updateLocal({ egoSpaceId: spaceId })
    ctx.log('info', `Создал рабочее пространство ego #${spaceId}`)
  }
  ctx.log('info', `Вступаю: ${candidates.length} сообществ, визитов сегодня ${used}/${cap}`)

  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i]!
    if (used >= cap) {
      ctx.log('warn', `Дневной лимит визитов достигнут (${used}/${cap})`)
      break
    }
    ctx.progress(i, candidates.length, c.name || c.url)
    let result: EgoJoinResult
    try {
      result = await attemptJoin(bin, { spaceId, url: c.url, email, password, screenshotDir: ctx.screenshotsDir }, ctx.signal)
    } catch (err) {
      if (ctx.signal.aborted) throw err
      const message = err instanceof Error ? err.message : String(err)
      // A space closed by the user is recoverable once; anything else stops the batch
      // (the bridge itself is broken, not this community).
      if (err instanceof EgoBridgeError && /space|not found|no such/i.test(message) && !counts.spaceRecreated) {
        counts.spaceRecreated = 1
        spaceId = await openTaskSpace(bin, SPACE_NAME)
        await ctx.updateLocal({ egoSpaceId: spaceId })
        ctx.log('warn', `Пространство ego пересоздано (#${spaceId}) после ошибки: ${message.slice(0, 200)}`)
        i--
        continue
      }
      ctx.log('error', `ego-browser: ${message.slice(0, 400)}`)
      counts.bridgeErrors = (counts.bridgeErrors ?? 0) + 1
      break
    }
    used++
    const terminal = TERMINAL_JOIN_STATUSES.has(result.status)
    await recordJoinAttempt(sql, {
      communityId: c.id,
      host: hostFromUrl(c.url),
      url: c.url,
      account: JOIN_ACCOUNT,
      status: result.status,
      terminal,
      detail: result.detail,
      deviceId: ctx.deviceId
    })
    counts[result.status] = (counts[result.status] ?? 0) + 1

    if (terminal) {
      await persistJoinOutcome(sql, c.id, result.status, result.detail)
      await closeTasks(sql, c.id, null, ['join_handoff'])
      if (result.status === 'joined') {
        const host = cookieHost(result, c.url)
        if (result.cookies?.length && host) {
          await connectSession(sql, { host, cookies: result.cookies, memberLabel: c.name, communityId: c.id, source: 'desktop_join' })
          ctx.log('success', `Вступил: ${c.name || c.url} — куки сохранены (${host})`)
        } else {
          await openTask(sql, {
            kind: 'session_missing',
            communityId: c.id,
            host,
            title: 'Вступили, но сессия не сохранилась',
            detail: 'Откройте сообщество в ego lite и нажмите «Вступить вручную» в карточке, чтобы приложение забрало куки.'
          })
          ctx.log('warn', `Вступил в ${c.name || c.url}, но куки не получены`)
        }
      } else {
        ctx.log('info', `${c.name || c.url}: ${result.status} — ${result.detail.slice(0, 160)}`)
      }
    } else {
      await openTask(sql, {
        kind: 'join_handoff',
        communityId: c.id,
        host: hostFromUrl(c.url),
        title: HANDOFF_TITLES[result.status] ?? `Нужно ваше участие (${result.status})`,
        detail: result.detail
      })
      ctx.log('warn', `${c.name || c.url}: нужен человек (${result.status})`)
      if (result.status === 'challenge_stop') {
        // Cloudflare is checking this IP: every next page would be challenged
        // too and still count as a visit. Stop and let the cool-down run.
        circleGovernor.trip('challenge page in ego lite during join')
        counts.stoppedByChallenge = 1
      }
    }
    await logActivity(sql, {
      kind: 'join',
      level: result.status === 'joined' ? 'success' : terminal ? 'info' : 'warning',
      community: c.slug,
      summary: `Desktop join ${result.status}: ${result.detail.slice(0, 300)}`,
      detail: { status: result.status, url: c.url }
    })
    ctx.progress(i + 1, candidates.length, c.name || c.url)
    if (counts.stoppedByChallenge) break
    if (i < candidates.length - 1 && used < cap) {
      const delay = (settings.join.minDelaySec + randomBetween(0, settings.join.jitterSec)) * 1000
      await sleep(delay, ctx.signal)
    }
  }

  counts.visitsToday = used
  const summary =
    `попыток ${Object.entries(counts).filter(([k]) => !['visitsToday', 'bridgeErrors', 'spaceRecreated', 'stoppedByChallenge'].includes(k)).reduce((a, [, v]) => a + v, 0)}, ` +
    `вступил ${counts.joined ?? 0}, платных пропущено ${counts.paid_skip ?? 0}, ждут одобрения ${counts.pending_approval ?? 0}, ` +
    `нужен человек ${(counts.needs_login ?? 0) + (counts.challenge_stop ?? 0) + (counts.application_form_detected ?? 0) + (counts.unclear ?? 0)}; ` +
    `визитов сегодня ${used}/${cap}`
  return { summary, counts }
}
