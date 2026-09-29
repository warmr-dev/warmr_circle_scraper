// Section 2: the communities the feed watcher checks -- how each is read, what
// it answered last, and how many leads it has given, all-time and today.
// ~300 rows, so the list is filtered in the browser.

import { api } from '../api.js';
import { $, ago, debounce, empty, html, num, pill, render, when } from '../dom.js';
import { openCommunity } from '../drawers.js';
import { CONN_BUCKET, JOIN_STATUS, WATCH_MODE, WATCH_TIER, label, watchStatus } from '../labels.js';
import { navigate, toObject } from '../router.js';

export const title = 'Мониторинг';
export const subtitle = 'Сообщества, которые мы проверяем: как читаем, чем ответили, сколько лидов дали.';
export const autoRefresh = true;

const SORTS = {
  leads: (a, b) => b.leads_all - a.leads_all || b.leads_today - a.leads_today,
  today: (a, b) => b.leads_today - a.leads_today || b.leads_all - a.leads_all,
  checked: (a, b) => String(b.last_checked_at || '').localeCompare(String(a.last_checked_at || '')),
  errors: (a, b) => b.consecutive_errors - a.consecutive_errors,
  name: (a, b) => a.name.localeCompare(b.name, 'ru'),
};

let ctx = null;
let data = null;
let state = {};

