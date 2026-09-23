import { app, BrowserWindow, Menu, nativeImage, powerSaveBlocker, shell, Tray } from 'electron'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { Engine } from '../engine/engine'
import { Store, secretsFromEnv } from './store'
import { registerIpc } from './ipc'
import { ElectronBrowserHost } from './browserHost'
import type { AppEvent } from '../shared/types'

// Warmr Circle desktop: one process runs the whole funnel (find, verify, join,
// read) against the shared Supabase database, and keeps running from the tray
// when the window is closed.

// Dev-only switches (never set in a packaged build by the app itself):
//   WARMR_USER_DATA   isolate config/secrets in another folder
//   WARMR_DB_URL      use this database instead of the stored secret (tests)
//   WARMR_SNAPSHOT_DIR open every page, save screenshots there, then quit
//   WARMR_IMPORT_ENV  import secrets from this .env on startup (same as the
//                     Settings button), for setting up a machine from a shell
if (process.env.WARMR_USER_DATA) app.setPath('userData', process.env.WARMR_USER_DATA)
app.setName('Warmr Circle')

let mainWindow: BrowserWindow | null = null
let tray: Tray | null = null
let quitting = false
let blockerId: number | null = null
let shutDownDone = false
let shownOnce = false

if (!app.requestSingleInstanceLock()) {
  app.quit()
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    title: 'Warmr Circle',
    backgroundColor: '#0f1115',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      // Snapshots capture a window that may be covered by others; keep painting.
      backgroundThrottling: !process.env.WARMR_SNAPSHOT_DIR
    }
  })
  // Started at login: stay in the tray (Windows passes --hidden; macOS reports wasOpenedAtLogin).
  const startHidden =
    process.argv.includes('--hidden') || (process.platform === 'darwin' && app.getLoginItemSettings().wasOpenedAtLogin)
  win.once('ready-to-show', () => {
    if (!startHidden || shownOnce) win.show()
    shownOnce = true
  })
  win.on('close', (e) => {
    if (!quitting) {
      e.preventDefault()
      win.hide()
    }
  })
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  if (process.env.ELECTRON_RENDERER_URL) void win.loadURL(process.env.ELECTRON_RENDERER_URL)
  else void win.loadFile(join(__dirname, '../renderer/index.html'))
  return win
}

function showWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) mainWindow = createWindow()
  mainWindow.show()
  mainWindow.focus()
}

function trayIcon(): Electron.NativeImage {
  const dir = app.isPackaged ? process.resourcesPath : join(__dirname, '../../resources')
  if (process.platform === 'darwin') {
    // Template image: macOS tints it for light and dark menu bars.
    const image = nativeImage.createFromPath(join(dir, 'trayTemplate.png'))
    image.setTemplateImage(true)
    return image
  }
  // Windows/Linux trays are often dark: use the colored app icon.
  return nativeImage.createFromPath(join(dir, 'icon.png')).resize({ width: 16, height: 16 })
}

function send(event: AppEvent): void {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('warmr:event', event)
}

app.on('second-instance', () => showWindow())

