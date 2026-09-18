import type { Sql } from './db/client'
import type { LocalSettings, LogLevel, SharedSettings } from '../shared/types'

export interface EngineSecrets {
  openaiKey?: string
  anthropicKey?: string
  circleEmail?: string
  circlePassword?: string
}

/**
 * What only a real browser can do, provided by the Electron shell. The engine
 * never imports Electron; without a BrowserHost the directory crawl is simply
 * unavailable (plain HTTP to discover.circle.so gets a Cloudflare challenge).
 */
export interface BrowserHost {
  fetchJson(origin: string, paths: string[], signal?: AbortSignal): Promise<Array<{ path: string; status: number; json: unknown }>>
  pageAnchors(url: string, signal?: AbortSignal): Promise<{ finalUrl: string; anchors: Array<{ text: string; href: string }>; challenged: boolean }>
}

export interface StageContext {
  sql: Sql
  deviceId: string
  settings: SharedSettings
  local: LocalSettings
  secrets: EngineSecrets
  signal: AbortSignal
  screenshotsDir: string
  browser: BrowserHost | null
  log(level: LogLevel, message: string): void
  progress(done: number, total: number, label?: string): void
  updateLocal(patch: Partial<LocalSettings>): Promise<void>
}

export interface StageResult {
  summary: string
  counts: Record<string, number>
}
