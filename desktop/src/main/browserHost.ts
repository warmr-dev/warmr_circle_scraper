import { app, BrowserWindow } from 'electron'
import type { BrowserHost } from '../engine/context'
import { sleep } from '../engine/util'

// A real Chromium window (hidden unless a human is needed) for pages that sit
// behind a Cloudflare managed challenge: discover.circle.so and its product
// pages. It is an ordinary browser with its own persistent profile: no stealth
// flags, no spoofed fingerprint, no challenge solving. If Cloudflare asks for
// an interactive check, the window is shown and the user passes it themselves.

const PARTITION = 'persist:warmr-browser'

const CHALLENGE_PROBE = `(() => {
  const t = (document.title || '') + ' ' + (document.body ? document.body.innerText.slice(0, 2000) : '')
  return /just a moment|verifying you are human|checking your browser|attention required/i.test(t)
})()`

export class ElectronBrowserHost implements BrowserHost {
  private win: BrowserWindow | null = null
  private queue: Promise<unknown> = Promise.resolve()
  private quitting = false

  constructor() {
    app.on('before-quit', () => {
      this.quitting = true
    })
  }

  private window(): BrowserWindow {
    if (this.win && !this.win.isDestroyed()) return this.win
    this.win = new BrowserWindow({
      width: 1100,
      height: 820,
      show: false,
      title: 'Warmr — проверка браузером',
      webPreferences: { partition: PARTITION, contextIsolation: true, sandbox: true, nodeIntegration: false }
    })
    this.win.on('close', (e) => {
      // Hide instead of destroying so the checked page stays loaded. Not while
      // the app quits: a prevented close cancels the quit (seen 2026-09-18).
      if (this.quitting) return
      if (this.win && !this.win.isDestroyed()) {
        e.preventDefault()
        this.win.hide()
      }
    })
    return this.win
  }

  /** Serialize: one hidden window, one navigation at a time. */
  private exclusive<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.queue.then(fn, fn)
    this.queue = next.catch(() => undefined)
    return next
  }

  private async challenged(win: BrowserWindow): Promise<boolean> {
    try {
      return Boolean(await win.webContents.executeJavaScript(CHALLENGE_PROBE, true))
    } catch {
      return false
    }
  }

  /** Load `url` and wait until any Cloudflare interstitial has cleared. */
  private async settle(url: string, signal?: AbortSignal): Promise<{ win: BrowserWindow; challenged: boolean }> {
    const win = this.window()
    await win.loadURL(url).catch(() => undefined)
    // A non-interactive challenge clears by itself within seconds.
    for (let i = 0; i < 20; i++) {
      if (!(await this.challenged(win))) return { win, challenged: false }
      await sleep(1000, signal)
    }
    // Needs a human: show the window and give them three minutes.
    win.show()
    win.focus()
    for (let i = 0; i < 180; i++) {
      if (!(await this.challenged(win))) {
        win.hide()
        return { win, challenged: false }
      }
      await sleep(1000, signal)
    }
    return { win, challenged: true }
  }

  fetchJson(origin: string, paths: string[], signal?: AbortSignal): Promise<Array<{ path: string; status: number; json: unknown }>> {
    return this.exclusive(async () => {
      const current = this.win && !this.win.isDestroyed() ? this.win.webContents.getURL() : ''
      let win: BrowserWindow
      if (!current.startsWith(origin)) {
        const settled = await this.settle(origin, signal)
        if (settled.challenged) return paths.map((path) => ({ path, status: 403, json: null }))
        win = settled.win
      } else {
        win = this.window()
      }
      const out: Array<{ path: string; status: number; json: unknown }> = []
      for (const path of paths) {
        const result = (await win.webContents.executeJavaScript(
          `fetch(${JSON.stringify(path)}, { headers: { Accept: 'application/json' }, credentials: 'include' })
             .then(async (r) => ({ status: r.status, text: await r.text() }))
             .catch((e) => ({ status: 0, text: String(e) }))`,
          true
        )) as { status: number; text: string }
        let json: unknown = null
        try {
          json = JSON.parse(result.text)
        } catch {
          json = null
        }
        out.push({ path, status: result.status, json })
      }
      return out
    })
  }

  pageAnchors(url: string, signal?: AbortSignal) {
    return this.exclusive(async () => {
      const { win, challenged } = await this.settle(url, signal)
      if (challenged) return { finalUrl: url, anchors: [], challenged: true }
      // Client-rendered pages: give the app shell a moment to hydrate.
      await sleep(1500, signal)
      const anchors = (await win.webContents.executeJavaScript(
        `[...document.querySelectorAll('a[href]')].map((a) => ({ text: (a.innerText || a.textContent || '').trim(), href: a.href }))`,
        true
      )) as Array<{ text: string; href: string }>
      return { finalUrl: win.webContents.getURL(), anchors, challenged: false }
    })
  }

  destroy(): void {
    if (this.win && !this.win.isDestroyed()) this.win.destroy()
    this.win = null
  }
}
