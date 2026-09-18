import { createHash } from 'node:crypto'

// Ports of circle_leads/scraper/normalize.py, member_api_reader._tiptap_text and
// storage/database.py content_hash/simhash. contentHash and simhash must stay
// bit-for-bit identical to the Python versions: both apps write posts.dedup_hash
// and posts.simhash, and the Python lead classifier dedups on them.

const TAG = /<[^>]+>/g
const WS = /\s+/g
const EMAIL = /\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b/g
// Same alternatives as normalize._PHONE (verbose regex flattened).
const PHONE = new RegExp(
  [
    String.raw`(?<![\w.])(?:`,
    String.raw`\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){1,4}`,
    String.raw`|\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}`,
    String.raw`|\d{3}[.-]\d{3}[.-]\d{4}`,
    String.raw`|(?:tel|phone|call|text|whatsapp|mobile|cell)\W{0,3}\+?[\d][\d\s.()-]{6,18}\d`,
    String.raw`)(?![\d])`
  ].join(''),
  'gi'
)

const ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
  '#39': "'"
}

export function unescapeHtml(value: string): string {
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, entity: string) => {
    const lower = entity.toLowerCase()
    if (lower.startsWith('#x')) return String.fromCodePoint(parseInt(lower.slice(2), 16))
    if (lower.startsWith('#')) return String.fromCodePoint(parseInt(lower.slice(1), 10))
    return ENTITIES[lower] ?? match
  })
}

export function stripHtml(value: string | null | undefined): string {
  if (!value) return ''
  return unescapeHtml(value.replace(TAG, ' ')).replace(WS, ' ').trim()
}

export function redactPii(text: string): string {
  return text.replace(EMAIL, '[email removed]').replace(PHONE, '[phone removed]')
}

/** ISO timestamp -> Date (UTC). Returns null for anything unparseable. */
export function parseTimestamp(value: unknown): Date | null {
  if (!value) return null
  const date = new Date(String(value))
  return Number.isNaN(date.getTime()) ? null : date
}

const BLOCK_TYPES = new Set(['paragraph', 'heading', 'listItem', 'blockquote'])

/** Flatten Circle's tiptap/ProseMirror JSON body to plain text. */
export function tiptapText(node: unknown): string {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (Array.isArray(node)) return node.map(tiptapText).join('')
  if (typeof node !== 'object') return ''
  const n = node as { type?: string; text?: unknown; content?: unknown[] }
  const out: string[] = []
  if (n.type === 'text' && typeof n.text === 'string') out.push(n.text)
  for (const child of n.content ?? []) out.push(tiptapText(child))
  if (n.type && BLOCK_TYPES.has(n.type)) out.push('\n')
  return out.join('')
}

/** Title + full body of a post or comment record (member_api_reader._extract_text). */
export function extractText(record: Record<string, unknown>): { title: string; body: string } {
  const title = stripHtml((record.name as string) || (record.title as string) || '')
  let body = tiptapText(record.tiptap_body).trim()
  if (!body) body = stripHtml((record.truncated_content as string) || '')
  if (!body) {
    for (const key of ['body_plain_text', 'plain_text', 'content', 'text']) {
      const value = record[key]
      if (typeof value === 'string' && value) {
        body = stripHtml(value)
        break
      }
    }
  }
  return { title, body }
}

export function composeContent(title: string, body: string): string {
  if (title && title !== body) return `${title}\n\n${body}`.trim()
  return body || title
}

export function contentHash(text: string): string {
  const normalized = (text || '').trim().toLowerCase().replace(/\s+/g, ' ')
  return createHash('sha256').update(normalized, 'utf8').digest('hex')
}

export function simhash(text: string, bits = 64): string {
  const tokens = (text || '').toLowerCase().match(/[a-z0-9]+/g) ?? []
  if (tokens.length === 0) return '0'.repeat(bits / 4)
  const shingles =
    tokens.length >= 3
      ? Array.from({ length: tokens.length - 2 }, (_, i) => `${tokens[i]} ${tokens[i + 1]} ${tokens[i + 2]}`)
      : tokens
  const vector = new Array<number>(bits).fill(0)
  for (const shingle of shingles) {
    // Python: int(md5(sh).hexdigest(), 16) then bit i for i < 64 -> the low
    // 64 bits of the 128-bit big-endian digest, i.e. its last 8 bytes.
    const digest = createHash('md5').update(shingle, 'utf8').digest()
    const low = digest.readBigUInt64BE(8)
    for (let i = 0; i < bits; i++) {
      vector[i] += (low >> BigInt(i)) & 1n ? 1 : -1
    }
  }
  let value = 0n
  for (let i = 0; i < bits; i++) {
    if (vector[i] > 0) value |= 1n << BigInt(i)
  }
  return value.toString(16).padStart(bits / 4, '0')
}
