import type { Sql } from './client'
import { uniqueSlugForHost } from '../discovery/hosts'

const NOW = "(now() at time zone 'utc')"

// ------------------------------------------------------------------ joins ---

export interface JoinCandidate {
  id: number
  slug: string
  url: string
  name: string | null
  joinType: string | null
  icpScore: number
  handoffs: number
}

const JOIN_TYPES_FREE = ['free_join']
const JOIN_TYPES_ALL = ['free_join', 'paid']

/**
 * The join queue: ICP-fit, proven Circle, joinable, never attempted. Hosts
 * that needed a human before sort last (fewest handoffs first), so one stuck
 * host cannot block the queue run after run (joiner.py _handoff_counts).
 * `paid` stays in when enabled: the bot never pays, it records paid_skip on the
 * checkout page, and "From $X" listings sometimes still have a free tier.
 */
export async function joinCandidates(sql: Sql, opts: { limit: number; includePaid: boolean; communityId?: number }): Promise<JoinCandidate[]> {
  const types = opts.includePaid ? JOIN_TYPES_ALL : JOIN_TYPES_FREE
  const rows = await sql`
    select c.id, c.slug, c.url, c.name, c.join_type, c.icp_score,
      (select count(*) from warmr_app.join_attempts a where a.community_id = c.id and not a.terminal) as handoffs
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where ${opts.communityId ? sql`c.id = ${opts.communityId}` : sql`c.icp_flag and c.join_type in ${sql(types)}`}
      and c.platform = 'circle'
      and coalesce(c.join_status, 'not_attempted') = 'not_attempted'
      and coalesce(s.exists_status, 'alive') <> 'not_circle'
    order by handoffs asc, c.icp_score desc nulls last, c.id
    limit ${opts.limit}`
  return rows.map((r) => ({
    id: Number(r.id),
    slug: String(r.slug),
    url: String(r.url),
    name: (r.name as string | null) ?? null,
    joinType: (r.join_type as string | null) ?? null,
    icpScore: Number(r.icp_score ?? 0),
    handoffs: Number(r.handoffs ?? 0)
  }))
}

export async function joinQueueSize(sql: Sql, includePaid: boolean): Promise<number> {
  const types = includePaid ? JOIN_TYPES_ALL : JOIN_TYPES_FREE
  const rows = await sql<{ n: string }[]>`
    select count(*) as n from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where c.icp_flag and c.join_type in ${sql(types)} and c.platform = 'circle'
      and coalesce(c.join_status, 'not_attempted') = 'not_attempted'
      and coalesce(s.exists_status, 'alive') <> 'not_circle'`
  return Number(rows[0]?.n ?? 0)
}

/** Browser page visits by this account since UTC midnight — the quantity Circle reacts to. */
export async function visitsToday(sql: Sql, account: string): Promise<number> {
  const rows = await sql<{ n: string }[]>`
    select count(*) as n from warmr_app.join_attempts
    where account = ${account} and created_at >= date_trunc('day', ${sql.unsafe(NOW)})`
  return Number(rows[0]?.n ?? 0)
}

export async function recordJoinAttempt(
  sql: Sql,
  a: { communityId: number; host: string | null; url: string; account: string; status: string; terminal: boolean; detail: string; deviceId: string }
): Promise<void> {
  await sql`
    insert into warmr_app.join_attempts (community_id, host, url, account, status, terminal, detail, device_id)
    values (${a.communityId}, ${a.host}, ${a.url}, ${a.account}, ${a.status}, ${a.terminal}, ${a.detail.slice(0, 2000)}, ${a.deviceId})`
}

export async function persistJoinOutcome(sql: Sql, communityId: number, status: string, detail: string): Promise<void> {
  await sql`
    update public.communities set
      join_status = ${status},
      join_status_detail = ${detail.slice(0, 2000)},
      join_attempted_at = ${sql.unsafe(NOW)},
      join_attempts = coalesce(join_attempts, 0) + 1,
      joined_at = case when ${status} = 'joined' then ${sql.unsafe(NOW)} else joined_at end,
      access_status = case when ${status} = 'joined' then 'joined' else access_status end,
      updated_at = ${sql.unsafe(NOW)}
    where id = ${communityId}`
}

