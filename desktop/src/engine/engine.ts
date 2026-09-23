import { EventEmitter } from 'node:events'
import { createSql, type Sql } from './db/client'
import { runMigrations } from './db/migrations'
import {
  acquireLock,
  heartbeatDevice,
  icpBacklog,
  lastRunByStage,
  lastRuns,
  lockHolder,
  pauseOldWorkerSchedules,
  probeBacklog,
  readAppSettings,
  readOldWorkerSchedules,
  releaseLock,
  resetProbe,
  setCommunityFit,
  startRun,
  finishRun,
  writeAppSettings,
  type Row
} from './db/repo'
import { resolveTask as resolveTaskRow, joinQueueSize, visitsToday } from './db/content'
import * as views from './db/views'
import { DEFAULT_SHARED, mergeSettings } from './defaults'
import type { BrowserHost, EngineSecrets, StageContext, StageResult } from './context'
import { runVerify, llmConfigFrom, icpVersionFor } from './stages/verify'
import { runJoin, JOIN_ACCOUNT } from './stages/join'
import { runScrape } from './stages/scrape'
import { runDiscover, importCommunities } from './stages/discover'
import { egoPing, egoVersion, resolveEgoBin } from './join/ego'
import { completeJson } from './llm/client'
import { z } from 'zod'
import { AbortedError, errorMessage } from './util'
import { circleGovernor, withPriority } from './circle/governor'
import { PROVIDER_LABEL } from '../shared/models'
import {
  STAGES,
  type ActionResult,
  type AppStatus,
  type CommunityFilter,
  type Funnel,
  type LocalSettings,
  type LogEntry,
  type LogLevel,
  type OldWorkerState,
  type PostFilter,
  type RunSummary,
  type SharedSettings,
  type StageName,
  type StageProgress,
  type StageStatus
} from '../shared/types'

export interface EngineDeps {
  deviceId: string
  appVersion: string
  platform: string
  screenshotsDir: string
  browser: BrowserHost | null
  getSecrets(): Promise<EngineSecrets & { dbUrl?: string }>
  getLocal(): LocalSettings
  updateLocal(patch: Partial<LocalSettings>): Promise<void>
}

interface RunningStage {
  controller: AbortController
  progress: StageProgress | null
  startedAt: Date
  lockTimer: NodeJS.Timeout | null
}

type StageOptions = { communityIds?: number[]; communityId?: number; crawl?: boolean; auto?: boolean }

const LOCK_TTL_SECONDS = 15 * 60
const HTTP_HEAVY: StageName[] = ['verify', 'scrape', 'discover']
const SHARED_KEY = 'shared'

function toRunSummary(r: Row): RunSummary {
  const iso = (v: unknown): string | null => (v instanceof Date ? v.toISOString() : v ? String(v) : null)
  return {
    id: Number(r.id),
    stage: String(r.stage) as StageName,
    startedAt: iso(r.started_at) ?? '',
    finishedAt: iso(r.finished_at),
    ok: r.ok == null ? null : Boolean(r.ok),
    summary: (r.summary as string | null) ?? null,
    counts: (r.counts as Record<string, number> | null) ?? null,
    error: (r.error as string | null) ?? null,
    deviceId: (r.device_id as string | null) ?? null
  }
}

export class Engine extends EventEmitter {
  private sql: Sql | null = null
  private shared: SharedSettings = DEFAULT_SHARED
  private readonly running = new Map<StageName, RunningStage>()
  private readonly logs: LogEntry[] = []
  private schedulerTimer: NodeJS.Timeout | null = null
  private heartbeatTimer: NodeJS.Timeout | null = null
  private dbError: string | null = null
  private health: { ego: { ok: boolean; message: string; version?: string }; checkedAt: number } = {
    ego: { ok: false, message: 'не проверено' },
    checkedAt: 0
  }
  private ticking = false
  private connecting: Promise<void> | null = null

  constructor(private readonly deps: EngineDeps) {
    super()
    const saved = deps.getLocal().circleCooldownUntil
    circleGovernor.restoreCooldown(saved ? new Date(saved) : null)
    circleGovernor.onTrip = (until, reason) => {
      this.log('warn', 'app', `Circle ограничил запросы с этого компьютера (${reason}). Все запросы к Circle на паузе до ${until.toLocaleTimeString('ru-RU')}.`)
      void this.deps.updateLocal({ circleCooldownUntil: until.toISOString() })
      this.statusChanged()
    }
  }

