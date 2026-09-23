// Small shared helpers. No Electron imports anywhere under src/engine, so the
// engine can be unit-tested with vitest and later run headless on a server.

export class AbortedError extends Error {
  constructor(message = 'stopped') {
    super(message)
    this.name = 'AbortedError'
  }
}

export function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new AbortedError()
}

/** Sleep that wakes up immediately (with AbortedError) when `signal` fires. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new AbortedError())
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, Math.max(0, ms))
    const onAbort = (): void => {
      clearTimeout(timer)
      reject(new AbortedError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** Naive-UTC timestamp string, the format every timestamp column in this DB holds. */
export function naiveUtc(date: Date = new Date()): string {
  return date.toISOString().replace('Z', '')
}

export function daysAgo(days: number, from: Date = new Date()): Date {
  return new Date(from.getTime() - days * 86_400_000)
}

export function startOfUtcDay(date: Date = new Date()): Date {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()))
}

export function truncate(value: string | null | undefined, max: number): string {
  const text = value ?? ''
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err)
}

/** Run `worker` over `items` with at most `concurrency` in flight and a stagger between starts. */
export async function mapPool<T, R>(
  items: T[],
  concurrency: number,
  worker: (item: T, index: number) => Promise<R>,
  opts: { staggerMs?: number; signal?: AbortSignal } = {}
): Promise<R[]> {
  const results = new Array<R>(items.length)
  let next = 0
  let lastStart = 0
  const stagger = opts.staggerMs ?? 0
  const lanes = Array.from({ length: Math.max(1, Math.min(concurrency, items.length)) }, async () => {
    while (true) {
      throwIfAborted(opts.signal)
      const index = next++
      if (index >= items.length) return
      if (stagger > 0) {
        const wait = lastStart + stagger - Date.now()
        lastStart = Math.max(Date.now(), lastStart + stagger)
        if (wait > 0) await sleep(wait, opts.signal)
      }
      results[index] = await worker(items[index] as T, index)
    }
  })
  await Promise.all(lanes)
  return results
}

export function randomBetween(min: number, max: number): number {
  return min + Math.random() * Math.max(0, max - min)
}

export function hostFromUrl(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url.includes('://') ? url : `https://${url}`)
    return parsed.hostname.toLowerCase().replace(/\.$/, '') || null
  } catch {
    return null
  }
}
