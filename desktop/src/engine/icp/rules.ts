// Port of the deterministic ICP rules (classifier/icp_relevance.py +
// discovery/validate_community.py signal tables), extended with the one signal
// the Python side never had: the community's public space names
// ("I Need A Consultant - Share Projects" says more than any meta description).

export const RELEVANCE_SIGNALS: Record<string, [number, string[]]> = {
  startup: [20, ['startup', 'startups', 'founder', 'founders', 'yc', 'seed stage']],
  saas: [20, ['saas', 'b2b', 'software business', 'micro-saas']],
  entrepreneur: [15, ['entrepreneur', 'business owner', 'solopreneur', 'bootstrapper']],
  technology: [15, ['tech', 'technology', 'software', 'engineering', 'developer', 'coding', 'no-code']],
  ai: [15, ['ai', 'artificial intelligence', 'machine learning', 'llm', 'automation']],
  agency: [15, ['agency', 'agencies', 'consultant', 'freelance', 'client work']],
  product: [10, ['product', 'product manager', 'mvp', 'launch']],
  ecommerce: [10, ['ecommerce', 'e-commerce', 'shopify', 'dtc']],
  marketing: [5, ['marketing', 'growth', 'seo']]
}

export const IRRELEVANT_SIGNALS: Record<string, [number, string[]]> = {
  hobby: [-20, ['knitting', 'gardening', 'cooking', 'recipes', 'pets', 'astrology', 'crochet']],
  fitness: [-15, ['yoga', 'fitness', 'weight loss', 'workout', 'nutrition']],
  personal: [-15, ['meditation', 'mindfulness', 'journaling', 'manifestation']],
  fandom: [-15, ['fandom', 'anime', 'gaming clan', 'book club']]
}

export const HIRING_SPACE_HINTS = [
  'job', 'jobs', 'hiring', 'gig', 'gigs', 'marketplace', 'opportunit', 'collaborat',
  'find a', 'looking for', 'talent', 'recruit', 'projects', 'consultant'
]

export const GOAL_SIGNALS: Record<string, number> = {
  'build-my-tech-skills': 20,
  'start-and-scale-my-business': 15,
  'advance-my-career': 5,
  'grow-my-brand-and-audience': 5
}
export const GOAL_NEGATIVE_SIGNALS: Record<string, number> = {
  'improve-my-health': -20,
  'strengthen-my-relationships': -20,
  'pursue-new-interests': -10
}

export const ICP_CONFIDENT_YES = 45
export const ICP_CONFIDENT_NO = -10
export const NO_METADATA_REASON = 'no_metadata'

export interface IcpInput {
  name: string | null
  description: string | null
  spaceNames: string[]
  goals: string[]
}

export interface RuleAssessment {
  score: number
  reasons: string[]
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function haystackFor(input: IcpInput): string {
  return [input.name, input.description, input.spaceNames.join(' · ')]
    .filter((part): part is string => Boolean(part && part.trim()))
    .join(' ')
    .toLowerCase()
    .trim()
}

export function hasText(input: IcpInput): boolean {
  return haystackFor(input).length > 0 || input.goals.some((g) => g.trim())
}

export function assessRules(input: IcpInput): RuleAssessment {
  const haystack = haystackFor(input)
  const result: RuleAssessment = { score: 0, reasons: [] }
  const goals = input.goals.map((g) => g.trim()).filter(Boolean)
  if (!haystack && goals.length === 0) return result
  for (const [label, [weight, terms]] of Object.entries(RELEVANCE_SIGNALS)) {
    if (terms.some((t) => new RegExp(`\\b${escapeRegExp(t.trim())}`).test(haystack))) {
      result.score += weight
      result.reasons.push(label)
    }
  }
  for (const [label, [weight, terms]] of Object.entries(IRRELEVANT_SIGNALS)) {
    if (terms.some((t) => new RegExp(`\\b${escapeRegExp(t.trim())}`).test(haystack))) {
      result.score += weight
      result.reasons.push(`not_${label}`)
    }
  }
  if (HIRING_SPACE_HINTS.some((hint) => haystack.includes(hint))) {
    result.score += 15
    result.reasons.push('hiring_related_space')
  }
  for (const goal of goals) {
    if (goal in GOAL_SIGNALS) {
      result.score += GOAL_SIGNALS[goal]!
      result.reasons.push(`goal:${goal}`)
    } else if (goal in GOAL_NEGATIVE_SIGNALS) {
      result.score += GOAL_NEGATIVE_SIGNALS[goal]!
      result.reasons.push(`not_goal:${goal}`)
    }
  }
  return result
}
