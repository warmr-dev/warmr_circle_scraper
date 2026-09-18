import { createHash } from 'node:crypto'
import type { StageContext, StageResult } from '../context'
import { fetchPublicSpaces, priceLabelFallback, probeHost } from '../circle/probe'
import { icpCandidates, probeCandidates, saveIcp, saveProbe, logActivity, type ProbeCandidate } from '../db/repo'
import { llmSpendToday, recordLlmCall } from '../db/content'
import { decideIcp, ICP_VERSION } from '../icp/classify'
import { hasText } from '../icp/rules'
import type { LlmConfig } from '../llm/client'
import { hostFromUrl, mapPool, throwIfAborted } from '../util'
import { RateLimitedError } from '../circle/governor'
import type { LlmProvider, SharedSettings } from '../../shared/types'
import type { EngineSecrets } from '../context'

export function llmConfigFrom(settings: SharedSettings, secrets: EngineSecrets): LlmConfig | null {
  if (!settings.icp.useLlm) return null
  const keys: Record<LlmProvider, string | undefined> = {
    openrouter: secrets.openrouterKey,
    openai: secrets.openaiKey,
    anthropic: secrets.anthropicKey
  }
  const apiKey = keys[settings.llm.provider]
  if (!apiKey) return null
  return { provider: settings.llm.provider, model: settings.llm.model, apiKey }
}

/** Changing the profile, model or mode re-judges every community once. */
export function icpVersionFor(settings: SharedSettings, llm: LlmConfig | null): string {
  const basis = JSON.stringify({
    profile: settings.icp.profile.trim(),
    mode: settings.icp.mode,
    llm: llm ? `${llm.provider}:${llm.model}` : 'rules-only',
    minConfidence: settings.icp.minConfidence,
    autoApprove: settings.icp.autoApproveLlm
  })
  return `${ICP_VERSION}:${createHash('sha1').update(basis).digest('hex').slice(0, 10)}`
}

async function probeOne(ctx: StageContext, candidate: ProbeCandidate, counts: Record<string, number>): Promise<void> {
  const host = hostFromUrl(candidate.url)
  if (!host) {
    counts.skipped = (counts.skipped ?? 0) + 1
    return
  }
  const probe = await probeHost(host, ctx.signal)
  let spaces = null
  if (probe.exists === 'alive' && !probe.isPrivate) {
    spaces = await fetchPublicSpaces(probe.answeredHost, ctx.signal)
  }
  const fallback = probe.exists !== 'alive' ? priceLabelFallback(candidate.priceLabel) : null
  await saveProbe(ctx.sql, candidate, probe, spaces, fallback)
  counts.probed = (counts.probed ?? 0) + 1
  const key = probe.exists === 'locked_or_absent' ? 'locked' : probe.exists === 'not_circle' ? 'notCircle' : probe.exists
  counts[key] = (counts[key] ?? 0) + 1
}

/**
 * Stage 2: does it exist, how can it be joined, does it fit.
 * Probe (one anonymous JSON call per host, 2-4 in flight: WORKLOG 2026-09-15
 * measured 12 threads losing 72% of responses silently) + public space names,
 * then ICP: rules, then the LLM for everything with text.
 */
