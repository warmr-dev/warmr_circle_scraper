// Contract shared by the engine (main process) and the UI (renderer).
// Plain data only: everything here crosses the IPC boundary as JSON.

export type StageName = 'discover' | 'verify' | 'join' | 'scrape'
export const STAGES: StageName[] = ['discover', 'verify', 'join', 'scrape']

export type LogLevel = 'info' | 'success' | 'warn' | 'error'

export interface LogEntry {
  at: string
  level: LogLevel
  stage: StageName | 'app'
  message: string
}

export interface StageProgress {
  done: number
  total: number
  label?: string
}

export interface RunSummary {
  id?: number
  stage: StageName
  startedAt: string
  finishedAt?: string | null
  ok?: boolean | null
  summary?: string | null
  counts?: Record<string, number> | null
  error?: string | null
  deviceId?: string | null
}

export interface StageStatus {
  stage: StageName
  running: boolean
  progress: StageProgress | null
  lastRun: RunSummary | null
  nextRunAt: string | null
  enabled: boolean
  blockedReason: string | null
}

export interface HealthItem {
  ok: boolean
  message: string
}

export interface AppStatus {
  configured: boolean
  db: HealthItem
  ego: HealthItem & { version?: string }
  llm: HealthItem & { provider?: string; model?: string }
  circleAccount: boolean
  platform: string
  appVersion: string
  device: { id: string; name: string }
  warnings: string[]
  stages: StageStatus[]
  autopilot: boolean
  circle: { cooldownUntil: string | null; requestsLastHour: number; maxPerHour: number }
}

export interface Funnel {
  total: number
  circle: number
  discoverUnresolved: number
  probed: number
  notProbed: number
  alive: number
  lockedOrAbsent: number
  unreachable: number
  withText: number
  icpJudged: number
  icpFit: number
  joinable: number
  joined: number
  withSession: number
  scrapedCommunities: number
  postsTotal: number
  postsNew24h: number
  openTasks: number
  visitsToday: number
  visitCap: number
  llmSpendToday: number
}

export interface CommunityRow {
  id: number
  slug: string
  url: string
  name: string | null
  platform: string | null
  joinType: string | null
  existsStatus: string | null
  icpScore: number | null
  icpFlag: boolean
  icpDecidedBy: string | null
  joinStatus: string | null
  spacesPublic: number | null
  membersTotal: number | null
  postsStored: number | null
  probedAt: string | null
  lastScrapedAt: string | null
  hasSession: boolean
}

export interface JoinAttemptRow {
  id: number
  communityId: number | null
  name: string | null
  host: string | null
  url: string | null
  account: string
  status: string
  terminal: boolean
  detail: string | null
  createdAt: string
}

export interface CommunityDetail extends CommunityRow {
  description: string | null
  priceLabel: string | null
  directoryGoals: string[]
  icpReasons: string[]
  joinTypeDetail: string | null
  probeDetail: string | null
  spaceNames: string[]
  icpLlmReason: string | null
  icpLlmConfidence: number | null
  joinStatusDetail: string | null
  joinedAt: string | null
  discoverySource: string | null
  connectionState: string | null
  connectionDetail: string | null
  attempts: JoinAttemptRow[]
}

export type IcpFilter = 'all' | 'fit' | 'notfit' | 'unjudged'

export interface CommunityFilter {
  search?: string
  platform?: string
  exists?: string
  icp?: IcpFilter
  joinStatus?: string
  joinType?: string
  hasSession?: boolean
  sort?: 'icp' | 'recent' | 'name' | 'posts'
  offset?: number
  limit?: number
}

export interface TaskRow {
  id: number
  kind: string
  communityId: number | null
  name: string | null
  host: string | null
  url: string | null
  title: string
  detail: string | null
  state: string
  createdAt: string
}

export interface PostRow {
  id: number
  communityId: number
  communityName: string | null
  host: string | null
  contentType: string
  title: string | null
  content: string
  url: string | null
  publishedAt: string | null
  authorName: string | null
  spaceName: string | null
}

export interface PostFilter {
  communityId?: number
  search?: string
  offset?: number
  limit?: number
}

export type LlmProvider = 'openrouter' | 'openai' | 'anthropic'

