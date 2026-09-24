import { useState } from 'react'
import { Activity, Gauge, LogIn, MessagesSquare, Settings as SettingsIcon, Users } from 'lucide-react'
import { api, useLoad } from './lib/api'
import { cx, Toggle, Badge } from './components/ui'
import { Overview } from './pages/Overview'
import { Communities } from './pages/Communities'
import { Joins } from './pages/Joins'
import { Posts } from './pages/Posts'
import { ActivityPage } from './pages/Activity'
import { Settings } from './pages/Settings'

export type Page = 'overview' | 'communities' | 'joins' | 'posts' | 'activity' | 'settings'

const NAV: Array<{ id: Page; title: string; icon: typeof Gauge }> = [
  { id: 'overview', title: 'Воронка', icon: Gauge },
  { id: 'communities', title: 'Сообщества', icon: Users },
  { id: 'joins', title: 'Вступления', icon: LogIn },
  { id: 'posts', title: 'Посты', icon: MessagesSquare },
  { id: 'activity', title: 'Журнал', icon: Activity },
  { id: 'settings', title: 'Настройки', icon: SettingsIcon }
]

export interface Nav {
  go: (page: Page, params?: { communityId?: number }) => void
  params: { communityId?: number }
}

export function App() {
  const [page, setPage] = useState<Page>('overview')
  const [params, setParams] = useState<{ communityId?: number }>({})
  const status = useLoad(() => api.getStatus(), [], 15_000)
  const nav: Nav = {
    go: (next, p = {}) => {
      setParams(p)
      setPage(next)
    },
    params
  }
  const s = status.data

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-panel">
        <div className="flex items-center gap-2.5 px-5 pb-5 pt-8" style={{ WebkitAppRegion: 'drag' } as React.CSSProperties}>
          <div className="grid h-8 w-8 place-items-center rounded-lg bg-accent">
            <div className="h-3.5 w-3.5 rounded-full border-[3px] border-white" />
          </div>
          <div>
            <div className="text-sm font-semibold leading-tight">Warmr Circle</div>
            <div className="text-[11px] text-muted">{s ? `v${s.appVersion} · ${s.device.name}` : '…'}</div>
          </div>
        </div>
        <nav className="grid gap-0.5 px-3">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => nav.go(item.id)}
              className={cx(
                'flex items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm transition cursor-default',
                page === item.id ? 'bg-panel-2 text-text' : 'text-muted hover:text-text hover:bg-panel-2/60'
              )}
            >
              <item.icon size={16} />
              {item.title}
            </button>
          ))}
        </nav>
        <div className="mt-auto grid gap-3 border-t border-line p-4">
          <Toggle
            checked={Boolean(s?.autopilot)}
            disabled={!s?.db.ok}
            onChange={async (on) => {
              await api.setAutopilot(on)
              status.reload()
            }}
            label={<span className="font-medium">Автопилот</span>}
          />
          <div className="text-[11px] leading-relaxed text-muted">
            {s?.autopilot ? 'Этапы запускаются сами по расписанию, пока приложение открыто (или в трее).' : 'Этапы запускаются только кнопками.'}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Badge tone={s?.db.ok ? 'ok' : 'bad'} title={s?.db.message}>
              база
            </Badge>
            <Badge tone={s?.llm.ok ? 'ok' : 'warn'} title={s?.llm.message}>
              LLM
            </Badge>
            <Badge tone={s?.ego.ok ? 'ok' : s?.platform === 'darwin' ? 'warn' : 'muted'} title={s?.ego.message}>
              ego
            </Badge>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[1200px] px-8 pb-16 pt-8">
          {page === 'overview' && <Overview nav={nav} status={s} reloadStatus={status.reload} />}
          {page === 'communities' && <Communities nav={nav} />}
          {page === 'joins' && <Joins nav={nav} status={s} />}
          {page === 'posts' && <Posts nav={nav} />}
          {page === 'activity' && <ActivityPage />}
          {page === 'settings' && <Settings onChanged={status.reload} dbConnected={Boolean(s?.db.ok)} />}
        </div>
      </main>
    </div>
  )
}