export async function runVerify(ctx: StageContext, opts: { communityIds?: number[] } = {}): Promise<StageResult> {
  const { sql, settings } = ctx
  const counts: Record<string, number> = {}

  const candidates = await probeCandidates(sql, {
    limit: opts.communityIds?.length ? opts.communityIds.length : settings.verify.batchSize,
    reprobeAliveDays: settings.verify.reprobeAliveDays,
    reprobeOtherDays: settings.verify.reprobeOtherDays,
    ids: opts.communityIds
  })
  if (candidates.length) ctx.log('info', `Проверяю существование: ${candidates.length} сообществ`)
  let done = 0
  try {
    await mapPool(
      candidates,
      Math.max(1, Math.min(4, settings.verify.concurrency)),
      async (candidate) => {
        try {
          await probeOne(ctx, candidate, counts)
        } catch (err) {
          if (ctx.signal.aborted || err instanceof RateLimitedError) throw err
          counts.errors = (counts.errors ?? 0) + 1
          ctx.log('warn', `Проверка ${candidate.url}: ${err instanceof Error ? err.message : String(err)}`)
        }
        done++
        ctx.progress(done, candidates.length, 'проверка')
      },
      { signal: ctx.signal }
    )
  } catch (err) {
    if (!(err instanceof RateLimitedError)) throw err
    // Nothing is recorded for the hosts we did not reach; ICP (LLM only) still runs.
    counts.rateLimited = 1
    ctx.log('warn', `${err.message}. Проверку продолжу после паузы, оценку ICP делаю сейчас.`)
  }

  throwIfAborted(ctx.signal)
  const llm = llmConfigFrom(settings, ctx.secrets)
  const version = icpVersionFor(settings, llm)
  let spent = await llmSpendToday(sql)
  // Out of budget: a row judged now would keep a rules-only verdict for good
  // (and never reach the join queue), so rows with text wait for tomorrow.
  const budgetOut = Boolean(llm) && spent >= settings.llm.dailyBudgetUsd
  const judged = await icpCandidates(sql, {
    limit: opts.communityIds?.length ? opts.communityIds.length : settings.icp.batchSize,
    version,
    withLlm: Boolean(llm),
    ids: opts.communityIds,
    textlessOnly: budgetOut && !opts.communityIds?.length
  })
  if (budgetOut) ctx.log('info', `Дневной бюджет LLM ($${settings.llm.dailyBudgetUsd}) исчерпан: сообщества с текстом оценю завтра`)
  if (judged.length) {
    ctx.log('info', `Оцениваю соответствие ICP: ${judged.length} сообществ${llm ? ` (LLM ${llm.model})` : ' (только правила)'}`)
  }
  let icpDone = 0
  let llmDown = false
  let unavailable = 0
  await mapPool(
    judged,
    llm ? 3 : 1,
    async (c) => {
      const input = {
        name: c.name,
        description: c.description,
        spaceNames: c.spaceNames,
        goals: c.goals,
        host: hostFromUrl(c.url),
        priceLabel: c.priceLabel,
        joinType: c.joinType,
        membersTotal: c.membersTotal
      }
      const defer = (): void => {
        // Not this community's fault: leave it due and judge it with the LLM later.
        counts.llmDeferred = (counts.llmDeferred ?? 0) + 1
        icpDone++
        ctx.progress(icpDone, judged.length, 'оценка ICP')
      }
      if (llm && llmDown && hasText(input)) return defer()
      const decision = await decideIcp(input, settings.icp, llm, { budgetOk: spent < settings.llm.dailyBudgetUsd, signal: ctx.signal })
      if (llm && decision.llmRetryable) {
        await recordLlmCall(sql, {
          purpose: 'icp',
          communityId: c.id,
          provider: llm.provider,
          model: llm.model,
          inputTokens: 0,
          outputTokens: 0,
          usd: 0,
          ok: false,
          error: decision.llmError
        })
        unavailable++
        if (unavailable === 1) ctx.log('warn', `LLM недоступен: ${decision.llmError}`)
        if (unavailable >= 3 && !llmDown) {
          llmDown = true
          ctx.log('warn', 'LLM не ответил три раза подряд: сообщества с текстом оценю в следующий запуск')
        }
        return defer()
      }
      if (llm && decision.skippedLlm === 'budget') return defer()
      unavailable = 0
      if (decision.llm && llm) {
        spent += decision.llm.usage.usd
        counts.llmCalls = (counts.llmCalls ?? 0) + 1
        await recordLlmCall(sql, {
          purpose: 'icp',
          communityId: c.id,
          provider: llm.provider,
          model: decision.llm.model,
          inputTokens: decision.llm.usage.inputTokens,
          outputTokens: decision.llm.usage.outputTokens,
          usd: decision.llm.usage.usd,
          ok: true,
          error: null
        })
      } else if (decision.llmError && llm) {
        counts.llmErrors = (counts.llmErrors ?? 0) + 1
        await recordLlmCall(sql, {
          purpose: 'icp',
          communityId: c.id,
          provider: llm.provider,
          model: llm.model,
          inputTokens: 0,
          outputTokens: 0,
          usd: 0,
          ok: false,
          error: decision.llmError
        })
        if ((counts.llmErrors ?? 0) <= 3) ctx.log('warn', `LLM не ответил для ${c.url}: ${decision.llmError}`)
      }
      await saveIcp(sql, c.id, decision, version)
      counts.icpJudged = (counts.icpJudged ?? 0) + 1
      if (decision.flag) counts.icpFit = (counts.icpFit ?? 0) + 1
      icpDone++
      ctx.progress(icpDone, judged.length, 'оценка ICP')
    },
    { signal: ctx.signal }
  )

  const summary =
    `проверено ${counts.probed ?? 0} (живых ${counts.alive ?? 0}, закрыто/нет данных ${counts.locked ?? 0}, ` +
    `недоступно ${counts.unreachable ?? 0}, не Circle ${counts.notCircle ?? 0}); ` +
    `ICP оценено ${counts.icpJudged ?? 0}, подходит ${counts.icpFit ?? 0}` +
    (counts.llmCalls ? `, LLM-вызовов ${counts.llmCalls}` : '') +
    (counts.llmDeferred ? `, отложено до LLM ${counts.llmDeferred}` : '') +
    (counts.rateLimited ? '; проверка приостановлена ограничением Circle' : '')
  if ((counts.probed ?? 0) + (counts.icpJudged ?? 0) > 0) {
    await logActivity(sql, { kind: 'classify', level: 'info', summary: `Desktop verify: ${summary}`, detail: counts, itemsSeen: counts.probed ?? 0 })
  }
  return { summary, counts }
}