function health(row) {
  if (row.mode === 'off') return 'off';
  const fine = ['ok', 'not_modified'].includes(row.last_status) && !row.consecutive_errors;
  return fine ? 'ok' : 'problem';
}

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <div class="kpis" data-kpis></div>
    <div class="toolbar">
      <input type="search" data-f="q" placeholder="Поиск: название или адрес">
      <select data-f="status">
        <option value="">активные</option>
        <option value="ok">работают</option>
        <option value="problem">с проблемами</option>
        <option value="off">выключенные</option>
        <option value="all">все</option>
      </select>
      <select data-f="mode">
        <option value="">любой способ</option>
        <option value="anon">анонимно</option>
        <option value="cookie">по сессии</option>
      </select>
      <select data-f="sort">
        <option value="leads">по лидам</option>
        <option value="today">по лидам за сегодня</option>
        <option value="checked">по времени проверки</option>
        <option value="errors">по ошибкам подряд</option>
        <option value="name">по названию</option>
      </select>
      <label class="check"><input type="checkbox" data-f="with_leads"> только с лидами</label>
    </div>
    <div data-table>${empty('Загрузка…')}</div>
    <div data-unwatched></div>`);
  const push = () => {
    const next = {};
    for (const el of ctx.body.querySelectorAll('[data-f]')) {
      const v = el.type === 'checkbox' ? (el.checked ? '1' : '') : el.value.trim();
      if (v) next[el.dataset.f] = v;
    }
    navigate('monitoring', next, { replace: true });
  };
  const typed = debounce(push, 250);
  ctx.body.addEventListener('input', (e) => { if (e.target.matches('input[type=search]')) typed(); });
  ctx.body.addEventListener('change', (e) => { if (e.target.matches('[data-f]:not([type=search])')) push(); });
  ctx.body.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-cid]');
    if (row && !e.target.closest('a')) openCommunity(Number(row.dataset.cid));
  });
}

function sync(params) {
  state = toObject(params);
  for (const el of ctx.body.querySelectorAll('[data-f]')) {
    const v = state[el.dataset.f] || '';
    if (el.type === 'checkbox') el.checked = v === '1';
    else if (document.activeElement !== el && el.value !== v) el.value = v || (el.tagName === 'SELECT' ? el.options[0].value : '');
  }
}

export async function update(params) {
  sync(params);
  if (!data) await load();
  else draw();
}

export async function refresh() {
  await load();
}

async function load() {
  data = await api('/api/dash/monitoring');
  ctx.setUpdated(data.generated_at);
  draw();
}

function statusCell(row) {
  const h = health(row);
  const tone = h === 'ok' ? 'good' : h === 'off' ? 'muted' : row.consecutive_errors >= 5 ? 'critical' : 'warning';
  const errors = row.consecutive_errors ? ` ×${row.consecutive_errors}` : '';
  return html`${pill(watchStatus(row.last_status) + errors, tone, row.last_detail || '')}
    <div class="muted small">${ago(row.last_checked_at)}</div>`;
}

function insideCell(row) {
  if (row.join_status === 'joined') return pill('вступили', 'good');
  if (row.has_session) {
    const bucket = row.conn?.bucket;
    return pill(bucket ? label(CONN_BUCKET, bucket) : 'сессия', bucket && bucket !== 'working' ? 'warning' : 'good', row.conn?.detail || '');
  }
  return html`<span class="muted small">${label(JOIN_STATUS, row.join_status)}</span>`;
}

function draw() {
  const s = data.summary;
  render($('[data-kpis]', ctx.body), html`
    <div class="kpi"><b>${num(s.active)}</b><span>проверяем</span></div>
    <div class="kpi"><b>${num(s.fast)}</b><span>каждые 2 мин</span></div>
    <div class="kpi"><b>${num(s.slow)}</b><span>каждые 15 мин</span></div>
    <div class="kpi"><b>${num(s.with_session)}</b><span>по сессии</span></div>
    <div class="kpi ${s.failing ? 'kpi-warn' : ''}"><b>${num(s.failing)}</b><span>с проблемами</span></div>
    <div class="kpi"><b>${num(s.off)}</b><span>выключены</span></div>
    <div class="kpi"><b>${num(s.leads_today)}</b><span>лидов сегодня · из ${num(s.communities_with_leads_today)} сообществ</span></div>`);

  const q = (state.q || '').toLowerCase();
  const status = state.status || '';
  let rows = data.rows.filter((r) => {
    const h = health(r);
    if (status === '' && h === 'off') return false;
    if (status && status !== 'all' && h !== status) return false;
    if (state.mode && r.mode !== state.mode) return false;
    if (state.with_leads === '1' && !r.leads_all) return false;
    if (q && !`${r.name} ${r.host}`.toLowerCase().includes(q)) return false;
    return true;
  });
  rows = rows.sort(SORTS[state.sort] || SORTS.leads);

  render($('[data-table]', ctx.body), rows.length ? html`
    <p class="muted small">Показано ${num(rows.length)} из ${num(data.rows.length)}</p>
    <div class="table-wrap"><table class="table">
      <thead><tr>
        <th>Сообщество</th><th>Как читаем</th><th>Последний ответ</th><th>Внутри</th>
        <th class="num">Лидов</th><th class="num">Сегодня</th><th>Последний лид</th>
      </tr></thead>
      <tbody>${rows.map((r) => html`
        <tr data-cid="${r.community_id}" class="row-link">
          <td><div class="strong">${r.name}</div><div class="muted small">${r.host}${r.icp_flag ? ' · ICP' : ''}</div></td>
          <td class="nowrap">${label(WATCH_MODE, r.mode)}${r.mode !== 'off' ? html`<div class="muted small">${label(WATCH_TIER, r.tier)}</div>` : ''}</td>
          <td class="nowrap">${statusCell(r)}</td>
          <td>${insideCell(r)}</td>
          <td class="num">${r.leads_all ? num(r.leads_all) : html`<span class="muted">0</span>`}</td>
          <td class="num">${r.leads_today ? html`<b class="up">+${num(r.leads_today)}</b>` : html`<span class="muted">0</span>`}</td>
          <td class="small">${r.last_lead_at ? when(r.last_lead_at) : html`<span class="muted">—</span>`}</td>
        </tr>`)}</tbody>
    </table></div>` : empty('Ничего не подходит под фильтр.'));

  const unwatched = data.unwatched_connections || [];
  render($('[data-unwatched]', ctx.body), unwatched.length ? html`
    <section class="panel">
      <h2>Сессии без опроса (${num(unwatched.length)})</h2>
      <p class="muted small">Для этих адресов сохранена сессия или заведено подключение, но наблюдатель их не опрашивает — обычно это сообщество, которого нет в базе под этим адресом.</p>
      <table class="table"><tbody>${unwatched.map((u) => html`
        <tr><td>${u.host}</td><td>${u.conn ? pill(label(CONN_BUCKET, u.conn.bucket), u.conn.bucket === 'working' ? 'good' : 'warning', u.conn.detail || '') : ''}</td>
        <td class="small muted">${u.session ? `куки сохранены ${when(u.session.created_at)}` : 'кук нет'}</td></tr>`)}</tbody></table>
    </section>` : '');
}
