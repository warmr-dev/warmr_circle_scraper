import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setFetcher } from '@engine/circle/http'
import { circleGovernor, RateLimitedError } from '@engine/circle/governor'
import { probeHost, fetchPublicSpaces } from '@engine/circle/probe'
import { CircleReader, SessionInvalidError, BudgetExhaustedError, sessionCookies } from '@engine/circle/reader'
import { hostFromInput, hostsFromText, platformFromHost } from '@engine/discovery/hosts'
import { buildJoinScript, parseJoinOutput, JOIN_DRIVER_SOURCE } from '@engine/join/ego'
import { cookieHost } from '@engine/stages/join'
import { pickJoinHost } from '@engine/stages/discover'
import { normalizePost, normalizeComment, postPermalink } from '@engine/stages/scrape'
import { storedSourceId } from '@engine/circle/text'
import { decideIcp, icpUserMessage } from '@engine/icp/classify'
import { mergeSettings, DEFAULT_SHARED } from '@engine/defaults'
import { mapPool, naiveUtc } from '@engine/util'
import { parseNaiveUtc, normalizeDbUrl } from '@engine/db/client'
import { completeJson, estimateUsd, LlmUnavailableError, priceFor } from '@engine/llm/client'
import { z } from 'zod'

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json; charset=utf-8', ...headers } })
}
function html(status: number, body: string, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { 'content-type': 'text/html; charset=UTF-8', ...headers } })
}

const calls: string[] = []
function fake(handler: Handler): void {
  calls.length = 0
  setFetcher(async (url, init) => {
    calls.push(url)
    return handler(url, init)
  })
}

beforeEach(() => {
  circleGovernor.reset()
  circleGovernor.configure({ requestsPerMinute: 1_000_000, maxPerHour: 0, cooldownMinutes: 60 })
})
afterEach(() => setFetcher((url, init) => fetch(url, init)))

describe('probe (existence + join type)', () => {
  it('JSON 200 with is_private proves the community exists', async () => {
    fake(() => json(200, { id: 7, name: 'Builders', is_private: false, allow_signups_to_public_community: true }))
    const r = await probeHost('builders.circle.so')
    expect(r.exists).toBe('alive')
    expect(r.joinType).toBe('free_join')
    expect(r.name).toBe('Builders')
    expect(r.circleId).toBe(7)
  })

  it('401 is "locked_or_absent", never proof of existence (wildcard 401)', async () => {
    fake(() => json(401, { message: 'You cannot perform this action' }))
    const r = await probeHost('zzinvented.circle.so')
    expect(r.exists).toBe('locked_or_absent')
    expect(r.joinType).toBe('locked_unknown')
    expect(calls).toHaveLength(1) // no www retry after a 401, and none on *.circle.so
  })

  it('retries www.<host> for a custom domain whose apex fails', async () => {
    fake((url) =>
      url.startsWith('https://www.') ? json(200, { name: 'Acme', is_private: false, has_non_draft_paywalls: true }) : html(404, 'not found')
    )
    const r = await probeHost('acme.com')
    expect(r.exists).toBe('alive')
    expect(r.joinType).toBe('paid')
    expect(r.answeredHost).toBe('www.acme.com')
    expect(r.detail).toContain('www retry')
  })

  it('a 200 page that is not Circle is not_circle', async () => {
    fake(() => html(200, '<html>marketing</html>'))
    const r = await probeHost('example.org')
    expect(r.exists).toBe('not_circle')
  })

  it('network failure is unreachable with the reason kept', async () => {
    fake(() => {
      throw new TypeError('fetch failed')
    })
    const r = await probeHost('dead.example')
    expect(r.exists).toBe('unreachable')
    expect(r.detail).toContain('request failed')
  })

  it('public spaces list yields names and counts', async () => {
    fake(() => json(200, { records: [{ id: 1, name: 'Jobs', space_type: 'basic', posts_count: 12, space_members_count: 300 }] }))
    const s = await fetchPublicSpaces('x.circle.so')
    expect(s.ok).toBe(true)
    expect(s.spaces[0]).toMatchObject({ id: '1', name: 'Jobs', type: 'basic', postsCount: 12, membersCount: 300 })
  })
})

