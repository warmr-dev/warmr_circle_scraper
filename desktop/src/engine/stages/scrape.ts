import type { StageContext, StageResult } from '../context'
import {
  BudgetExhaustedError,
  CircleReader,
  SessionInvalidError,
  sessionCookies,
  spaceHasPosts,
  type CircleSpace
} from '../circle/reader'
import { RateLimitedError } from '../circle/governor'
import { composeContent, contentHash, extractText, parseTimestamp, redactPii, simhash, storedContent, storedSourceId } from '../circle/text'
import {
  closeTasks,
  commentsFetched,
  communityForHost,
  getSpaceSync,
  linkConnection,
  markConnection,
  markScraped,
  openTask,
  parseCookieBlob,
  savePostMeta,
  saveSpaceSync,
  scrapeTargets,
  upsertAuthors,
  upsertPosts,
  upsertSpace,
  type AuthorInput,
  type PostInput,
  type PostMetaInput,
  type ScrapeTarget
} from '../db/content'
import { logActivity } from '../db/repo'
import { daysAgo } from '../util'

type Json = Record<string, unknown>

/**
 * One post or comment ready to store. The row key (sourceContentId) is
 * Python's content key; Circle's own id travels alongside for API calls and
 * for warmr_app.post_meta.
 */
export interface NormalizedItem extends PostInput {
  circleId: string
  kind: 'post' | 'comment'
  author: AuthorInput | null
}

interface CommunityTotals {
  spaces: number
  readable: number
  newPosts: number
  seenPosts: number
  comments: number
  complete: boolean
}

function authorOf(record: Json): AuthorInput | null {
  const raw = (record.user || record.community_member || record.author) as Json | undefined
  if (!raw || typeof raw !== 'object' || raw.id == null) return null
  return {
    sourceAuthorId: String(raw.id),
    displayName: (raw.name as string) || (raw.full_name as string) || (raw.display_name as string) || null,
    profileUrl: (raw.url as string) || (raw.profile_url as string) || null
  }
}

function absolute(url: unknown, base: string): string | null {
  if (typeof url !== 'string' || !url) return null
  return url.startsWith('/') ? `${base}${url}` : url
}

export function postPermalink(record: Json, space: CircleSpace, base: string): string | null {
  const direct = absolute(record.url, base)
  if (direct) return direct
  if (typeof record.slug === 'string' && record.slug && space.slug) return `${base}/c/${space.slug}/${record.slug}`
  return null
}

export function normalizePost(record: Json, space: CircleSpace, base: string, permission: string): (NormalizedItem & { commentsCount: number; createdAt: Date | null }) | null {
  const id = record.id ?? record.post_id
  if (id == null) return null
  const { title, body } = extractText(record)
  const content = storedContent(title || null, redactPii(composeContent(title, body)))
  const createdAt = parseTimestamp(record.created_at ?? record.published_at)
  return {
    circleId: String(id),
    kind: 'post',
    sourceContentId: storedSourceId(content),
    contentType: 'post',
    threadId: String(id),
    spacePk: null,
    authorPk: null,
    title: title || null,
    content,
    url: postPermalink(record, space, base),
    publishedAt: createdAt,
    dedupHash: contentHash(content),
    simhash: simhash(content),
    permissionReference: permission,
    author: authorOf(record),
    commentsCount: typeof record.comments_count === 'number' ? record.comments_count : 0,
    createdAt
  }
}

export function normalizeComment(record: Json, threadId: string, base: string, permission: string, fallbackUrl: string | null): NormalizedItem | null {
  if (record.id == null) return null
  const { body } = extractText(record)
  const content = redactPii(body)
  if (!content.trim()) return null
  return {
    circleId: String(record.id),
    kind: 'comment',
    sourceContentId: storedSourceId(content),
    contentType: 'post',
    threadId,
    spacePk: null,
    authorPk: null,
    title: null,
    content,
    url: absolute(record.show_url, base) || absolute(record.url, base) || fallbackUrl,
    publishedAt: parseTimestamp(record.created_at),
    dedupHash: contentHash(content),
    simhash: simhash(content),
    permissionReference: permission,
    author: authorOf(record)
  }
}

/**
 * Store a batch of normalized items with authors resolved first. Returns the
 * row id for each Circle id (two identical comments share one row, as in
 * Python).
 */
