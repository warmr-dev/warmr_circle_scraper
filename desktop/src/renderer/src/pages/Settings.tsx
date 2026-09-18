import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Check, FileUp } from 'lucide-react'
import type { ActionResult, LlmProvider, SecretName, SettingsBundle, SharedSettings } from '@shared/types'
import { MODEL_CHOICES, DEFAULT_MODEL } from '@shared/models'
import { api, cleanError, useLoad } from '../lib/api'
import { Badge, Button, Card, ErrorNote, Field, Input, Select, Textarea, Toggle } from '../components/ui'

type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] }

function Result({ result }: { result: ActionResult | null }) {
  if (!result) return null
  return <div className={`text-sm ${result.ok ? 'text-ok' : 'text-bad'}`}>{result.message}</div>
}

function SecretInput({ name, label, set, placeholder, onSaved }: { name: SecretName; label: ReactNode; set: boolean; placeholder?: string; onSaved: (b: SettingsBundle) => void }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  return (
    <Field
      label={
        <span className="inline-flex items-center gap-2">
          {label}
          {set ? <Badge tone="ok">задано</Badge> : <Badge>не задано</Badge>}
        </span>
      }
    >
      <div className="flex gap-2">
        <Input type="password" value={value} placeholder={set ? '•••••••• (оставьте пустым, чтобы не менять)' : placeholder} onChange={(e) => setValue(e.target.value)} />
        <Button
          disabled={!value.trim() || busy}
          onClick={async () => {
            setBusy(true)
            try {
              onSaved(await api.setSecret(name, value))
              setValue('')
            } finally {
              setBusy(false)
            }
          }}
        >
          Сохранить
        </Button>
        {set && (
          <Button variant="ghost" onClick={async () => onSaved(await api.setSecret(name, ''))} title="Удалить">
            Очистить
          </Button>
        )}
      </div>
    </Field>
  )
}

function NumberField({ label, value, onChange, hint, min = 0, step = 1 }: { label: ReactNode; value: number; onChange: (v: number) => void; hint?: ReactNode; min?: number; step?: number }) {
  return (
    <Field label={label} hint={hint}>
      <Input type="number" min={min} step={step} value={Number.isFinite(value) ? value : 0} onChange={(e) => onChange(Number(e.target.value))} />
    </Field>
  )
}

