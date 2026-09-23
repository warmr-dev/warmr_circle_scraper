import { useEffect, useState } from 'react'
import { ExternalLink, Plus, RefreshCw } from 'lucide-react'
import type { CommunityDetail, CommunityFilter, CommunityRow } from '@shared/types'
import { api, cleanError, useLoad } from '../lib/api'
import {
  ago,
  dateTime,
  EXISTS_LABELS,
  hostOf,
  JOIN_STATUS_LABELS,
  JOIN_TYPE_LABELS,
  label,
  num,
  PLATFORM_LABELS
} from '../lib/format'
import { Badge, Button, Card, Drawer, Empty, ErrorNote, Input, KeyValue, Select, Textarea } from '../components/ui'
import type { Nav } from '../App'

const PAGE = 100

function existsTone(v: string | null): 'ok' | 'warn' | 'bad' | 'muted' {
  if (v === 'alive') return 'ok'
  if (v === 'locked_or_absent') return 'warn'
  if (v === 'unreachable' || v === 'not_circle') return 'bad'
  return 'muted'
}

function IcpCell({ row }: { row: CommunityRow }) {
  if (row.icpFlag) return <Badge tone="accent">подходит · {Math.round(row.icpScore ?? 0)}</Badge>
  if (row.icpDecidedBy) return <span className="text-xs text-muted">нет · {Math.round(row.icpScore ?? 0)}</span>
  return <span className="text-xs text-muted">—</span>
}

function ImportBox({ onDone }: { onDone: () => void }) {
  const [text, setText] = useState('')
  const [result, setResult] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  return (
    <Card title="Добавить сообщества списком">
      <div className="grid gap-3">
        <Textarea
          rows={4}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={'Адреса или ссылки, по одному в строке или через запятую:\nfounders.circle.so\nhttps://community.example.com/c/general'}
        />
        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            disabled={!text.trim() || busy}
            onClick={async () => {
              setBusy(true)
              try {
                const r = await api.importCommunities(text)
                setResult(`Добавлено ${r.added}, уже были ${r.existing}, не распознано или не Circle ${r.invalid}. Новые проверит этап «Проверка».`)
                setText('')
                onDone()
              } catch (err) {
                setResult(cleanError(err))
              } finally {
                setBusy(false)
              }
            }}
          >
            <Plus size={14} /> Добавить
          </Button>
          {result && <div className="text-sm text-muted">{result}</div>}
        </div>
      </div>
    </Card>
  )
}

