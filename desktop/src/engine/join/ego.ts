import { spawn } from 'node:child_process'
import { access, constants } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'
import driverSource from './ego_join_driver.mjs?raw'

// Bridge to ego lite's `ego-browser nodejs` CLI (port of join/ego_bridge.py).
// Protocol facts, all confirmed live in the Python app (WORKLOG 2026-09-15):
// - every `ego-browser nodejs` call is a fresh Node process; only the
//   TaskSpace survives between calls, by numeric id;
// - custom env vars are NOT passed into the script, so parameters (including
//   the Circle password) are injected as JSON literals into the script text;
// - the script's console.log goes to stderr, so both streams are scanned;
// - ego lite exists only for macOS (lite.ego.app, verified 2026-09-18).

export const JOIN_DRIVER_SOURCE: string = driverSource

export type EgoJoinStatus =
  | 'joined'
  | 'paid_skip'
  | 'pending_approval'
  | 'subscription_expired_skip'
  | 'invite_skip'
  | 'needs_login'
  | 'challenge_stop'
  | 'application_form_detected'
  | 'unclear'

export const TERMINAL_JOIN_STATUSES = new Set<EgoJoinStatus>([
  'joined',
  'paid_skip',
  'pending_approval',
  'subscription_expired_skip',
  'invite_skip'
])

export interface EgoCookie {
  name: string
  value: string
  domain: string
}

export interface EgoJoinResult {
  status: EgoJoinStatus
  detail: string
  screenshot: string | null
  cookies: EgoCookie[] | null
}

export class EgoBridgeError extends Error {}

const CANDIDATE_PATHS = [
  join(homedir(), '.local', 'bin', 'ego-browser'),
  '/usr/local/bin/ego-browser',
  '/opt/homebrew/bin/ego-browser'
]

async function isExecutable(path: string): Promise<boolean> {
  try {
    await access(path, constants.X_OK)
    return true
  } catch {
    return false
  }
}

/** Absolute path to the CLI, or null. Login-item launches have no shell PATH. */
export async function resolveEgoBin(configured?: string | null): Promise<string | null> {
  if (process.platform !== 'darwin') return null
  const candidates = [configured?.trim(), ...CANDIDATE_PATHS].filter((p): p is string => Boolean(p))
  for (const candidate of candidates) {
    const expanded = candidate.startsWith('~') ? join(homedir(), candidate.slice(1)) : candidate
    if (await isExecutable(expanded)) return expanded
  }
  return null
}

interface RunResult {
  code: number | null
  stdout: string
  stderr: string
}

function run(bin: string, args: string[], input: string | null, timeoutMs: number, signal?: AbortSignal): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    let settled = false
    const finish = (fn: () => void): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      fn()
    }
    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      finish(() => reject(new EgoBridgeError(`ego-browser timed out after ${Math.round(timeoutMs / 1000)}s`)))
    }, timeoutMs)
    const onAbort = (): void => {
      child.kill('SIGKILL')
      finish(() => reject(new EgoBridgeError('stopped')))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    child.stdout.on('data', (d: Buffer) => (stdout += d.toString()))
    child.stderr.on('data', (d: Buffer) => (stderr += d.toString()))
    child.on('error', (err) => finish(() => reject(new EgoBridgeError(`cannot start ego-browser: ${err.message}`))))
    child.on('close', (code) => finish(() => resolve({ code, stdout, stderr })))
    if (input != null) child.stdin.end(input)
    else child.stdin.end()
  })
}

export async function egoVersion(bin: string): Promise<string> {
  const res = await run(bin, ['--version'], null, 20_000)
  const line = (res.stdout || res.stderr).split('\n').find((l) => l.trim())
  if (res.code !== 0 || !line) throw new EgoBridgeError(`ego-browser --version failed (${res.code}): ${res.stderr.slice(0, 300)}`)
  return line.trim()
}

export async function runEgoScript(bin: string, script: string, timeoutMs: number, signal?: AbortSignal): Promise<RunResult> {
  const res = await run(bin, ['nodejs'], script, timeoutMs, signal)
  if (res.code !== 0) {
    throw new EgoBridgeError(`ego-browser nodejs exited ${res.code}: ${(res.stderr || res.stdout).slice(-800)}`)
  }
  return res
}

/** Harmless round trip: proves the CLI can reach a running ego lite. */
export async function egoPing(bin: string): Promise<boolean> {
  const res = await runEgoScript(bin, 'console.log("EGO_PING_OK")\n', 30_000)
  return `${res.stdout}\n${res.stderr}`.includes('EGO_PING_OK')
}

export async function openTaskSpace(bin: string, name: string): Promise<number> {
  const script = `const task = await taskSpace(${JSON.stringify(name)});\nconsole.log("EGO_JOIN_SPACE_ID=" + task.spaceId);\n`
  const res = await runEgoScript(bin, script, 60_000)
  for (const line of `${res.stdout}\n${res.stderr}`.split('\n')) {
    if (line.startsWith('EGO_JOIN_SPACE_ID=')) return Number(line.split('=', 2)[1])
  }
  throw new EgoBridgeError('could not read a task space id from ego-browser output')
}

export function buildJoinScript(args: {
  spaceId: number
  url: string
  email: string | null
  password: string | null
  screenshotDir: string | null
}): string {
  // Replacer functions, not replacement strings: a string replacement would
  // expand `$&`, `$'`, `$$` inside a password into other text.
  const values: Record<string, string> = {
    __EGO_JOIN_SPACE_ID__: JSON.stringify(args.spaceId),
    __EGO_JOIN_URL__: JSON.stringify(args.url),
    __EGO_JOIN_SCREENSHOT_DIR__: JSON.stringify(args.screenshotDir ?? ''),
    __EGO_JOIN_EMAIL__: JSON.stringify(args.email ?? ''),
    __EGO_JOIN_PASSWORD__: JSON.stringify(args.password ?? '')
  }
  let script = JOIN_DRIVER_SOURCE
  for (const [token, value] of Object.entries(values)) {
    script = script.replaceAll(token, () => value)
  }
  return script
}

export function parseJoinOutput(stdout: string, stderr: string): EgoJoinResult {
  const lines = `${stdout}\n${stderr}`.split('\n')
  const line = [...lines].reverse().find((l) => l.startsWith('EGO_JOIN_RESULT='))
  if (!line) throw new EgoBridgeError(`ego-browser produced no EGO_JOIN_RESULT line: ${stderr.slice(-500)}`)
  const payload = JSON.parse(line.slice('EGO_JOIN_RESULT='.length)) as Partial<EgoJoinResult>
  if (!payload.status) throw new EgoBridgeError('EGO_JOIN_RESULT without a status')
  return {
    status: payload.status as EgoJoinStatus,
    detail: payload.detail ?? '',
    screenshot: payload.screenshot ?? null,
    cookies: Array.isArray(payload.cookies) ? payload.cookies : null
  }
}

export async function attemptJoin(
  bin: string,
  args: { spaceId: number; url: string; email: string | null; password: string | null; screenshotDir: string | null },
  signal?: AbortSignal
): Promise<EgoJoinResult> {
  const res = await runEgoScript(bin, buildJoinScript(args), 150_000, signal)
  return parseJoinOutput(res.stdout, res.stderr)
}