describe('member reader', () => {
  const opts = () => ({ cookies: { _circle_session: 'abc' }, budget: { remaining: 100 } })

  it('paginates posts until has_next_page is false', async () => {
    fake((url) => {
      const page = Number(new URL(url).searchParams.get('page'))
      return json(200, { records: [{ id: page }], has_next_page: page < 3 })
    })
    const reader = new CircleReader('x.circle.so', opts())
    const pages = [await reader.postsPage('9', 1), await reader.postsPage('9', 2), await reader.postsPage('9', 3)]
    expect(pages.map((p) => p.hasNext)).toEqual([true, true, false])
  })

  it('reads every comment page', async () => {
    fake((url) => {
      const page = Number(new URL(url).searchParams.get('page'))
      return json(200, { records: [{ id: `c${page}` }], has_next_page: page < 4 })
    })
    const reader = new CircleReader('x.circle.so', opts())
    expect((await reader.comments('5')).map((c) => c.id)).toEqual(['c1', 'c2', 'c3', 'c4'])
  })

  it('401 on the spaces list with a cookie means the session expired', async () => {
    fake(() => json(401, {}))
    await expect(new CircleReader('x.circle.so', opts()).listSpaces()).rejects.toBeInstanceOf(SessionInvalidError)
  })

  it('a Cloudflare challenge stops the reader instead of being worked around', async () => {
    fake(() => html(403, '<html>__cf_chl_opt</html>', { 'cf-mitigated': 'challenge' }))
    await expect(new CircleReader('x.circle.so', opts()).listSpaces()).rejects.toBeInstanceOf(RateLimitedError)
  })

  it('reads comment counts from post_details (the posts list has none)', async () => {
    fake((url) => {
      expect(url).toContain('/internal_api/post_details?post_ids=1%2C2&space_id=9')
      return json(200, [{ id: 1, comments_count: 3, last_replied_or_posted: '2026-09-01T00:00:00Z' }, { id: 2, comments_count: 0 }])
    })
    const details = await new CircleReader('x.circle.so', opts()).postDetails('9', ['1', '2'])
    expect(details?.get('1')).toEqual({ commentsCount: 3, lastActivity: '2026-09-01T00:00:00Z' })
    expect(details?.get('2')?.commentsCount).toBe(0)
  })

  it('respects the request budget', async () => {
    fake(() => json(200, { records: [], has_next_page: false }))
    const reader = new CircleReader('x.circle.so', { ...opts(), budget: { remaining: 1 } })
    await reader.postsPage('1', 1)
    await expect(reader.postsPage('1', 2)).rejects.toBeInstanceOf(BudgetExhaustedError)
  })

  it('keeps only the three session cookies', () => {
    expect(
      sessionCookies([
        { name: '_circle_session', value: 'a' },
        { name: 'cf_clearance', value: 'x' },
        { name: 'user_session_identifier', value: 'b' }
      ])
    ).toEqual({ _circle_session: 'a', user_session_identifier: 'b' })
  })
})