function Detail({ id, onChanged, nav }: { id: number; onChanged: () => void; nav: Nav }) {
  const detail = useLoad(() => api.getCommunity(id), [id])
  const [note, setNote] = useState<string | null>(null)
  const d: CommunityDetail | null = detail.data
  const act = async (fn: () => Promise<{ ok: boolean; message: string } | void>) => {
    try {
      const r = await fn()
      if (r) setNote(r.message)
      detail.reload()
      onChanged()
    } catch (err) {
      setNote(cleanError(err))
    }
  }
  if (!d) return detail.error ? <ErrorNote>{detail.error}</ErrorNote> : <div className="text-muted">Загрузка…</div>
  const host = hostOf(d.url)
  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => act(() => api.reverifyCommunity(d.id))}>
          <RefreshCw size={12} /> Перепроверить
        </Button>
        {d.icpFlag ? (
          <Button size="sm" onClick={() => act(() => api.setCommunityFit(d.id, false))}>Не подходит</Button>
        ) : (
          <Button size="sm" onClick={() => act(() => api.setCommunityFit(d.id, true))}>Подходит</Button>
        )}
        <Button size="sm" onClick={() => act(() => api.joinOne(d.id))} title="Через ego lite (только Mac)">
          Вступить (ego)
        </Button>
        <Button size="sm" onClick={() => act(() => api.assistedJoin(d.id))} title="Окно приложения: вы вступаете сами, куки сохраняются автоматически">
          Вступить вручную
        </Button>
        <Button size="sm" onClick={() => act(() => api.scrapeOne(d.id))} disabled={!d.hasSession && !(d.spacesPublic && d.icpFlag)}>
          Прочитать посты
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void api.openExternal(d.url)}>
          <ExternalLink size={12} /> Открыть
        </Button>
        {(d.postsStored ?? 0) > 0 && (
          <Button size="sm" variant="ghost" onClick={() => nav.go('posts', { communityId: d.id })}>
            Посты ({num(d.postsStored)})
          </Button>
        )}
      </div>
      {note && <div className="rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">{note}</div>}

      <KeyValue
        items={[
          ['Адрес', <span className="font-mono text-xs">{host}</span>],
          ['Платформа', label(PLATFORM_LABELS, d.platform)],
          ['Существует', <Badge tone={existsTone(d.existsStatus)}>{label(EXISTS_LABELS, d.existsStatus, 'не проверено')}</Badge>],
          ['Проверка', <span className="text-xs text-muted">{d.probeDetail || '—'} · {ago(d.probedAt)}</span>],
          ['Как вступить', `${label(JOIN_TYPE_LABELS, d.joinType)}${d.priceLabel ? ` · ${d.priceLabel}` : ''}`],
          ['Разделы (публичные)', d.spacesPublic == null ? '—' : num(d.spacesPublic)],
          ['Участников', d.membersTotal ? `≈ ${num(d.membersTotal)}` : '—'],
          ['Источник', d.discoverySource || '—']
        ]}
      />

      <div className="grid gap-2">
        <div className="text-sm font-semibold">Соответствие (ICP)</div>
        <div className="flex flex-wrap items-center gap-2">
          {d.icpFlag ? <Badge tone="accent">подходит</Badge> : <Badge>не подходит</Badge>}
          <span className="text-sm text-muted">
            оценка {Math.round(d.icpScore ?? 0)} · решил: {d.icpDecidedBy === 'llm' ? 'LLM' : d.icpDecidedBy === 'human' ? 'вы' : d.icpDecidedBy === 'rules' ? 'правила' : '—'}
            {d.icpLlmConfidence != null ? ` · уверенность ${Math.round(d.icpLlmConfidence * 100)}%` : ''}
          </span>
        </div>
        {d.icpLlmReason && <div className="rounded-md bg-panel-2 px-3 py-2 text-sm">{d.icpLlmReason}</div>}
        {d.icpReasons.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {d.icpReasons.map((r, i) => (
              <Badge key={i}>{r.replace(/^llm:/, '')}</Badge>
            ))}
          </div>
        )}
        {d.description && <div className="text-sm leading-relaxed text-muted">{d.description}</div>}
        {d.spaceNames.length > 0 && (
          <div className="text-xs leading-relaxed text-muted">Разделы: {d.spaceNames.join(' · ')}</div>
        )}
      </div>

      <div className="grid gap-2">
        <div className="text-sm font-semibold">Вступление и чтение</div>
        <KeyValue
          items={[
            ['Статус', label(JOIN_STATUS_LABELS, d.joinStatus, 'не пробовали')],
            ['Подробности', <span className="text-xs text-muted">{d.joinStatusDetail || '—'}</span>],
            ['Вступили', dateTime(d.joinedAt)],
            ['Сессия', d.hasSession ? `есть · ${d.connectionState ?? ''}` : 'нет'],
            ['Чтение', d.lastScrapedAt ? `${ago(d.lastScrapedAt)} · постов сохранено ${num(d.postsStored)}` : '—']
          ]}
        />
        {d.attempts.length > 0 && (
          <div className="grid gap-1.5">
            {d.attempts.map((a) => (
              <div key={a.id} className="rounded-md bg-panel-2 px-3 py-2 text-xs">
                <span className="text-text">{label(JOIN_STATUS_LABELS, a.status)}</span> · {dateTime(a.createdAt)}
                <div className="mt-0.5 text-muted">{a.detail}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export function Communities({ nav }: { nav: Nav }) {
  const [filter, setFilter] = useState<CommunityFilter>({ icp: 'all', sort: 'icp', offset: 0, limit: PAGE })
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<number | null>(nav.params.communityId ?? null)
  const list = useLoad(() => api.listCommunities(filter), [JSON.stringify(filter)])

  useEffect(() => {
    const t = setTimeout(() => setFilter((f) => ({ ...f, search, offset: 0 })), 300)
    return () => clearTimeout(t)
  }, [search])

  const set = (patch: Partial<CommunityFilter>) => setFilter((f) => ({ ...f, ...patch, offset: 0 }))
  const rows = list.data?.rows ?? []
  const total = list.data?.total ?? 0
  const offset = filter.offset ?? 0

  return (
    <div className="grid gap-6">
      <div>
        <h1 className="text-xl font-semibold">Сообщества</h1>
        <div className="mt-1 text-sm text-muted">Всё, что есть в базе. Нажмите на строку, чтобы увидеть проверку, оценку и действия.</div>
      </div>
      <ImportBox onDone={list.reload} />
      <div className="flex flex-wrap items-center gap-2">
        <Input className="w-64" placeholder="Поиск по названию или адресу" value={search} onChange={(e) => setSearch(e.target.value)} />
        <Select className="w-48" value={filter.icp} onChange={(e) => set({ icp: e.target.value as CommunityFilter['icp'] })}>
          <option value="all">ICP: все</option>
          <option value="fit">подходят</option>
          <option value="notfit">не подходят</option>
          <option value="unjudged">не оценены</option>
        </Select>
        <Select className="w-52" value={filter.exists ?? ''} onChange={(e) => set({ exists: e.target.value || undefined })}>
          <option value="">Существование: все</option>
          <option value="alive">существуют</option>
          <option value="locked_or_absent">закрыты или нет</option>
          <option value="unreachable">не отвечают</option>
          <option value="not_circle">не Circle</option>
          <option value="unprobed">не проверены</option>
        </Select>
        <Select className="w-48" value={filter.joinStatus ?? ''} onChange={(e) => set({ joinStatus: e.target.value || undefined })}>
          <option value="">Вступление: все</option>
          <option value="not_attempted">не пробовали</option>
          <option value="joined">вступили</option>
          <option value="paid_skip">платные</option>
          <option value="pending_approval">ждут одобрения</option>
          <option value="invite_skip">по приглашению</option>
        </Select>
        <Select className="w-44" value={filter.sort} onChange={(e) => set({ sort: e.target.value as CommunityFilter['sort'] })}>
          <option value="icp">сначала подходящие</option>
          <option value="posts">больше постов</option>
          <option value="recent">новые</option>
          <option value="name">по названию</option>
        </Select>
        <div className="ml-auto text-sm text-muted">{num(total)} шт.</div>
      </div>
      {list.error && <ErrorNote>{list.error}</ErrorNote>}
      <div className="overflow-hidden rounded-xl border border-line">
        <table className="w-full table-fixed text-sm">
          <thead className="bg-panel text-left text-xs text-muted">
            <tr>
              <th className="w-[34%] px-4 py-2.5 font-medium">Сообщество</th>
              <th className="px-3 py-2.5 font-medium">Существует</th>
              <th className="px-3 py-2.5 font-medium">Вступить</th>
              <th className="px-3 py-2.5 font-medium">ICP</th>
              <th className="px-3 py-2.5 font-medium">Статус</th>
              <th className="w-20 px-3 py-2.5 text-right font-medium">Посты</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} onClick={() => setSelected(row.id)} className="border-t border-line hover:bg-panel-2/60">
                <td className="px-4 py-2.5">
                  <div className="truncate font-medium">{row.name || row.slug}</div>
                  <div className="truncate font-mono text-[11px] text-muted">{hostOf(row.url)}</div>
                </td>
                <td className="px-3 py-2.5">
                  <Badge tone={existsTone(row.existsStatus)}>{label(EXISTS_LABELS, row.existsStatus, 'не проверено')}</Badge>
                </td>
                <td className="px-3 py-2.5 text-xs text-muted">{label(JOIN_TYPE_LABELS, row.joinType)}</td>
                <td className="px-3 py-2.5">
                  <IcpCell row={row} />
                </td>
                <td className="px-3 py-2.5 text-xs">
                  {row.joinStatus === 'joined' ? <Badge tone="ok">вступили</Badge> : <span className="text-muted">{label(JOIN_STATUS_LABELS, row.joinStatus, '—')}</span>}
                  {row.hasSession && <span className="ml-1.5 text-[11px] text-ok">сессия</span>}
                </td>
                <td className="tabular px-3 py-2.5 text-right text-xs">{row.postsStored ? num(row.postsStored) : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && !list.loading && (
          <div className="p-4">
            <Empty>Ничего не найдено</Empty>
          </div>
        )}
      </div>
      {total > PAGE && (
        <div className="flex items-center justify-center gap-3">
          <Button size="sm" disabled={offset === 0} onClick={() => setFilter((f) => ({ ...f, offset: Math.max(0, offset - PAGE) }))}>
            Назад
          </Button>
          <span className="text-sm text-muted">
            {num(offset + 1)}–{num(Math.min(offset + PAGE, total))} из {num(total)}
          </span>
          <Button size="sm" disabled={offset + PAGE >= total} onClick={() => setFilter((f) => ({ ...f, offset: offset + PAGE }))}>
            Дальше
          </Button>
        </div>
      )}
      <Drawer
        open={selected != null}
        onClose={() => setSelected(null)}
        title={rows.find((r) => r.id === selected)?.name || rows.find((r) => r.id === selected)?.slug || 'Сообщество'}
      >
        {selected != null && <Detail id={selected} onChanged={list.reload} nav={nav} />}
      </Drawer>
    </div>
  )
}
