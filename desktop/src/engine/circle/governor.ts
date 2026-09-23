import { AsyncLocalStorage } from 'node:async_hooks'
import { mkdirSync, readFileSync, renameSync, rmdirSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { sleep } from '../util'

// One budget for ALL traffic to Circle from this machine (probes, space lists,
// member reads, membership checks) — Circle's Cloudflare rate-limits per IP
// across every community, custom domains included.
//
// Measured 2026-09-18 from a home IP: ~550 requests in ~10 minutes (bursts of
// several per second) earned HTTP 429 + `cf-mitigated: challenge` on every
// Circle host, anonymous calls included. The Railway worker hit the same wall
// on 2026-09-16 and stayed blocked. So: a sustained per-minute pace, an hourly
// ceiling, and a long cool-down the moment Circle pushes back.
//
// The budget is shared with every other process on this Mac — the Python CLI
// and scripts use circle_leads/scraper/governor.py, same file, same protocol:
// `mkdir <file>.lock` as the lock, then `nextAt` (shared pace), `hour` (start
// times for the hourly ceiling), `cooldownUntil`, and `highSeenAt`/`lowNextAt`
// so low-priority work (verify, discover) takes every other slot while a
// reading job is active. `limits` in the file are the one setting; this app
// writes its network settings there.

export class RateLimitedError extends Error {
  constructor(readonly until: Date) {
    super(`Circle временно ограничил запросы с этого компьютера — пауза до ${until.toLocaleTimeString('ru-RU')}`)
    this.name = 'RateLimitedError'
  }
}

export interface GovernorLimits {
  requestsPerMinute: number
  maxPerHour: number
  cooldownMinutes: number
}

export type Priority = 'high' | 'low'

interface SharedState {
  limits?: Partial<GovernorLimits>
  nextAt?: number
  lowNextAt?: number
  highSeenAt?: number
  cooldownUntil?: number
  cooldownReason?: string
  hour?: number[]
}

const DEFAULT_LIMITS: GovernorLimits = { requestsPerMinute: 20, maxPerHour: 600, cooldownMinutes: 60 }
/** A high-priority request this recent means a reading job is running. */
const HIGH_ACTIVE_MS = 30_000
/** A lock older than this belongs to a crashed process. */
const STALE_LOCK_MS = 10_000

const priorityStore = new AsyncLocalStorage<Priority>()

/** Run `fn` with every Circle request inside it at the given priority. */
export function withPriority<T>(priority: Priority, fn: () => Promise<T>): Promise<T> {
  return priorityStore.run(priority, fn)
}

function defaultStatePath(): string {
  if (process.env.WARMR_GOVERNOR_FILE) return process.env.WARMR_GOVERNOR_FILE
  // Tests must never spend this machine's real budget.
  if (process.env.VITEST) return join(tmpdir(), `warmr-governor-test-${process.pid}`, 'circle-governor.json')
  return join(homedir(), '.warmr', 'circle-governor.json')
}

function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)
}

export class CircleGovernor {
  private path = defaultStatePath()
  private memory: SharedState = {}
  private fileOk: boolean | null = null
  onTrip: ((until: Date, reason: string) => void) | null = null

  /** Point at another state file (tests). */
  usePath(path: string): void {
    this.path = path
    this.fileOk = null
    this.memory = {}
  }

  // ------------------------------------------------------------ storage ---

  private usable(): boolean {
    if (this.fileOk === null) {
      try {
        mkdirSync(dirname(this.path), { recursive: true })
        this.fileOk = true
      } catch {
        this.fileOk = false
      }
    }
    return this.fileOk
  }