/**
 * Store a member session exactly the way the Python app reads it
 * (web/replay_store.py): replay_sessions.encrypted_cookies = 'plain:' + JSON
 * list of {name, value, domain}, plus a circle_connections row — the scanner
 * only reads hosts that have both. Plaintext is the user's recorded choice
 * (WORKLOG 2026-09-17: CIRCLE_CRED_KEY deliberately unset).
 */
export async function connectSession(
  sql: Sql,
  args: { host: string; cookies: Array<{ name: string; value: string; domain: string }>; memberLabel: string | null; communityId: number | null; source: string }
): Promise<void> {
  const blob = `plain:${JSON.stringify(args.cookies.map((c) => ({ name: c.name, value: c.value, domain: c.domain })))}`
  await sql.begin(async (tx) => {
    await tx`
      insert into public.replay_sessions (host, source, encrypted_cookies, cookie_count, member_label, last_result, last_detail, created_at, updated_at)
      values (${args.host}, ${args.source.slice(0, 16)}, ${blob}, ${args.cookies.length}, ${args.memberLabel?.slice(0, 255) ?? null},
        null, 'Stored by the desktop app', ${tx.unsafe(NOW)}, ${tx.unsafe(NOW)})
      on conflict (host) do update set encrypted_cookies = excluded.encrypted_cookies, cookie_count = excluded.cookie_count,
        member_label = coalesce(excluded.member_label, public.replay_sessions.member_label), source = excluded.source,
        last_result = null, last_detail = excluded.last_detail, updated_at = excluded.updated_at`
    await tx`
      insert into public.circle_connections (host, community_id, name, member_label, priority, state, state_detail,
        spaces_total, spaces_readable, created_at, updated_at)
      values (${args.host}, ${args.communityId}, ${args.memberLabel?.slice(0, 512) ?? null}, ${null}, 'normal', 'not_connected',
        'session stored by the desktop app', 0, 0, ${tx.unsafe(NOW)}, ${tx.unsafe(NOW)})
      on conflict (host) do update set
        community_id = coalesce(public.circle_connections.community_id, excluded.community_id),
        state = case when public.circle_connections.state in ('session_expired', 'authentication_required', 'error', 'access_denied')
          then 'not_connected' else public.circle_connections.state end,
        state_detail = excluded.state_detail,
        updated_at = excluded.updated_at`
  })
}

// ------------------------------------------------------------------ tasks ---

export async function openTask(
  sql: Sql,
  t: { kind: string; communityId: number | null; host: string | null; title: string; detail: string }
): Promise<void> {
  await sql`
    insert into warmr_app.tasks (kind, community_id, host, title, detail)
    values (${t.kind}, ${t.communityId}, ${t.host}, ${t.title.slice(0, 300)}, ${t.detail.slice(0, 2000)})
    on conflict (kind, coalesce(community_id, 0), coalesce(host, '')) where state = 'open'
    do update set title = excluded.title, detail = excluded.detail, created_at = ${sql.unsafe(NOW)}`
}

export async function resolveTask(sql: Sql, id: number, state: 'done' | 'dismissed'): Promise<void> {
  await sql`update warmr_app.tasks set state = ${state}, resolved_at = ${sql.unsafe(NOW)} where id = ${id}`
}

export async function closeTasks(sql: Sql, communityId: number | null, host: string | null, kinds: string[]): Promise<void> {
  await sql`
    update warmr_app.tasks set state = 'done', resolved_at = ${sql.unsafe(NOW)}
    where state = 'open' and kind in ${sql(kinds)}
      and (${communityId}::int is not null and community_id = ${communityId} or ${host}::text is not null and host = ${host})`
}

// ------------------------------------------------------------- scraping ---