  private applyNetworkLimits(): void {
    circleGovernor.configure(this.shared.network)
  }

  // ------------------------------------------------------------- logging ---

  log(level: LogLevel, stage: StageName | 'app', message: string): void {
    const entry: LogEntry = { at: new Date().toISOString(), level, stage, message }
    this.logs.push(entry)
    if (this.logs.length > 800) this.logs.splice(0, this.logs.length - 800)
    this.emit('log', entry)
  }

  getLogs(): LogEntry[] {
    return [...this.logs]
  }

  private statusChanged(): void {
    this.emit('status')
  }

  // ---------------------------------------------------------- connection ---

  get connected(): boolean {
    return this.sql !== null
  }

  /** For shell features that write through the same connection (manual join). */
  sqlForShell(): Sql | null {
    return this.sql
  }

  anyRunning(): boolean {
    return this.running.size > 0
  }

  private requireSql(): Sql {
    if (!this.sql) throw new Error(this.dbError ? `База недоступна: ${this.dbError}` : 'База не настроена: укажите строку подключения Supabase в настройках')
    return this.sql
  }

  /** Resolves once any in-flight connect (first start, new DB URL) has finished. */
  async settled(): Promise<void> {
    await this.connecting?.catch(() => undefined)
  }

  connect(): Promise<void> {
    this.connecting = this.doConnect()
    return this.connecting
  }

  private async doConnect(): Promise<void> {
    await this.disconnect()
    const secrets = await this.deps.getSecrets()
    if (!secrets.dbUrl) {
      this.dbError = null
      this.statusChanged()
      return
    }
    const sql = createSql(secrets.dbUrl)
    try {
      await sql`select 1`
      const applied = await runMigrations(sql)
      if (applied.length) this.log('info', 'app', `Схема warmr_app обновлена: ${applied.join(', ')}`)
      this.sql = sql
      this.dbError = null
      this.shared = mergeSettings(DEFAULT_SHARED, await readAppSettings(sql, SHARED_KEY))
      this.applyNetworkLimits()
      await this.heartbeat()
      this.log('success', 'app', 'Подключено к базе Supabase')
    } catch (err) {
      this.dbError = errorMessage(err)
      this.log('error', 'app', `Не удалось подключиться к базе: ${this.dbError}`)
      await sql.end({ timeout: 2 }).catch(() => {})
    }
    this.statusChanged()
  }

  async disconnect(): Promise<void> {
    if (!this.sql) return
    const sql = this.sql
    this.sql = null
    await sql.end({ timeout: 5 }).catch(() => {})
  }

  start(): void {
    this.schedulerTimer = setInterval(() => void this.tick(), 30_000)
    this.heartbeatTimer = setInterval(() => void this.heartbeat(), 60_000)
    setTimeout(() => void this.tick(), 5_000)
  }

