import { app, safeStorage } from 'electron'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { hostname } from 'node:os'
import { join } from 'node:path'
import { DEFAULT_LOCAL, mergeSettings } from '../engine/defaults'
import type { LocalSettings, SecretName, SecretsStatus } from '../shared/types'

// Per-machine state in the app's userData folder:
//  - deviceId and local settings (ego path, ego task space, launch at login);
//  - secrets (DB URL, LLM keys, Circle login), encrypted with the OS keychain
//    through Electron safeStorage (Keychain on macOS, DPAPI on Windows).
// Secrets never go to the shared database and never reach the renderer.

export interface Secrets {
  dbUrl?: string
  openrouterKey?: string
  openaiKey?: string
  anthropicKey?: string
  circleEmail?: string
  circlePassword?: string
}

interface ConfigFile {
  deviceId: string
  local: LocalSettings
  secrets: string | null
  secretsEncoding: 'safeStorage' | 'plain' | null
}

const SECRET_NAMES: SecretName[] = ['dbUrl', 'openrouterKey', 'openaiKey', 'anthropicKey', 'circleEmail', 'circlePassword']

export class Store {
  private readonly path: string
  private config: ConfigFile
  private secretsCache: Secrets | null = null

  constructor() {
    const dir = app.getPath('userData')
    mkdirSync(dir, { recursive: true })
    this.path = join(dir, 'config.json')
    this.config = this.read()
  }

  private read(): ConfigFile {
    const fresh: ConfigFile = {
      deviceId: randomUUID(),
      local: { ...DEFAULT_LOCAL, deviceName: hostname() },
      secrets: null,
      secretsEncoding: null
    }
    if (!existsSync(this.path)) {
      this.config = fresh
      this.write()
      return fresh
    }
    try {
      const raw = JSON.parse(readFileSync(this.path, 'utf8')) as Partial<ConfigFile>
      return {
        deviceId: raw.deviceId || fresh.deviceId,
        local: mergeSettings({ ...DEFAULT_LOCAL, deviceName: hostname() }, raw.local ?? {}),
        secrets: raw.secrets ?? null,
        secretsEncoding: raw.secretsEncoding ?? null
      }
    } catch {
      return fresh
    }
  }

  private write(): void {
    const tmp = `${this.path}.tmp`
    writeFileSync(tmp, JSON.stringify(this.config, null, 2), { mode: 0o600 })
    renameSync(tmp, this.path)
  }

  get deviceId(): string {
    return this.config.deviceId
  }

  get userDataDir(): string {
    return app.getPath('userData')
  }

  getLocal(): LocalSettings {
    return { ...this.config.local }
  }

  updateLocal(patch: Partial<LocalSettings>): LocalSettings {
    this.config.local = mergeSettings(this.config.local, patch)
    this.write()
    return this.getLocal()
  }

  async getSecrets(): Promise<Secrets> {
    if (this.secretsCache) return { ...this.secretsCache }
    if (!this.config.secrets) {
      this.secretsCache = {}
      return {}
    }
    try {
      const buffer = Buffer.from(this.config.secrets, 'base64')
      const text =
        this.config.secretsEncoding === 'safeStorage'
          ? (await safeStorage.decryptStringAsync(buffer)).result
          : buffer.toString('utf8')
      this.secretsCache = JSON.parse(text) as Secrets
    } catch {
      this.secretsCache = {}
    }
    return { ...this.secretsCache }
  }

  async setSecrets(patch: Partial<Secrets>): Promise<void> {
    const current = await this.getSecrets()
    const next: Secrets = { ...current }
    for (const [key, value] of Object.entries(patch) as Array<[SecretName, string | undefined]>) {
      if (!SECRET_NAMES.includes(key)) continue
      if (value && value.trim()) next[key] = value.trim()
      else delete next[key]
    }
    const text = JSON.stringify(next)
    if (await safeStorage.isAsyncEncryptionAvailable()) {
      const encrypted = await safeStorage.encryptStringAsync(text)
      this.config.secrets = Buffer.from(encrypted).toString('base64')
      this.config.secretsEncoding = 'safeStorage'
    } else {
      // No OS keychain (rare Linux setups): stored unencrypted, file is 0600.
      this.config.secrets = Buffer.from(text, 'utf8').toString('base64')
      this.config.secretsEncoding = 'plain'
    }
    this.secretsCache = next
    this.write()
  }

  async secretsStatus(): Promise<SecretsStatus> {
    const s = await this.getSecrets()
    return {
      dbUrl: Boolean(s.dbUrl),
      openrouterKey: Boolean(s.openrouterKey),
      openaiKey: Boolean(s.openaiKey),
      anthropicKey: Boolean(s.anthropicKey),
      circleEmail: Boolean(s.circleEmail),
      circlePassword: Boolean(s.circlePassword)
    }
  }
}

/** Parse KEY=VALUE lines from a .env file into the secrets this app understands. */
export function secretsFromEnv(text: string): Partial<Secrets> {
  const env: Record<string, string> = {}
  for (const line of text.split(/\r?\n/)) {
    const m = /^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)\s*$/.exec(line)
    if (!m) continue
    let value = m[2]!.trim()
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1)
    env[m[1]!] = value
  }
  const out: Partial<Secrets> = {}
  if (env.CIRCLE_LEADS_DB) out.dbUrl = env.CIRCLE_LEADS_DB
  if (env.OPENROUTER_API_KEY) out.openrouterKey = env.OPENROUTER_API_KEY
  if (env.OPENAI_API_KEY) out.openaiKey = env.OPENAI_API_KEY
  if (env.ANTHROPIC_API_KEY) out.anthropicKey = env.ANTHROPIC_API_KEY
  if (env.CIRCLE_EMAIL) out.circleEmail = env.CIRCLE_EMAIL
  if (env.CIRCLE_PASSWORD) out.circlePassword = env.CIRCLE_PASSWORD
  return out
}
