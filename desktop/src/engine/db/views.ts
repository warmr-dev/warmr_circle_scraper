import type { Sql } from './client'
import type {
  CommunityDetail,
  CommunityFilter,
  CommunityRow,
  Funnel,
  JoinAttemptRow,
  PostFilter,
  PostRow,
  TaskRow
} from '../../shared/types'
import { joinQueueSize, llmSpendToday, visitsToday } from './content'

const NOW = "(now() at time zone 'utc')"

function iso(value: unknown): string | null {
  if (!value) return null
  const date = value instanceof Date ? value : new Date(String(value))
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

export async function funnel(sql: Sql, opts: { includePaid: boolean; account: string; visitCap: number }): Promise<Funnel> {
  const [c] = await sql`
    select
      count(*) as total,
      count(*) filter (where c.platform = 'circle') as circle,
      count(*) filter (where c.platform = 'discover') as discover_unresolved,
      count(s.probed_at) as probed,
      count(*) filter (where (c.platform is null or c.platform = 'circle') and s.probed_at is null) as not_probed,
      count(*) filter (where s.exists_status = 'alive') as alive,
      count(*) filter (where s.exists_status = 'locked_or_absent') as locked,
      count(*) filter (where s.exists_status = 'unreachable') as unreachable,
      count(*) filter (where c.name is not null or c.description is not null or s.space_names is not null) as with_text,
      count(*) filter (where s.icp_version is not null) as icp_judged,
      count(*) filter (where c.icp_flag) as icp_fit,
      count(*) filter (where c.join_status = 'joined') as joined,
      count(*) filter (where s.last_scraped_at is not null) as scraped
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id`
  const [extra] = await sql`
    select
      (select count(*) from public.replay_sessions) as sessions,
      (select count(*) from public.posts) as posts,
      (select count(*) from public.posts where scraped_at >= ${sql.unsafe(NOW)} - interval '1 day'
         and permission_reference in ('member_session', 'public')) as posts_24h,
      (select count(*) from warmr_app.tasks where state = 'open') as tasks`
  const n = (v: unknown): number => Number(v ?? 0)
  return {
    total: n(c!.total),
    circle: n(c!.circle),
    discoverUnresolved: n(c!.discover_unresolved),
    probed: n(c!.probed),
    notProbed: n(c!.not_probed),
    alive: n(c!.alive),
    lockedOrAbsent: n(c!.locked),
    unreachable: n(c!.unreachable),
    withText: n(c!.with_text),
    icpJudged: n(c!.icp_judged),
    icpFit: n(c!.icp_fit),
    joinable: await joinQueueSize(sql, opts.includePaid),
    joined: n(c!.joined),
    withSession: n(extra!.sessions),
    scrapedCommunities: n(c!.scraped),
    postsTotal: n(extra!.posts),
    postsNew24h: n(extra!.posts_24h),
    openTasks: n(extra!.tasks),
    visitsToday: await visitsToday(sql, opts.account),
    visitCap: opts.visitCap,
    llmSpendToday: await llmSpendToday(sql)
  }
}

const ROW_SELECT = (sql: Sql) => sql`
  c.id, c.slug, c.url, c.name, c.platform, c.join_type, c.icp_score, c.icp_flag, c.icp_decided_by, c.join_status,
  s.exists_status, s.spaces_public, s.members_total, s.posts_stored, s.probed_at, s.last_scraped_at,
  exists (select 1 from public.replay_sessions rs
          where rs.host = s.probe_host or 'https://' || rs.host = rtrim(c.url, '/')) as has_session`

function toRow(r: Record<string, unknown>): CommunityRow {
  return {
    id: Number(r.id),
    slug: String(r.slug),
    url: String(r.url),
    name: (r.name as string | null) ?? null,
    platform: (r.platform as string | null) ?? null,
    joinType: (r.join_type as string | null) ?? null,
    existsStatus: (r.exists_status as string | null) ?? null,
    icpScore: r.icp_score == null ? null : Number(r.icp_score),
    icpFlag: Boolean(r.icp_flag),
    icpDecidedBy: (r.icp_decided_by as string | null) ?? null,
    joinStatus: (r.join_status as string | null) ?? null,
    spacesPublic: r.spaces_public == null ? null : Number(r.spaces_public),
    membersTotal: r.members_total == null ? null : Number(r.members_total),
    postsStored: r.posts_stored == null ? null : Number(r.posts_stored),
    probedAt: iso(r.probed_at),
    lastScrapedAt: iso(r.last_scraped_at),
    hasSession: Boolean(r.has_session)
  }
}

export async function listCommunities(sql: Sql, f: CommunityFilter): Promise<{ rows: CommunityRow[]; total: number }> {
  const conditions = []
  if (f.search?.trim()) {
    const like = `%${f.search.trim().toLowerCase()}%`
    conditions.push(sql`(lower(coalesce(c.name, '')) like ${like} or lower(c.url) like ${like})`)
  }
  if (f.platform) conditions.push(sql`c.platform = ${f.platform}`)
  if (f.exists === 'unprobed') conditions.push(sql`s.probed_at is null`)
  else if (f.exists) conditions.push(sql`s.exists_status = ${f.exists}`)
  if (f.icp === 'fit') conditions.push(sql`c.icp_flag`)
  if (f.icp === 'notfit') conditions.push(sql`s.icp_version is not null and not coalesce(c.icp_flag, false)`)
  if (f.icp === 'unjudged') conditions.push(sql`s.icp_version is null`)
  if (f.joinStatus) conditions.push(sql`coalesce(c.join_status, 'not_attempted') = ${f.joinStatus}`)
  if (f.joinType) conditions.push(sql`c.join_type = ${f.joinType}`)
  if (f.hasSession) {
    conditions.push(sql`exists (select 1 from public.replay_sessions rs where rs.host = s.probe_host or 'https://' || rs.host = rtrim(c.url, '/'))`)
  }
  const where = conditions.length
    ? conditions.reduce((acc, cond, i) => (i === 0 ? sql`where ${cond}` : sql`${acc} and ${cond}`), sql``)
    : sql``
  const order =
    f.sort === 'recent'
      ? sql`order by c.discovered_at desc nulls last, c.id desc`
      : f.sort === 'name'
        ? sql`order by lower(coalesce(c.name, c.slug)), c.id`
        : f.sort === 'posts'
          ? sql`order by s.posts_stored desc nulls last, c.id`
          : sql`order by c.icp_flag desc nulls last, c.icp_score desc nulls last, c.id`
  const limit = Math.min(Math.max(f.limit ?? 100, 1), 500)
  const offset = Math.max(f.offset ?? 0, 0)
  const rows = await sql`
    select ${ROW_SELECT(sql)}
    from public.communities c left join warmr_app.community_state s on s.community_id = c.id
    ${where} ${order} limit ${limit} offset ${offset}`
  const [count] = await sql`
    select count(*) as n from public.communities c left join warmr_app.community_state s on s.community_id = c.id ${where}`
  return { rows: rows.map(toRow), total: Number(count?.n ?? 0) }
}

export async function communityDetail(sql: Sql, id: number): Promise<CommunityDetail | null> {
  const [r] = await sql`
    select ${ROW_SELECT(sql)}, c.description, c.price_label, c.directory_goals, c.icp_reasons, c.join_type_detail,
      c.join_status_detail, c.joined_at, c.discovery_source, s.probe_detail, s.space_names, s.icp_llm_reason,
      s.icp_llm_confidence, cc.state as connection_state, cc.state_detail as connection_detail
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    left join public.circle_connections cc on cc.host = s.probe_host or 'https://' || cc.host = rtrim(c.url, '/')
    where c.id = ${id}
    limit 1`
  if (!r) return null
  const attempts = await joinAttempts(sql, 20, id)
  return {
    ...toRow(r),
    description: (r.description as string | null) ?? null,
    priceLabel: (r.price_label as string | null) ?? null,
    directoryGoals: Array.isArray(r.directory_goals) ? (r.directory_goals as unknown[]).map(String) : [],
    icpReasons: Array.isArray(r.icp_reasons) ? (r.icp_reasons as unknown[]).map(String) : [],
    joinTypeDetail: (r.join_type_detail as string | null) ?? null,
    probeDetail: (r.probe_detail as string | null) ?? null,
    spaceNames: Array.isArray(r.space_names) ? (r.space_names as string[]) : [],
    icpLlmReason: (r.icp_llm_reason as string | null) ?? null,
    icpLlmConfidence: r.icp_llm_confidence == null ? null : Number(r.icp_llm_confidence),
    joinStatusDetail: (r.join_status_detail as string | null) ?? null,
    joinedAt: iso(r.joined_at),
    discoverySource: (r.discovery_source as string | null) ?? null,
    connectionState: (r.connection_state as string | null) ?? null,
    connectionDetail: (r.connection_detail as string | null) ?? null,
    attempts
  }
}

export async function joinQueue(sql: Sql, opts: { limit: number; includePaid: boolean }): Promise<CommunityRow[]> {
  const types = opts.includePaid ? ['free_join', 'paid'] : ['free_join']
  const rows = await sql`
    select ${ROW_SELECT(sql)},
      (select count(*) from warmr_app.join_attempts a where a.community_id = c.id and not a.terminal) as handoffs
    from public.communities c left join warmr_app.community_state s on s.community_id = c.id
    where c.icp_flag and c.join_type in ${sql(types)} and c.platform = 'circle'
      and coalesce(c.join_status, 'not_attempted') = 'not_attempted'
      and coalesce(s.exists_status, 'alive') <> 'not_circle'
    order by handoffs asc, c.icp_score desc nulls last, c.id
    limit ${opts.limit}`
  return rows.map(toRow)
}

export async function joinAttempts(sql: Sql, limit: number, communityId?: number): Promise<JoinAttemptRow[]> {
  const rows = await sql`
    select a.id, a.community_id, c.name, a.host, a.url, a.account, a.status, a.terminal, a.detail, a.created_at
    from warmr_app.join_attempts a left join public.communities c on c.id = a.community_id
    ${communityId ? sql`where a.community_id = ${communityId}` : sql``}
    order by a.created_at desc limit ${limit}`
  return rows.map((r) => ({
    id: Number(r.id),
    communityId: r.community_id == null ? null : Number(r.community_id),
    name: (r.name as string | null) ?? null,
    host: (r.host as string | null) ?? null,
    url: (r.url as string | null) ?? null,
    account: String(r.account),
    status: String(r.status),
    terminal: Boolean(r.terminal),
    detail: (r.detail as string | null) ?? null,
    createdAt: iso(r.created_at) ?? ''
  }))
}

export async function tasks(sql: Sql, state: string): Promise<TaskRow[]> {
  const rows = await sql`
    select t.id, t.kind, t.community_id, c.name, t.host, c.url, t.title, t.detail, t.state, t.created_at
    from warmr_app.tasks t left join public.communities c on c.id = t.community_id
    ${state === 'all' ? sql`` : sql`where t.state = ${state}`}
    order by t.created_at desc limit 300`
  return rows.map((r) => ({
    id: Number(r.id),
    kind: String(r.kind),
    communityId: r.community_id == null ? null : Number(r.community_id),
    name: (r.name as string | null) ?? null,
    host: (r.host as string | null) ?? null,
    url: (r.url as string | null) ?? null,
    title: String(r.title),
    detail: (r.detail as string | null) ?? null,
    state: String(r.state),
    createdAt: iso(r.created_at) ?? ''
  }))
}

export async function posts(sql: Sql, f: PostFilter): Promise<{ rows: PostRow[]; total: number }> {
  const conditions = []
  if (f.communityId) conditions.push(sql`p.community_id = ${f.communityId}`)
  if (f.search?.trim()) conditions.push(sql`p.content ilike ${`%${f.search.trim()}%`}`)
  const where = conditions.length
    ? conditions.reduce((acc, cond, i) => (i === 0 ? sql`where ${cond}` : sql`${acc} and ${cond}`), sql``)
    : sql``
  const limit = Math.min(Math.max(f.limit ?? 50, 1), 200)
  const rows = await sql`
    select p.id, p.community_id, c.name as community_name, c.url as community_url, p.content_type, p.title, p.content, p.url,
      p.published_at, a.display_name, sp.name as space_name
    from public.posts p
    join public.communities c on c.id = p.community_id
    left join public.authors a on a.id = p.author_id
    left join public.spaces sp on sp.id = p.space_id
    ${where}
    order by p.published_at desc nulls last, p.id desc
    limit ${limit} offset ${Math.max(f.offset ?? 0, 0)}`
  const [count] = await sql`select count(*) as n from public.posts p ${where}`
  return {
    rows: rows.map((r) => ({
      id: Number(r.id),
      communityId: Number(r.community_id),
      communityName: (r.community_name as string | null) ?? null,
      host: (() => {
        try {
          return new URL(String(r.community_url)).hostname
        } catch {
          return null
        }
      })(),
      contentType: String(r.content_type),
      title: (r.title as string | null) ?? null,
      content: String(r.content).slice(0, 4000),
      url: (r.url as string | null) ?? null,
      publishedAt: iso(r.published_at),
      authorName: (r.display_name as string | null) ?? null,
      spaceName: (r.space_name as string | null) ?? null
    })),
    total: Number(count?.n ?? 0)
  }
}
