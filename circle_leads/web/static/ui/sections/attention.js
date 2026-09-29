// Section 5: what needs a person (rules on the database, each item with what
// to do), and the activity log. An item can be marked as seen; it comes back
// by itself when its state changes.

import { api } from '../api.js';
import { setBadge } from '../badge.js';
import { $, ago, debounce, empty, html, num, pill, render, toast, when } from '../dom.js';
import { KIND, LEVEL, LEVEL_TONE, SEVERITY, SEVERITY_TONE, label } from '../labels.js';
import { follow, navigate, toObject } from '../router.js';

export const title = 'Требует внимания';
export const subtitle = 'Что проверить руками, и журнал всего, что делала система.';
export const autoRefresh = true;

let ctx = null;
let state = {};
let rules = [];
let logRows = [];
let nextBefore = null;

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <div class="tabs">
      <button class="tab" data-tab="">Требует внимания</button>
      <button class="tab" data-tab="log">Журнал</button>
    </div>
    <div data-pane></div>`);
  ctx.body.addEventListener('click', onClick);
  ctx.body.addEventListener('change', (e) => {
    if (e.target.matches('[data-show-acked]')) draw();
    if (e.target.matches('[data-lf]:not([type=search])')) pushLog();
  });
  const typed = debounce(() => pushLog(), 300);
  ctx.body.addEventListener('input', (e) => { if (e.target.matches('[data-lf][type=search]')) typed(); });
}

function pushLog() {
  const next = { tab: 'log' };
  for (const el of ctx.body.querySelectorAll('[data-lf]')) if (el.value.trim()) next[el.dataset.lf] = el.value.trim();
  navigate('attention', next, { replace: true });
}

async function onClick(e) {
  const tab = e.target.closest('[data-tab]');
  if (tab) { navigate('attention', tab.dataset.tab ? { tab: tab.dataset.tab } : {}); return; }
  const go = e.target.closest('[data-follow]');
  if (go) { follow(JSON.parse(go.dataset.follow)); return; }
  const ack = e.target.closest('[data-ack]');
  if (ack) {
    const [rule, key] = [ack.dataset.rule, ack.dataset.key];
    const note = ack.dataset.ack === 'on' ? '' : null;
    try {
      if (ack.dataset.ack === 'on') {
        await api('/api/dash/attention/ack', { method: 'POST', body: { rule, item_key: key, fingerprint: ack.dataset.fp, note } });
      } else {
        await api('/api/dash/attention/unack', { method: 'POST', body: { rule, item_key: key } });
      }
      await loadAttention();
    } catch (err) { toast(err.message, 'error'); }
    return;
  }
  const more = e.target.closest('[data-more]');
  if (more) { await loadLog(true); return; }
  const row = e.target.closest('[data-log-row]');
  if (row) row.classList.toggle('open');
}

export async function update(params) {
  state = toObject(params);
  for (const b of ctx.body.querySelectorAll('.tabs [data-tab]')) {
    b.setAttribute('aria-selected', (b.dataset.tab || '') === (state.tab || '') ? 'true' : 'false');
  }
  if (state.tab === 'log') await loadLog(false);
  else await loadAttention();
}

export async function refresh() {
  if (state.tab === 'log') await loadLog(false);
  else await loadAttention();
}

// --- attention --------------------------------------------------------------------

async function loadAttention() {
  const data = await api('/api/dash/attention');
  rules = data.rules;
  setBadge(data);
  ctx.setUpdated(data.evaluated_at);
  draw(data);
}

function itemView(rule, item) {
  const link = item.link ? html`<button class="btn btn-small" data-follow="${JSON.stringify(item.link)}">Открыть</button>` : '';
  const ack = item.acked
    ? html`<button class="btn btn-small btn-ghost" data-ack="off" data-rule="${rule.key}" data-key="${item.key}">Вернуть</button>`
    : html`<button class="btn btn-small btn-ghost" data-ack="on" data-rule="${rule.key}" data-key="${item.key}" data-fp="${item.fp}" title="Скрыть, пока состояние не изменится">Проверено</button>`;
  return html`
    <li class="item${item.acked ? ' acked' : ''}">
      <div class="item-main">
        <div class="strong">${item.title}</div>
        ${item.detail ? html`<div class="muted small detail" title="${item.detail}">${item.detail}</div>` : ''}
        <div class="muted small">${item.at ? `${when(item.at)} · ${ago(item.at)}` : ''}${item.acked ? ` · отмечено ${ago(item.acked_at)}` : ''}</div>
      </div>
      <div class="item-actions">${link}${ack}</div>
    </li>`;
}

function draw(data) {
  if (state.tab === 'log') return;
  const showAcked = $('[data-show-acked]', ctx.body)?.checked || false;
  const bySeverity = data?.by_severity || rules.reduce((acc, r) => {
    acc[r.severity] = (acc[r.severity] || 0) + r.open; return acc;
  }, {});
  const visible = rules.filter((r) => r.error || r.items.some((i) => showAcked || !i.acked));
  const acked = rules.reduce((n, r) => n + (r.count - r.open), 0);
  render($('[data-pane]', ctx.body), html`
    <div class="toolbar">
      ${['crit', 'warn', 'info'].map((s) => pill(`${SEVERITY[s]}: ${num(bySeverity[s] || 0)}`, bySeverity[s] ? SEVERITY_TONE[s] : 'muted'))}
      <label class="check"><input type="checkbox" data-show-acked ${showAcked ? 'checked' : ''}> показать отмеченные (${num(acked)})</label>
    </div>
    ${visible.length ? visible.map((r) => html`
      <section class="panel rule rule-${r.severity}">
        <header class="rule-head">
          ${pill(SEVERITY[r.severity], SEVERITY_TONE[r.severity])}
          <h2>${r.title}</h2>
          <span class="muted">${num(r.open)}${r.count !== r.open ? ` из ${num(r.count)}` : ''}</span>
        </header>
        <p class="muted small">${r.todo}</p>
        ${r.error ? html`<p class="error small">Правило не сработало: ${r.error}</p>` : ''}
        <ul class="items">${r.items.filter((i) => showAcked || !i.acked).map((i) => itemView(r, i))}</ul>
        ${r.count > r.items.length ? html`<p class="muted small">Показано ${num(r.items.length)} из ${num(r.count)}.</p>` : ''}
      </section>`) : empty('Всё в порядке: проверять нечего.')}`);
  const box = $('[data-show-acked]', ctx.body);
  if (box) box.checked = showAcked;
}

// --- log -----------------------------------------------------------------------------

function logFilters() {
  return html`
    <div class="toolbar">
      <input type="search" data-lf="q" placeholder="Поиск: сообщество или текст" value="${state.q || ''}">
      <select data-lf="kind">
        <option value="">все виды</option>
        ${Object.entries(KIND).map(([k, t]) => html`<option value="${k}" ${state.kind === k ? 'selected' : ''}>${t}</option>`)}
      </select>
      <select data-lf="level">
        <option value="">любой уровень</option>
        <option value="warning,error" ${state.level === 'warning,error' ? 'selected' : ''}>только проблемы</option>
        ${Object.entries(LEVEL).map(([k, t]) => html`<option value="${k}" ${state.level === k ? 'selected' : ''}>${t}</option>`)}
      </select>
      <select data-lf="hours">
        <option value="">за всё время</option>
        ${[['1', 'за час'], ['24', 'за сутки'], ['168', 'за неделю'], ['720', 'за месяц']].map(([v, t]) => html`
          <option value="${v}" ${state.hours === v ? 'selected' : ''}>${t}</option>`)}
      </select>
    </div>`;
}

function logRow(r) {
  const detail = r.detail && Object.keys(r.detail).length ? JSON.stringify(r.detail, null, 2) : '';
  return html`
    <tr data-log-row class="${detail ? 'row-link' : ''}">
      <td class="small nowrap">${when(r.created_at)}</td>
      <td class="small">${label(KIND, r.kind)}</td>
      <td>${pill(label(LEVEL, r.level), LEVEL_TONE[r.level] || 'neutral')}</td>
      <td class="small">${r.community || ''}</td>
      <td>${r.summary}${detail ? html`<pre class="json log-detail">${detail}</pre>` : ''}</td>
    </tr>`;
}

async function loadLog(more) {
  const params = { kind: state.kind, level: state.level, q: state.q, hours: state.hours, limit: 100 };
  if (more && nextBefore) params.before_id = nextBefore;
  const data = await api('/api/dash/activity', { params });
  logRows = more ? logRows.concat(data.rows) : data.rows;
  nextBefore = data.next_before_id;
  ctx.setUpdated();
  const pane = $('[data-pane]', ctx.body);
  const focused = document.activeElement?.dataset?.lf;
  render(pane, html`
    ${logFilters()}
    ${logRows.length ? html`
      <div class="table-wrap"><table class="table log">
        <thead><tr><th>Когда</th><th>Вид</th><th>Уровень</th><th>Сообщество</th><th>Что</th></tr></thead>
        <tbody>${logRows.map(logRow)}</tbody>
      </table></div>
      ${nextBefore ? html`<div class="pager"><button class="btn" data-more>Показать ещё</button></div>` : ''}`
      : empty('Записей нет.')}`);
  if (focused) {
    const el = $(`[data-lf="${focused}"]`, pane);
    if (el) { el.focus(); if (el.type === 'search') el.setSelectionRange(el.value.length, el.value.length); }
  }
}