async function store(
  ctx: StageContext,
  communityId: number,
  spacePk: number | null,
  items: NormalizedItem[]
): Promise<{ inserted: number; ids: Map<string, number> }> {
  const authors = await upsertAuthors(
    ctx.sql,
    communityId,
    items.map((i) => i.author).filter((a): a is AuthorInput => Boolean(a))
  )
  const rows: PostInput[] = items.map((i) => ({
    ...i,
    spacePk,
    authorPk: i.author ? (authors.get(i.author.sourceAuthorId) ?? null) : null
  }))
  const saved = await upsertPosts(ctx.sql, communityId, rows)
  const rowByKey = new Map(saved.map((s) => [s.sourceContentId, s.id]))
  const ids = new Map<string, number>()
  for (const item of items) {
    const rowId = rowByKey.get(item.sourceContentId)
    if (rowId != null) ids.set(item.circleId, rowId)
  }
  return { inserted: saved.filter((s) => s.inserted).length, ids }
}

async function readComments(
  ctx: StageContext,
  reader: CircleReader,
  communityId: number,
  spacePk: number,
  post: { circleId: string; url: string | null },
  permission: string
): Promise<number> {
  const comments = await reader.comments(post.circleId)
  const items: NormalizedItem[] = []
  for (const comment of comments) {
    const normalized = normalizeComment(comment, post.circleId, reader.base, permission, post.url)
    if (normalized) items.push(normalized)
    const replies = typeof comment.replies_count === 'number' ? comment.replies_count : 0
    if (replies > 0 && comment.id != null) {
      for (const reply of await reader.replies(String(comment.id))) {
        const r = normalizeComment(reply, post.circleId, reader.base, permission, normalized?.url ?? post.url)
        if (r) items.push(r)
      }
    }
  }
  if (!items.length) return 0
  const { inserted, ids } = await store(ctx, communityId, spacePk, items)
  await savePostMeta(
    ctx.sql,
    items.flatMap((c) => {
      const postId = ids.get(c.circleId)
      return postId == null
        ? []
        : [{ postId, communityId, sourcePostId: c.circleId, kind: 'comment' as const, parentSourceId: post.circleId, commentsCount: null, fetched: null }]
    })
  )
  return inserted
}

/**
 * Read one space. First visit = full backfill (every page until the end,
 * resumable page by page); afterwards incremental: newest pages until a page
 * holds nothing newer than the stored watermark (pinned old posts on page 1
 * do not end the walk early).
 */
async function readSpace(
  ctx: StageContext,
  reader: CircleReader,
  communityId: number,
  space: CircleSpace,
  permission: string,
  totals: CommunityTotals
): Promise<void> {
  const spacePk = await upsertSpace(ctx.sql, communityId, space, reader.host)
  const sync = await getSpaceSync(ctx.sql, communityId, space.id)
  const backfilling = !sync?.backfillDone
  const watermark = sync?.newestSeenAt ?? null
  const ageLimitDays = ctx.settings.scrape.maxPostAgeDays
  const cutoff = ageLimitDays > 0 ? daysAgo(ageLimitDays) : null
  let page = backfilling ? Math.max(1, (sync?.backfillNextPage ?? 1) - 1) : 1
  let newest: Date | null = watermark
  let reachedEnd = false
  let stoppedAtKnown = false
  let seen = 0
  let state = 'ok'
  let detail = ''

  try {
    while (true) {
      const res = await reader.postsPage(space.id, page, 20)
      if (res.status === 401 || res.status === 403) {
        state = 'forbidden'
        detail = `HTTP ${res.status}: no access to this space`
        break
      }
      if (res.status !== 200) {
        state = 'error'
        detail = `HTTP ${res.status} on page ${page}`
        break
      }
      if (!res.records.length) {
        reachedEnd = true
        break
      }
      const normalized = res.records
        .map((r) => normalizePost(r, space, reader.base, permission))
        .filter((p): p is NonNullable<typeof p> => Boolean(p))
      const kept = cutoff ? normalized.filter((p) => !p.createdAt || p.createdAt >= cutoff) : normalized
      const { inserted, ids } = await store(ctx, communityId, spacePk, kept)
      totals.newPosts += inserted
      totals.seenPosts += kept.length
      seen += kept.length
      for (const p of kept) if (p.createdAt && (!newest || p.createdAt > newest)) newest = p.createdAt

      if (ctx.settings.scrape.withComments && kept.length) {
        const details = await reader.postDetails(space.id, kept.map((p) => p.circleId))
        const postIds = kept.map((p) => ids.get(p.circleId)).filter((id): id is number => id != null)
        const fetched = await commentsFetched(ctx.sql, postIds)
        const meta: PostMetaInput[] = []
        for (const p of kept) {
          const postId = ids.get(p.circleId)
          if (postId == null) continue
          const count = details?.get(p.circleId)?.commentsCount ?? (p.commentsCount || null)
          const already = fetched.get(postId) ?? 0
          let fetchedNow: number | null = null
          if (count != null && count > already) {
            totals.comments += await readComments(ctx, reader, communityId, spacePk, p, permission)
            fetchedNow = count
          }
          meta.push({ postId, communityId, sourcePostId: p.circleId, kind: 'post', parentSourceId: null, commentsCount: count, fetched: fetchedNow })
        }
        await savePostMeta(ctx.sql, meta)
      }

      const dated = normalized.filter((p) => p.createdAt)
      if (!backfilling && watermark && dated.length && dated.every((p) => p.createdAt! <= watermark)) {
        stoppedAtKnown = true
        break
      }
      if (cutoff && dated.length && dated.every((p) => p.createdAt! < cutoff)) {
        reachedEnd = true
        break
      }
      if (!res.hasNext) {
        reachedEnd = true
        break
      }
      page++
      if (backfilling) {
        await saveSpaceSync(ctx.sql, {
          communityId,
          sourceSpaceId: space.id,
          spacePk,
          name: space.name,
          type: space.type,
          newestSeenAt: newest,
          backfillDone: false,
          backfillNextPage: page,
          postsSeenDelta: 0,
          state: 'reading',
          detail: `backfill at page ${page}`
        })
      }
    }
  } finally {
    const backfillDone = backfilling ? reachedEnd : true
    await saveSpaceSync(ctx.sql, {
      communityId,
      sourceSpaceId: space.id,
      spacePk,
      name: space.name,
      type: space.type,
      newestSeenAt: newest,
      backfillDone,
      backfillNextPage: backfillDone ? 1 : page,
      postsSeenDelta: seen,
      state: backfillDone || stoppedAtKnown ? state : 'partial',
      detail: detail || (backfillDone ? 'up to date' : `stopped at page ${page}`)
    })
  }
  if (state === 'ok') totals.readable++
  // A space we may not read is not "unfinished history"; it costs one request per run.
  if (backfilling && !reachedEnd && state !== 'forbidden') totals.complete = false
}