export interface SharedSettings {
  autopilot: boolean
  verify: {
    enabled: boolean
    everyMinutes: number
    batchSize: number
    concurrency: number
    reprobeAliveDays: number
    reprobeOtherDays: number
  }
  icp: {
    useLlm: boolean
    mode: 'all' | 'ambiguous'
    minConfidence: number
    autoApproveLlm: boolean
    profile: string
    batchSize: number
  }
  llm: {
    provider: LlmProvider
    model: string
    dailyBudgetUsd: number
  }
  discover: {
    enabled: boolean
    directoryEveryHours: number
    resolveBatch: number
  }
  join: {
    enabled: boolean
    everyHours: number
    maxVisitsPerDay: number
    minDelaySec: number
    jitterSec: number
    batchSize: number
    includePaid: boolean
    /** Scheduled joins only take communities the LLM or a human approved. */
    approvedOnly: boolean
  }
  scrape: {
    enabled: boolean
    everyHours: number
    maxRequestsPerRun: number
    /** One big community must not use a whole run: the rest wait otherwise. */
    maxRequestsPerCommunity: number
    includePublic: boolean
    maxPostAgeDays: number
    withComments: boolean
  }
  network: {
    requestsPerMinute: number
    maxPerHour: number
    cooldownMinutes: number
  }
}

export interface LocalSettings {
  deviceName: string
  egoPath: string
  egoSpaceId: number | null
  launchAtLogin: boolean
  circleCooldownUntil: string | null
}

export interface SecretsStatus {
  dbUrl: boolean
  openrouterKey: boolean
  openaiKey: boolean
  anthropicKey: boolean
  circleEmail: boolean
  circlePassword: boolean
}

export type SecretName = keyof SecretsStatus

export interface SettingsBundle {
  shared: SharedSettings
  local: LocalSettings
  secrets: SecretsStatus
}

export interface OldWorkerState {
  harvest: string | null
  search: string | null
  icp: string | null
  active: boolean
}

export interface ImportResult {
  added: number
  existing: number
  invalid: number
}

export interface ActionResult {
  ok: boolean
  message: string
}

export type AppEvent =
  | { type: 'log'; entry: LogEntry }
  | { type: 'status' }
  | { type: 'progress'; stage: StageName; progress: StageProgress | null }

export interface WarmrApi {
  getStatus(): Promise<AppStatus>
  refreshHealth(): Promise<AppStatus>
  getFunnel(): Promise<Funnel>
  runStage(stage: StageName): Promise<ActionResult>
  stopStage(stage: StageName): Promise<void>
  setAutopilot(on: boolean): Promise<void>
  listCommunities(filter: CommunityFilter): Promise<{ rows: CommunityRow[]; total: number }>
  getCommunity(id: number): Promise<CommunityDetail | null>
  reverifyCommunity(id: number): Promise<ActionResult>
  setCommunityFit(id: number, fit: boolean): Promise<void>
  joinOne(id: number): Promise<ActionResult>
  assistedJoin(id: number): Promise<ActionResult>
  scrapeOne(id: number): Promise<ActionResult>
  listJoinQueue(limit?: number): Promise<CommunityRow[]>
  listJoinAttempts(limit?: number): Promise<JoinAttemptRow[]>
  listTasks(state?: string): Promise<TaskRow[]>
  resolveTask(id: number, action: 'done' | 'dismiss'): Promise<void>
  listPosts(filter: PostFilter): Promise<{ rows: PostRow[]; total: number }>
  listRuns(limit?: number): Promise<RunSummary[]>
  getLogs(): Promise<LogEntry[]>
  getSettings(): Promise<SettingsBundle>
  saveSharedSettings(patch: Partial<SharedSettings>): Promise<SettingsBundle>
  saveLocalSettings(patch: Partial<LocalSettings>): Promise<SettingsBundle>
  setSecret(name: SecretName, value: string): Promise<SettingsBundle>
  testDb(): Promise<ActionResult>
  testLlm(): Promise<ActionResult>
  testEgo(): Promise<ActionResult>
  importEnvFile(): Promise<ActionResult>
  importCommunities(text: string): Promise<ImportResult>
  getOldWorker(): Promise<OldWorkerState>
  pauseOldWorker(): Promise<OldWorkerState>
  openExternal(url: string): Promise<void>
  onEvent(cb: (event: AppEvent) => void): () => void
}
