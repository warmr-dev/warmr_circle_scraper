import type { Sql } from './client'
import type { ProbeResult, PublicSpaces, JoinClassification } from '../circle/probe'
import type { IcpDecision } from '../icp/classify'
import { uniqueSlugForHost, platformFromHost } from '../discovery/hosts'

// All SQL the engine runs, in one place. Contract with the shared database:
// - `public.*` tables belong to the Python app too; only columns it already
//   has are written, with the vocabularies it already uses, naive-UTC times,
//   and every NOT NULL column filled (several have no server default).
// - `warmr_app.*` tables are this app's own.

const NOW = "(now() at time zone 'utc')"

export type Row = Record<string, unknown>

// ---------------------------------------------------------------- devices ---

export async function heartbeatDevice(
  sql: Sql,
  device: { id: string; name: string; platform: string; version: string; stage: string | null }
): Promise<void> {
  await sql`
    insert into warmr_app.devices (id, name, platform, app_version, current_stage, last_seen_at)
    values (${device.id}, ${device.name}, ${device.platform}, ${device.version}, ${device.stage}, ${sql.unsafe(NOW)})
    on conflict (id) do update set name = excluded.name, platform = excluded.platform,
      app_version = excluded.app_version, current_stage = excluded.current_stage, last_seen_at = excluded.last_seen_at`
}

// --------------------------------------------------------------- settings ---

export async function readAppSettings(sql: Sql, key: string): Promise<unknown | null> {
  const rows = await sql<{ value: unknown }[]>`select value from warmr_app.settings where key = ${key}`
  return rows[0]?.value ?? null
}

export async function writeAppSettings(sql: Sql, key: string, value: unknown, by: string): Promise<void> {
  await sql`
    insert into warmr_app.settings (key, value, updated_by, updated_at)
    values (${key}, ${sql.json(value as never)}, ${by}, ${sql.unsafe(NOW)})
    on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at`
}

/** The Python worker's own schedule keys in public.settings. */
export async function readOldWorkerSchedules(sql: Sql): Promise<Record<string, string | null>> {
  const rows = await sql<{ key: string; value: string | null }[]>`
    select key, value from public.settings
    where key in ('harvest_schedule', 'harvest_search', 'icp_classification_schedule')`
  const out: Record<string, string | null> = {
    harvest_schedule: null,
    harvest_search: null,
    icp_classification_schedule: null
  }
  for (const r of rows) out[r.key] = r.value
  return out
}

export async function pauseOldWorkerSchedules(sql: Sql): Promise<void> {
  for (const key of ['harvest_schedule', 'harvest_search', 'icp_classification_schedule']) {
    await sql`
      insert into public.settings (key, value, updated_at) values (${key}, 'off', ${sql.unsafe(NOW)})
      on conflict (key) do update set value = 'off', updated_at = excluded.updated_at`
  }
}

// ------------------------------------------------------------------ locks ---

/** Cross-device stage lock with a TTL, so two machines never run one stage at once. */
export async function acquireLock(sql: Sql, name: string, holder: string, ttlSeconds: number): Promise<boolean> {
  const rows = await sql`
    insert into warmr_app.locks (name, holder, expires_at)
    values (${name}, ${holder}, ${sql.unsafe(NOW)} + make_interval(secs => ${ttlSeconds}))
    on conflict (name) do update set holder = excluded.holder, expires_at = excluded.expires_at
    where warmr_app.locks.expires_at < ${sql.unsafe(NOW)} or warmr_app.locks.holder = ${holder}
    returning name`
  return rows.length > 0
}

export async function releaseLock(sql: Sql, name: string, holder: string): Promise<void> {
  await sql`delete from warmr_app.locks where name = ${name} and holder = ${holder}`
}

export async function lockHolder(sql: Sql, name: string): Promise<string | null> {
  const rows = await sql<{ holder: string }[]>`
    select holder from warmr_app.locks where name = ${name} and expires_at >= ${sql.unsafe(NOW)}`
  return rows[0]?.holder ?? null
}

// ------------------------------------------------------------------- runs ---

export async function startRun(sql: Sql, stage: string, deviceId: string): Promise<number> {
  const rows = await sql<{ id: string }[]>`
    insert into warmr_app.runs (stage, device_id) values (${stage}, ${deviceId}) returning id`
  return Number(rows[0]!.id)
}

