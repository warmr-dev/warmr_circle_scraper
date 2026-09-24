import { circleGovernor } from './governor'

/** A normal desktop Chrome UA, same string the Python reader sends. The API does
 * not gate on it; it only avoids a default library UA that some WAFs flag. */
export const BROWSER_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'

export interface JsonResponse {
  status: number
  contentType: string
  json: unknown
  isJson: boolean
  challenged: boolean
  location: string | null
  finalUrl: string
  networkError: string | null
}

export interface GetJsonOptions {
  cookies?: Record<string, string> | null
  timeoutMs?: number
  followRedirects?: boolean
  signal?: AbortSignal
}

export type Fetcher = (url: string, init: RequestInit) => Promise<Response>

let fetcher: Fetcher = (url, init) => fetch(url, init)

/** Swap the network layer (tests use a fake; the app may route through Chromium). */
export function setFetcher(next: Fetcher): void {
  fetcher = next
}

function cookieHeader(cookies: Record<string, string>): string {
  return Object.entries(cookies)
    .filter(([, value]) => Boolean(value))
    .map(([name, value]) => `${name}=${value}`)
    .join('; ')
}

/**
 * A real Cloudflare challenge, not just a page carrying Cloudflare's scripts.
 * Cloudflare injects /cdn-cgi/challenge-platform/scripts/jsd into ordinary
 * HTML (404 pages included), so "challenge-platform" in a body proves nothing;
 * treating it as a challenge would pause all traffic on every custom-domain 404.
 */
export function looksChallenged(status: number, headers: Headers, body: string): boolean {
  if ((headers.get('cf-mitigated') || '').toLowerCase() === 'challenge') return true
  if (status !== 403 && status !== 503) return false
  return /__cf_chl_|cf-chl-|<title>\s*Just a moment/i.test(body)
}

/**
 * GET a Circle URL as JSON. Every call waits for the shared CircleGovernor.
 * A 429 or a Cloudflare challenge trips the governor's cool-down and throws
 * RateLimitedError: callers stop instead of recording a false "unreachable".
 */
export async function getJson(url: string, opts: GetJsonOptions = {}): Promise<JsonResponse> {
  await circleGovernor.acquire(opts.signal)
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), opts.timeoutMs ?? 15_000)
  const onOuterAbort = (): void => controller.abort()
  opts.signal?.addEventListener('abort', onOuterAbort, { once: true })
  const headers: Record<string, string> = { 'User-Agent': BROWSER_UA, Accept: 'application/json' }
  if (opts.cookies) {
    const header = cookieHeader(opts.cookies)
    if (header) headers.Cookie = header
  }
  let res: Response
  try {
    res = await fetcher(url, {
      headers,
      redirect: opts.followRedirects === false ? 'manual' : 'follow',
      signal: controller.signal
    })
  } catch (err) {
    clearTimeout(timeout)
    opts.signal?.removeEventListener('abort', onOuterAbort)
    if (opts.signal?.aborted) throw err
    const name = err instanceof Error ? (err.name === 'AbortError' ? 'Timeout' : err.name) : 'Error'
    const cause = err instanceof Error && err.cause instanceof Error ? `: ${err.cause.message}` : ''
    return {
      status: 0,
      contentType: '',
      json: null,
      isJson: false,
      challenged: false,
      location: null,
      finalUrl: url,
      networkError: `${name}${cause}`.slice(0, 200)
    }
  }
  try {
    const contentType = res.headers.get('content-type') || ''
    const text = await res.text()
    const isJsonType = contentType.includes('application/json')
    const challenged = !isJsonType && looksChallenged(res.status, res.headers, text.slice(0, 20_000))
    if (res.status === 429 || challenged) {
      throw circleGovernor.trip(`HTTP ${res.status}${challenged ? ' challenge' : ''} on ${new URL(url).host}`)
    }
    let json: unknown = null
    let isJson = false
    if (isJsonType && text) {
      try {
        json = JSON.parse(text)
        isJson = true
      } catch {
        isJson = false
      }
    }
    return {
      status: res.status,
      contentType,
      json,
      isJson,
      challenged: false,
      location: res.headers.get('location'),
      finalUrl: res.url || url,
      networkError: null
    }
  } finally {
    clearTimeout(timeout)
    opts.signal?.removeEventListener('abort', onOuterAbort)
  }
}
