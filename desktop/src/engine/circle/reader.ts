import { getJson } from './http'
import { sleep } from '../util'

// Reader for Circle's /internal_api (port of scraper/member_api_reader.py and
// the anonymous half of public_reader.py), rebuilt for "read ALL posts":
// full pagination on every level and a hard request budget per run. Pacing is
// not done here: every request goes through the shared CircleGovernor, which
// also turns a 429 / Cloudflare challenge into a machine-wide cool-down.

export class SessionInvalidError extends Error {}
export class BudgetExhaustedError extends Error {}

export interface CircleSpace {
  id: string
  name: string
  slug: string
  type: string
  postsCount: number | null
  isPrivate: boolean | null
}

export interface PostsPage {
  status: number
  records: Array<Record<string, unknown>>
  hasNext: boolean
}

export interface ReaderOptions {
  cookies: Record<string, string> | null
  budget: { remaining: number }
  signal?: AbortSignal
}

export interface PostDetail {
  commentsCount: number | null
  lastActivity: string | null
}

export const SESSION_COOKIE_NAMES = ['_circle_session', 'remember_user_token', 'user_session_identifier'] as const

/** Only the session cookies; cf_clearance etc. are IP/device-bound and never replayed. */
export function sessionCookies(cookies: Array<{ name: string; value: string }>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const c of cookies) {
    if ((SESSION_COOKIE_NAMES as readonly string[]).includes(c.name) && c.value) out[c.name] = c.value
  }
  return out
}

function recordsOf(json: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(json)) return json as Array<Record<string, unknown>>
  if (json && typeof json === 'object') {
    const records = (json as { records?: unknown }).records
    if (Array.isArray(records)) return records as Array<Record<string, unknown>>
  }
  return []
}

function hasNextPage(json: unknown): boolean {
  return Boolean(json && typeof json === 'object' && !Array.isArray(json) && (json as { has_next_page?: unknown }).has_next_page)
}

export class CircleReader {
  readonly base: string
  requests = 0

  constructor(
    readonly host: string,
    private readonly opts: ReaderOptions
  ) {
    this.base = `https://${host}`
  }

  get authenticated(): boolean {
    return Boolean(this.opts.cookies && Object.keys(this.opts.cookies).length)
  }

  private async request(path: string): Promise<{ status: number; json: unknown }> {
    for (let attempt = 0; attempt < 3; attempt++) {
      if (this.opts.budget.remaining <= 0) throw new BudgetExhaustedError('request budget for this run is used up')
      this.opts.budget.remaining--
      this.requests++
      // Throws RateLimitedError on 429 / challenge (and trips the cool-down).
      const res = await getJson(this.base + path, {
        cookies: this.opts.cookies,
        followRedirects: false,
        timeoutMs: 30_000,
        signal: this.opts.signal
      })
      if (res.networkError) {
        if (attempt < 2) {
          await sleep(5000 * (attempt + 1), this.opts.signal)
          continue
        }
        throw new Error(`network error on ${path}: ${res.networkError}`)
      }
      return { status: res.status, json: res.isJson ? res.json : null }
    }
    throw new Error(`gave up on ${path}`)
  }

  async listSpaces(): Promise<CircleSpace[]> {
    const res = await this.request('/internal_api/spaces')
    const refused = res.status === 401 || res.status === 403 || (res.status >= 300 && res.status < 400)
    if (refused) {
      if (this.authenticated) throw new SessionInvalidError(`Circle rejected the session (HTTP ${res.status})`)
      return []
    }
    if (res.status !== 200) return []
    return recordsOf(res.json)
      .filter((s) => s.id != null)
      .map((s) => ({
        id: String(s.id),
        name: String(s.name ?? s.slug ?? ''),
        slug: String(s.slug ?? ''),
        type: String(s.space_type ?? s.post_type ?? ''),
        postsCount: typeof s.posts_count === 'number' ? s.posts_count : null,
        isPrivate: typeof s.is_private === 'boolean' ? s.is_private : null
      }))
  }

  async postsPage(spaceId: string, page: number, perPage = 20): Promise<PostsPage> {
    const res = await this.request(`/internal_api/spaces/${encodeURIComponent(spaceId)}/posts?page=${page}&per_page=${perPage}&sort=latest`)
    if (res.status !== 200) return { status: res.status, records: [], hasNext: false }
    return { status: 200, records: recordsOf(res.json), hasNext: hasNextPage(res.json) }
  }

  private async allPages(pathFor: (page: number) => string, maxPages: number): Promise<Array<Record<string, unknown>>> {
    const out: Array<Record<string, unknown>> = []
    for (let page = 1; page <= maxPages; page++) {
      const res = await this.request(pathFor(page))
      if (res.status !== 200) break
      const batch = recordsOf(res.json)
      out.push(...batch)
      if (!batch.length || !hasNextPage(res.json)) break
    }
    return out
  }

  /**
   * Comment counts for a page of posts. The posts list itself has no
   * comments_count (only is_comments_enabled); Circle's own frontend asks
   * /internal_api/post_details for it (seen in recorded HAR traffic).
   */
  async postDetails(spaceId: string, postIds: string[]): Promise<Map<string, PostDetail> | null> {
    if (!postIds.length) return new Map()
    const res = await this.request(
      `/internal_api/post_details?post_ids=${postIds.map(encodeURIComponent).join('%2C')}&space_id=${encodeURIComponent(spaceId)}`
    )
    if (res.status !== 200) return null
    const out = new Map<string, PostDetail>()
    for (const d of recordsOf(res.json)) {
      if (d.id == null) continue
      out.set(String(d.id), {
        commentsCount: typeof d.comments_count === 'number' ? d.comments_count : null,
        lastActivity: typeof d.last_replied_or_posted === 'string' ? d.last_replied_or_posted : null
      })
    }
    return out
  }

  comments(postId: string): Promise<Array<Record<string, unknown>>> {
    return this.allPages((p) => `/internal_api/posts/${encodeURIComponent(postId)}/comments?page=${p}&per_page=30`, 40)
  }

  replies(commentId: string): Promise<Array<Record<string, unknown>>> {
    return this.allPages((p) => `/internal_api/comments/${encodeURIComponent(commentId)}/comments?page=${p}&per_page=30`, 20)
  }
}

/** Space types that have a posts feed. Chat rooms use a different realtime API. */
export function spaceHasPosts(type: string): boolean {
  return !['chat', 'course', 'members', 'members_directory'].includes(type)
}