export async function finishRun(
  sql: Sql,
  id: number,
  result: { ok: boolean; summary: string; counts: Record<string, number>; error: string | null }
): Promise<void> {
  await sql`
    update warmr_app.runs set finished_at = ${sql.unsafe(NOW)}, ok = ${result.ok}, summary = ${result.summary},
      counts = ${sql.json(result.counts)}, error = ${result.error}
    where id = ${id}`
}

export async function lastRuns(sql: Sql, limit: number): Promise<Row[]> {
  return sql`
    select id, stage, device_id, started_at, finished_at, ok, summary, counts, error
    from warmr_app.runs order by started_at desc limit ${limit}`
}

export async function lastRunByStage(sql: Sql): Promise<Row[]> {
  return sql`
    select distinct on (stage) id, stage, device_id, started_at, finished_at, ok, summary, counts, error
    from warmr_app.runs order by stage, started_at desc`
}

// ----------------------------------------------------------- activity log ---

export async function logActivity(
  sql: Sql,
  entry: { kind: string; level: string; community?: string | null; summary: string; detail?: Record<string, unknown>; itemsSeen?: number; decidedBy?: string | null }
): Promise<void> {
  await sql`
    insert into public.activity_log (created_at, kind, level, community, summary, detail, items_seen, leads_found, decided_by)
    values (${sql.unsafe(NOW)}, ${entry.kind.slice(0, 32)}, ${entry.level.slice(0, 16)}, ${entry.community?.slice(0, 255) ?? null},
      ${entry.summary.slice(0, 500)}, ${JSON.stringify({ source: 'desktop', ...(entry.detail ?? {}) })}::json,
      ${entry.itemsSeen ?? 0}, 0, ${entry.decidedBy ?? null})`
}

// ------------------------------------------------------------ communities ---

export interface ProbeCandidate {
  id: number
  url: string
  slug: string
  platform: string | null
  name: string | null
  priceLabel: string | null
  joinType: string | null
}

export async function probeCandidates(
  sql: Sql,
  opts: { limit: number; reprobeAliveDays: number; reprobeOtherDays: number; ids?: number[] }
): Promise<ProbeCandidate[]> {
  const due = opts.ids?.length
    ? sql`c.id in ${sql(opts.ids)}`
    : sql`(c.platform is null or c.platform = 'circle')
      and (
        s.probed_at is null
        or (s.exists_status = 'alive' and s.probed_at < ${sql.unsafe(NOW)} - make_interval(days => ${opts.reprobeAliveDays}))
        or (s.exists_status is distinct from 'alive' and s.probed_at < ${sql.unsafe(NOW)} - make_interval(days => ${opts.reprobeOtherDays}))
      )`
  const rows = await sql`
    select c.id, c.url, c.slug, c.platform, c.name, c.price_label, c.join_type
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where ${due}
    order by (s.probed_at is not null), c.icp_flag desc nulls last, (c.name is null), c.id
    limit ${opts.limit}`
  return rows.map((r) => ({
    id: Number(r.id),
    url: String(r.url),
    slug: String(r.slug),
    platform: (r.platform as string | null) ?? null,
    name: (r.name as string | null) ?? null,
    priceLabel: (r.price_label as string | null) ?? null,
    joinType: (r.join_type as string | null) ?? null
  }))
}

export async function probeBacklog(sql: Sql, opts: { reprobeAliveDays: number; reprobeOtherDays: number }): Promise<number> {
  const rows = await sql<{ n: string }[]>`
    select count(*) as n
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where (c.platform is null or c.platform = 'circle')
      and (
        s.probed_at is null
        or (s.exists_status = 'alive' and s.probed_at < ${sql.unsafe(NOW)} - make_interval(days => ${opts.reprobeAliveDays}))
        or (s.exists_status is distinct from 'alive' and s.probed_at < ${sql.unsafe(NOW)} - make_interval(days => ${opts.reprobeOtherDays}))
      )`
  return Number(rows[0]?.n ?? 0)
}

const DEFINITIVE_JOIN_TYPES = new Set(['free_join', 'paid', 'invite_only', 'subscription_expired'])

/**
 * Store one probe. `join_type` only moves to a weaker value when the old one
 * was not definitive: a flaky request must not erase a free_join that a
 * previous probe proved.
 */