async function readCommunity(
  ctx: StageContext,
  target: ScrapeTarget,
  communityId: number,
  reader: CircleReader,
  totals: CommunityTotals
): Promise<CommunityTotals> {
  const permission = target.kind === 'session' ? 'member_session' : 'public'
  const spaces = (await reader.listSpaces()).filter((s) => spaceHasPosts(s.type))
  totals.spaces = spaces.length
  for (const space of spaces) {
    if (ctx.signal.aborted) break
    await readSpace(ctx, reader, communityId, space, permission, totals)
  }
  return totals
}

/**
 * Stage 4: read every post (and comment) of the communities we can read —
 * stored member sessions, plus ICP-fit communities with public spaces.
 */
export async function runScrape(ctx: StageContext, opts: { communityId?: number } = {}): Promise<StageResult> {
  const { sql, settings } = ctx
  const counts: Record<string, number> = { communities: 0, newPosts: 0, comments: 0, requests: 0 }
  const targets = await scrapeTargets(sql, { includePublic: settings.scrape.includePublic, communityId: opts.communityId })
  if (!targets.length) return { summary: 'нечего читать: нет сохранённых сессий и публичных ICP-сообществ', counts }
  const budget = { remaining: settings.scrape.maxRequestsPerRun }
  // A per-community share keeps one huge community (Silicon Slopes: 265
  // spaces) from using every run; its history resumes on the next pass. A
  // community picked by hand gets the whole run.
  const perCommunity =
    !opts.communityId && settings.scrape.maxRequestsPerCommunity > 0 ? settings.scrape.maxRequestsPerCommunity : budget.remaining
  ctx.log('info', `Читаю посты: ${targets.length} сообществ в очереди, запросов на прогон ${budget.remaining}, на одно сообщество до ${perCommunity}`)

  for (let i = 0; i < targets.length; i++) {
    const target = targets[i]!
    if (ctx.signal.aborted || budget.remaining <= 0) break
    ctx.progress(i, targets.length, target.name || target.host)
    let cookies: Record<string, string> | null = null
    if (target.kind === 'session') {
      const parsed = parseCookieBlob(target.cookieBlob)
      cookies = parsed ? sessionCookies(parsed) : null
      if (!cookies || !Object.keys(cookies).length) {
        counts.unreadableSessions = (counts.unreadableSessions ?? 0) + 1
        ctx.log('warn', `${target.host}: куки зашифрованы или пусты — пропускаю`)
        continue
      }
    }
    const communityId = target.communityId ?? (await communityForHost(sql, target.host, target.name))
    if (target.kind === 'session') await linkConnection(sql, target.host, communityId)
    const share = { remaining: Math.min(budget.remaining, perCommunity) }
    const reader = new CircleReader(target.host, { cookies, budget: share, signal: ctx.signal })
    const totals: CommunityTotals = { spaces: 0, readable: 0, newPosts: 0, seenPosts: 0, comments: 0, complete: true }
    const addPartial = (): void => {
      counts.newPosts += totals.newPosts
      counts.comments += totals.comments
    }
    try {
      await readCommunity(ctx, target, communityId, reader, totals)
      counts.communities++
      counts.newPosts += totals.newPosts
      counts.comments += totals.comments
      const detail = `${totals.readable}/${totals.spaces} разделов, новых постов ${totals.newPosts}, комментариев ${totals.comments}${totals.complete ? '' : ' (история дочитывается)'}`
      if (target.kind === 'session') {
        await markConnection(sql, target.host, { state: 'connected', detail: `Desktop read: ${detail}`, spacesTotal: totals.spaces, spacesReadable: totals.readable, synced: true })
        await closeTasks(sql, communityId, target.host, ['session_expired', 'scrape_challenge'])
      }
      await markScraped(sql, communityId, { state: totals.complete ? 'ok' : 'partial', detail, newPosts: totals.newPosts, synced: true })
      await logActivity(sql, { kind: 'read', level: 'info', community: target.host, summary: `Desktop read ${target.host}: ${detail}`, itemsSeen: totals.seenPosts })
      ctx.log(totals.newPosts ? 'success' : 'info', `${target.name || target.host}: ${detail}`)
    } catch (err) {
      if (err instanceof BudgetExhaustedError || err instanceof RateLimitedError) {
        addPartial()
        counts.partial = (counts.partial ?? 0) + 1
        const runLeft = budget.remaining - reader.requests
        const why =
          err instanceof RateLimitedError
            ? err.message
            : runLeft > 0
              ? `доля запросов на сообщество (${perCommunity}) исчерпана`
              : 'лимит запросов прогона исчерпан'
        await markScraped(sql, communityId, { state: 'partial', detail: why, newPosts: totals.newPosts, synced: false })
        if (err instanceof RateLimitedError) {
          counts.rateLimited = 1
          ctx.log('warn', `${target.host}: ${why}. Чтение остановлено, продолжу после паузы`)
          break
        }
        ctx.log('info', `${target.name || target.host}: ${why}, новых постов ${totals.newPosts}, комментариев ${totals.comments}; дочитаю в следующий прогон`)
        if (runLeft > 0) continue
        break
      }
      if (err instanceof SessionInvalidError) {
        addPartial()
        counts.expired = (counts.expired ?? 0) + 1
        await markConnection(sql, target.host, { state: 'session_expired', detail: err.message })
        await markScraped(sql, communityId, { state: 'session_expired', detail: err.message, newPosts: 0, synced: false })
        await openTask(sql, {
          kind: 'session_expired',
          communityId,
          host: target.host,
          title: 'Сессия истекла — войдите заново',
          detail: `Circle больше не принимает сохранённые куки для ${target.host}. Войдите в сообщество в ego lite или нажмите «Вступить вручную».`
        })
        ctx.log('warn', `${target.host}: сессия истекла`)
        continue
      }
      if (ctx.signal.aborted) throw err
      addPartial()
      counts.errors = (counts.errors ?? 0) + 1
      const message = err instanceof Error ? err.message : String(err)
      await markScraped(sql, communityId, { state: 'error', detail: message, newPosts: 0, synced: false })
      ctx.log('error', `${target.host}: ${message.slice(0, 300)}`)
    } finally {
      budget.remaining -= reader.requests
      counts.requests += reader.requests
    }
  }
  ctx.progress(targets.length, targets.length)
  const summary =
    `прочитано сообществ ${counts.communities}${counts.partial ? ` (+${counts.partial} частично)` : ''}, новых постов ${counts.newPosts}, ` +
    `комментариев ${counts.comments}, запросов ${counts.requests}` +
    (counts.expired ? `, истекших сессий ${counts.expired}` : '') +
    (counts.rateLimited ? ', остановлено ограничением Circle' : '')
  return { summary, counts }
}
