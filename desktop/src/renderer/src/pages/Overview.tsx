import { useState } from 'react'
import { AlertTriangle, ArrowRight, Play, Square } from 'lucide-react'
import type { AppStatus, StageName, StageStatus } from '@shared/types'
import { STAGES } from '@shared/types'
import { api, cleanError, useLoad, useEngineEvents } from '../lib/api'
import { ago, num, STAGE_HINTS, STAGE_TITLES, usd } from '../lib/format'
import { Badge, Button, Card, ErrorNote, Progress } from '../components/ui'
import type { Nav } from '../App'

function StageCard({ stage, onRun, onStop }: { stage: StageStatus; onRun: () => void; onStop: () => void }) {
  const last = stage.lastRun
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-line bg-panel p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold">{STAGE_TITLES[stage.stage]}</div>
          <div className="mt-1 text-xs leading-relaxed text-muted">{STAGE_HINTS[stage.stage]}</div>
        </div>
        {stage.running ? (
          <Badge tone="accent">работает</Badge>
        ) : !stage.enabled ? (
          <Badge>выключен</Badge>
        ) : stage.blockedReason ? (
          <Badge tone="warn" title={stage.blockedReason}>
            недоступен
          </Badge>
        ) : last?.ok === false ? (
          <Badge tone="bad">ошибка</Badge>
        ) : (
          <Badge tone="ok">готов</Badge>
        )}
      </div>
      {stage.running && stage.progress && (
        <div className="grid gap-1.5">
          <Progress value={stage.progress.done} total={stage.progress.total} />
          <div className="truncate text-xs text-muted">
            {stage.progress.label ? `${stage.progress.label}: ` : ''}
            {num(stage.progress.done)} из {num(stage.progress.total)}
          </div>
        </div>
      )}
      <div className="min-h-[36px] text-xs leading-relaxed text-muted">
        {stage.blockedReason && !stage.running ? (
          <span className="text-warn">{stage.blockedReason}</span>
        ) : last ? (
          <>
            <span className="text-text/80">{ago(last.finishedAt || last.startedAt)}:</span> {last.error && last.ok === false ? last.error : last.summary}
          </>
        ) : (
          'ещё не запускался'
        )}
      </div>
      <div className="mt-auto flex items-center justify-between gap-2">
        <div className="text-[11px] text-muted">{stage.nextRunAt && !stage.running ? `следующий ${ago(stage.nextRunAt)}` : ''}</div>
        {stage.running ? (
          <Button size="sm" variant="danger" onClick={onStop}>
            <Square size={12} /> Стоп
          </Button>
        ) : (
          <Button size="sm" variant="primary" onClick={onRun} disabled={Boolean(stage.blockedReason)}>
            <Play size={12} /> Запустить
          </Button>
        )}
      </div>
    </div>
  )
}

function Step({ label, value, sub, tone }: { label: string; value: number | undefined; sub?: string; tone?: 'accent' }) {
  return (
    <div className="min-w-0 rounded-lg border border-line bg-panel-2/40 px-3 py-2.5">
      <div className="truncate text-[11px] text-muted">{label}</div>
      <div className={`tabular text-xl font-semibold ${tone === 'accent' ? 'text-accent' : ''}`}>{num(value)}</div>
      {sub && <div className="truncate text-[11px] text-muted">{sub}</div>}
    </div>
  )
}