export async function saveProbe(
  sql: Sql,
  candidate: ProbeCandidate,
  probe: ProbeResult,
  spaces: PublicSpaces | null,
  fallback: JoinClassification | null
): Promise<void> {
  let joinType: string = probe.joinType
  let joinDetail = probe.detail
  if (probe.exists !== 'alive' && fallback) {
    joinType = fallback.joinType
    joinDetail = `${fallback.detail} (live check inconclusive: ${probe.detail})`
  }
  const keepOld = probe.exists !== 'alive' && !fallback && candidate.joinType && DEFINITIVE_JOIN_TYPES.has(candidate.joinType)
  const newPlatform =
    candidate.platform ?? (probe.exists === 'alive' ? 'circle' : probe.exists === 'not_circle' ? 'other' : null)
  const nameFromProbe = probe.name && (!candidate.name || candidate.name === candidate.slug) ? probe.name : null
  const names = spaces?.ok ? spaces.spaces.map((s) => s.name).filter(Boolean).slice(0, 60) : []
  // Empty arrays carry no element type for postgres.js to infer; store null
  // (spaces_public = 0 still records "read, and there were none").
  const spaceNames = names.length ? names : null
  const postsPublic = spaces?.ok ? spaces.spaces.reduce((acc, s) => acc + (s.postsCount ?? 0), 0) : null
  const membersTotal = spaces?.ok ? spaces.spaces.reduce((acc, s) => Math.max(acc, s.membersCount ?? 0), 0) : null

  await sql.begin(async (tx) => {
    await tx`
      update public.communities set
        join_type = ${keepOld ? candidate.joinType : joinType},
        join_type_detail = ${(keepOld ? `kept ${candidate.joinType}; latest probe: ${probe.detail}` : joinDetail).slice(0, 2000)},
        join_type_checked_at = ${tx.unsafe(NOW)},
        name = coalesce(${nameFromProbe}, name),
        platform = ${newPlatform},
        updated_at = ${tx.unsafe(NOW)}
      where id = ${candidate.id}`
    await tx`
      insert into warmr_app.community_state as s (
        community_id, exists_status, probed_at, probe_host, probe_http, probe_detail, circle_community_id,
        is_private, allow_signups, has_paywalls, spaces_public, space_names, posts_public, members_total, updated_at)
      values (${candidate.id}, ${probe.exists}, ${tx.unsafe(NOW)}, ${probe.answeredHost}, ${probe.httpStatus},
        ${probe.detail.slice(0, 1000)}, ${probe.circleId}, ${probe.isPrivate}, ${probe.allowSignups}, ${probe.hasPaywalls},
        ${spaces?.ok ? spaces.spaces.length : null}, ${spaceNames}, ${postsPublic}, ${membersTotal}, ${tx.unsafe(NOW)})
      on conflict (community_id) do update set
        exists_status = excluded.exists_status,
        probed_at = excluded.probed_at,
        probe_host = excluded.probe_host,
        probe_http = excluded.probe_http,
        probe_detail = excluded.probe_detail,
        circle_community_id = coalesce(excluded.circle_community_id, s.circle_community_id),
        is_private = coalesce(excluded.is_private, s.is_private),
        allow_signups = coalesce(excluded.allow_signups, s.allow_signups),
        has_paywalls = coalesce(excluded.has_paywalls, s.has_paywalls),
        spaces_public = coalesce(excluded.spaces_public, s.spaces_public),
        space_names = coalesce(excluded.space_names, s.space_names),
        posts_public = coalesce(excluded.posts_public, s.posts_public),
        members_total = coalesce(excluded.members_total, s.members_total),
        icp_version = case
          when excluded.space_names is distinct from s.space_names and excluded.space_names is not null then null
          else s.icp_version end,
        updated_at = excluded.updated_at`
    if (nameFromProbe) {
      // A new name is new evidence: let the ICP stage judge the row again.
      await tx`update warmr_app.community_state set icp_version = null where community_id = ${candidate.id}`
    }
  })
}

export interface IcpCandidate {
  id: number
  url: string
  slug: string
  name: string | null
  description: string | null
  priceLabel: string | null
  goals: string[]
  joinType: string | null
  spaceNames: string[]
  membersTotal: number | null
  existsStatus: string | null
}

