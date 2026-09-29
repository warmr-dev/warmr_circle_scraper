// Section 6: the schedules and lead rules the worker reads (editable), and how
// the services are doing (read-only). A save changes only what the form shows
// and starts nothing.

import { api } from '../api.js';
import { $, ago, empty, html, num, pill, render, toast, when } from '../dom.js';
import { JOB_STATE, JOB_STATE_TONE, SCHEDULE, STAGE, label } from '../labels.js';

export const title = 'Конфиг';
export const subtitle = 'Расписания и правила лидов, которые читает воркер, и состояние сервисов.';

let ctx = null;

const NUMBER_FIELDS = [
  ['minimum_confidence', 'Минимальная уверенность модели (0–1)', 0.05],
  ['max_post_age_days', 'Не старше, дней (0 — без ограничения)', 1],
  ['llm_escalation_threshold', 'Порог правил, после которого спрашиваем модель (лиды)', 1],
  ['icp_escalation_threshold', 'Порог правил, после которого спрашиваем модель (ICP)', 1],
  ['max_search_niches', 'Сколько ниш искать за раз (0 — все)', 1],
  ['max_expanded_niches', 'Предел ниш после расширения моделью', 1],
];
const SCORING = [
  ['hiring_intent', 'Намерение нанять'],
  ['target_role_match', 'Совпала роль'],
  ['target_skill_match', 'Совпал навык'],
  ['budget_mentioned', 'Есть бюджет'],
  ['company_identified', 'Есть компания'],
  ['recent_post', 'Свежий пост'],
  ['recency_days', 'Свежий — это не старше, дней'],
];

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <div class="grid-2">
      <section class="panel" data-schedules>${empty('Загрузка…')}</section>
      <section class="panel" data-actions>
        <h2>Действия</h2>
        <p class="muted small">Ставят задачу в очередь воркера; он возьмёт её, как только освободится.</p>
        <div class="row wrap">
          <button class="btn" data-job="harvest">Запустить сбор сейчас</button>
          <button class="btn btn-ghost" data-job="scan_all">Прочитать все сессии</button>
        </div>
      </section>
    </div>
    <section class="panel" data-services>${empty('Загрузка…')}</section>
    <section class="panel" data-rules>${empty('Загрузка…')}</section>
    <section class="panel" data-jobs></section>`);
  ctx.body.addEventListener('click', async (e) => {
    const job = e.target.closest('[data-job]');
    if (!job) return;
    try {
      const res = await api('/api/dash/jobs', { method: 'POST', body: { kind: job.dataset.job } });
      toast(`Задача #${res.job_id} в очереди`);
      loadRuntime();
    } catch (err) { toast(err.message, 'error'); }
  });
}

export async function update() {
  await refresh();
}

export async function refresh() {
  await Promise.all([loadSchedules(), loadRules(), loadRuntime()]);
  ctx.setUpdated();
}

// --- schedules ---------------------------------------------------------------------

async function loadSchedules() {
  const data = await api('/api/dash/schedule');
  const host = $('[data-schedules]', ctx.body);
  render(host, html`
    <h2>Расписания</h2>
    <form class="stack" data-schedule-form>
      ${Object.entries(data).map(([key, s]) => html`
        <label class="field">${STAGE[key] || key}
          <select name="${key}">
            ${s.options.map((o) => html`<option value="${o}" ${o === s.value ? 'selected' : ''}>${label(SCHEDULE, o)}</option>`)}
            ${s.options.includes(s.value) ? '' : html`<option value="${s.value}" selected>${s.value}</option>`}
          </select>
        </label>`)}
      <div class="row"><button class="btn" type="submit">Сохранить расписания</button></div>
    </form>`);
  $('[data-schedule-form]', host).addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    const changed = Object.fromEntries(Object.entries(body).filter(([k, v]) => data[k]?.value !== v));
    if (!Object.keys(changed).length) { toast('Ничего не изменилось'); return; }
    try {
      await api('/api/dash/schedule', { method: 'POST', body: changed });
      toast('Расписания сохранены');
      await loadSchedules();
      await loadRuntime();
    } catch (err) { toast(err.message, 'error'); }
  });
}

// --- lead rules --------------------------------------------------------------------

