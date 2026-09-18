import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { createSql, type Sql } from '@engine/db/client'
import { runMigrations } from '@engine/db/migrations'
import { runVerify } from '@engine/stages/verify'
import { runScrape } from '@engine/stages/scrape'
import { importCommunities } from '@engine/stages/discover'
import { DEFAULT_SHARED, DEFAULT_LOCAL, mergeSettings } from '@engine/defaults'
import type { StageContext } from '@engine/context'
import * as views from '@engine/db/views'
import { joinCandidates, visitsToday } from '@engine/db/content'
import { acquireLock, releaseLock, upsertDirectoryListings, resolveListing, readOldWorkerSchedules } from '@engine/db/repo'
import type { SharedSettings } from '@shared/types'

// Runs the real stages against a DISPOSABLE database (never prod): set
// WARMR_TEST_DB to a local Postgres seeded with the Python schema. Hits real
// Circle endpoints (anonymous probes; one small member read).
const DB = process.env.WARMR_TEST_DB
const d = DB ? describe : describe.skip

function context(sql: Sql, settings: SharedSettings, logs: string[]): StageContext {
  return {
    sql,
    deviceId: 'test-device',
    settings,
    local: { ...DEFAULT_LOCAL },
    secrets: {},
    signal: new AbortController().signal,
    screenshotsDir: '/tmp',
    browser: null,
    log: (level, message) => logs.push(`${level}: ${message}`),
    progress: () => {},
    updateLocal: async () => {}
  }
}

