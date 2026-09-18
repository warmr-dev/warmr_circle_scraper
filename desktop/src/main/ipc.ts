import { BrowserWindow, dialog, ipcMain, shell, app } from 'electron'
import { readFileSync } from 'node:fs'
import type { Engine } from '../engine/engine'
import type { Store } from './store'
import { secretsFromEnv } from './store'
import { openAssistedJoin } from './assistedJoin'
import type {
  ActionResult,
  CommunityFilter,
  LocalSettings,
  PostFilter,
  SecretName,
  SettingsBundle,
  SharedSettings,
  StageName
} from '../shared/types'
import { errorMessage } from '../engine/util'

// Every renderer call lands here. The renderer never sees secrets: it can set
// them and learn whether each one is present, nothing more.

export function registerIpc(deps: { engine: Engine; store: Store; mainWindow: () => BrowserWindow | null }): void {
  const { engine, store } = deps

  const bundle = async (): Promise<SettingsBundle> => ({
    shared: engine.sharedSettings(),
    local: store.getLocal(),
    secrets: await store.secretsStatus()
  })

  const handle = <A extends unknown[], R>(channel: string, fn: (...args: A) => Promise<R> | R): void => {
    ipcMain.handle(channel, async (_event, ...args) => fn(...(args as A)))
  }

  handle('status', () => engine.status())
  handle('refreshHealth', async () => {
    await engine.checkEgo(true)
    return engine.status()
  })
  handle('funnel', () => engine.funnel())
  handle('runStage', (stage: StageName) => engine.runStage(stage))
  handle('stopStage', (stage: StageName) => engine.stopStage(stage))
  handle('setAutopilot', (on: boolean) => engine.setAutopilot(on))
  handle('listCommunities', (filter: CommunityFilter) => engine.listCommunities(filter))
  handle('getCommunity', (id: number) => engine.getCommunity(id))
  handle('reverifyCommunity', (id: number) => engine.reverifyCommunity(id))
  handle('setCommunityFit', (id: number, fit: boolean) => engine.setCommunityFit(id, fit))
  handle('joinOne', (id: number) => engine.joinOne(id))
  handle('scrapeOne', (id: number) => engine.scrapeOne(id))
  handle('assistedJoin', async (id: number): Promise<ActionResult> => {
    const community = await engine.getCommunity(id)
    if (!community) return { ok: false, message: 'Сообщество не найдено' }
    const sql = engine.sqlForShell()
    if (!sql) return { ok: false, message: 'Нет подключения к базе' }
    openAssistedJoin({
      sql,
      deviceId: store.deviceId,
      community: { id: community.id, url: community.url, name: community.name },
      onDone: (level, message) => engine.log(level, 'join', message)
    })
    return { ok: true, message: 'Открыл окно: войдите, вступите и закройте окно — сессию заберу сам' }
  })
  handle('listJoinQueue', (limit?: number) => engine.listJoinQueue(limit))
  handle('listJoinAttempts', (limit?: number) => engine.listJoinAttempts(limit))
  handle('listTasks', (state?: string) => engine.listTasks(state))
  handle('resolveTask', (id: number, action: 'done' | 'dismiss') => engine.resolveTask(id, action))
  handle('listPosts', (filter: PostFilter) => engine.listPosts(filter))
  handle('listRuns', (limit?: number) => engine.listRuns(limit))
  handle('getLogs', () => engine.getLogs())
  handle('getSettings', () => bundle())
  handle('saveSharedSettings', async (patch: Partial<SharedSettings>) => {
    await engine.saveShared(patch)
    return bundle()
  })
  handle('saveLocalSettings', async (patch: Partial<LocalSettings>) => {
    const local = store.updateLocal(patch)
    if (patch.launchAtLogin !== undefined && app.isPackaged) {
      app.setLoginItemSettings({ openAtLogin: local.launchAtLogin, args: ['--hidden'] })
    }
    if (patch.egoPath !== undefined) await engine.checkEgo(true)
    return bundle()
  })
  handle('setSecret', async (name: SecretName, value: string) => {
    await store.setSecrets({ [name]: value })
    if (name === 'dbUrl') await engine.connect()
    return bundle()
  })
  handle('testDb', async (): Promise<ActionResult> => {
    try {
      return await engine.testDb()
    } catch (err) {
      return { ok: false, message: errorMessage(err) }
    }
  })
  handle('testLlm', () => engine.testLlm())
  handle('testEgo', () => engine.testEgo())
  handle('importEnvFile', async (): Promise<ActionResult> => {
    const win = deps.mainWindow()
    const options = {
      title: 'Выберите .env файл проекта',
      properties: ['openFile' as const, 'showHiddenFiles' as const]
    }
    const picked = win ? await dialog.showOpenDialog(win, options) : await dialog.showOpenDialog(options)
    if (picked.canceled || !picked.filePaths[0]) return { ok: false, message: 'Отменено' }
    const found = secretsFromEnv(readFileSync(picked.filePaths[0], 'utf8'))
    const names = Object.keys(found)
    if (!names.length) return { ok: false, message: 'В файле нет CIRCLE_LEADS_DB, OPENAI_API_KEY, ANTHROPIC_API_KEY, CIRCLE_EMAIL, CIRCLE_PASSWORD' }
    await store.setSecrets(found)
    if (found.dbUrl) await engine.connect()
    const labels: Record<string, string> = {
      dbUrl: 'база',
      openaiKey: 'ключ OpenAI',
      anthropicKey: 'ключ Anthropic',
      circleEmail: 'email Circle',
      circlePassword: 'пароль Circle'
    }
    return { ok: true, message: `Импортировано: ${names.map((n) => labels[n] ?? n).join(', ')}` }
  })
  handle('importCommunities', (text: string) => engine.importCommunities(text))
  handle('getOldWorker', () => engine.oldWorker())
  handle('pauseOldWorker', () => engine.pauseOldWorker())
  handle('openExternal', async (url: string) => {
    if (/^https?:\/\//i.test(url)) await shell.openExternal(url)
  })
}