/**
 * Rows the ICP stage may (re)judge. A person's own verdict ('human') is never
 * overwritten; without an LLM, an earlier LLM verdict is kept too, so running
 * rules-only never downgrades what a model already judged.
 */
function icpDue(sql: Sql, version: string, withLlm: boolean) {
  const protectedBy = withLlm ? ['human'] : ['human', 'llm']
  return sql`(s.community_id is not null or c.platform = 'discover')
      and (c.platform is null or c.platform in ('circle', 'discover'))
      and coalesce(s.exists_status, '') <> 'not_circle'
      and s.icp_version is distinct from ${version}
      and coalesce(c.icp_decided_by, '') not in ${sql(protectedBy)}`
}

export async function icpCandidates(
  sql: Sql,
  opts: { limit: number; version: string; withLlm: boolean; ids?: number[]; textlessOnly?: boolean }
): Promise<IcpCandidate[]> {
  const due = opts.ids?.length ? sql`c.id in ${sql(opts.ids)}` : icpDue(sql, opts.version, opts.withLlm)
  // While the LLM is out of budget, only rows it would never see anyway.
  const textless = opts.textlessOnly
    ? sql`and coalesce(c.name, '') = '' and coalesce(c.description, '') = ''
        and coalesce(cardinality(s.space_names), 0) = 0
        and (c.directory_goals is null or json_typeof(c.directory_goals) <> 'array' or json_array_length(c.directory_goals) = 0)`
    : sql``
  const rows = await sql`
    select c.id, c.url, c.slug, c.name, c.description, c.price_label, c.directory_goals, c.join_type,
      s.space_names, s.members_total, s.exists_status
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where ${due} ${textless}
    order by (s.exists_status = 'alive') desc nulls last,
      (c.name is not null or c.description is not null or s.space_names is not null) desc,
      c.icp_flag desc nulls last, c.id
    limit ${opts.limit}`
  return rows.map((r) => ({
    id: Number(r.id),
    url: String(r.url),
    slug: String(r.slug),
    name: (r.name as string | null) ?? null,
    description: (r.description as string | null) ?? null,
    priceLabel: (r.price_label as string | null) ?? null,
    goals: Array.isArray(r.directory_goals) ? (r.directory_goals as unknown[]).map(String) : [],
    joinType: (r.join_type as string | null) ?? null,
    spaceNames: Array.isArray(r.space_names) ? (r.space_names as string[]) : [],
    membersTotal: r.members_total == null ? null : Number(r.members_total),
    existsStatus: (r.exists_status as string | null) ?? null
  }))
}

export async function icpBacklog(sql: Sql, version: string, withLlm: boolean): Promise<number> {
  const rows = await sql<{ n: string }[]>`
    select count(*) as n
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where ${icpDue(sql, version, withLlm)}`
  return Number(rows[0]?.n ?? 0)
}

export async function saveIcp(sql: Sql, communityId: number, decision: IcpDecision, version: string): Promise<void> {
  await sql.begin(async (tx) => {
    await tx`
      update public.communities set
        icp_score = ${decision.score},
        icp_flag = ${decision.flag},
        icp_reasons = ${JSON.stringify(decision.reasons.slice(0, 30))}::json,
        icp_checked_at = ${tx.unsafe(NOW)},
        icp_decided_by = ${decision.decidedBy},
        updated_at = ${tx.unsafe(NOW)}
      where id = ${communityId}`
    await tx`
      insert into warmr_app.community_state as s (community_id, icp_version, icp_at, icp_rule_score, icp_llm_model,
        icp_llm_fit, icp_llm_score, icp_llm_confidence, icp_llm_reason, updated_at)
      values (${communityId}, ${version}, ${tx.unsafe(NOW)}, ${decision.ruleScore}, ${decision.llm?.model ?? null},
        ${decision.llm?.fit ?? null}, ${decision.llm?.score ?? null}, ${decision.llm?.confidence ?? null},
        ${decision.llm?.reason ?? decision.llmError ?? null}, ${tx.unsafe(NOW)})
      on conflict (community_id) do update set
        icp_version = excluded.icp_version, icp_at = excluded.icp_at, icp_rule_score = excluded.icp_rule_score,
        icp_llm_model = coalesce(excluded.icp_llm_model, s.icp_llm_model),
        icp_llm_fit = coalesce(excluded.icp_llm_fit, s.icp_llm_fit),
        icp_llm_score = coalesce(excluded.icp_llm_score, s.icp_llm_score),
        icp_llm_confidence = coalesce(excluded.icp_llm_confidence, s.icp_llm_confidence),
        icp_llm_reason = coalesce(excluded.icp_llm_reason, s.icp_llm_reason),
        updated_at = excluded.updated_at`
  })
}

