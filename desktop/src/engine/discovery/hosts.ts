// Host parsing and the community key rules (port of
// discovery/discover_communities.py). Getting the slug wrong has merged
// unrelated communities twice in the Python app (P10 2026-09-10 "www",
// 2026-09-17 "forum"/"learn"), because rows match on `slug OR url`.

const INFRA_HOST_LABELS = new Set([
  'www', 'app', 'apps', 'community', 'communities', 'portal', 'hub', 'go', 'my', 'members', 'member', 'login', 'get',
  'join', 'circle', 'space'
])
const TWO_PART_SUFFIXES = new Set([
  'co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'me.uk', 'co.nz', 'org.nz', 'co.za', 'co.il', 'co.in', 'co.kr', 'co.jp', 'ne.jp',
  'or.jp', 'com.au', 'net.au', 'org.au', 'com.br', 'com.mx', 'com.ar', 'com.co', 'com.sg', 'com.tr', 'com.cn', 'com.hk',
  'com.tw', 'com.pl', 'com.ua'
])

const PLATFORM_SUFFIXES: Array<[string, string]> = [
  ['.slack.com', 'slack'],
  ['.skool.com', 'skool'],
  ['.mn.co', 'mighty_networks'],
  ['.mightynetworks.com', 'mighty_networks'],
  ['.facebook.com', 'facebook'],
  ['.discord.com', 'discord'],
  ['.discord.gg', 'discord'],
  ['.heartbeat.chat', 'heartbeat'],
  ['.meetup.com', 'meetup'],
  ['.geneva.com', 'geneva'],
  ['.discourse.group', 'discourse']
]
const PLATFORM_EXACT: Record<string, string> = {
  'facebook.com': 'facebook', 'fb.com': 'facebook', 'm.facebook.com': 'facebook', 'slack.com': 'slack',
  'join.slack.com': 'slack', 'discord.com': 'discord', 'discord.gg': 'discord', 'skool.com': 'skool',
  'meetup.com': 'meetup', 'linkedin.com': 'linkedin', 'www.linkedin.com': 'linkedin', 't.me': 'telegram',
  'telegram.me': 'telegram'
}
const CIRCLE_INFRA_LABELS = new Set([
  'app', 'login', 'discover', 'www', 'help', 'status', 'signup', 'sign-in', 'auth', 'api', 'assets', 'cdn', 'marketing',
  'compass', 'email', 'mail', 'circle-compass-assets', 'assets-v2'
])

const HOST_RE = /^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/

/** "https://Foo.circle.so/c/x?y" | "foo.circle.so" | "www.x.com/" -> host, or null. */
export function hostFromInput(raw: string): string | null {
  let value = raw.trim().replace(/^["'<(]+|[>"'),;]+$/g, '')
  if (!value) return null
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) value = `https://${value}`
  let host: string
  try {
    host = new URL(value).hostname.toLowerCase().replace(/\.$/, '')
  } catch {
    return null
  }
  return HOST_RE.test(host) ? host : null
}

export function communitySlugForHost(rawHost: string): string {
  const host = rawHost.trim().toLowerCase().replace(/^\.+|\.+$/g, '')
  if (!host) return 'unknown'
  if (host.endsWith('.circle.so')) return host.slice(0, -'.circle.so'.length).split('.').pop() || 'circle'
  let labels = host.split('.')
  if (labels.length <= 2) return labels[0]!
  if (INFRA_HOST_LABELS.has(labels[0]!)) labels = labels.slice(1)
  if (labels.length <= 2) return labels[0]!
  if (TWO_PART_SUFFIXES.has(labels.slice(-2).join('.'))) return labels[labels.length - 3]!
  return labels[labels.length - 2]!
}

/** The collision-free community key: subdomain for *.circle.so, the host itself otherwise. */
export function uniqueSlugForHost(rawHost: string): string {
  const host = rawHost.trim().toLowerCase().replace(/^\.+|\.+$/g, '')
  if (!host) return 'unknown'
  if (host.endsWith('.circle.so')) return communitySlugForHost(host)
  return host
}

/** Platform by name alone; null means "custom domain, a probe must decide". */
export function platformFromHost(rawHost: string): string | null {
  const host = rawHost.trim().toLowerCase().replace(/^\.+|\.+$/g, '')
  if (!host) return null
  if (PLATFORM_EXACT[host]) return PLATFORM_EXACT[host]!
  for (const [suffix, platform] of PLATFORM_SUFFIXES) if (host.endsWith(suffix)) return platform
  if (host === 'circle.so' || host === 'www.circle.so') return 'circle_infra'
  if (host.endsWith('.circle.so')) {
    const label = host.slice(0, -'.circle.so'.length).split('.').pop() || ''
    if (label === 'discover') return 'discover'
    return CIRCLE_INFRA_LABELS.has(label) ? 'circle_infra' : 'circle'
  }
  return null
}

/** Pull every candidate host out of free text (one per line, CSV, pasted links). */
export function hostsFromText(text: string): { hosts: string[]; invalid: number } {
  const seen = new Set<string>()
  let invalid = 0
  for (const token of text.split(/[\s,;]+/)) {
    if (!token.trim()) continue
    const host = hostFromInput(token)
    if (!host) {
      invalid++
      continue
    }
    seen.add(host)
  }
  return { hosts: [...seen], invalid }
}