  async shutdown(): Promise<void> {
    if (this.schedulerTimer) clearInterval(this.schedulerTimer)
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer)
    for (const stage of this.running.keys()) this.stopStage(stage)
    await new Promise((r) => setTimeout(r, 300))
    await this.disconnect()
  }

  private async heartbeat(): Promise<void> {
    if (!this.sql) return
    const stages = [...this.running.keys()].join(',') || null
    await heartbeatDevice(this.sql, {
      id: this.deps.deviceId,
      name: this.deps.getLocal().deviceName || this.deps.platform,
      platform: this.deps.platform,
      version: this.deps.appVersion,
      stage: stages
    }).catch(() => {})
  }

  // ------------------------------------------------------------ settings ---

  sharedSettings(): SharedSettings {
    return this.shared
  }

  async saveShared(patch: Partial<SharedSettings>): Promise<SharedSettings> {
    const sql = this.requireSql()
    // Re-read first: another device may have saved in the meantime.
    const current = mergeSettings(DEFAULT_SHARED, await readAppSettings(sql, SHARED_KEY))
    this.shared = mergeSettings(current, patch)
    this.applyNetworkLimits()
    await writeAppSettings(sql, SHARED_KEY, this.shared, this.deps.deviceId)
    this.statusChanged()
    return this.shared
  }

  async setAutopilot(on: boolean): Promise<void> {
    await this.saveShared({ autopilot: on })
    this.log('info', 'app', on ? 'Автопилот включён' : 'Автопилот выключен')
    if (on) void this.tick()
  }

  private async refreshShared(): Promise<void> {
    if (!this.sql) return
    try {
      this.shared = mergeSettings(DEFAULT_SHARED, await readAppSettings(this.sql, SHARED_KEY))
      this.applyNetworkLimits()
    } catch {
      // keep the last good settings
    }
  }

  // -------------------------------------------------------------- stages ---

  isRunning(stage: StageName): boolean {
    return this.running.has(stage)
  }

  stopStage(stage: StageName): void {
    const run = this.running.get(stage)
    if (run) {
      run.controller.abort()
      this.log('info', stage, 'Останавливаю…')
    }
  }

  async runStage(stage: StageName, opts: StageOptions = {}): Promise<ActionResult> {
    if (this.running.has(stage)) return { ok: false, message: 'Этот этап уже выполняется' }
    let sql: Sql
    try {
      sql = this.requireSql()
    } catch (err) {
      return { ok: false, message: errorMessage(err) }
    }
    const lockName = `stage:${stage}`
    const gotLock = await acquireLock(sql, lockName, this.deps.deviceId, LOCK_TTL_SECONDS).catch(() => false)
    if (!gotLock) {
      const holder = await lockHolder(sql, lockName).catch(() => null)
      return { ok: false, message: `Этап уже выполняется на другом компьютере (${holder ?? 'неизвестно'})` }
    }
    const controller = new AbortController()
    const run: RunningStage = { controller, progress: null, startedAt: new Date(), lockTimer: null }
    run.lockTimer = setInterval(() => void acquireLock(sql, lockName, this.deps.deviceId, LOCK_TTL_SECONDS).catch(() => {}), 5 * 60_000)
    this.running.set(stage, run)
    this.statusChanged()
    void this.heartbeat()

    const secrets = await this.deps.getSecrets()
    const ctx: StageContext = {
      sql,
      deviceId: this.deps.deviceId,
      settings: this.shared,
      local: this.deps.getLocal(),
      secrets,
      signal: controller.signal,
      screenshotsDir: this.deps.screenshotsDir,
      browser: this.deps.browser,
      log: (level, message) => this.log(level, stage, message),
      progress: (done, total, label) => {
        run.progress = { done, total, label }
        this.emit('progress', stage, run.progress)
      },
      updateLocal: (patch) => this.deps.updateLocal(patch)
    }
    const runId = await startRun(sql, stage, this.deps.deviceId).catch(() => null)
    this.log('info', stage, opts.auto ? 'Запуск по расписанию' : 'Запуск')
    void (async () => {
      let result: StageResult | null = null
      let error: string | null = null
      try {
        // Verify and discover yield Circle's per-IP budget to reading and
        // joining, which are what turn into leads.
        if (stage === 'verify') result = await withPriority('low', () => runVerify(ctx, { communityIds: opts.communityIds }))
        else if (stage === 'join') result = await runJoin(ctx, { communityId: opts.communityId })
        else if (stage === 'scrape') result = await runScrape(ctx, { communityId: opts.communityId })
        else result = await withPriority('low', () => runDiscover(ctx, { crawl: opts.crawl }))
        this.log('success', stage, `Готово: ${result.summary}`)
      } catch (err) {
        if (err instanceof AbortedError || controller.signal.aborted) {
          error = 'остановлено'
          this.log('warn', stage, 'Остановлено')
        } else {
          error = errorMessage(err)
          this.log('error', stage, `Ошибка: ${error}`)
        }
      } finally {
        if (run.lockTimer) clearInterval(run.lockTimer)
        this.running.delete(stage)
        if (runId != null) {
          await finishRun(sql, runId, {
            ok: error == null,
            summary: result?.summary ?? error ?? '',
            counts: result?.counts ?? {},
            error
          }).catch(() => {})
        }
        await releaseLock(sql, lockName, this.deps.deviceId).catch(() => {})
        this.emit('progress', stage, null)
        this.statusChanged()
        void this.heartbeat()
      }
    })()
    return { ok: true, message: 'Запущено' }
  }

  // ----------------------------------------------------------- scheduler ---

  private async lastRunTimes(): Promise<Map<StageName, RunSummary>> {
    const out = new Map<StageName, RunSummary>()
    if (!this.sql) return out
    for (const r of await lastRunByStage(this.sql)) {
      const summary = toRunSummary(r)
      out.set(summary.stage, summary)
    }
    return out
  }

  private intervalMinutes(stage: StageName, backlog: number): number {
    const s = this.shared
    if (stage === 'verify') return backlog > 0 ? s.verify.everyMinutes : 6 * 60
    if (stage === 'scrape') return s.scrape.everyHours * 60
    if (stage === 'discover') return s.discover.directoryEveryHours * 60
    return s.join.everyHours * 60
  }

  private joinQueueOptions(): { includePaid: boolean; approvedOnly: boolean } {
    return { includePaid: this.shared.join.includePaid, approvedOnly: this.shared.join.approvedOnly }
  }

  private stageEnabled(stage: StageName): boolean {
    return this.shared[stage].enabled
  }

  private async blockedReason(stage: StageName): Promise<string | null> {
    if (!this.sql) return 'нет подключения к базе'
    const cooling = circleGovernor.coolingDownUntil
    if (cooling && stage !== 'verify') {
      return `Circle ограничил запросы с этого компьютера — пауза до ${cooling.toLocaleTimeString('ru-RU')}`
    }
    if (stage === 'join') {
      if (this.deps.platform !== 'darwin') return 'автоджойн есть только на Mac (ego lite)'
      const bin = await resolveEgoBin(this.deps.getLocal().egoPath)
      if (!bin) return 'ego lite не установлен'
      const secrets = await this.deps.getSecrets()
      if (!secrets.circleEmail || !secrets.circlePassword) return null // logged-in hosts still work; others hand off
    }
    if (stage === 'discover' && !this.deps.browser) return 'нужен встроенный браузер'
    return null
  }

  private async tick(): Promise<void> {
    if (this.ticking || !this.sql) return
    this.ticking = true
    try {
      await this.refreshShared()
      if (!this.shared.autopilot) return
      const last = await this.lastRunTimes()
      let backlog = 0
      const llm = llmConfigFrom(this.shared, await this.deps.getSecrets())
      backlog =
        (await probeBacklog(this.sql, {
          reprobeAliveDays: this.shared.verify.reprobeAliveDays,
          reprobeOtherDays: this.shared.verify.reprobeOtherDays
        })) + (await icpBacklog(this.sql, icpVersionFor(this.shared, llm), Boolean(llm)))
      for (const stage of ['verify', 'scrape', 'discover', 'join'] as StageName[]) {
        if (this.running.has(stage) || !this.stageEnabled(stage)) continue
        if (HTTP_HEAVY.includes(stage) && HTTP_HEAVY.some((s) => this.running.has(s))) continue
        if (await this.blockedReason(stage)) continue
        if (stage === 'join') {
          const used = await visitsToday(this.sql, JOIN_ACCOUNT)
          if (used >= this.shared.join.maxVisitsPerDay) continue
          if ((await joinQueueSize(this.sql, this.joinQueueOptions())) === 0) continue
        }
        const lastRun = last.get(stage)
        const lastAt = lastRun ? new Date(lastRun.finishedAt || lastRun.startedAt).getTime() : 0
        const failedRecently = lastRun && lastRun.ok === false
        const wait = failedRecently ? Math.min(30, this.intervalMinutes(stage, backlog)) : this.intervalMinutes(stage, backlog)
        if (Date.now() - lastAt >= wait * 60_000) {
          const res = await this.runStage(stage, { auto: true })
          if (!res.ok) this.log('info', stage, `Пропуск по расписанию: ${res.message}`)
          if (HTTP_HEAVY.includes(stage)) break
        }
      }
    } catch (err) {
      this.log('warn', 'app', `Планировщик: ${errorMessage(err)}`)
    } finally {
      this.ticking = false
    }
  }

  async nextRunAt(stage: StageName, last: RunSummary | null, backlog: number): Promise<string | null> {
    if (!this.shared.autopilot || !this.stageEnabled(stage)) return null
    const lastAt = last ? new Date(last.finishedAt || last.startedAt).getTime() : 0
    const wait = last?.ok === false ? Math.min(30, this.intervalMinutes(stage, backlog)) : this.intervalMinutes(stage, backlog)
    return new Date(Math.max(Date.now(), lastAt + wait * 60_000)).toISOString()
  }

  // -------------------------------------------------------------- health ---

  async checkEgo(force = false): Promise<{ ok: boolean; message: string; version?: string }> {
    if (!force && Date.now() - this.health.checkedAt < 5 * 60_000) return this.health.ego
    let ego: { ok: boolean; message: string; version?: string }
    if (this.deps.platform !== 'darwin') {
      ego = { ok: false, message: 'ego lite есть только для macOS; на Windows вступление вручную' }
    } else {
      const bin = await resolveEgoBin(this.deps.getLocal().egoPath)
      if (!bin) ego = { ok: false, message: 'ego-browser не найден (установите ego lite: lite.ego.app)' }
      else {
        try {
          const version = await egoVersion(bin)
          ego = { ok: true, message: `найден: ${bin}`, version }
        } catch (err) {
          ego = { ok: false, message: errorMessage(err) }
        }
      }
    }
    this.health = { ego, checkedAt: Date.now() }
    return ego
  }

  async status(): Promise<AppStatus> {
    const secrets = await this.deps.getSecrets()
    const ego = await this.checkEgo()
    const llm = llmConfigFrom(this.shared, secrets)
    const warnings: string[] = []
    let lastRuns = new Map<StageName, RunSummary>()
    let backlog = 0
    if (this.sql) {
      try {
        lastRuns = await this.lastRunTimes()
        backlog =
          (await probeBacklog(this.sql, {
            reprobeAliveDays: this.shared.verify.reprobeAliveDays,
            reprobeOtherDays: this.shared.verify.reprobeOtherDays
          })) + (await icpBacklog(this.sql, icpVersionFor(this.shared, llm), Boolean(llm)))
        const old = await this.oldWorker()
        if (old.active) {
          warnings.push(
            'Старый Python-воркер на Railway всё ещё работает по расписанию. Он будет параллельно читать сообщества и переоценивать ICP. Если новое приложение вас устраивает, выключите его в Настройках.'
          )
        }
      } catch (err) {
        warnings.push(`Ошибка чтения базы: ${errorMessage(err)}`)
      }
    }
    if (!secrets.dbUrl) warnings.push('Укажите строку подключения к базе Supabase в Настройках.')
    if (this.shared.icp.useLlm && !llm) {
      warnings.push(`Нет API-ключа ${PROVIDER_LABEL[this.shared.llm.provider]}: оценка ICP идёт только по правилам.`)
    }
    if (this.shared.join.enabled && !secrets.circleEmail) {
      warnings.push('Для автоджойна на новых доменах нужен логин и пароль Circle (Настройки → Аккаунт Circle).')
    }
    const stages: StageStatus[] = []
    for (const stage of STAGES) {
      const run = this.running.get(stage)
      const last = lastRuns.get(stage) ?? null
      stages.push({
        stage,
        running: Boolean(run),
        progress: run?.progress ?? null,
        lastRun: last,
        nextRunAt: await this.nextRunAt(stage, last, backlog),
        enabled: this.stageEnabled(stage),
        blockedReason: await this.blockedReason(stage)
      })
    }
    return {
      configured: Boolean(secrets.dbUrl),
      db: this.sql ? { ok: true, message: 'подключено' } : { ok: false, message: this.dbError ?? 'не настроено' },
      ego,
      llm: llm
        ? { ok: true, message: 'ключ задан', provider: llm.provider, model: llm.model }
        : { ok: false, message: this.shared.icp.useLlm ? 'нет ключа' : 'выключено' },
      circleAccount: Boolean(secrets.circleEmail && secrets.circlePassword),
      platform: this.deps.platform,
      appVersion: this.deps.appVersion,
      device: { id: this.deps.deviceId, name: this.deps.getLocal().deviceName || this.deps.platform },
      warnings,
      stages,
      autopilot: this.shared.autopilot,
      circle: {
        cooldownUntil: circleGovernor.coolingDownUntil?.toISOString() ?? null,
        requestsLastHour: circleGovernor.usedLastHour,
        maxPerHour: this.shared.network.maxPerHour
      }
    }
  }

  // --------------------------------------------------------------- tests ---

  async testDb(): Promise<ActionResult> {
    await this.connect()
    if (!this.sql) return { ok: false, message: this.dbError ?? 'строка подключения не задана' }
    const [row] = await this.sql`select count(*) as n from public.communities`
    return { ok: true, message: `Подключено. Сообществ в базе: ${Number(row?.n ?? 0)}` }
  }

  async testEgo(): Promise<ActionResult> {
    const ego = await this.checkEgo(true)
    if (!ego.ok) return { ok: false, message: ego.message }
    const bin = await resolveEgoBin(this.deps.getLocal().egoPath)
    try {
      const reachable = await egoPing(bin!)
      return reachable
        ? { ok: true, message: `${ego.version}: ego lite отвечает` }
        : { ok: false, message: 'ego-browser запустился, но не ответил. Откройте приложение ego lite.' }
    } catch (err) {
      return { ok: false, message: `ego lite не отвечает: ${errorMessage(err)}. Откройте приложение ego lite.` }
    }
  }

  async testLlm(): Promise<ActionResult> {
    const llm = llmConfigFrom({ ...this.shared, icp: { ...this.shared.icp, useLlm: true } }, await this.deps.getSecrets())
    if (!llm) return { ok: false, message: 'Нет API-ключа для выбранного провайдера' }
    try {
      const res = await completeJson(llm, {
        system: 'Reply with JSON only.',
        user: 'Return {"ok": true}.',
        schema: z.object({ ok: z.boolean() }),
        jsonSchema: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'], additionalProperties: false },
        schemaName: 'ping',
        maxTokens: 2000
      })
      return { ok: res.data.ok === true, message: `${res.model} отвечает (${res.usage.inputTokens}+${res.usage.outputTokens} токенов)` }
    } catch (err) {
      return { ok: false, message: errorMessage(err) }
    }
  }

  // ------------------------------------------------------ data for the UI ---

  async funnel(): Promise<Funnel> {
    return views.funnel(this.requireSql(), {
      ...this.joinQueueOptions(),
      account: JOIN_ACCOUNT,
      visitCap: this.shared.join.maxVisitsPerDay
    })
  }

  listCommunities(filter: CommunityFilter) {
    return views.listCommunities(this.requireSql(), filter)
  }

  getCommunity(id: number) {
    return views.communityDetail(this.requireSql(), id)
  }

  listJoinQueue(limit = 50) {
    return views.joinQueue(this.requireSql(), { limit, ...this.joinQueueOptions() })
  }

  listJoinAttempts(limit = 100) {
    return views.joinAttempts(this.requireSql(), limit)
  }

  listTasks(state = 'open') {
    return views.tasks(this.requireSql(), state)
  }

  resolveTask(id: number, action: 'done' | 'dismiss') {
    return resolveTaskRow(this.requireSql(), id, action === 'done' ? 'done' : 'dismissed')
  }

  listPosts(filter: PostFilter) {
    return views.posts(this.requireSql(), filter)
  }

  async listRuns(limit = 50): Promise<RunSummary[]> {
    return (await lastRuns(this.requireSql(), limit)).map(toRunSummary)
  }

  async reverifyCommunity(id: number): Promise<ActionResult> {
    await resetProbe(this.requireSql(), id)
    return this.runStage('verify', { communityIds: [id] })
  }

  async setCommunityFit(id: number, fit: boolean): Promise<void> {
    await setCommunityFit(this.requireSql(), id, fit)
  }

  joinOne(id: number): Promise<ActionResult> {
    return this.runStage('join', { communityId: id })
  }

  scrapeOne(id: number): Promise<ActionResult> {
    return this.runStage('scrape', { communityId: id })
  }

  async importCommunities(text: string) {
    return importCommunities({ sql: this.requireSql() }, text)
  }

  async oldWorker(): Promise<OldWorkerState> {
    const s = await readOldWorkerSchedules(this.requireSql())
    // A missing key means the Python default is in force (settings_store.py:
    // twice_daily / daily / hourly), i.e. ON. Only an explicit 'off' stops it.
    const effective = (v: string | null | undefined, fallback: string): string => v || fallback
    const harvest = effective(s.harvest_schedule, 'twice_daily')
    const search = effective(s.harvest_search, 'daily')
    const icp = effective(s.icp_classification_schedule, 'hourly')
    return { harvest, search, icp, active: [harvest, search, icp].some((v) => v !== 'off') }
  }

  async pauseOldWorker(): Promise<OldWorkerState> {
    await pauseOldWorkerSchedules(this.requireSql())
    this.log('info', 'app', 'Расписание старого воркера выключено (harvest, search, ICP = off)')
    this.statusChanged()
    return this.oldWorker()
  }
}