const lines = (list) => (list || []).join('\n');
const parseLines = (text) => text.split('\n').map((x) => x.trim()).filter(Boolean);

async function loadRules() {
  const { config } = await api('/api/dash/config');
  const host = $('[data-rules]', ctx.body);
  render(host, html`
    <h2>Правила лидов</h2>
    <p class="muted small">Здесь только поля, которые воркер действительно читает. Сохранение меняет только их и ничего не запускает; воркер подхватит изменения в течение минуты.</p>
    <form class="stack" data-rules-form>
      <div class="grid-2">
        <label class="field">Целевые роли (по одной в строке)
          <textarea name="target_roles" rows="8">${lines(config.target_roles)}</textarea></label>
        <label class="field">Целевые навыки (по одному в строке)
          <textarea name="target_skills" rows="8">${lines(config.target_skills)}</textarea></label>
      </div>
      <label class="field">Слова-исключения: пост с ними не лид (по одному в строке)
        <textarea name="keywords.exclude" rows="5">${lines(config.keywords?.exclude)}</textarea></label>
      <div class="grid-3">
        ${NUMBER_FIELDS.map(([name, text, step]) => html`
          <label class="field">${text}<input type="number" name="${name}" step="${step}" value="${config[name]}"></label>`)}
        <label class="check"><input type="checkbox" name="expand_search_with_ai" ${config.expand_search_with_ai ? 'checked' : ''}> расширять поиск ниш моделью</label>
      </div>
      <h3>Оценка лида</h3>
      <div class="grid-4">
        ${SCORING.map(([name, text]) => html`
          <label class="field">${text}<input type="number" name="scoring.${name}" step="1" value="${config.scoring?.[name]}"></label>`)}
        <label class="field">Высокий приоритет от<input type="number" name="priority_thresholds.high" value="${config.priority_thresholds?.high}"></label>
        <label class="field">Средний приоритет от<input type="number" name="priority_thresholds.medium" value="${config.priority_thresholds?.medium}"></label>
      </div>
      <div class="row"><button class="btn" type="submit">Сохранить правила</button></div>
    </form>`);
  $('[data-rules-form]', host).addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const payload = {
      target_roles: parseLines(form.elements.target_roles.value),
      target_skills: parseLines(form.elements.target_skills.value),
      keywords: { exclude: parseLines(form.elements['keywords.exclude'].value) },
      expand_search_with_ai: form.elements.expand_search_with_ai.checked,
      scoring: {},
      priority_thresholds: {},
    };
    for (const [name] of NUMBER_FIELDS) payload[name] = Number(form.elements[name].value);
    for (const [name] of SCORING) payload.scoring[name] = Number(form.elements[`scoring.${name}`].value);
    for (const name of ['high', 'medium']) payload.priority_thresholds[name] = Number(form.elements[`priority_thresholds.${name}`].value);
    try {
      const res = await api('/api/dash/config', { method: 'POST', body: payload });
      toast(res.changed.length ? `Сохранено: ${res.changed.join(', ')}` : 'Ничего не изменилось');
    } catch (err) { toast(err.message, 'error'); }
  });
}

// --- services, stages, jobs ---------------------------------------------------------

function heartbeat(name, hb) {
  const tone = hb.state === 'ok' ? 'good' : 'critical';
  const text = hb.state === 'ok' ? `жив · ${ago(hb.at)}` : hb.state === 'never' ? 'ни разу не отчитывался' : `молчит · ${ago(hb.at)}`;
  return html`<div class="kpi ${hb.state === 'ok' ? '' : 'kpi-warn'}"><b>${name}</b><span>${pill(text, tone)}</span></div>`;
}

