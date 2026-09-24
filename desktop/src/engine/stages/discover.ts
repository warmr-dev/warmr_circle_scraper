import type { StageContext, StageResult } from '../context'
import { resolveListing, unresolvedListings, upsertDirectoryListings, logActivity, insertHosts, type DirectoryListing } from '../db/repo'
import { hostFromInput, hostsFromText, platformFromHost } from '../discovery/hosts'
import { sleep, throwIfAborted } from '../util'
import { circleGovernor } from '../circle/governor'

// discover.circle.so is Circle's own zone too: keep the browser calls slow.
const DIRECTORY_GAP_MS = 2500

// Stage 1: find communities.
// Source 1 — Circle's own marketplace, discover.circle.so. Plain HTTP gets a
// Cloudflare challenge there (curl: cf-mitigated: challenge), so the calls run
// inside the app's own Chromium window: same-origin fetches to the site's own
// public API (captured from its frontend, see circle_directory.py).
// Source 2 — lists the user pastes or imports (hosts, links, CSV).
// Existence is NOT decided here; the verify stage proves it with a JSON 200.

const DISCOVER_ORIGIN = 'https://discover.circle.so'
const JOIN_ANCHOR_TEXTS = ['join', 'sign up', 'get access', 'get started', 'enroll', 'buy', 'register', 'request to join']

interface RawListing {
  id?: number | string
  slug?: string
  name?: string
  short_description?: string
  human_readable_price?: string
}

function listingsPath(page: number, goal?: string): string {
  const goalParam = goal ? `goal_slugs[]=${encodeURIComponent(goal)}&` : ''
  return `/discover/public_api/listings/search?${goalParam}tags=&page=${page}&per_page=100`
}

async function crawlDirectory(ctx: StageContext): Promise<DirectoryListing[]> {
  const browser = ctx.browser!
  const [goalsRes] = await browser.fetchJson(DISCOVER_ORIGIN, ['/discover/public_api/goals?featured=true&per_page=20'], ctx.signal)
  if (!goalsRes || goalsRes.status !== 200) throw new Error(`каталог не ответил на список категорий (HTTP ${goalsRes?.status ?? 0})`)
  const goals = (((goalsRes.json as { records?: Array<{ slug?: string }> })?.records ?? []).map((g) => g.slug).filter(Boolean)) as string[]

  const byId = new Map<string, DirectoryListing>()
  const collect = (records: RawListing[], goal?: string): void => {
    for (const r of records) {
      if (r.id == null || !r.slug) continue
      const id = String(r.id)
      const existing = byId.get(id)
      if (existing) {
        if (goal && !existing.goals.includes(goal)) existing.goals.push(goal)
        continue
      }
      byId.set(id, {
        externalId: id,
        slug: r.slug,
        name: r.name?.trim() || null,
        description: r.short_description?.trim() || null,
        priceLabel: r.human_readable_price?.trim() || null,
        goals: goal ? [goal] : []
      })
    }
  }

  for (const goal of [undefined, ...goals]) {
    let page = 1
    while (true) {
      throwIfAborted(ctx.signal)
      await sleep(DIRECTORY_GAP_MS, ctx.signal)
      const [res] = await browser.fetchJson(DISCOVER_ORIGIN, [listingsPath(page, goal)], ctx.signal)
      if (!res || res.status !== 200) {
        ctx.log('warn', `Каталог: HTTP ${res?.status ?? 0} на странице ${page}${goal ? ` (${goal})` : ''}`)
        break
      }
      const body = res.json as { records?: RawListing[]; pagination?: { total_count?: number } }
      const records = body?.records ?? []
      collect(records, goal)
      const total = body?.pagination?.total_count ?? 0
      ctx.progress(byId.size, Math.max(byId.size, total), 'каталог')
      if (!records.length || page * 100 >= total || page >= 60) break
      page++
    }
  }
  return [...byId.values()]
}

