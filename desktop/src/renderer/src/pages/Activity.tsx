import { useEffect, useRef, useState } from 'react'
import type { LogEntry } from '@shared/types'
import { api, useEngineEvents, useLoad } from '../lib/api'
import { ago, STAGE_TITLES } from '../lib/format'
import { Badge, Card, cx, Empty } from '../components/ui'

const LEVEL_STYLE: Record<LogEntry['level'], string> = {
  info: 'text-muted',
  success: 'text-ok',
  warn: 'text-warn',
  error: 'text-bad'
}

export function ActivityPage() {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const runs = useLoad(() => api.listRuns(40), [], 30_000)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    void api.getLogs().then(setLogs)
  }, [])
  useEngineEvents((e) => {
    if (e.type === 'log') setLogs((prev) => [...prev.slice(-799), e.entry])
  })
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [logs.length])

  return (
    <div className="grid gap-6">
      <div>
        <h1 className="text-xl font-semibold">Журнал</h1>
        <div className="mt-1 text-sm text-muted">Что приложение делает прямо сейчас и чем закончились прошлые запуски на всех ваших компьютерах.</div>
      </div>
      <Card title="Сейчас (этот компьютер)">
        <div className="h-[360px] overflow-y-auto rounded-md bg-bg p-3 font-mono text-xs leading-relaxed">
          {!logs.length && <div className="text-muted">Пока тихо</div>}
          {logs.map((l, i) => (
            <div key={i} className="flex gap-3">
              <span className="shrink-0 text-muted/70">{new Date(l.at).toLocaleTimeString('ru-RU')}</span>
              <span className="w-28 shrink-0 truncate text-muted">{l.stage === 'app' ? 'приложение' : STAGE_TITLES[l.stage]}</span>
              <span className={cx('min-w-0 break-words', LEVEL_STYLE[l.level])}>{l.message}</span>
            </div>
          ))}
          <div ref={bottom} />
        </div>
      </Card>
      <Card title="Запуски">
        {!runs.data?.length ? (
          <Empty>Запусков ещё не было</Empty>
        ) : (
          <div className="grid gap-1.5">
            {runs.data.map((r) => (
              <div key={r.id} className="grid grid-cols-[140px_110px_1fr] gap-3 rounded-md px-2 py-1.5 text-sm hover:bg-panel-2">
                <div className="text-muted">{STAGE_TITLES[r.stage]}</div>
                <div className="text-xs text-muted">{ago(r.startedAt)}</div>
                <div className="min-w-0">
                  {r.finishedAt == null ? (
                    <Badge tone="accent">идёт</Badge>
                  ) : r.ok ? (
                    <span className="text-text/85">{r.summary}</span>
                  ) : (
                    <span className="text-bad">{r.error || r.summary}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