function snapshot(name, snap) {
  if (!snap) return html`<div class="card"><h3>${name}</h3>${empty('Сервис ещё не присылал снимок: работает старая версия или он не запущен.')}</div>`;
  const env = snap.env || {};
  const gov = snap.governor || {};
  const stage = snap.stage?.stage;
  return html`
    <div class="card">
      <h3>${name}</h3>
      <dl class="facts">
        <dt>Хост</dt><dd>${snap.host} · pid ${snap.pid}</dd>
        <dt>Версия кода</dt><dd>${snap.code_version}</dd>
        <dt>Запущен</dt><dd>${when(snap.started_at)} · снимок ${ago(snap.beat_at)}</dd>
        <dt>Сейчас</dt><dd>${stage ? html`${label(STAGE, stage)} <span class="muted small">с ${when(snap.stage.since)}</span>` : 'ждёт работы'}</dd>
        <dt>Модель</dt><dd>${snap.llm?.backend ? `${snap.llm.backend} · ${snap.llm.model || '—'}${snap.llm.base_host ? ` · ${snap.llm.base_host}` : ''}` : pill('нет модели', 'warning')}</dd>
        <dt>Лимит Circle</dt><dd>${gov.enabled === false ? 'выключен' : gov.limits
          ? html`${num(gov.usedLastHour)} запросов за час из ${num(gov.limits.maxPerHour)} · ${num(gov.limits.requestsPerMinute)}/мин${gov.cooldownUntil ? html` · ${pill('пауза', 'warning', gov.cooldownReason || '')}` : ''}`
          : '—'}</dd>
        <dt>Ключи</dt><dd class="keys">${Object.entries(env).map(([k, v]) => pill(k, v ? 'good' : 'muted', v ? 'задан' : 'не задан'))}</dd>
      </dl>
    </div>`;
}

function stageRow(key, s) {
  const err = s.last_error;
  const failedLast = err && (!s.last_finish || new Date(err.at) > new Date(s.last_finish));
  return html`
    <tr>
      <td>${STAGE[key] || key}</td>
      <td>${label(SCHEDULE, s.schedule)}</td>
      <td class="small">${s.last_run ? html`${when(s.last_run)}<div class="muted">${ago(s.last_run)}</div>` : '—'}</td>
      <td class="small">${s.last_finish ? html`${when(s.last_finish)}<div class="muted">${ago(s.last_finish)}</div>` : '—'}</td>
      <td class="small">${err ? html`${pill(failedLast ? 'упал' : 'была ошибка', failedLast ? 'critical' : 'muted')} <span class="muted">${when(err.at)}</span><div class="detail">${err.error}</div>` : '—'}</td>
      <td class="small">${s.last_result ? html`<code class="compact">${JSON.stringify(s.last_result)}</code>` : ''}${key === 'icp_classification' && s.enrichment_moved_at ? html`<div class="muted">обогащение: курсор ${s.enrichment_cursor}, сдвинулся ${ago(s.enrichment_moved_at)}</div>` : ''}</td>
    </tr>`;
}

async function loadRuntime() {
  const r = await api('/api/dash/runtime');
  render($('[data-services]', ctx.body), html`
    <h2>Сервисы</h2>
    <div class="kpis">
      ${heartbeat('Воркер', r.heartbeats.worker)}
      ${heartbeat('Наблюдатель', r.heartbeats.watcher)}
      <div class="kpi"><b>Дашборд</b><span class="muted small">${r.dashboard.commit || 'локально'}${r.dashboard.region ? ` · ${r.dashboard.region}` : ''}</span></div>
    </div>
    <div class="grid-2">${snapshot('Воркер', r.runtime.worker)}${snapshot('Наблюдатель', r.runtime.watcher)}</div>
    <h3>Этапы воркера</h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Этап</th><th>Расписание</th><th>Последний запуск</th><th>Закончился</th><th>Ошибка</th><th>Итог</th></tr></thead>
      <tbody>${Object.entries(r.stages).map(([k, s]) => stageRow(k, s))}</tbody>
    </table></div>`);
  render($('[data-jobs]', ctx.body), html`
    <h2>Последние задачи воркера</h2>
    ${r.jobs.length ? html`<div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>Задача</th><th>Состояние</th><th>Создана</th><th>Закончена</th><th>Итог</th></tr></thead>
      <tbody>${r.jobs.map((j) => html`<tr>
        <td class="small">${j.id}</td><td>${j.kind}${j.host ? html` <span class="muted small">${j.host}</span>` : ''}</td>
        <td>${pill(label(JOB_STATE, j.state), JOB_STATE_TONE[j.state] || 'neutral')}</td>
        <td class="small">${when(j.created_at)}</td><td class="small">${when(j.finished_at)}</td>
        <td class="small detail">${j.detail || ''}</td></tr>`)}</tbody>
    </table></div>` : empty('Задач не было.')}`);
}
