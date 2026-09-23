import { useCallback, useEffect, useRef, useState } from 'react'
import type { AppEvent, WarmrApi } from '@shared/types'

export const api: WarmrApi = window.warmr

/** Subscribe to engine events for the component's lifetime. */
export function useEngineEvents(handler: (event: AppEvent) => void): void {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => api.onEvent((event) => ref.current(event)), [])
}

/**
 * Load data, reload on engine status events and every `intervalMs`.
 * Keeps the last good value on error and exposes the error text.
 */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[], intervalMs = 0): {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
} {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const seq = useRef(0)

  const reload = useCallback(() => {
    const mine = ++seq.current
    loaderRef
      .current()
      .then((value) => {
        if (mine !== seq.current) return
        setData(value)
        setError(null)
      })
      .catch((err: unknown) => {
        if (mine !== seq.current) return
        setError(err instanceof Error ? err.message.replace(/^Error invoking remote method '[^']+': (Error: )?/, '') : String(err))
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
  }, [])

  useEffect(() => {
    setLoading(true)
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (!intervalMs) return
    const t = setInterval(reload, intervalMs)
    return () => clearInterval(t)
  }, [intervalMs, reload])

  useEngineEvents((event) => {
    if (event.type === 'status') reload()
  })

  return { data, error, loading, reload }
}

export function cleanError(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err)
  return text.replace(/^Error invoking remote method '[^']+': (Error: )?/, '')
}
