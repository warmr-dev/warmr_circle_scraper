import { useState } from 'react'
import { Check, ExternalLink, LogIn, X } from 'lucide-react'
import type { AppStatus } from '@shared/types'
import { api, cleanError, useLoad } from '../lib/api'
import { dateTime, hostOf, JOIN_STATUS_LABELS, JOIN_TYPE_LABELS, label, num } from '../lib/format'
import { Badge, Button, Card, Empty, ErrorNote, Progress } from '../components/ui'
import type { Nav } from '../App'

export function Joins({ nav, status }: { nav: Nav; status: AppStatus | null }) {
  const tasks = useLoad(() => api.listTasks('open'), [], 20_000)
  const queue = useLoad(() => api.listJoinQueue(40), [], 30_000)
  const attempts = useLoad(() => api.listJoinAttempts(60), [], 30_000)
  const funnel = useLoad(() => api.getFunnel(), [], 30_000)
  const [note, setNote] = useState<string | null>(null)
  const joinStage = status?.stages.find((s) => s.stage === 'join')

  const act = async (fn: () => Promise<{ ok: boolean; message: string } | void>) => {
    try {
      const r = await fn()
      if (r) setNote(r.message)
      tasks.reload()
      queue.reload()
      attempts.reload()
    } catch (err) {
      setNote(cleanError(err))
    }
  }

  return (
    <div className="grid gap-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold">Вступления</h1>
          <div className="mt-1 max-w-3xl text-sm text-muted">
            Бот вступает в ваш аккаунт Circle через ego lite (только Mac), не чаще лимита визитов в день. Платные и закрытые пропускает. Всё, где нужен человек (вход, двухфакторная проверка, анкета), попадает в список ниже. На Windows вступайте кнопкой «Вступить вручную»: куки сохранятся сами.
          </div>
        </div>
        <Button variant="primary" disabled={Boolean(joinStage?.running || joinStage?.blockedReason)} onClick={() => act(() => api.runStage('join'))}>
          <LogIn size={14} /> Вступить сейчас
        </Button>
      </div>
      {joinStage?.blockedReason && <ErrorNote>{joinStage.blockedReason}</ErrorNote>}
      {note && <div className="rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">{note}</div>}

      {funnel.data && (
        <Card>
          <div className="grid gap-2">
            <div className="flex justify-between text-sm">
              <span>Визитов сегодня</span>
              <span className="tabular">
                {funnel.data.visitsToday} / {funnel.data.visitCap}
              </span>
            </div>
            <Progress value={funnel.data.visitsToday} total={funnel.data.visitCap} />
            <div className="text-xs text-muted">
              Лимит защищает аккаунт: около 94 визитов за день уже вызывали запрос двухфакторной проверки. Каждый открытый бота сайт считается визитом, даже без вступления.
            </div>
          </div>
        </Card>
      )}

      <Card title={`Нужно ваше участие${tasks.data?.length ? ` · ${tasks.data.length}` : ''}`}>
        {tasks.error && <ErrorNote>{tasks.error}</ErrorNote>}
        {!tasks.data?.length ? (
          <Empty>Ничего не ждёт вас</Empty>
        ) : (
          <div className="grid gap-2">
            {tasks.data.map((t) => (
              <div key={t.id} className="flex items-start justify-between gap-3 rounded-lg border border-line bg-panel-2/50 px-3.5 py-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium">{t.title}</div>
                  <div className="mt-0.5 text-xs text-muted">
                    {t.name || t.host} · {dateTime(t.createdAt)}
                  </div>
                  {t.detail && <div className="mt-1.5 line-clamp-3 text-xs text-muted">{t.detail}</div>}
                </div>
                <div className="flex shrink-0 gap-1.5">
                  {t.url && (
                    <Button size="sm" variant="ghost" onClick={() => void api.openExternal(t.url!)} title="Открыть в обычном браузере">
                      <ExternalLink size={12} />
                    </Button>
                  )}
                  {t.communityId && (
                    <Button size="sm" onClick={() => act(() => api.assistedJoin(t.communityId!))}>
                      Вступить вручную
                    </Button>
                  )}
                  <Button size="sm" onClick={() => act(() => api.resolveTask(t.id, 'done'))} title="Сделано">
                    <Check size={12} />
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => act(() => api.resolveTask(t.id, 'dismiss'))} title="Скрыть">
                    <X size={12} />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="grid grid-cols-2 gap-6">
        <Card title={`Очередь${funnel.data ? ` · ${num(funnel.data.joinable)}` : ''}`}>
          {!queue.data?.length ? (
            <Empty>Очередь пуста: нет подходящих бесплатных сообществ, в которые ещё не вступали</Empty>
          ) : (
            <div className="grid gap-1">
              {queue.data.map((c) => (
                <button
                  key={c.id}
                  onClick={() => nav.go('communities', { communityId: c.id })}
                  className="flex w-full min-w-0 cursor-default items-center justify-between gap-3 rounded-md px-2 py-1.5 text-left hover:bg-panel-2"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm">{c.name || c.slug}</div>
                    <div className="truncate font-mono text-[11px] text-muted">{hostOf(c.url)}</div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <Badge>{label(JOIN_TYPE_LABELS, c.joinType)}</Badge>
                    <Badge tone="accent">{Math.round(c.icpScore ?? 0)}</Badge>
                  </div>
                </button>
              ))}
            </div>
          )}
        </Card>
        <Card title="История попыток">
          {!attempts.data?.length ? (
            <Empty>Попыток ещё не было</Empty>
          ) : (
            <div className="grid gap-1.5">
              {attempts.data.map((a) => (
                <div key={a.id} className="rounded-md px-2 py-1.5 hover:bg-panel-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="truncate text-sm">{a.name || a.host}</div>
                    <Badge tone={a.status === 'joined' ? 'ok' : a.terminal ? 'muted' : 'warn'}>{label(JOIN_STATUS_LABELS, a.status)}</Badge>
                  </div>
                  <div className="text-[11px] text-muted">
                    {dateTime(a.createdAt)} · {a.account}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