describe('post normalization', () => {
  const space = { id: '3', name: 'Jobs', slug: 'jobs', type: 'basic', postsCount: null, isPrivate: null }

  it('uses the full tiptap body, redacts contacts, keys the row like Python', () => {
    const post = normalizePost(
      {
        id: 42,
        name: 'Hiring a React dev',
        slug: 'hiring-a-react-dev',
        created_at: '2026-09-01T10:00:00.000Z',
        comments_count: 2,
        tiptap_body: { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Email me: a@b.co' }] }] },
        user: { id: 9, name: 'Ann' }
      },
      space,
      'https://x.circle.so',
      'member_session'
    )!
    expect(post.circleId).toBe('42')
    expect(post.kind).toBe('post')
    expect(post.sourceContentId).toBe(storedSourceId(post.content))
    expect(post.contentType).toBe('post')
    expect(post.content).toBe('Hiring a React dev\n\nEmail me: [email removed]')
    expect(post.url).toBe('https://x.circle.so/c/jobs/hiring-a-react-dev')
    expect(post.author).toEqual({ sourceAuthorId: '9', displayName: 'Ann', profileUrl: null })
    expect(post.commentsCount).toBe(2)
    expect(post.publishedAt?.toISOString()).toBe('2026-09-01T10:00:00.000Z')
  })

  it('comments are stored like Python (content key, type post) and remember their post', () => {
    const c = normalizeComment({ id: 7, body_plain_text: 'DM me', created_at: '2026-09-02T00:00:00Z' }, '42', 'https://x.circle.so', 'member_session', null)!
    expect(c.circleId).toBe('7')
    expect(c.kind).toBe('comment')
    expect(c.sourceContentId).toBe(storedSourceId('DM me'))
    expect(c.threadId).toBe('42')
    expect(c.contentType).toBe('post')
  })

  it('prefers an explicit url over the built permalink', () => {
    expect(postPermalink({ url: '/c/jobs/x', slug: 'x' }, space, 'https://h')).toBe('https://h/c/jobs/x')
  })
})

describe('hosts', () => {
  it('parses links, bare hosts and junk', () => {
    expect(hostFromInput('https://Foo.circle.so/c/general?x=1')).toBe('foo.circle.so')
    expect(hostFromInput('community.acme.com/')).toBe('community.acme.com')
    expect(hostFromInput('not a host')).toBeNull()
    const { hosts, invalid } = hostsFromText('a.circle.so\nhttps://b.com/x, a.circle.so; ???')
    expect(hosts).toEqual(['a.circle.so', 'b.com'])
    expect(invalid).toBe(1)
  })

  it('classifies platforms by name', () => {
    expect(platformFromHost('app.circle.so')).toBe('circle_infra')
    expect(platformFromHost('builders.circle.so')).toBe('circle')
    expect(platformFromHost('x.skool.com')).toBe('skool')
    expect(platformFromHost('community.acme.com')).toBeNull()
  })
})

describe('ego bridge', () => {
  it('injects parameters as JSON literals, safe for $ in passwords', () => {
    const script = buildJoinScript({ spaceId: 2, url: 'https://x.circle.so', email: 'a@b.co', password: "p$&w$'d\"", screenshotDir: null })
    expect(script).toContain('const spaceId = 2;')
    expect(script).toContain('const url = "https://x.circle.so";')
    expect(script).toContain(`const loginPassword = ${JSON.stringify("p$&w$'d\"")};`)
    expect(script).not.toMatch(/__EGO_JOIN_(SPACE_ID|URL|EMAIL|PASSWORD|SCREENSHOT_DIR)__/)
  })

  it('the bundled driver is the proven one', () => {
    expect(JOIN_DRIVER_SOURCE).toContain('EGO_JOIN_RESULT=')
    expect(JOIN_DRIVER_SOURCE).toContain('takeOverTaskSpace')
  })

  it('reads the result marker from stderr', () => {
    const r = parseJoinOutput('', 'noise\nEGO_JOIN_RESULT={"status":"joined","detail":"ok","cookies":[{"name":"_circle_session","value":"v","domain":"x.circle.so"}]}\n')
    expect(r.status).toBe('joined')
    expect(cookieHost(r, 'https://discover.circle.so/products/x')).toBe('x.circle.so')
  })

  it('falls back to the candidate host when cookies have no usable domain', () => {
    expect(cookieHost({ status: 'joined', detail: '', screenshot: null, cookies: [{ name: 'a', value: 'b', domain: '.circle.so' }] }, 'https://y.circle.so/')).toBe('y.circle.so')
  })
})

describe('directory', () => {
  it('picks the join anchor that leaves the directory', () => {
    expect(
      pickJoinHost([
        { text: 'Log in', href: 'https://login.circle.so' },
        { text: 'Join for free', href: 'https://discover.circle.so/checkout/1' },
        { text: 'Join', href: 'https://community.acme.com/join?invitation_token=1' }
      ]).host
    ).toBe('community.acme.com')
    expect(pickJoinHost([{ text: 'Buy', href: '/checkout' }]).host).toBeNull()
  })
})

describe('ICP decision', () => {
  const settings = { useLlm: true, mode: 'all' as const, minConfidence: 0.6, autoApproveLlm: true, profile: '' }

  it('never sends an empty row to the LLM', async () => {
    const d = await decideIcp(
      { name: null, description: null, spaceNames: [], goals: [], host: 'x', priceLabel: null, joinType: null, membersTotal: null },
      settings,
      { provider: 'openai', model: 'gpt-4o-mini', apiKey: 'sk-test' },
      { budgetOk: true }
    )
    expect(d.reasons).toEqual(['no_metadata'])
    expect(d.skippedLlm).toBe('no_text')
  })

  it('falls back to rules without a key, using space names as evidence', async () => {
    const d = await decideIcp(
      { name: 'Makers', description: null, spaceNames: ['SaaS founders', 'Hiring & projects'], goals: [], host: 'm', priceLabel: null, joinType: null, membersTotal: null },
      settings,
      null,
      { budgetOk: true }
    )
    expect(d.decidedBy).toBe('rules')
    expect(d.ruleScore).toBeGreaterThanOrEqual(45)
    expect(d.flag).toBe(true)
  })

  it('the prompt carries sections and categories', () => {
    const msg = icpUserMessage({ name: 'N', description: null, spaceNames: ['A', 'B'], goals: ['g'], host: 'h', priceLabel: 'Free', joinType: 'free_join', membersTotal: 10 })
    expect(msg).toContain('Sections: A · B')
    expect(msg).toContain('Directory categories: g')
  })

  it('an unreachable LLM marks the verdict retryable instead of final', async () => {
    vi.stubGlobal('fetch', async () => json(402, { error: { message: 'Insufficient credits' } }))
    try {
      const d = await decideIcp(
        { name: 'Makers', description: 'SaaS founders', spaceNames: [], goals: [], host: 'm', priceLabel: null, joinType: null, membersTotal: null },
        settings,
        { provider: 'openrouter', model: 'anthropic/claude-sonnet-5', apiKey: 'sk-or-test' },
        { budgetOk: true }
      )
      expect(d.decidedBy).toBe('rules')
      expect(d.llmRetryable).toBe(true)
      expect(d.llmError).toContain('402')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('OpenRouter client', () => {
  const cfg = { provider: 'openrouter' as const, model: 'anthropic/claude-sonnet-5', apiKey: 'sk-or-test' }
  const schema = z.object({ ok: z.boolean() })
  const args = {
    system: 'sys',
    user: 'usr',
    schema,
    jsonSchema: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'], additionalProperties: false },
    schemaName: 'ping'
  }
  afterEach(() => vi.unstubAllGlobals())

  it('sends a strict schema only to endpoints that enforce it and counts the billed cost', async () => {
    let seen: { url: string; init: RequestInit } | null = null
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      seen = { url, init }
      return json(200, {
        model: 'anthropic/claude-sonnet-5',
        choices: [{ message: { content: '{"ok": true}' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 850, completion_tokens: 130, cost: 0.0031 }
      })
    })
    const res = await completeJson(cfg, args)
    expect(res.data).toEqual({ ok: true })
    expect(res.usage).toEqual({ inputTokens: 850, outputTokens: 130, usd: 0.0031, priced: true })
    expect(seen!.url).toBe('https://openrouter.ai/api/v1/chat/completions')
    expect((seen!.init.headers as Record<string, string>).Authorization).toBe('Bearer sk-or-test')
    const body = JSON.parse(String(seen!.init.body))
    expect(body.model).toBe('anthropic/claude-sonnet-5')
    expect(body.response_format).toEqual({ type: 'json_schema', json_schema: { name: 'ping', strict: true, schema: args.jsonSchema } })
    expect(body.provider).toEqual({ require_parameters: true })
    expect(body.messages.map((m: { role: string }) => m.role)).toEqual(['system', 'user'])
  })

  it('falls back to list prices when no cost is reported, and reads fenced JSON', async () => {
    vi.stubGlobal('fetch', async () =>
      json(200, { choices: [{ message: { content: '```json\n{"ok": false}\n```' } }], usage: { prompt_tokens: 1_000_000, completion_tokens: 0 } })
    )
    const res = await completeJson({ ...cfg, model: 'anthropic/claude-haiku-4.5' }, args)
    expect(res.data).toEqual({ ok: false })
    expect(res.usage.usd).toBeCloseTo(1)
    expect(res.usage.priced).toBe(true)
  })

  it('no credits or a bad key means "unavailable", not a verdict about the item', async () => {
    vi.stubGlobal('fetch', async () => json(402, { error: { message: 'Insufficient credits' } }))
    await expect(completeJson(cfg, args)).rejects.toBeInstanceOf(LlmUnavailableError)
    vi.stubGlobal('fetch', async () => json(401, { error: { message: 'No auth credentials found' } }))
    await expect(completeJson(cfg, args)).rejects.toThrow(/неверный ключ/)
  })

  it('a moderation refusal is about this input, so it is not retried as an outage', async () => {
    vi.stubGlobal('fetch', async () => json(403, { error: { message: 'flagged', metadata: { reasons: ['harassment'] } } }))
    const err = await completeJson(cfg, args).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(Error)
    expect(err).not.toBeInstanceOf(LlmUnavailableError)
  })
})

describe('Circle governor (per-IP rate limit)', () => {
  it('a 429 trips a machine-wide cool-down: probes stop instead of recording "unreachable"', async () => {
    fake(() => html(429, 'Too Many Requests', { 'cf-mitigated': 'challenge' }))
    await expect(probeHost('a.circle.so')).rejects.toBeInstanceOf(RateLimitedError)
    expect(circleGovernor.coolingDownUntil).not.toBeNull()
    fake(() => json(200, { is_private: false }))
    await expect(probeHost('b.circle.so')).rejects.toBeInstanceOf(RateLimitedError)
    expect(calls).toHaveLength(0) // nothing leaves the machine during the cool-down
  })

  it('paces requests to the per-minute limit', async () => {
    circleGovernor.configure({ requestsPerMinute: 600 }) // one per 100 ms
    fake(() => json(200, { is_private: false }))
    const started = Date.now()
    for (let i = 0; i < 4; i++) await probeHost(`h${i}.circle.so`)
    expect(Date.now() - started).toBeGreaterThanOrEqual(290)
  })

  it('an ordinary 404 page with Cloudflare scripts is NOT a challenge (no false cool-down)', async () => {
    fake(() => html(404, '<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>Not found</html>'))
    const r = await probeHost('gone.example.com')
    expect(r.exists).toBe('unreachable')
    expect(circleGovernor.coolingDownUntil).toBeNull()
  })

  it('a managed challenge page (403 + Just a moment) is a challenge', async () => {
    fake(() => html(403, '<html><head><title>Just a moment...</title></head></html>'))
    await expect(probeHost('c.circle.so')).rejects.toBeInstanceOf(RateLimitedError)
  })

  it('restores a cool-down saved before a restart', () => {
    const until = new Date(Date.now() + 60_000)
    circleGovernor.restoreCooldown(until)
    expect(circleGovernor.coolingDownUntil?.getTime()).toBe(until.getTime())
  })
})

describe('misc', () => {
  it('merges settings without accepting unknown keys or wrong types', () => {
    const merged = mergeSettings(DEFAULT_SHARED, { join: { maxVisitsPerDay: 10, bogus: 1 }, autopilot: 'yes' })
    expect(merged.join.maxVisitsPerDay).toBe(10)
    expect((merged.join as Record<string, unknown>).bogus).toBeUndefined()
    expect(merged.autopilot).toBe(false)
  })

  it('naive UTC round trip', () => {
    const d = new Date('2026-09-18T08:36:00.123Z')
    expect(naiveUtc(d)).toBe('2026-09-18T08:36:00.123')
    expect(parseNaiveUtc('2026-09-18 08:36:00.123').toISOString()).toBe('2026-09-18T08:36:00.123Z')
  })

  it('rewrites a session-mode Supabase pooler URL to the transaction pooler', () => {
    expect(normalizeDbUrl('postgresql://u:p@aws-0-eu.pooler.supabase.com:5432/postgres')).toContain(':6543/')
    expect(normalizeDbUrl('"postgresql+psycopg://u:p@h:6543/db"')).toBe('postgresql://u:p@h:6543/db')
  })

  it('prices known models and marks unknown ones', () => {
    expect(priceFor('claude-opus-5').known).toBe(true)
    expect(priceFor('some-new-model').known).toBe(false)
    expect(priceFor('anthropic/claude-haiku-4.5')).toEqual({ rates: [1, 5], known: true })
    expect(priceFor('openai/gpt-4.1-mini')).toEqual({ rates: [0.4, 1.6], known: true })
    expect(priceFor('anthropic/claude-sonnet-5').rates).toEqual([2, 10])
    expect(estimateUsd('gpt-4o-mini', 1_000_000, 0).usd).toBeCloseTo(0.15)
  })

  it('mapPool keeps order and bounds concurrency', async () => {
    let active = 0
    let peak = 0
    const out = await mapPool([1, 2, 3, 4, 5], 2, async (n) => {
      active++
      peak = Math.max(peak, active)
      await new Promise((r) => setTimeout(r, 5))
      active--
      return n * 2
    })
    expect(out).toEqual([2, 4, 6, 8, 10])
    expect(peak).toBeLessThanOrEqual(2)
  })
})