export function Overview({ nav, status, reloadStatus }: { nav: Nav; status: AppStatus | null; reloadStatus: () => void }) {
  const funnel = useLoad(() => api.getFunnel(), [], 20_000)
  const [progress, setProgress] = useState<Record<string, StageStatus['progress']>>({})
  const [message, setMessage] = useState<string | null>(null)
  useEngineEvents((e) => {
    if (e.type === 'progress') setProgress((p) => ({ ...p, [e.stage]: e.progress }))
  })

  const run = async (stage: StageName) => {
    try {
      const res = await api.runStage(stage)
      setMessage(res.ok ? null : res.message)
    } catch (err) {
      setMessage(cleanError(err))
    }
    reloadStatus()
  }
  const f = funnel.data

  return (
    <div className="grid gap-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold">Воронка</h1>
          <div className="mt-1 text-sm text-muted">Найти → проверить и оценить → вступить → читать все посты. Одна база Supabase на всех.</div>
        </div>
        <Button variant="ghost" onClick={() => { funnel.reload(); reloadStatus() }}>Обновить</Button>
      </div>

      {status?.warnings.map((w, i) => (
        <div key={i} className="flex items-start gap-2.5 rounded-lg border border-[#4d3d15] bg-[#2c2410] px-3.5 py-2.5 text-sm text-warn">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <div className="flex-1">{w}</div>
          {w.includes('Python-воркер') && (
            <Button size="sm" onClick={() => nav.go('settings')}>
              Настройки
            </Button>
          )}
        </div>
      ))}
      {status?.circle.cooldownUntil && (
        <div className="flex items-start gap-2.5 rounded-lg border border-[#4d2323] bg-[#2c1515] px-3.5 py-2.5 text-sm text-bad">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <div>
            Circle временно ограничил запросы с этого компьютера. Проверка, чтение и вступление стоят на паузе до{' '}
            {new Date(status.circle.cooldownUntil).toLocaleTimeString('ru-RU')}. Оценка ICP через LLM продолжает работать.
          </div>
        </div>
      )}
      {message && <ErrorNote>{message}</ErrorNote>}
      {funnel.error && <ErrorNote>{funnel.error}</ErrorNote>}

      <div className="grid grid-cols-4 gap-3">
        {(status?.stages ?? []).map((stage) => (
          <StageCard
            key={stage.stage}
            stage={{ ...stage, progress: progress[stage.stage] ?? stage.progress }}
            onRun={() => void run(stage.stage)}
            onStop={() => void api.stopStage(stage.stage).then(reloadStatus)}
          />
        ))}
        {!status && STAGES.map((s) => <div key={s} className="h-48 animate-pulse rounded-xl border border-line bg-panel" />)}
      </div>

      <Card title="Путь сообщества">
        <div className="grid grid-cols-[repeat(5,minmax(0,1fr))] items-stretch gap-2">
          <Step label="Найдено всего" value={f?.total} sub={f ? `${num(f.discoverUnresolved)} в каталоге без адреса` : undefined} />
          <Step label="Проверено" value={f?.probed} sub={f ? `ещё ${num(f.notProbed)} ждут` : undefined} />
          <Step label="Существуют (JSON 200)" value={f?.alive} sub={f ? `${num(f.lockedOrAbsent)} закрыты или нет` : undefined} />
          <Step label="Оценено ICP" value={f?.icpJudged} sub={f ? `${num(f.withText)} с текстом` : undefined} />
          <Step label="Подходят нам" value={f?.icpFit} tone="accent" />
        </div>
        <div className="my-3 flex justify-center text-muted">
          <ArrowRight size={16} className="rotate-90" />
        </div>
        <div className="grid grid-cols-[repeat(5,minmax(0,1fr))] gap-2">
          <Step label="В очереди на вступление" value={f?.joinable} />
          <Step label="Вступили" value={f?.joined} sub={f ? `визитов сегодня ${f.visitsToday}/${f.visitCap}` : undefined} />
          <Step label="Сессий для чтения" value={f?.withSession} />
          <Step label="Прочитано сообществ" value={f?.scrapedCommunities} />
          <Step label="Постов в базе" value={f?.postsTotal} sub={f ? `+${num(f.postsNew24h)} за сутки` : undefined} tone="accent" />
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-muted">
          <span>LLM сегодня: {f ? usd(f.llmSpendToday) : '—'}</span>
          <span>
            Запросов к Circle за час: {status ? `${status.circle.requestsLastHour} из ${status.circle.maxPerHour}` : '—'}
          </span>
          {f && f.openTasks > 0 && (
            <button className="cursor-default text-warn underline-offset-2 hover:underline" onClick={() => nav.go('joins')}>
              Нужно ваше участие: {f.openTasks}
            </button>
          )}
        </div>
      </Card>
    </div>
  )
}