export async function setCommunityFit(sql: Sql, communityId: number, fit: boolean): Promise<void> {
  await sql`
    update public.communities set icp_flag = ${fit}, icp_decided_by = 'human',
      icp_reasons = ${JSON.stringify([fit ? 'human_approved' : 'human_rejected'])}::json,
      icp_checked_at = ${sql.unsafe(NOW)}, updated_at = ${sql.unsafe(NOW)}
    where id = ${communityId}`
  await sql`
    insert into warmr_app.community_state (community_id, icp_version, icp_at) values (${communityId}, 'human', ${sql.unsafe(NOW)})
    on conflict (community_id) do update set icp_version = 'human', icp_at = excluded.icp_at`
}

export async function resetProbe(sql: Sql, communityId: number): Promise<void> {
  await sql`update warmr_app.community_state set probed_at = null, icp_version = null where community_id = ${communityId}`
}

/**
 * Insert new communities by host. Returns how many were new. Existing rows
 * (same url or same slug) are left alone — the slug rule is the one that
 * cannot merge unrelated custom domains.
 */
export async function insertHosts(sql: Sql, hosts: string[], source: string): Promise<{ added: number; existing: number }> {
  let added = 0
  let existing = 0
  for (const host of hosts) {
    const url = `https://${host}`
    const slug = uniqueSlugForHost(host)
    const platform = platformFromHost(host)
    try {
      const rows = await sql`
        insert into public.communities (slug, url, discovered_at, discovery_source, platform, access_status, permission_status,
          relevance_score, relevant, updated_at, icp_score, icp_flag, join_status, join_attempts, watching)
        select ${slug}, ${url}, ${sql.unsafe(NOW)}, ${source.slice(0, 255)}, ${platform}, 'not_visited', 'candidate',
          0, false, ${sql.unsafe(NOW)}, 0, false, 'not_attempted', 0, false
        where not exists (
          select 1 from public.communities where url in (${url}, ${`${url}/`}, ${`http://${host}`}) or slug = ${slug})
        returning id`
      if (rows.length) added++
      else existing++
    } catch {
      existing++ // lost a race on the unique url/slug: someone else inserted it
    }
  }
  return { added, existing }
}

// ------------------------------------------------------------- directory ---

export interface DirectoryListing {
  externalId: string
  slug: string
  name: string | null
  description: string | null
  priceLabel: string | null
  goals: string[]
}

/** Upsert discover.circle.so listings; matched by external id, then by product URL. */
export async function upsertDirectoryListings(sql: Sql, listings: DirectoryListing[]): Promise<{ added: number; updated: number }> {
  let added = 0
  let updated = 0
  for (const listing of listings) {
    const productUrl = `https://discover.circle.so/products/${listing.slug}`
    const existing = await sql<{ id: number; directory_goals: unknown }[]>`
      select id, directory_goals from public.communities
      where external_directory_id = ${listing.externalId} or url = ${productUrl}
      order by (external_directory_id = ${listing.externalId}) desc limit 1`
    if (existing.length) {
      const prevGoals = Array.isArray(existing[0]!.directory_goals) ? (existing[0]!.directory_goals as string[]) : []
      const goals = [...new Set([...prevGoals, ...listing.goals])]
      await sql`
        update public.communities set
          external_directory_id = ${listing.externalId},
          directory_goals = ${JSON.stringify(goals)}::json,
          directory_synced_at = ${sql.unsafe(NOW)},
          name = coalesce(name, ${listing.name}),
          description = coalesce(description, ${listing.description}),
          price_label = coalesce(${listing.priceLabel}, price_label),
          updated_at = ${sql.unsafe(NOW)}
        where id = ${existing[0]!.id}`
      updated++
      continue
    }
    const taken = await sql`select 1 from public.communities where slug = ${listing.slug}`
    const slug = taken.length ? `discover-${listing.slug}`.slice(0, 255) : listing.slug
    const inserted = await sql`
      insert into public.communities (slug, url, name, description, price_label, discovered_at, discovery_source, platform,
        external_directory_id, directory_goals, directory_synced_at, access_status, permission_status, relevance_score,
        relevant, updated_at, icp_score, icp_flag, join_status, join_attempts, watching)
      values (${slug}, ${productUrl}, ${listing.name}, ${listing.description}, ${listing.priceLabel}, ${sql.unsafe(NOW)},
        'desktop:circle_directory', 'discover', ${listing.externalId}, ${JSON.stringify(listing.goals)}::json,
        ${sql.unsafe(NOW)}, 'not_visited', 'candidate', 0, false, ${sql.unsafe(NOW)}, 0, false, 'not_attempted', 0, false)
      on conflict do nothing
      returning id`
    if (inserted.length) added++
  }
  return { added, updated }
}

export interface UnresolvedListing {
  id: number
  slug: string
  url: string
  name: string | null
}

/** Directory rows still pointing at discover.circle.so, most promising first. */
export async function unresolvedListings(sql: Sql, limit: number): Promise<UnresolvedListing[]> {
  const rows = await sql`
    select c.id, c.slug, c.url, c.name
    from public.communities c
    left join warmr_app.community_state s on s.community_id = c.id
    where c.platform = 'discover' and c.url like 'https://discover.circle.so/products/%'
      and s.directory_resolved_at is null
      and (c.icp_flag or coalesce(s.icp_llm_fit, false) or c.icp_score >= 45)
    order by c.icp_score desc nulls last, c.id
    limit ${limit}`
  return rows.map((r) => ({ id: Number(r.id), slug: String(r.slug), url: String(r.url), name: (r.name as string | null) ?? null }))
}

/**
 * Point a directory row at its real community host. When another row already
 * owns that host, the listing's directory info is copied there instead and the
 * listing row is left as a resolved duplicate.
 */
export async function resolveListing(
  sql: Sql,
  listingId: number,
  outcome: { host: string | null; detail: string }
): Promise<'resolved' | 'merged' | 'unresolved'> {
  return sql.begin(async (tx) => {
    const mark = async (detail: string): Promise<void> => {
      await tx`
        insert into warmr_app.community_state (community_id, directory_resolved_at, directory_resolve_detail)
        values (${listingId}, ${tx.unsafe(NOW)}, ${detail.slice(0, 500)})
        on conflict (community_id) do update set directory_resolved_at = excluded.directory_resolved_at,
          directory_resolve_detail = excluded.directory_resolve_detail`
    }
    if (!outcome.host) {
      await mark(outcome.detail)
      return 'unresolved' as const
    }
    const url = `https://${outcome.host}`
    const slug = uniqueSlugForHost(outcome.host)
    const owner = await tx<{ id: number }[]>`
      select id from public.communities where (url in (${url}, ${`${url}/`}) or slug = ${slug}) and id <> ${listingId} limit 1`
    if (owner.length) {
      await tx`
        update public.communities t set
          external_directory_id = coalesce(t.external_directory_id, l.external_directory_id),
          directory_goals = coalesce(t.directory_goals, l.directory_goals),
          name = coalesce(t.name, l.name),
          description = coalesce(t.description, l.description),
          price_label = coalesce(t.price_label, l.price_label),
          updated_at = ${tx.unsafe(NOW)}
        from public.communities l
        where t.id = ${owner[0]!.id} and l.id = ${listingId}`
      await tx`update warmr_app.community_state set icp_version = null where community_id = ${owner[0]!.id}`
      await mark(`duplicate of community ${owner[0]!.id} (${outcome.host}); ${outcome.detail}`)
      return 'merged' as const
    }
    await tx`
      update public.communities set url = ${url}, slug = ${slug}, platform = null, updated_at = ${tx.unsafe(NOW)}
      where id = ${listingId}`
    await mark(`resolved to ${outcome.host}; ${outcome.detail}`)
    // platform = null + no probe yet: the verify stage probes it next and
    // sets platform = 'circle' only on a JSON 200.
    await tx`update warmr_app.community_state set probed_at = null, icp_version = null where community_id = ${listingId}`
    return 'resolved' as const
  })
}
