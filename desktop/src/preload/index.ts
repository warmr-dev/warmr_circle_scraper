import { contextBridge, ipcRenderer } from 'electron'
import type { AppEvent, WarmrApi } from '../shared/types'

const invoke =
  (channel: string) =>
  (...args: unknown[]): Promise<never> =>
    ipcRenderer.invoke(channel, ...args) as Promise<never>

const api: WarmrApi = {
  getStatus: invoke('status'),
  refreshHealth: invoke('refreshHealth'),
  getFunnel: invoke('funnel'),
  runStage: invoke('runStage'),
  stopStage: invoke('stopStage'),
  setAutopilot: invoke('setAutopilot'),
  listCommunities: invoke('listCommunities'),
  getCommunity: invoke('getCommunity'),
  reverifyCommunity: invoke('reverifyCommunity'),
  setCommunityFit: invoke('setCommunityFit'),
  joinOne: invoke('joinOne'),
  assistedJoin: invoke('assistedJoin'),
  scrapeOne: invoke('scrapeOne'),
  listJoinQueue: invoke('listJoinQueue'),
  listJoinAttempts: invoke('listJoinAttempts'),
  listTasks: invoke('listTasks'),
  resolveTask: invoke('resolveTask'),
  listPosts: invoke('listPosts'),
  listRuns: invoke('listRuns'),
  getLogs: invoke('getLogs'),
  getSettings: invoke('getSettings'),
  saveSharedSettings: invoke('saveSharedSettings'),
  saveLocalSettings: invoke('saveLocalSettings'),
  setSecret: invoke('setSecret'),
  testDb: invoke('testDb'),
  testLlm: invoke('testLlm'),
  testEgo: invoke('testEgo'),
  importEnvFile: invoke('importEnvFile'),
  importCommunities: invoke('importCommunities'),
  getOldWorker: invoke('getOldWorker'),
  pauseOldWorker: invoke('pauseOldWorker'),
  openExternal: invoke('openExternal'),
  onEvent: (cb: (event: AppEvent) => void) => {
    const listener = (_: unknown, event: AppEvent): void => cb(event)
    ipcRenderer.on('warmr:event', listener)
    return () => ipcRenderer.removeListener('warmr:event', listener)
  }
}

contextBridge.exposeInMainWorld('warmr', api)
