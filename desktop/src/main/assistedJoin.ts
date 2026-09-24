import { BrowserWindow, session } from 'electron'
import type { Sql } from '../engine/db/client'
import { getJson } from '../engine/circle/http'
import { sessionCookies, SESSION_COOKIE_NAMES } from '../engine/circle/reader'
import { closeTasks, connectSession, persistJoinOutcome, recordJoinAttempt } from '../engine/db/content'
import { hostFromUrl } from '../engine/util'

// Manual ("assisted") join, for Windows (no ego lite) and for any host the
// automatic driver handed back. The user joins in a normal app window with its
// own persistent Circle login; when they close it, the app reads the session
// cookies from that window's profile and connects the community for reading.
// No clicks are automated here: a human does the joining.

export const CIRCLE_PARTITION = 'persist:warmr-circle'

const MEMBER_CHECK_PATHS = [
  '/internal_api/current_community_member',
  '/internal_api/community_members/me',
  '/internal_api/me'
]

async function memberCheck(host: string, cookies: Record<string, string>): Promise<'member' | 'not_member' | 'unknown'> {
  for (const path of MEMBER_CHECK_PATHS) {
    const res = await getJson(`https://${host}${path}`, { cookies, followRedirects: false, timeoutMs: 15_000 })
    if (res.status === 200 && res.isJson) return 'member'
    if (res.status === 401 || res.status === 403) return 'not_member'
  }
  return 'unknown'
}

export function openAssistedJoin(args: {
  sql: Sql
  deviceId: string
  community: { id: number; url: string; name: string | null }
  onDone: (level: 'success' | 'warn' | 'info', message: string) => void
}): void {
  const win = new BrowserWindow({
    width: 1200,
    height: 860,
    title: `Вступите в «${args.community.name || args.community.url}» и закройте окно`,
    webPreferences: { partition: CIRCLE_PARTITION, contextIsolation: true, sandbox: true, nodeIntegration: false }
  })
  void win.loadURL(args.community.url)
  let lastHost = hostFromUrl(args.community.url)
  win.webContents.on('did-navigate', (_e, url) => {
    const host = hostFromUrl(url)
    // Remember the community's real host (custom-domain redirects), not login pages.
    if (host && !/^(login|app|auth)\.circle\.so$/.test(host)) lastHost = host
  })
  win.on('closed', () => {
    void (async () => {
      const host = lastHost
      if (!host) return args.onDone('warn', 'Не удалось определить адрес сообщества')
      const all = await session.fromPartition(CIRCLE_PARTITION).cookies.get({ url: `https://${host}` })
      const kept = all.filter((c) => (SESSION_COOKIE_NAMES as readonly string[]).includes(c.name))
      const cookies = sessionCookies(kept)
      if (!Object.keys(cookies).length) {
        return args.onDone('info', `${host}: вход не найден — куки не сохранены`)
      }
      const verdict = await memberCheck(host, cookies)
      if (verdict === 'not_member') {
        return args.onDone('warn', `${host}: вы вошли, но Circle не считает вас участником — куки не сохранены`)
      }
      await connectSession(args.sql, {
        host,
        cookies: kept.map((c) => ({ name: c.name, value: c.value, domain: (c.domain || host).replace(/^\./, '') })),
        memberLabel: args.community.name,
        communityId: args.community.id,
        source: 'desktop_manual'
      })
      if (verdict === 'member') {
        await recordJoinAttempt(args.sql, {
          communityId: args.community.id,
          host,
          url: args.community.url,
          account: 'manual',
          status: 'joined',
          terminal: true,
          detail: 'joined by hand in the app window',
          deviceId: args.deviceId
        })
        await persistJoinOutcome(args.sql, args.community.id, 'joined', 'joined by hand in the app window')
        await closeTasks(args.sql, args.community.id, host, ['join_handoff', 'session_missing', 'session_expired'])
        args.onDone('success', `${host}: вступление подтверждено, сессия сохранена — посты будут читаться`)
      } else {
        args.onDone('info', `${host}: сессия сохранена; членство не удалось подтвердить, проверит этап чтения`)
      }
    })().catch((err) => args.onDone('warn', `Ручное вступление: ${err instanceof Error ? err.message : String(err)}`))
  })
}
