import type { LocalSettings, SharedSettings } from '../shared/types'
import { DEFAULT_ICP_PROFILE } from './icp/classify'

export const DEFAULT_SHARED: SharedSettings = {
  autopilot: false,
  verify: { enabled: true, everyMinutes: 20, batchSize: 300, concurrency: 3, reprobeAliveDays: 14, reprobeOtherDays: 7 },
  icp: { useLlm: true, mode: 'all', minConfidence: 0.6, autoApproveLlm: true, profile: DEFAULT_ICP_PROFILE, batchSize: 150 },
  llm: { provider: 'openai', model: 'gpt-4o-mini', dailyBudgetUsd: 3 },
  discover: { enabled: true, directoryEveryHours: 24, resolveBatch: 40 },
  // Off until the user switches it on: it drives their real Circle account.
  join: { enabled: false, everyHours: 4, maxVisitsPerDay: 25, minDelaySec: 30, jitterSec: 60, batchSize: 8, includePaid: true },
  scrape: {
    enabled: true,
    everyHours: 6,
    maxRequestsPerRun: 400,
    includePublic: true,
    maxPostAgeDays: 0,
    withComments: true
  },
  // Shared by every stage that talks to Circle from this machine (per-IP limit).
  network: { requestsPerMinute: 20, maxPerHour: 600, cooldownMinutes: 60 }
}

export const DEFAULT_LOCAL: LocalSettings = {
  deviceName: '',
  egoPath: '',
  egoSpaceId: null,
  launchAtLogin: false,
  circleCooldownUntil: null
}

type Plain = Record<string, unknown>

function isPlain(v: unknown): v is Plain {
  return Boolean(v) && typeof v === 'object' && !Array.isArray(v)
}

/** Deep-merge `patch` over `base`, keeping only keys `base` knows (schema drift safe). */
export function mergeSettings<T>(base: T, patch: unknown): T {
  if (!isPlain(base) || !isPlain(patch)) return base
  const out: Plain = { ...(base as Plain) }
  for (const [key, value] of Object.entries(patch)) {
    if (!(key in out)) continue
    const current = out[key]
    if (isPlain(current)) out[key] = mergeSettings(current, value)
    else if (value !== undefined && (current === null || typeof value === typeof current)) out[key] = value
  }
  return out as T
}
