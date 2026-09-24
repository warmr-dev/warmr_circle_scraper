import { getJson } from './http'

// Port of circle_leads/discovery/join_type.py plus the existence verdict the
// Python side never stored explicitly.
//
// Measured facts this encodes (WORKLOG 2026-09-11, 2026-09-17, re-checked from
// this Mac 2026-09-18):
// - GET /internal_api/communities/current works anonymously over plain HTTPS
//   and is not behind the Cloudflare challenge.
// - HTTP 200 + a JSON object with `is_private` is the ONLY proof a community
//   exists. Every *.circle.so subdomain, including invented ones, answers 401
//   JSON, so 401/403 proves nothing ("locked_or_absent").
// - The HTML root is behind a Cloudflare managed challenge (403,
//   cf-mitigated: challenge) even for invented hosts, so the Python
//   "redirects to circle.so marketing site" check cannot work from plain HTTP.
//   It is deliberately not ported.
// - Custom domains sometimes 404 on the apex for this path while www works.

export type JoinType =
  | 'free_join'
  | 'paid'
  | 'invite_only'
  | 'locked_unknown'
  | 'unknown'
  | 'subscription_expired'

export type ExistsStatus = 'alive' | 'locked_or_absent' | 'unreachable' | 'not_circle'

export interface ProbeResult {
  host: string
  answeredHost: string
  exists: ExistsStatus
  joinType: JoinType
  detail: string
  httpStatus: number
  name: string | null
  circleId: number | null
  isPrivate: boolean | null
  allowSignups: boolean | null
  hasPaywalls: boolean | null
  subscriptionCancelled: boolean | null
}

export interface JoinClassification {
  joinType: JoinType
  detail: string
}

export function classifyPayload(data: Record<string, unknown>): JoinClassification {
  if (data.subscription_cancelled) return { joinType: 'subscription_expired', detail: 'subscription_cancelled=true' }
  if (data.is_private) return { joinType: 'invite_only', detail: 'is_private=true' }
  if (data.allow_signups_to_public_community)
    return { joinType: 'free_join', detail: 'allow_signups_to_public_community=true' }
  if (data.has_non_draft_paywalls) return { joinType: 'paid', detail: 'paywall present, no public free signup' }
  return { joinType: 'invite_only', detail: 'no public signup, no paywall detected' }
}

/** Discover's own price label, used only when the live check is inconclusive. */
export function priceLabelFallback(priceLabel: string | null | undefined): JoinClassification | null {
  const label = (priceLabel || '').trim()
  if (!label) return null
  const lower = label.toLowerCase()
  if (lower.includes('free') || lower === '$0' || lower === 'from $0') {
    return { joinType: 'free_join', detail: `price_label fallback: '${label}'` }
  }
  return { joinType: 'paid', detail: `price_label fallback: '${label}'` }
}

function emptyResult(host: string, answeredHost: string, httpStatus: number): ProbeResult {
  return {
    host,
    answeredHost,
    exists: 'unreachable',
    joinType: 'unknown',
    detail: '',
    httpStatus,
    name: null,
    circleId: null,
    isPrivate: null,
    allowSignups: null,
    hasPaywalls: null,
    subscriptionCancelled: null
  }
}

async function probeOnce(host: string, answeredHost: string, signal?: AbortSignal): Promise<ProbeResult> {
  const res = await getJson(`https://${answeredHost}/internal_api/communities/current`, {
    timeoutMs: 15_000,
    signal
  })
  const result = emptyResult(host, answeredHost, res.status)
  if (res.networkError) {
    result.detail = `request failed: ${res.networkError}`
    return result
  }
  if (res.status === 401 || res.status === 403) {
    result.exists = 'locked_or_absent'
    result.joinType = 'locked_unknown'
    result.detail = `HTTP ${res.status} on communities/current (proves nothing: *.circle.so answers 401 for invented hosts too)`
    return result
  }
  if (res.status !== 200) {
    result.detail = `HTTP ${res.status}${res.challenged ? ' (challenge page)' : ''}`
    return result
  }
  const data = res.json
  if (!res.isJson || !data || typeof data !== 'object' || Array.isArray(data) || !('is_private' in data)) {
    // Something answered 200, but not Circle's API: a marketing site, an SPA
    // shell, a non-Circle platform.
    result.exists = 'not_circle'
    result.detail = res.isJson ? 'JSON without Circle community fields' : `non-JSON 200 (${res.contentType || 'no content-type'})`
    return result
  }
  const payload = data as Record<string, unknown>
  const classification = classifyPayload(payload)
  result.exists = 'alive'
  result.joinType = classification.joinType
  result.detail = classification.detail
  result.name = typeof payload.name === 'string' && payload.name.trim() ? payload.name.trim() : null
  result.circleId = typeof payload.id === 'number' ? payload.id : null
  result.isPrivate = Boolean(payload.is_private)
  result.allowSignups = Boolean(payload.allow_signups_to_public_community)
  result.hasPaywalls = Boolean(payload.has_non_draft_paywalls)
  result.subscriptionCancelled = Boolean(payload.subscription_cancelled)
  return result
}

/**
 * Probe one host. Never throws for network/HTTP problems (only on abort).
 * Retries once on www.<host> when the apex was unreachable or answered with a
 * non-Circle 200 — never after a 401/403, which already means "something
 * refused us here".
 */
export async function probeHost(host: string, signal?: AbortSignal): Promise<ProbeResult> {
  const clean = host.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '')
  if (!clean) {
    const empty = emptyResult(host, host, 0)
    empty.detail = 'empty host'
    return empty
  }
  let first = await probeOnce(clean, clean, signal)
  // A silently empty/garbled body happens under load (WORKLOG 2026-09-15);
  // one quiet retry before believing it.
  if (first.exists === 'not_circle' && first.detail.startsWith('JSON without')) {
    first = await probeOnce(clean, clean, signal)
  }
  const retryable = first.exists === 'unreachable' || first.exists === 'not_circle'
  if (retryable && !clean.startsWith('www.') && !clean.endsWith('.circle.so')) {
    const retry = await probeOnce(clean, `www.${clean}`, signal)
    if (retry.exists === 'alive' || retry.exists === 'locked_or_absent') {
      retry.detail = `${retry.detail} (www retry; apex: ${first.detail})`
      return retry
    }
  }
  return first
}

export interface PublicSpaces {
  ok: boolean
  status: number
  spaces: Array<{ id: string; name: string; slug: string; type: string; postsCount: number | null; membersCount: number | null }>
}

/** Anonymous /internal_api/spaces: space names are the best free ICP signal. */
export async function fetchPublicSpaces(host: string, signal?: AbortSignal): Promise<PublicSpaces> {
  const res = await getJson(`https://${host}/internal_api/spaces`, { timeoutMs: 15_000, signal })
  if (res.status !== 200 || !res.isJson) return { ok: false, status: res.status, spaces: [] }
  const raw = Array.isArray(res.json)
    ? res.json
    : ((res.json as { records?: unknown[] } | null)?.records ?? [])
  const spaces = (raw as Array<Record<string, unknown>>)
    .filter((s) => s && s.id != null)
    .map((s) => ({
      id: String(s.id),
      name: String(s.name ?? s.slug ?? ''),
      slug: String(s.slug ?? ''),
      type: String(s.space_type ?? s.post_type ?? ''),
      postsCount: typeof s.posts_count === 'number' ? s.posts_count : null,
      membersCount: typeof s.space_members_count === 'number' ? s.space_members_count : null
    }))
  return { ok: true, status: 200, spaces }
}