/** Find the "Join"-style anchor on a product page that leaves discover.circle.so. */
export function pickJoinHost(anchors: Array<{ text: string; href: string }>): { host: string | null; detail: string } {
  for (const wanted of JOIN_ANCHOR_TEXTS) {
    for (const a of anchors) {
      const text = a.text.trim().toLowerCase()
      if (!text.startsWith(wanted)) continue
      const host = hostFromInput(a.href)
      if (!host || host === 'discover.circle.so') continue
      const platform = platformFromHost(host)
      if (platform === 'circle_infra') continue
      return { host, detail: `anchor "${a.text.trim().slice(0, 40)}" -> ${host}` }
    }
  }
  return { host: null, detail: 'no join anchor leading off discover.circle.so (paid checkout inside the directory, or an unknown layout)' }
}

export async function runDiscover(ctx: StageContext, opts: { crawl?: boolean } = {}): Promise<StageResult> {
  const counts: Record<string, number> = {}
  if (!ctx.browser) throw new Error('каталогу нужен встроенный браузер приложения')
  const cooling = circleGovernor.coolingDownUntil
  if (cooling) return { summary: `пауза: Circle ограничил запросы до ${cooling.toLocaleTimeString('ru-RU')}`, counts }
  if (opts.crawl !== false) {
    ctx.log('info', 'Обхожу каталог discover.circle.so')
    const listings = await crawlDirectory(ctx)
    const { added, updated } = await upsertDirectoryListings(ctx.sql, listings)
    counts.listings = listings.length
    counts.added = added
    counts.updated = updated
    ctx.log('success', `Каталог: ${listings.length} карточек, новых ${added}`)
  }

  const pending = await unresolvedListings(ctx.sql, ctx.settings.discover.resolveBatch)
  if (pending.length) ctx.log('info', `Ищу настоящие адреса для ${pending.length} подходящих карточек каталога`)
  for (let i = 0; i < pending.length; i++) {
    throwIfAborted(ctx.signal)
    const listing = pending[i]!
    ctx.progress(i, pending.length, listing.name || listing.slug)
    if (i > 0) await sleep(DIRECTORY_GAP_MS, ctx.signal)
    try {
      const page = await ctx.browser.pageAnchors(listing.url, ctx.signal)
      if (page.challenged) {
        ctx.log('warn', 'Каталог показал проверку Cloudflare — останавливаюсь, пройдите её в открывшемся окне и запустите снова')
        counts.challenged = 1
        break
      }
      const outcome = pickJoinHost(page.anchors)
      const result = await resolveListing(ctx.sql, listing.id, outcome)
      counts[result] = (counts[result] ?? 0) + 1
    } catch (err) {
      if (ctx.signal.aborted) throw err
      counts.errors = (counts.errors ?? 0) + 1
      ctx.log('warn', `${listing.url}: ${err instanceof Error ? err.message : String(err)}`)
    }
  }
  const summary =
    (counts.listings != null ? `каталог: ${counts.listings} карточек, новых ${counts.added ?? 0}; ` : '') +
    `адресов найдено ${counts.resolved ?? 0}, совпало с известными ${counts.merged ?? 0}, без адреса ${counts.unresolved ?? 0}`
  await logActivity(ctx.sql, { kind: 'discover', level: 'info', summary: `Desktop discover: ${summary}`, detail: counts })
  return { summary, counts }
}

/** Manual import: any text with hosts or links, one per line or comma separated. */
export async function importCommunities(ctx: Pick<StageContext, 'sql'>, text: string): Promise<{ added: number; existing: number; invalid: number }> {
  const { hosts, invalid } = hostsFromText(text)
  const keep = hosts.filter((h) => {
    const platform = platformFromHost(h)
    return platform === null || platform === 'circle'
  })
  const { added, existing } = await insertHosts(ctx.sql, keep, 'desktop:import')
  return { added, existing, invalid: invalid + (hosts.length - keep.length) }
}
