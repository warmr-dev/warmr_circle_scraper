import { describe, expect, it } from 'vitest'
import { createSql } from '@engine/db/client'
import { runScrape } from '@engine/stages/scrape'
import { DEFAULT_SHARED, DEFAULT_LOCAL, mergeSettings } from '@engine/defaults'

// One-off live read of a second small community into the DISPOSABLE test DB,
// to exercise comments and replies end to end. Skipped unless WARMR_TEST_DB
// and WARMR_TEST_HOST are set; refuses to run against Supabase.
const DB = process.env.WARMR_TEST_DB
const HOST = process.env.WARMR_TEST_HOST
const d = DB && HOST ? describe : describe.skip

d('live scrape of one host', () => {
  it('reads posts, comments and replies', async () => {
    if (/supabase\.com/.test(DB!)) throw new Error('refusing to run against Supabase')
    const sql = createSql(DB!)
    const [row] = await sql`select id from public.communities where url = ${`https://${HOST}`}`
    const logs: string[] = []
    const settings = mergeSettings(DEFAULT_SHARED, { scrape: { maxRequestsPerRun: 120, includePublic: false } })
    const result = await runScrape(
      {
        sql, deviceId: 't', settings, local: { ...DEFAULT_LOCAL }, secrets: {}, signal: new AbortController().signal,
        screenshotsDir: '/tmp', browser: null, log: (l, m) => logs.push(`${l}: ${m}`), progress: () => {}, updateLocal: async () => {}
      },
      { communityId: Number(row!.id) }
    )
    console.log(result.summary, '\n', logs.join('\n'))
    expect(result.counts.communities).toBe(1)
    await sql.end()
  }, 600_000)
})
