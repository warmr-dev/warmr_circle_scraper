// Section 2: the communities we read -- how each is read, what it answered
// last, and how many leads it has given, all-time and today. By default only
// the ones read now or read before (the user, 2026-09-29); a feed that has
// always refused a visitor is "private" and sits behind its own filter.
// ~300 rows, so the list is filtered in the browser.

import { api } from '../api.js';
import { $, ago, debounce, empty, html, num, pill, render, when } from '../dom.js';
import { openCommunity } from '../drawers.js';
import { CONN_BUCKET, JOIN_STATUS, WATCH_MODE, WATCH_TIER, label, watchStatus } from '../labels.js';
import { navigate, toObject } from '../router.js';

export const title = 'Мониторинг';
export const subtitle = 'Сообщества, которые мы читаем или читали: как читаем, чем ответили, сколько лидов дали.';
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

// What the default list shows: read now, or read before.
const DEFAULT_GROUPS = ['ok', 'broken'];

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <div class="kpis" data-kpis></div>
    <div class="toolbar">
      <input type="search" data-f="q" placeholder="Поиск: название или адрес">
      <select data-f="status">
        <option value="">читаем или читали</option>
        <option value="ok">читаем сейчас</option>
        <option value="broken">перестали читаться</option>
        <option value="private">приватные — ни разу не читали</option>
        <option value="other">другие — не читали</option>
        <option value="all">все</option>
      </select>
      <select data-f="mode">
        <option value="">любой способ</option>
        <option value="anon">лента анонимно</option>
        <option value="cookie">лента по сессии</option>
        <option value="session">есть рабочая сессия</option>
      </select>
      <select data-f="tier">
        <option value="">любая частота</option>
        <option value="fast">каждые 2 мин</option>
        <option value="slow">каждые 15 мин</option>
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

const GROUP_TONE = { ok: 'good', broken: 'warning', private: 'muted', other: 'warning' };

function statusCell(row) {
  // The pill is about the feed: a failing feed is never green, even when the
  // session still reads the community (then it is a warning, not a verdict).
  let tone = GROUP_TONE[row.group];
  if (row.feed_ok) tone = 'good';
  else if (row.group === 'ok') tone = 'warning';
  else if (row.group === 'broken' && row.consecutive_errors >= 5) tone = 'critical';
  const errors = row.consecutive_errors ? ` ×${row.consecutive_errors}` : '';
  return html`${pill(watchStatus(row.last_status, row.mode, row.session_ok) + errors, tone, row.last_detail || '')}
    <div class="muted small">${ago(row.last_checked_at)}</div>`;
}

// Two ways in: the feed (every 2 or 15 minutes, anonymously or with the
// session) and the session's full read of every space inside the 6-hour pass.
function readCell(row) {
  const feed = row.mode === 'off'
    ? html`<span class="muted">лента не читается</span>`
    : html`лента ${label(WATCH_MODE, row.mode)}<div class="muted small">${label(WATCH_TIER, row.tier)}</div>`;
  let session = '';
  if (row.session_ok) session = html`<div class="small">по сессии — всё, раз в 6 ч</div>`;
  else if (row.has_session) session = html`<div class="small warn-text">сессия не работает</div>`;
  return html`${feed}${session}`;
}

function insideCell(row) {
  const who = row.account ? html`<div class="muted small">аккаунт: ${row.account}</div>` : '';
  if (row.join_status === 'joined') return html`${pill('вступили', 'good')}${who || html`<div class="muted small">аккаунт не записан</div>`}`;
  if (row.has_session) {
    const bucket = row.conn?.bucket;
    return html`${pill(bucket ? label(CONN_BUCKET, bucket) : 'сессия', bucket && bucket !== 'working' ? 'warning' : 'good', row.conn?.detail || '')}${who}`;
  }
  return html`<span class="muted small">${label(JOIN_STATUS, row.join_status)}</span>`;
}

function draw() {
  const s = data.summary;
  render($('[data-kpis]', ctx.body), html`
    <div class="kpi"><b>${num(s.reading)}</b><span>читаем сейчас</span></div>
    <div class="kpi"><b>${num(s.feed_fast)}</b><span>лента каждые 2 мин</span></div>
    <div class="kpi"><b>${num(s.feed_slow)}</b><span>лента каждые 15 мин</span></div>
    <div class="kpi"><b>${num(s.session_ok)}</b><span>по сессии — всё, раз в 6 ч</span></div>
    <div class="kpi ${s.broken ? 'kpi-warn' : ''}"><b>${num(s.broken)}</b><span>перестали читаться</span></div>
    <div class="kpi"><b>${num(s.private)}</b><span>приватные, не читаем</span></div>
    <div class="kpi"><b>${num(s.leads_today)}</b><span>лидов сегодня · из ${num(s.communities_with_leads_today)} сообществ</span></div>`);

  const q = (state.q || '').toLowerCase();
  const status = state.status || '';
  let rows = data.rows.filter((r) => {
    if (status === '' && !DEFAULT_GROUPS.includes(r.group)) return false;
    if (status && status !== 'all' && r.group !== status) return false;
    if (state.mode === 'session' ? !r.session_ok : state.mode && r.mode !== state.mode) return false;
    if (state.tier && (r.mode === 'off' || r.tier !== state.tier)) return false;
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
          <td class="nowrap">${readCell(r)}</td>
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
      <div class="table-wrap"><table class="table"><tbody>${unwatched.map((u) => html`
        <tr><td>${u.host}</td><td>${u.conn ? pill(label(CONN_BUCKET, u.conn.bucket), u.conn.bucket === 'working' ? 'good' : 'warning', u.conn.detail || '') : ''}</td>
        <td class="small muted">${u.session ? `куки сохранены ${when(u.session.created_at)}` : 'кук нет'}</td></tr>`)}</tbody></table></div>
    </section>` : '');
}