export interface ScrapeTarget {
  kind: 'session' | 'public'
  host: string
  communityId: number | null
  name: string | null
  cookieBlob: string | null
  priority: string
  connectionState: string | null
}

/**
 * Hosts to read: every stored member session that is not paused or known
 * expired (VIP first, least recently read first), then — when enabled — ICP-fit
 * communities whose spaces are public, read anonymously.
 */
export async function scrapeTargets(sql: Sql, opts: { includePublic: boolean; communityId?: number }): Promise<ScrapeTarget[]> {
  const sessions = await sql`
    select rs.host, rs.encrypted_cookies, cc.priority, cc.state, coalesce(cc.community_id, c.id) as community_id, c.name,
      s.last_scraped_at
    from public.replay_sessions rs
    join public.circle_connections cc on cc.host = rs.host
    left join public.communities c on c.id = cc.community_id
      or (cc.community_id is null and (c.url in ('https://' || rs.host, 'https://' || rs.host || '/')))
    left join warmr_app.community_state s on s.community_id = coalesce(cc.community_id, c.id)
    where coalesce(cc.priority, 'normal') <> 'paused'
      and coalesce(cc.state, '') not in ('session_expired', 'access_denied', 'authentication_required')
    order by case cc.priority when 'vip' then 0 when 'normal' then 1 when 'low' then 2 else 3 end,
      s.last_scraped_at nulls first, rs.host`
  const seenHosts = new Set<string>()
  let targets: ScrapeTarget[] = sessions
    .filter((r) => {
      const host = String(r.host)
      if (seenHosts.has(host)) return false // url with and without a trailing slash can both match
      seenHosts.add(host)
      return true
    })
    .map((r) => ({
    kind: 'session' as const,
    host: String(r.host),
    communityId: r.community_id == null ? null : Number(r.community_id),
    name: (r.name as string | null) ?? null,
    cookieBlob: String(r.encrypted_cookies),
    priority: String(r.priority ?? 'normal'),
    connectionState: (r.state as string | null) ?? null
  }))
  if (opts.includePublic) {
    const sessionHosts = new Set(targets.map((t) => t.host))
    const publicRows = await sql`
      select c.id, c.url, c.name, s.probe_host
      from public.communities c
      join warmr_app.community_state s on s.community_id = c.id
      where c.icp_flag and s.exists_status = 'alive' and coalesce(s.spaces_public, 0) > 0
      order by s.last_scraped_at nulls first, c.icp_score desc nulls last
      limit 200`
    for (const r of publicRows) {
      const host = String(r.probe_host || '') || new URL(String(r.url)).hostname
      if (sessionHosts.has(host)) continue
      targets.push({
        kind: 'public',
        host,
        communityId: Number(r.id),
        name: (r.name as string | null) ?? null,
        cookieBlob: null,
        priority: 'normal',
        connectionState: null
      })
    }
  }
  if (opts.communityId) targets = targets.filter((t) => t.communityId === opts.communityId)
  return targets
}