d('integration (disposable DB, live Circle)', () => {
  let sql: Sql
  const logs: string[] = []
  const settings = mergeSettings(DEFAULT_SHARED, {
    verify: { batchSize: 60, concurrency: 3 },
    icp: { useLlm: false, batchSize: 200 },
    scrape: { maxRequestsPerRun: 400, includePublic: false }
  })

  beforeAll(async () => {
    if (DB && /supabase\.com/.test(DB)) throw new Error('refusing to run integration tests against Supabase')
    sql = createSql(DB!)
    await runMigrations(sql)
  })
  afterAll(async () => {
    await sql?.end({ timeout: 5 })
  })

  it('migrations are idempotent and create the warmr_app schema', async () => {
    expect(await runMigrations(sql)).toEqual([])
    const tables = await sql`select table_name from information_schema.tables where table_schema = 'warmr_app' order by 1`
    expect(tables.map((t) => t.table_name)).toEqual(
      expect.arrayContaining(['community_state', 'join_attempts', 'tasks', 'space_sync', 'post_meta', 'llm_calls', 'locks', 'runs', 'settings', 'devices'])
    )
  })

  it('verify: probes real hosts, stores state, judges ICP by rules', async () => {
    const result = await runVerify(context(sql, settings, logs))
    expect(result.counts.probed).toBeGreaterThan(20)
    const states = await sql`select exists_status, count(*)::int as n from warmr_app.community_state where probed_at is not null group by 1`
    const byStatus = Object.fromEntries(states.map((s) => [s.exists_status, s.n]))
    expect(byStatus.alive ?? 0).toBeGreaterThan(0)
    const alive = await sql`
      select c.join_type, c.join_type_checked_at, c.platform, s.space_names from public.communities c
      join warmr_app.community_state s on s.community_id = c.id where s.exists_status = 'alive' limit 5`
    for (const row of alive) {
      expect(['free_join', 'paid', 'invite_only', 'subscription_expired']).toContain(row.join_type)
      expect(row.platform).toBe('circle')
      expect(row.join_type_checked_at).toBeInstanceOf(Date)
    }
    const judged = await sql`select count(*)::int as n from warmr_app.community_state where icp_version is not null`
    expect(judged[0]!.n).toBeGreaterThan(0)
    console.log('verify:', result.summary)
  }, 240_000)

  it('scrape: reads a whole small community with real Circle ids, then only what is new', async () => {
    const target = await sql`select id from public.communities where url = 'https://circle-for-impact.circle.so'`
    expect(target.length).toBe(1)
    const communityId = Number(target[0]!.id)
    const first = await runScrape(context(sql, settings, logs), { communityId })
    console.log('scrape #1:', first.summary)
    const posts = await sql`
      select content_type, count(*)::int as n,
        count(*) filter (where source_content_id like 'triage:%')::int as legacy,
        count(*) filter (where space_id is null)::int as no_space
      from public.posts where community_id = ${communityId} group by 1`
    const byType = Object.fromEntries(posts.map((p) => [p.content_type, p]))
    expect(byType.post?.n ?? 0).toBeGreaterThan(0)
    expect(byType.post?.legacy ?? 0).toBe(0)
    expect(byType.post?.no_space ?? 0).toBe(0)
    const syncs = await sql`select count(*)::int as n, bool_and(backfill_done) as done from warmr_app.space_sync where community_id = ${communityId}`
    expect(syncs[0]!.n).toBeGreaterThan(0)
    const conn = await sql`select state from public.circle_connections where host = 'circle-for-impact.circle.so'`
    expect(['connected', 'error', 'session_expired']).toContain(conn[0]!.state)

    if (conn[0]!.state === 'connected' && syncs[0]!.done) {
      const second = await runScrape(context(sql, settings, logs), { communityId })
      console.log('scrape #2:', second.summary)
      expect(second.counts.newPosts).toBeLessThanOrEqual(3)
      expect(second.counts.requests).toBeLessThan(first.counts.requests)
    }
  }, 600_000)

  it('import: adds new hosts once, skips non-Circle platforms', async () => {
    const r1 = await importCommunities({ sql }, 'zz-warmr-test-1.circle.so\nhttps://zz-warmr-test-2.example.org/c/x\nfoo.slack.com\n???')
    expect(r1.added).toBe(2)
    const r2 = await importCommunities({ sql }, 'zz-warmr-test-1.circle.so')
    expect(r2.added).toBe(0)
    expect(r2.existing).toBe(1)
    const row = await sql`select slug, platform, join_status, icp_flag from public.communities where url = 'https://zz-warmr-test-2.example.org'`
    expect(row[0]).toMatchObject({ slug: 'zz-warmr-test-2.example.org', platform: null, join_status: 'not_attempted', icp_flag: false })
  })

  it('directory listings upsert and resolve (merge into an existing host)', async () => {
    const res = await upsertDirectoryListings(sql, [
      { externalId: 'test-999001', slug: 'zz-test-listing', name: 'ZZ Test Listing', description: 'founders building SaaS', priceLabel: 'Free', goals: ['start-and-scale-my-business'] }
    ])
    expect(res.added + res.updated).toBe(1)
    const listing = await sql`select id from public.communities where external_directory_id = 'test-999001'`
    const merged = await resolveListing(sql, Number(listing[0]!.id), { host: 'zz-warmr-test-1.circle.so', detail: 'test' })
    expect(merged).toBe('merged')
    const target = await sql`select external_directory_id, name from public.communities where url = 'https://zz-warmr-test-1.circle.so'`
    expect(target[0]!.external_directory_id).toBe('test-999001')
  })

  it('UI queries run on real shapes', async () => {
    const f = await views.funnel(sql, { includePaid: true, account: 'main', visitCap: 25 })
    expect(f.total).toBeGreaterThan(100)
    const list = await views.listCommunities(sql, { icp: 'all', sort: 'icp', limit: 20 })
    expect(list.rows.length).toBeGreaterThan(0)
    const detail = await views.communityDetail(sql, list.rows[0]!.id)
    expect(detail?.id).toBe(list.rows[0]!.id)
    const fits = await views.listCommunities(sql, { icp: 'fit', exists: 'alive', search: 'a' })
    expect(Array.isArray(fits.rows)).toBe(true)
    const p = await views.posts(sql, { limit: 5 })
    expect(p.total).toBeGreaterThanOrEqual(0)
    expect(await views.tasks(sql, 'open')).toBeInstanceOf(Array)
    expect(await joinCandidates(sql, { limit: 5, includePaid: true })).toBeInstanceOf(Array)
    expect(await visitsToday(sql, 'main')).toBe(0)
    expect((await readOldWorkerSchedules(sql)).harvest_schedule).toBeDefined()
  })

  it('stage locks exclude a second holder until released', async () => {
    expect(await acquireLock(sql, 'stage:test', 'a', 60)).toBe(true)
    expect(await acquireLock(sql, 'stage:test', 'b', 60)).toBe(false)
    expect(await acquireLock(sql, 'stage:test', 'a', 60)).toBe(true)
    await releaseLock(sql, 'stage:test', 'a')
    expect(await acquireLock(sql, 'stage:test', 'b', 60)).toBe(true)
    await releaseLock(sql, 'stage:test', 'b')
  })
})

d('ICP never downgrades earlier decisions (disposable DB)', () => {
  it('rules-only keeps LLM and human verdicts; an LLM run still skips human ones', async () => {
    if (/supabase\.com/.test(DB!)) throw new Error('refusing to run against Supabase')
    const { icpCandidates } = await import('@engine/db/repo')
    const sql = createSql(DB!)
    const rows = await sql`
      select c.id from public.communities c join warmr_app.community_state s on s.community_id = c.id
      where s.exists_status = 'alive' order by c.id limit 2`
    const [llmRow, humanRow] = rows.map((r) => Number(r.id))
    await sql`update public.communities set icp_decided_by = 'llm' where id = ${llmRow!}`
    await sql`update public.communities set icp_decided_by = 'human' where id = ${humanRow!}`
    await sql`update warmr_app.community_state set icp_version = null where community_id in ${sql([llmRow!, humanRow!])}`
    const rulesOnly = (await icpCandidates(sql, { limit: 10_000, version: 'v-test', withLlm: false })).map((c) => c.id)
    expect(rulesOnly).not.toContain(llmRow)
    expect(rulesOnly).not.toContain(humanRow)
    const withLlm = (await icpCandidates(sql, { limit: 10_000, version: 'v-test', withLlm: true })).map((c) => c.id)
    expect(withLlm).toContain(llmRow)
    expect(withLlm).not.toContain(humanRow)
    await sql.end()
  })
})