  /** Read-modify-write under the cross-process lock. Synchronous on purpose:
   * the critical section is a few hundred bytes of JSON, and no await inside
   * means no interleaving within this process either. If the file can't be
   * used, the budget falls back to this process only — never an error. */
  private locked<T>(fn: (state: SharedState) => { state?: SharedState; result: T }): T {
    if (!this.usable()) {
      const out = fn({ ...this.memory })
      if (out.state) this.memory = out.state
      return out.result
    }
    const lock = `${this.path}.lock`
    const deadline = Date.now() + 5_000
    for (;;) {
      try {
        mkdirSync(lock)
        break
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'EEXIST') {
          this.fileOk = false
          return this.locked(fn)
        }
        try {
          // A crashed holder, or one wedged past the deadline: take the lock
          // over rather than stop all of this machine's traffic.
          if (Date.now() - statSync(lock).mtimeMs > STALE_LOCK_MS || Date.now() > deadline) rmdirSync(lock)
        } catch {
          /* the holder just released it */
        }
        sleepSync(20)
      }
    }
    try {
      let state: SharedState = {}
      try {
        const parsed: unknown = JSON.parse(readFileSync(this.path, 'utf8'))
        if (parsed && typeof parsed === 'object') state = parsed as SharedState
      } catch {
        state = {}
      }
      const out = fn(state)
      if (out.state) {
        const tmp = `${this.path}.${process.pid}.tmp`
        try {
          writeFileSync(tmp, JSON.stringify(out.state))
          renameSync(tmp, this.path)
        } catch {
          try {
            unlinkSync(tmp)
          } catch {
            /* nothing to clean */
          }
          this.memory = out.state
        }
      }
      return out.result
    } finally {
      try {
        rmdirSync(lock)
      } catch {
        /* already gone */
      }
    }
  }

  private static limitsOf(state: SharedState): GovernorLimits {
    const limits = { ...DEFAULT_LIMITS }
    for (const key of Object.keys(DEFAULT_LIMITS) as (keyof GovernorLimits)[]) {
      const val = state.limits?.[key]
      if (typeof val === 'number' && val >= 0) limits[key] = val
    }
    return limits
  }

  // -------------------------------------------------------------- budget ---

  get limits(): GovernorLimits {
    return this.locked((state) => ({ result: CircleGovernor.limitsOf(state) }))
  }

  /** Write this app's network settings as the machine-wide limits. */
  configure(limits: Partial<GovernorLimits>): void {
    this.locked((state) => ({
      state: { ...state, limits: { ...CircleGovernor.limitsOf(state), ...limits } },
      result: undefined
    }))
  }

  /** Restore a cool-down that started before a restart. */
  restoreCooldown(until: Date | null): void {
    if (!until || until.getTime() <= Date.now()) return
    this.locked((state) =>
      (state.cooldownUntil ?? 0) >= until.getTime()
        ? { result: undefined }
        : { state: { ...state, cooldownUntil: until.getTime() }, result: undefined }
    )
  }

  get coolingDownUntil(): Date | null {
    const until = this.locked((state) => ({ result: state.cooldownUntil ?? 0 }))
    return until > Date.now() ? new Date(until) : null
  }

  get usedLastHour(): number {
    const now = Date.now()
    return this.locked((state) => ({
      result: (state.hour ?? []).filter((t) => t > now - 3_600_000 && t <= now).length
    }))
  }

  /** Claim the next slot: ms to wait, or the end of a cool-down. */
  private reserve(priority: Priority): { waitMs: number; cooldownUntil: number } {
    return this.locked((state) => {
      const now = Date.now()
      const cooldown = state.cooldownUntil ?? 0
      if (cooldown > now) return { result: { waitMs: 0, cooldownUntil: cooldown } }
      const limits = CircleGovernor.limitsOf(state)
      const interval = 60_000 / Math.max(1, limits.requestsPerMinute)
      const hour = (state.hour ?? []).filter((t) => typeof t === 'number' && t > now - 3_600_000)
      let slot = Math.max(now, state.nextAt ?? 0)
      if (priority === 'low' && now - (state.highSeenAt ?? 0) < HIGH_ACTIVE_MS) {
        slot = Math.max(slot, state.lowNextAt ?? 0)
      }
      if (limits.maxPerHour > 0 && hour.length >= limits.maxPerHour) {
        slot = Math.max(slot, hour[hour.length - limits.maxPerHour]! + 3_600_000 + 50)
      }
      const next: SharedState = { ...state, limits, nextAt: slot + interval, hour: [...hour, slot] }
      if (priority === 'low') next.lowNextAt = slot + 2 * interval
      else next.highSeenAt = now
      return { state: next, result: { waitMs: Math.max(0, slot - now), cooldownUntil: 0 } }
    })
  }

  /** Wait for a slot. Throws RateLimitedError while cooling down. */
  async acquire(signal?: AbortSignal): Promise<void> {
    const { waitMs, cooldownUntil } = this.reserve(priorityStore.getStore() ?? 'high')
    if (cooldownUntil) throw new RateLimitedError(new Date(cooldownUntil))
    if (waitMs > 0) await sleep(waitMs, signal)
    const cooling = this.coolingDownUntil
    if (cooling) throw new RateLimitedError(cooling)
  }

  /** Circle pushed back (429 / challenge): stop all traffic on this machine. */
  trip(reason: string): RateLimitedError {
    const { until, extended } = this.locked((state) => {
      const limits = CircleGovernor.limitsOf(state)
      const proposed = Date.now() + Math.max(1, limits.cooldownMinutes) * 60_000
      const current = state.cooldownUntil ?? 0
      if (proposed <= current) return { result: { until: current, extended: false } }
      return {
        state: { ...state, cooldownUntil: proposed, cooldownReason: reason.slice(0, 200) },
        result: { until: proposed, extended: true }
      }
    })
    if (extended) this.onTrip?.(new Date(until), reason)
    return new RateLimitedError(new Date(until))
  }

  /** Tests only. */
  reset(): void {
    this.memory = {}
    this.locked(() => ({ state: {}, result: undefined }))
  }
}

export const circleGovernor = new CircleGovernor()