app.whenReady().then(async () => {
  const store = new Store()
  if (process.env.WARMR_IMPORT_ENV) {
    const { readFileSync } = await import('node:fs')
    const found = secretsFromEnv(readFileSync(process.env.WARMR_IMPORT_ENV, 'utf8'))
    await store.setSecrets(found)
    console.log(`imported secrets: ${Object.keys(found).join(', ') || 'none'}`)
  }
  const screenshotsDir = join(store.userDataDir, 'screenshots')
  mkdirSync(screenshotsDir, { recursive: true })
  const browser = new ElectronBrowserHost()
  const engine = new Engine({
    deviceId: store.deviceId,
    appVersion: app.getVersion(),
    platform: process.platform,
    screenshotsDir,
    browser,
    getSecrets: async () => {
      const secrets = await store.getSecrets()
      return process.env.WARMR_DB_URL ? { ...secrets, dbUrl: process.env.WARMR_DB_URL } : secrets
    },
    getLocal: () => store.getLocal(),
    updateLocal: async (patch) => {
      store.updateLocal(patch)
    }
  })

  engine.on('log', (entry) => send({ type: 'log', entry }))
  engine.on('progress', (stage, progress) => send({ type: 'progress', stage, progress }))
  engine.on('status', () => {
    send({ type: 'status' })
    // Keep the process from being suspended while a stage runs (lid-close sleep still wins).
    if (engine.anyRunning() && blockerId == null) blockerId = powerSaveBlocker.start('prevent-app-suspension')
    if (!engine.anyRunning() && blockerId != null) {
      powerSaveBlocker.stop(blockerId)
      blockerId = null
    }
    updateTray(engine)
  })

  registerIpc({ engine, store, mainWindow: () => mainWindow })

  if (process.platform === 'darwin') {
    app.dock?.setMenu(Menu.buildFromTemplate([{ label: 'Открыть Warmr', click: showWindow }]))
  }
  tray = new Tray(trayIcon())
  tray.setToolTip('Warmr Circle')
  tray.on('click', showWindow)
  updateTray(engine)

  mainWindow = createWindow()
  await engine.connect()
  if (process.env.WARMR_SNAPSHOT_DIR) {
    void snapshotPages(process.env.WARMR_SNAPSHOT_DIR)
  } else {
    engine.start()
  }

  app.on('activate', showWindow)
  app.on('before-quit', () => {
    quitting = true
  })
  app.on('will-quit', (e) => {
    if (shutDownDone) return
    // Stop running stages and close the DB pool before exiting.
    e.preventDefault()
    shutDownDone = true
    void engine.shutdown().finally(() => {
      browser.destroy()
      app.quit()
    })
  })
})

function updateTray(engine: Engine): void {
  if (!tray) return
  const running = engine.anyRunning()
  tray.setToolTip(running ? 'Warmr Circle — работает' : 'Warmr Circle')
  if (process.platform === 'darwin') tray.setTitle(running ? ' ●' : '')
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Открыть Warmr', click: showWindow },
      { type: 'separator' },
      {
        label: 'Выйти',
        click: () => {
          quitting = true
          app.quit()
        }
      }
    ])
  )
}

/** Dev: click through every page and save a screenshot of each. */
async function snapshotPages(dir: string): Promise<void> {
  const { writeFileSync } = await import('node:fs')
  const win = mainWindow!
  await new Promise<void>((resolve) => {
    if (win.webContents.isLoading()) win.webContents.once('did-finish-load', () => resolve())
    else resolve()
  })
  win.setSize(1400, 900)
  win.show()
  const pages = ['Воронка', 'Сообщества', 'Вступления', 'Посты', 'Журнал', 'Настройки']
  for (const [i, title] of pages.entries()) {
    await win.webContents.executeJavaScript(
      `[...document.querySelectorAll('nav button')].find((b) => b.textContent.trim() === ${JSON.stringify(title)})?.click()`
    )
    await new Promise((r) => setTimeout(r, 2500))
    const image = await win.webContents.capturePage()
    writeFileSync(join(dir, `${i + 1}-${title}.png`), image.toPNG())
  }
  // One community drawer.
  await win.webContents.executeJavaScript(`[...document.querySelectorAll('nav button')].find((b) => b.textContent.trim() === 'Сообщества')?.click()`)
  for (let i = 0; i < 20; i++) {
    const hasRow = await win.webContents.executeJavaScript(`Boolean(document.querySelector('tbody tr'))`)
    if (hasRow) break
    await new Promise((r) => setTimeout(r, 500))
  }
  await win.webContents.executeJavaScript(`document.querySelector('tbody tr')?.click()`)
  await new Promise((r) => setTimeout(r, 3000))
  writeFileSync(join(dir, '7-карточка.png'), (await win.webContents.capturePage()).toPNG())
  quitting = true
  app.quit()
}

app.on('window-all-closed', () => {
  // Stay alive in the tray: the funnel keeps running without a window.
})
