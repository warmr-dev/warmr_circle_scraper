import { useEffect, useState } from 'react'
import { ExternalLink } from 'lucide-react'
import { api, useLoad } from '../lib/api'
import { dateTime, num } from '../lib/format'
import { Badge, Button, Empty, ErrorNote, Input } from '../components/ui'
import type { Nav } from '../App'

const PAGE = 50

export function Posts({ nav }: { nav: Nav }) {
  const [communityId, setCommunityId] = useState<number | undefined>(nav.params.communityId)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const list = useLoad(() => api.listPosts({ communityId, search: query, offset, limit: PAGE }), [communityId, query, offset])

  useEffect(() => {
    const t = setTimeout(() => {
      setQuery(search)
      setOffset(0)
    }, 350)
    return () => clearTimeout(t)
  }, [search])

  const rows = list.data?.rows ?? []
  const total = list.data?.total ?? 0

  return (
    <div className="grid gap-6">
      <div>
        <h1 className="text-xl font-semibold">Посты</h1>
        <div className="mt-1 text-sm text-muted">Всё, что прочитано: посты и комментарии с исходными ID Circle. Email и телефоны вырезаются при сохранении.</div>
      </div>
      <div className="flex items-center gap-2">
        <Input className="w-80" placeholder="Поиск по тексту" value={search} onChange={(e) => setSearch(e.target.value)} />
        {communityId && (
          <Button size="sm" onClick={() => setCommunityId(undefined)}>
            Сбросить фильтр сообщества
          </Button>
        )}
        <div className="ml-auto text-sm text-muted">{num(total)} шт.</div>
      </div>
      {list.error && <ErrorNote>{list.error}</ErrorNote>}
      {!rows.length && !list.loading ? (
        <Empty>Постов нет</Empty>
      ) : (
        <div className="grid gap-3">
          {rows.map((p) => (
            <article key={p.id} className="rounded-xl border border-line bg-panel p-4">
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted">
                <button className="cursor-default font-medium text-text hover:underline" onClick={() => setCommunityId(p.communityId)}>
                  {p.communityName || p.host}
                </button>
                {p.spaceName && <span>· {p.spaceName}</span>}
                <span>· {p.authorName || 'автор неизвестен'}</span>
                <span>· {dateTime(p.publishedAt)}</span>
                {p.contentType === 'comment' && <Badge>комментарий</Badge>}
                {p.url && (
                  <button className="ml-auto cursor-default text-muted hover:text-text" onClick={() => void api.openExternal(p.url!)} title="Открыть в браузере">
                    <ExternalLink size={13} />
                  </button>
                )}
              </div>
              {p.title && <div className="mb-1 font-medium">{p.title}</div>}
              <div className="whitespace-pre-wrap text-sm leading-relaxed text-text/85">
                {p.title && p.content.startsWith(p.title) ? p.content.slice(p.title.length).trim() : p.content}
              </div>
            </article>
          ))}
        </div>
      )}
      {total > PAGE && (
        <div className="flex items-center justify-center gap-3">
          <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            Назад
          </Button>
          <span className="text-sm text-muted">
            {num(offset + 1)}–{num(Math.min(offset + PAGE, total))} из {num(total)}
          </span>
          <Button size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
            Дальше
          </Button>
        </div>
      )}
    </div>
  )
}