export function Settings({ onChanged, dbConnected }: { onChanged: () => void; dbConnected: boolean }) {
  const loaded = useLoad(() => api.getSettings(), [])
  const oldWorker = useLoad(() => api.getOldWorker(), [])
  const [bundle, setBundle] = useState<SettingsBundle | null>(null)
  const [draft, setDraft] = useState<SharedSettings | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, ActionResult | null>>({})

  useEffect(() => {
    if (loaded.data) {
      setBundle(loaded.data)
      setDraft(loaded.data.shared)
    }
  }, [loaded.data])

  const dirty = useMemo(() => bundle && draft && JSON.stringify(bundle.shared) !== JSON.stringify(draft), [bundle, draft])
  const patch = <K extends keyof SharedSettings>(key: K, value: DeepPartial<SharedSettings[K]>) =>
    setDraft((d) => (d ? { ...d, [key]: typeof value === 'object' && value !== null ? { ...(d[key] as object), ...value } : value } : d))
  const run = async (key: string, fn: () => Promise<ActionResult>) => {
    setResults((r) => ({ ...r, [key]: { ok: true, message: 'Проверяю…' } }))
    try {
      const res = await fn()
      setResults((r) => ({ ...r, [key]: res }))
    } catch (err) {
      setResults((r) => ({ ...r, [key]: { ok: false, message: cleanError(err) } }))
    }
    onChanged()
  }
  const savedBundle = (b: SettingsBundle) => {
    setBundle(b)
    setDraft(b.shared)
    onChanged()
  }

  if (loaded.error && !bundle) return <ErrorNote>{loaded.error}</ErrorNote>
  if (!bundle || !draft) return <div className="text-muted">Загрузка…</div>
  const s = bundle.secrets
  const provider = draft.llm.provider
  const models = MODEL_CHOICES[provider]
  const customModel = !models.some((m) => m.id === draft.llm.model)
  const dbReady = s.dbUrl || dbConnected

  return (
    <div className="grid gap-6 pb-20">
      <div>
        <h1 className="text-xl font-semibold">Настройки</h1>
        <div className="mt-1 text-sm text-muted">
          Секреты (строка базы, ключи, пароль Circle) хранятся только на этом компьютере, в зашифрованном виде через системную связку ключей. Остальные настройки общие: они лежат в базе и одинаковы на всех ваших компьютерах.
        </div>
      </div>

      <Card
        title="База данных Supabase"
        actions={
          <Button size="sm" onClick={() => run('env', async () => { const r = await api.importEnvFile(); savedBundle(await api.getSettings()); return r })}>
            <FileUp size={12} /> Импорт из .env
          </Button>
        }
      >
        <div className="grid gap-4">
          <SecretInput name="dbUrl" label="Строка подключения (CIRCLE_LEADS_DB)" set={s.dbUrl} placeholder="postgresql://postgres.xxx:пароль@aws-0-….pooler.supabase.com:6543/postgres" onSaved={savedBundle} />
          <div className="flex items-center gap-3">
            <Button onClick={() => run('db', () => api.testDb())}>Проверить подключение</Button>
            <Result result={results.db ?? results.env ?? null} />
          </div>
          <div className="text-xs leading-relaxed text-muted">
            Используется тот же проект, что у старого дашборда. Приложение добавляет свои таблицы в отдельную схему warmr_app и не меняет существующие колонки.
          </div>
        </div>
      </Card>

      {dbReady && (
        <>
          <Card title="LLM для оценки сообществ">
            <div className="grid gap-4">
              <Toggle checked={draft.icp.useLlm} onChange={(v) => patch('icp', { useLlm: v })} label="Использовать LLM (без неё — только правила по ключевым словам)" />
              <div className="grid grid-cols-2 gap-4">
                <Field label="Провайдер">
                  <Select
                    value={provider}
                    onChange={(e) => {
                      const next = e.target.value as LlmProvider
                      patch('llm', { provider: next, model: DEFAULT_MODEL[next] })
                    }}
                  >
                    <option value="openai">OpenAI</option>
                    <option value="anthropic">Anthropic (Claude)</option>
                  </Select>
                </Field>
                <Field label="Модель" hint={provider === 'anthropic' ? 'По умолчанию Opus 5; дешевле — Sonnet 5 или Haiku 4.5. Решать вам.' : undefined}>
                  <Select value={customModel ? '__custom' : draft.llm.model} onChange={(e) => patch('llm', { model: e.target.value === '__custom' ? '' : e.target.value })}>
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                    <option value="__custom">другая…</option>
                  </Select>
                </Field>
              </div>
              {customModel && (
                <Field label="ID модели">
                  <Input value={draft.llm.model} onChange={(e) => patch('llm', { model: e.target.value })} placeholder="например gpt-4.1-nano" />
                </Field>
              )}
              {provider === 'openai' ? (
                <SecretInput name="openaiKey" label="Ключ OpenAI" set={s.openaiKey} placeholder="sk-…" onSaved={savedBundle} />
              ) : (
                <SecretInput name="anthropicKey" label="Ключ Anthropic" set={s.anthropicKey} placeholder="sk-ant-…" onSaved={savedBundle} />
              )}
              <div className="grid grid-cols-3 gap-4">
                <NumberField label="Бюджет в день, $" value={draft.llm.dailyBudgetUsd} step={0.5} onChange={(v) => patch('llm', { dailyBudgetUsd: v })} hint="После лимита оценка идёт по правилам до завтра" />
                <NumberField
                  label="Мин. уверенность LLM"
                  value={draft.icp.minConfidence}
                  step={0.05}
                  onChange={(v) => patch('icp', { minConfidence: v })}
                  hint="0–1. Ниже — «подходит» не ставится"
                />
                <Field label="Что отдавать LLM">
                  <Select value={draft.icp.mode} onChange={(e) => patch('icp', { mode: e.target.value as 'all' | 'ambiguous' })}>
                    <option value="all">все сообщества с текстом</option>
                    <option value="ambiguous">только спорные по правилам</option>
                  </Select>
                </Field>
              </div>
              <Toggle
                checked={draft.icp.autoApproveLlm}
                onChange={(v) => patch('icp', { autoApproveLlm: v })}
                label="Решение LLM «подходит» сразу ставит сообщество в очередь на вступление"
              />
              <Field label="Кто мы и что нам подходит (промпт для LLM)" hint="Изменение текста, модели или режима запускает переоценку всех сообществ.">
                <Textarea rows={8} value={draft.icp.profile} onChange={(e) => patch('icp', { profile: e.target.value })} />
              </Field>
              <div className="flex items-center gap-3">
                <Button onClick={() => run('llm', () => api.testLlm())} disabled={Boolean(dirty)} title={dirty ? 'Сначала сохраните изменения' : ''}>
                  Проверить LLM
                </Button>
                <Result result={results.llm ?? null} />
              </div>
            </div>
          </Card>

          <Card title="Аккаунт Circle и браузер ego lite">
            <div className="grid gap-4">
              <div className="grid grid-cols-2 gap-4">
                <SecretInput name="circleEmail" label="Email аккаунта Circle" set={s.circleEmail} onSaved={savedBundle} />
                <SecretInput name="circlePassword" label="Пароль Circle" set={s.circlePassword} onSaved={savedBundle} />
              </div>
              <div className="text-xs leading-relaxed text-muted">
                Нужны, чтобы бот мог войти на сообществах со своим доменом: вход на *.circle.so туда не переносится. Двухфакторную проверку и «вы человек?» бот не обходит, а создаёт задачу для вас.
              </div>
              <Field label="Путь к ego-browser (обычно определяется сам)" hint="macOS: ~/.local/bin/ego-browser. На Windows ego lite пока нет — вступайте кнопкой «Вступить вручную».">
                <Input
                  value={bundle.local.egoPath}
                  placeholder="~/.local/bin/ego-browser"
                  onChange={(e) => setBundle({ ...bundle, local: { ...bundle.local, egoPath: e.target.value } })}
                  onBlur={async () => savedBundle(await api.saveLocalSettings({ egoPath: bundle.local.egoPath }))}
                />
              </Field>
              <div className="flex items-center gap-3">
                <Button onClick={() => run('ego', () => api.testEgo())}>Проверить ego lite</Button>
                <Result result={results.ego ?? null} />
              </div>
              {bundle.local.egoSpaceId != null && (
                <div className="text-xs text-muted">
                  Рабочее пространство ego: #{bundle.local.egoSpaceId}.{' '}
                  <button className="cursor-default underline" onClick={async () => savedBundle(await api.saveLocalSettings({ egoSpaceId: null }))}>
                    Создать новое при следующем запуске
                  </button>
                </div>
              )}
            </div>
          </Card>

          <Card title="Расписание и лимиты">
            <div className="grid gap-6">
              <div className="grid gap-3">
                <Toggle checked={draft.discover.enabled} onChange={(v) => patch('discover', { enabled: v })} label={<b>Поиск</b>} />
                <div className="grid grid-cols-3 gap-4">
                  <NumberField label="Обход каталога, раз в N часов" value={draft.discover.directoryEveryHours} onChange={(v) => patch('discover', { directoryEveryHours: v })} />
                  <NumberField label="Карточек за прогон (поиск адреса)" value={draft.discover.resolveBatch} onChange={(v) => patch('discover', { resolveBatch: v })} />
                </div>
              </div>
              <div className="grid gap-3">
                <Toggle checked={draft.verify.enabled} onChange={(v) => patch('verify', { enabled: v })} label={<b>Проверка и ICP</b>} />
                <div className="grid grid-cols-3 gap-4">
                  <NumberField label="Пока есть очередь: раз в N минут" value={draft.verify.everyMinutes} onChange={(v) => patch('verify', { everyMinutes: v })} />
                  <NumberField label="Проверок за прогон" value={draft.verify.batchSize} onChange={(v) => patch('verify', { batchSize: v })} />
                  <NumberField label="Оценок ICP за прогон" value={draft.icp.batchSize} onChange={(v) => patch('icp', { batchSize: v })} />
                  <NumberField label="Параллельно (1–4)" value={draft.verify.concurrency} min={1} onChange={(v) => patch('verify', { concurrency: v })} hint="Больше 4 — Circle молча отдаёт пустые ответы" />
                  <NumberField label="Перепроверять живые, дней" value={draft.verify.reprobeAliveDays} onChange={(v) => patch('verify', { reprobeAliveDays: v })} />
                  <NumberField label="Перепроверять остальные, дней" value={draft.verify.reprobeOtherDays} onChange={(v) => patch('verify', { reprobeOtherDays: v })} />
                </div>
              </div>
              <div className="grid gap-3">
                <Toggle checked={draft.join.enabled} onChange={(v) => patch('join', { enabled: v })} label={<b>Автоджойн (работает с вашим настоящим аккаунтом Circle)</b>} />
                <div className="grid grid-cols-3 gap-4">
                  <NumberField label="Раз в N часов" value={draft.join.everyHours} onChange={(v) => patch('join', { everyHours: v })} />
                  <NumberField label="Визитов в день, максимум" value={draft.join.maxVisitsPerDay} onChange={(v) => patch('join', { maxVisitsPerDay: v })} hint="~94 в день уже вызывали 2FA" />
                  <NumberField label="Сообществ за прогон" value={draft.join.batchSize} onChange={(v) => patch('join', { batchSize: v })} />
                  <NumberField label="Пауза между, сек" value={draft.join.minDelaySec} onChange={(v) => patch('join', { minDelaySec: v })} />
                  <NumberField label="+ случайно до, сек" value={draft.join.jitterSec} onChange={(v) => patch('join', { jitterSec: v })} />
                </div>
                <Toggle checked={draft.join.includePaid} onChange={(v) => patch('join', { includePaid: v })} label="Пробовать и «платные»: у части есть бесплатный уровень, на оплате бот останавливается" />
              </div>
              <div className="grid gap-3">
                <Toggle checked={draft.scrape.enabled} onChange={(v) => patch('scrape', { enabled: v })} label={<b>Чтение постов</b>} />
                <div className="grid grid-cols-3 gap-4">
                  <NumberField label="Раз в N часов" value={draft.scrape.everyHours} onChange={(v) => patch('scrape', { everyHours: v })} />
                  <NumberField label="Запросов за прогон" value={draft.scrape.maxRequestsPerRun} onChange={(v) => patch('scrape', { maxRequestsPerRun: v })} />
                  <NumberField label="Не старше, дней (0 — все)" value={draft.scrape.maxPostAgeDays} onChange={(v) => patch('scrape', { maxPostAgeDays: v })} />
                </div>
                <Toggle checked={draft.scrape.withComments} onChange={(v) => patch('scrape', { withComments: v })} label="Читать комментарии и ответы" />
                <Toggle checked={draft.scrape.includePublic} onChange={(v) => patch('scrape', { includePublic: v })} label="Читать публичные разделы подходящих сообществ без вступления" />
              </div>
              <div className="grid gap-3">
                <div className="text-sm font-semibold">Скорость запросов к Circle (общая для всех этапов)</div>
                <div className="text-xs leading-relaxed text-muted">
                  Circle через Cloudflare ограничивает запросы с одного IP сразу по всем сообществам. 18.09 около 550 запросов за 10 минут закрыли доступ с этого компьютера ко всем сообществам (ответ 429). После первого же отказа приложение само ставит паузу на все запросы к Circle.
                </div>
                <div className="grid grid-cols-3 gap-4">
                  <NumberField label="Запросов в минуту" value={draft.network.requestsPerMinute} min={1} onChange={(v) => patch('network', { requestsPerMinute: v })} />
                  <NumberField label="Запросов в час, максимум" value={draft.network.maxPerHour} onChange={(v) => patch('network', { maxPerHour: v })} />
                  <NumberField label="Пауза после отказа, минут" value={draft.network.cooldownMinutes} min={1} onChange={(v) => patch('network', { cooldownMinutes: v })} />
                </div>
              </div>
            </div>
          </Card>

          <Card title="Старый Python-воркер на Railway">
            <div className="grid gap-3 text-sm">
              {oldWorker.data ? (
                <>
                  <div className="text-muted">
                    Расписание: чтение и поиск — {oldWorker.data.harvest}, веб-поиск — {oldWorker.data.search}, переоценка ICP — {oldWorker.data.icp}.
                  </div>
                  {oldWorker.data.active ? (
                    <div className="flex items-center gap-3">
                      <Button
                        variant="danger"
                        onClick={async () => {
                          if (!window.confirm('Выключить расписание старого воркера? Он перестанет сам читать сообщества и переоценивать ICP. Вернуть можно в старом дашборде.')) return
                          await api.pauseOldWorker()
                          oldWorker.reload()
                          onChanged()
                        }}
                      >
                        Выключить его расписание
                      </Button>
                      <span className="text-xs text-muted">Иначе он параллельно переоценивает ICP и читает те же сообщества.</span>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 text-ok">
                      <Check size={14} /> выключено — всё делает это приложение
                    </div>
                  )}
                </>
              ) : (
                <div className="text-muted">{oldWorker.error ?? 'Загрузка…'}</div>
              )}
            </div>
          </Card>

          <Card title="Этот компьютер">
            <div className="grid gap-4">
              <Field label="Название (видно в журнале запусков)">
                <Input
                  value={bundle.local.deviceName}
                  onChange={(e) => setBundle({ ...bundle, local: { ...bundle.local, deviceName: e.target.value } })}
                  onBlur={async () => savedBundle(await api.saveLocalSettings({ deviceName: bundle.local.deviceName }))}
                />
              </Field>
              <Toggle
                checked={bundle.local.launchAtLogin}
                onChange={async (v) => savedBundle(await api.saveLocalSettings({ launchAtLogin: v }))}
                label="Запускать при входе в систему (свёрнутым в трей)"
              />
            </div>
          </Card>
        </>
      )}

      {dirty && (
        <div className="fixed inset-x-0 bottom-0 z-30 flex items-center justify-end gap-3 border-t border-line bg-panel/95 px-8 py-3 backdrop-blur">
          {saveError && <span className="text-sm text-bad">{saveError}</span>}
          <span className="text-sm text-muted">Есть несохранённые изменения</span>
          <Button variant="ghost" onClick={() => setDraft(bundle.shared)}>
            Отменить
          </Button>
          <Button
            variant="primary"
            disabled={saving}
            onClick={async () => {
              setSaving(true)
              setSaveError(null)
              try {
                savedBundle(await api.saveSharedSettings(draft))
              } catch (err) {
                setSaveError(cleanError(err))
              } finally {
                setSaving(false)
              }
            }}
          >
            Сохранить
          </Button>
        </div>
      )}
    </div>
  )
}