/** Parse a stored cookie blob; encrypted (non-'plain:') blobs cannot be read without the Python key. */
export function parseCookieBlob(blob: string | null): Array<{ name: string; value: string; domain?: string }> | null {
  if (!blob || !blob.startsWith('plain:')) return null
  try {
    const parsed = JSON.parse(blob.slice('plain:'.length)) as Array<{ name: string; value: string; domain?: string }>
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

/** Find the community row for a host, creating it (Python-compatible) if missing. */
export async function communityForHost(sql: Sql, host: string, name: string | null): Promise<number> {
  const url = `https://${host}`
  const slug = uniqueSlugForHost(host)
  const found = await sql<{ id: number }[]>`
    select id from public.communities where url in (${url}, ${`${url}/`}) or slug = ${slug}
    order by (url = ${url}) desc limit 1`
  if (found.length) return Number(found[0]!.id)
  const created = await sql<{ id: number }[]>`
    insert into public.communities (slug, url, name, discovered_at, discovery_source, platform, access_status, permission_status,
      relevance_score, relevant, updated_at, icp_score, icp_flag, join_status, join_attempts, watching)
    values (${slug}, ${url}, ${name}, ${sql.unsafe(NOW)}, 'member_session', 'circle', 'joined', 'candidate', 0, false,
      ${sql.unsafe(NOW)}, 0, false, 'not_attempted', 0, false)
    returning id`
  return Number(created[0]!.id)
}

export async function linkConnection(sql: Sql, host: string, communityId: number): Promise<void> {
  await sql`update public.circle_connections set community_id = ${communityId} where host = ${host} and community_id is null`
}

export async function markConnection(
  sql: Sql,
  host: string,
  update: { state: string; detail: string; spacesTotal?: number; spacesReadable?: number; synced?: boolean }
): Promise<void> {
  await sql`
    update public.circle_connections set
      state = ${update.state},
      state_detail = ${update.detail.slice(0, 2000)},
      spaces_total = coalesce(${update.spacesTotal ?? null}, spaces_total),
      spaces_readable = coalesce(${update.spacesReadable ?? null}, spaces_readable),
      last_sync_at = case when ${update.synced ?? false} then ${sql.unsafe(NOW)} else last_sync_at end,
      updated_at = ${sql.unsafe(NOW)}
    where host = ${host}`
  await sql`
    update public.replay_sessions set last_result = ${update.state === 'connected' ? 'ok' : update.state === 'session_expired' ? 'expired' : update.state === 'error' ? 'error' : null},
      last_detail = ${update.detail.slice(0, 2000)}, last_attempt_at = ${sql.unsafe(NOW)}
    where host = ${host}`
}

export async function upsertSpace(
  sql: Sql,
  communityId: number,
  space: { id: string; name: string; slug: string; type: string },
  host: string
): Promise<number> {
  const rows = await sql<{ id: number }[]>`
    insert into public.spaces (community_id, source_space_id, name, slug, space_type, url, approved, last_synced_at)
    values (${communityId}, ${space.id}, ${space.name.slice(0, 512)}, ${space.slug.slice(0, 255)}, ${space.type.slice(0, 64)},
      ${space.slug ? `https://${host}/c/${space.slug}` : null}, true, ${sql.unsafe(NOW)})
    on conflict (community_id, source_space_id) do update set name = excluded.name, slug = excluded.slug,
      space_type = excluded.space_type, url = excluded.url, last_synced_at = excluded.last_synced_at
    returning id`
  return Number(rows[0]!.id)
}

export interface SpaceSync {
  newestSeenAt: Date | null
  backfillDone: boolean
  backfillNextPage: number
  postsSeen: number
}

export async function getSpaceSync(sql: Sql, communityId: number, sourceSpaceId: string): Promise<SpaceSync | null> {
  const rows = await sql`
    select newest_seen_at, backfill_done, backfill_next_page, posts_seen from warmr_app.space_sync
    where community_id = ${communityId} and source_space_id = ${sourceSpaceId}`
  const r = rows[0]
  if (!r) return null
  return {
    newestSeenAt: (r.newest_seen_at as Date | null) ?? null,
    backfillDone: Boolean(r.backfill_done),
    backfillNextPage: Number(r.backfill_next_page ?? 1),
    postsSeen: Number(r.posts_seen ?? 0)
  }
}

export async function saveSpaceSync(
  sql: Sql,
  s: {
    communityId: number
    sourceSpaceId: string
    spacePk: number
    name: string
    type: string
    newestSeenAt: Date | null
    backfillDone: boolean
    backfillNextPage: number
    postsSeenDelta: number
    state: string
    detail: string
  }
): Promise<void> {
  await sql`
    insert into warmr_app.space_sync as t (community_id, source_space_id, space_pk, name, space_type, newest_seen_at,
      backfill_done, backfill_next_page, posts_seen, last_read_at, state, detail)
    values (${s.communityId}, ${s.sourceSpaceId}, ${s.spacePk}, ${s.name}, ${s.type}, ${s.newestSeenAt}, ${s.backfillDone},
      ${s.backfillNextPage}, ${s.postsSeenDelta}, ${sql.unsafe(NOW)}, ${s.state}, ${s.detail.slice(0, 1000)})
    on conflict (community_id, source_space_id) do update set
      space_pk = excluded.space_pk, name = excluded.name, space_type = excluded.space_type,
      newest_seen_at = greatest(t.newest_seen_at, excluded.newest_seen_at),
      backfill_done = excluded.backfill_done, backfill_next_page = excluded.backfill_next_page,
      posts_seen = t.posts_seen + ${s.postsSeenDelta}, last_read_at = excluded.last_read_at,
      state = excluded.state, detail = excluded.detail`
}

export interface AuthorInput {
  sourceAuthorId: string
  displayName: string | null
  profileUrl: string | null
}

export async function upsertAuthors(sql: Sql, communityId: number, authors: AuthorInput[]): Promise<Map<string, number>> {
  const unique = new Map<string, AuthorInput>()
  for (const a of authors) if (a.sourceAuthorId) unique.set(a.sourceAuthorId, a)
  const out = new Map<string, number>()
  if (!unique.size) return out
  const rows = [...unique.values()].map((a) => ({
    community_id: communityId,
    source_author_id: a.sourceAuthorId.slice(0, 128),
    display_name: a.displayName?.slice(0, 512) ?? null,
    profile_url: a.profileUrl?.slice(0, 1024) ?? null
  }))
  const result = await sql`
    insert into public.authors ${sql(rows, 'community_id', 'source_author_id', 'display_name', 'profile_url')}
    on conflict (community_id, source_author_id) do update set
      display_name = coalesce(excluded.display_name, public.authors.display_name),
      profile_url = coalesce(excluded.profile_url, public.authors.profile_url)
    returning id, source_author_id`
  for (const r of result) out.set(String(r.source_author_id), Number(r.id))
  return out
}

export interface PostInput {
  sourceContentId: string
  contentType: 'post' | 'comment'
  threadId: string
  spacePk: number | null
  authorPk: number | null
  title: string | null
  content: string
  url: string | null
  publishedAt: Date | null
  dedupHash: string
  simhash: string
  permissionReference: string
}

export interface UpsertedPost {
  id: number
  sourceContentId: string
  contentType: string
  inserted: boolean
}

export async function upsertPosts(sql: Sql, communityId: number, posts: PostInput[]): Promise<UpsertedPost[]> {
  const unique = new Map<string, PostInput>()
  for (const p of posts) if (p.content.trim()) unique.set(`${p.contentType}:${p.sourceContentId}`, p)
  if (!unique.size) return []
  const rows = [...unique.values()].map((p) => ({
    community_id: communityId,
    space_id: p.spacePk,
    author_id: p.authorPk,
    source_content_id: p.sourceContentId.slice(0, 128),
    content_type: p.contentType,
    thread_id: p.threadId.slice(0, 128),
    title: p.title,
    content: p.content,
    url: p.url?.slice(0, 1024) ?? null,
    published_at: p.publishedAt,
    scraped_at: new Date(),
    dedup_hash: p.dedupHash,
    simhash: p.simhash,
    permission_reference: p.permissionReference,
    classified: false
  }))
  const result = await sql`
    insert into public.posts ${sql(
      rows,
      'community_id', 'space_id', 'author_id', 'source_content_id', 'content_type', 'thread_id', 'title', 'content', 'url',
      'published_at', 'scraped_at', 'dedup_hash', 'simhash', 'permission_reference', 'classified'
    )}
    on conflict (community_id, source_content_id, content_type) do update set
      content = excluded.content,
      title = coalesce(excluded.title, public.posts.title),
      url = coalesce(excluded.url, public.posts.url),
      space_id = coalesce(excluded.space_id, public.posts.space_id),
      author_id = coalesce(excluded.author_id, public.posts.author_id),
      thread_id = coalesce(excluded.thread_id, public.posts.thread_id),
      published_at = coalesce(excluded.published_at, public.posts.published_at),
      edited_at = case when public.posts.dedup_hash <> excluded.dedup_hash then excluded.scraped_at else public.posts.edited_at end,
      classified = case when public.posts.dedup_hash <> excluded.dedup_hash then false else public.posts.classified end,
      dedup_hash = excluded.dedup_hash,
      simhash = excluded.simhash,
      scraped_at = excluded.scraped_at
    returning id, source_content_id, content_type, (xmax = 0) as inserted`
  return result.map((r) => ({
    id: Number(r.id),
    sourceContentId: String(r.source_content_id),
    contentType: String(r.content_type),
    inserted: Boolean(r.inserted)
  }))
}

export async function commentsFetched(sql: Sql, postIds: number[]): Promise<Map<number, number>> {
  const out = new Map<number, number>()
  if (!postIds.length) return out
  const rows = await sql`
    select post_id, comments_fetched_count from warmr_app.post_meta where post_id in ${sql(postIds)}`
  for (const r of rows) out.set(Number(r.post_id), Number(r.comments_fetched_count ?? 0))
  return out
}

export async function savePostMeta(
  sql: Sql,
  rows: Array<{ postId: number; communityId: number; sourcePostId: string; commentsCount: number | null; fetched: number | null }>
): Promise<void> {
  if (!rows.length) return
  const values = rows.map((r) => ({
    post_id: r.postId,
    community_id: r.communityId,
    source_post_id: r.sourcePostId,
    comments_count: r.commentsCount,
    comments_fetched_count: r.fetched,
    comments_fetched_at: r.fetched == null ? null : new Date()
  }))
  await sql`
    insert into warmr_app.post_meta ${sql(values, 'post_id', 'community_id', 'source_post_id', 'comments_count', 'comments_fetched_count', 'comments_fetched_at')}
    on conflict (post_id) do update set
      comments_count = excluded.comments_count,
      comments_fetched_count = coalesce(excluded.comments_fetched_count, warmr_app.post_meta.comments_fetched_count),
      comments_fetched_at = coalesce(excluded.comments_fetched_at, warmr_app.post_meta.comments_fetched_at)`
}

export async function markScraped(
  sql: Sql,
  communityId: number,
  s: { state: string; detail: string; newPosts: number; synced: boolean }
): Promise<void> {
  await sql`
    insert into warmr_app.community_state as t (community_id, last_scraped_at, scrape_state, scrape_detail, posts_stored, updated_at)
    values (${communityId}, ${sql.unsafe(NOW)}, ${s.state}, ${s.detail.slice(0, 1000)}, ${s.newPosts}, ${sql.unsafe(NOW)})
    on conflict (community_id) do update set last_scraped_at = excluded.last_scraped_at, scrape_state = excluded.scrape_state,
      scrape_detail = excluded.scrape_detail, posts_stored = t.posts_stored + ${s.newPosts}, updated_at = excluded.updated_at`
  if (s.synced) {
    await sql`update public.communities set last_synced_at = ${sql.unsafe(NOW)}, updated_at = ${sql.unsafe(NOW)} where id = ${communityId}`
  }
}

// -------------------------------------------------------------------- llm ---

export async function recordLlmCall(
  sql: Sql,
  c: { purpose: string; communityId: number | null; provider: string; model: string; inputTokens: number; outputTokens: number; usd: number; ok: boolean; error: string | null }
): Promise<void> {
  await sql`
    insert into warmr_app.llm_calls (purpose, community_id, provider, model, input_tokens, output_tokens, usd, ok, error)
    values (${c.purpose}, ${c.communityId}, ${c.provider}, ${c.model}, ${c.inputTokens}, ${c.outputTokens}, ${c.usd}, ${c.ok}, ${c.error})`
}

export async function llmSpendToday(sql: Sql): Promise<number> {
  const rows = await sql<{ usd: string | null }[]>`
    select coalesce(sum(usd), 0) as usd from warmr_app.llm_calls where created_at >= date_trunc('day', ${sql.unsafe(NOW)})`
  return Number(rows[0]?.usd ?? 0)
}
