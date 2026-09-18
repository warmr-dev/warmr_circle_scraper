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

export class CircleGovernor {
  private nextAt = 0
  private hour: number[] = []
  private cooldownUntil = 0
  limits: GovernorLimits = { requestsPerMinute: 20, maxPerHour: 600, cooldownMinutes: 60 }
  onTrip: ((until: Date, reason: string) => void) | null = null

  configure(limits: Partial<GovernorLimits>): void {
    this.limits = { ...this.limits, ...limits }
  }

  /** Restore a cool-down that started before a restart. */
  restoreCooldown(until: Date | null): void {
    if (until && until.getTime() > Date.now()) this.cooldownUntil = until.getTime()
  }

  get coolingDownUntil(): Date | null {
    return this.cooldownUntil > Date.now() ? new Date(this.cooldownUntil) : null
  }

  get usedLastHour(): number {
    const cutoff = Date.now() - 3_600_000
    this.hour = this.hour.filter((t) => t > cutoff)
    return this.hour.length
  }

  /** Wait for a slot. Throws RateLimitedError while cooling down. */
  async acquire(signal?: AbortSignal): Promise<void> {
    if (Date.now() < this.cooldownUntil) throw new RateLimitedError(new Date(this.cooldownUntil))
    const perMinute = Math.max(1, this.limits.requestsPerMinute)
    const interval = 60_000 / perMinute
    // Hourly ceiling: wait for the oldest request in the window to age out.
    while (this.limits.maxPerHour > 0 && this.usedLastHour >= this.limits.maxPerHour) {
      const waitMs = this.hour[0]! + 3_600_000 - Date.now() + 50
      await sleep(Math.min(Math.max(waitMs, 1000), 60_000), signal)
      if (Date.now() < this.cooldownUntil) throw new RateLimitedError(new Date(this.cooldownUntil))
    }
    const now = Date.now()
    const slot = Math.max(now, this.nextAt)
    this.nextAt = slot + interval
    if (slot > now) await sleep(slot - now, signal)
    if (Date.now() < this.cooldownUntil) throw new RateLimitedError(new Date(this.cooldownUntil))
    this.hour.push(Date.now())
  }

  /** Circle pushed back (429 / challenge): stop all traffic for the cool-down. */
  trip(reason: string): RateLimitedError {
    const until = Date.now() + Math.max(1, this.limits.cooldownMinutes) * 60_000
    if (until > this.cooldownUntil) {
      this.cooldownUntil = until
      this.onTrip?.(new Date(until), reason)
    }
    return new RateLimitedError(new Date(this.cooldownUntil))
  }

  /** Tests only. */
  reset(): void {
    this.nextAt = 0
    this.hour = []
    this.cooldownUntil = 0
  }
}

export const circleGovernor = new CircleGovernor()
