// Section 4: every community in the database -- ~14k rows, so search,
// filters and paging run on the server. A row opens the same panel as in
// monitoring. "Add by link" lives here too.

import { api } from '../api.js';
import { $, debounce, empty, html, num, pager, pill, raw, render, toast, when } from '../dom.js';
import { openCommunity } from '../drawers.js';
import {
  JOIN_STATUS, JOIN_TYPE, PLATFORM, READ_OUTCOME, SOURCE, WATCH_MODE, label,
} from '../labels.js';
import { navigate, toObject } from '../router.js';

export const title = 'База сообществ';
export const subtitle = 'Все сообщества, которые мы когда-либо нашли, с поиском и фильтрами.';

const LIMIT = 50;
const NULL = '__null__';
let ctx = null;
let state = {};
let facets = null;

function options(values, map, allText) {
  return html`<option value="">${allText}</option>${values.map((f) => html`
    <option value="${f.value}">${f.value === NULL ? 'не указано' : label(map, f.value)} (${num(f.count)})</option>`)}`;
}

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <form class="toolbar" data-add>
      <input class="grow" name="url" placeholder="Добавить сообщество по ссылке: https://…" autocomplete="off">
      <button class="btn" type="submit">Добавить</button>
    </form>
    <div class="toolbar">
      <input type="search" data-f="q" placeholder="Поиск: название, адрес, описание">
      <select data-f="icp">
        <option value="">ICP: все</option>
        <option value="fit">подходят (в воронке)</option>
        <option value="flag">помечены ICP (все)</option>
        <option value="no">не подходят</option>
        <option value="unchecked">ещё не проверены</option>
      </select>
      <select data-f="platform"><option value="">платформа: все</option></select>
      <select data-f="join_type"><option value="">тип входа: все</option></select>
      <select data-f="join_status"><option value="">вступление: все</option></select>
      <select data-f="read_outcome"><option value="">чтение: все</option></select>
      <select data-f="monitored">
        <option value="">опрос: все</option>
        <option value="1">опрашиваем</option>
        <option value="off">опрос выключен</option>
        <option value="0">не в опросе</option>
      </select>
      <select data-f="access">
        <option value="">доступ: все</option>
        <option value="1">есть доступ</option>
        <option value="0">нет доступа</option>
      </select>
      <select data-f="source"><option value="">источник: все</option></select>
      <select data-f="sort">
        <option value="icp">по ICP</option>
        <option value="leads">по лидам</option>
        <option value="discovered">по дате находки</option>
        <option value="read">по последнему чтению</option>
        <option value="joined">по дате вступления</option>
        <option value="name">по названию</option>
      </select>
      <select data-f="dir">
        <option value="">по убыванию</option>
        <option value="asc">по возрастанию</option>
      </select>
    </div>
    <div data-table>${empty('Загрузка…')}</div>
    <div data-pager></div>`);

  const push = () => {
    const next = { ...state, page: '' };
    for (const el of ctx.body.querySelectorAll('[data-f]')) next[el.dataset.f] = el.value.trim();
    navigate('communities', next, { replace: true });
  };
  const typed = debounce(push, 300);
  ctx.body.addEventListener('input', (e) => { if (e.target.matches('input[type=search]')) typed(); });
  ctx.body.addEventListener('change', (e) => { if (e.target.matches('[data-f]:not([type=search])')) push(); });
  ctx.body.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-cid]');
    if (row && !e.target.closest('a')) openCommunity(Number(row.dataset.cid));
  });
  $('[data-add]', ctx.body).addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = e.target.elements.url;
    const url = input.value.trim();
    if (!url) return;
    const button = e.target.querySelector('button');
    button.disabled = true;
    try {
      const res = await api('/api/communities/add', { method: 'POST', body: { url } });
      toast(res.note || 'Добавлено');
      input.value = '';
      navigate('communities', { q: res.slug }, { replace: true });
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      button.disabled = false;
    }
  });
  loadFacets();
}

async function loadFacets() {
  try {
    facets = await api('/api/dash/communities/facets');
  } catch {
    return;
  }
  const fill = (key, map, allText) => {
    const el = $(`[data-f="${key}"]`, ctx.body);
    const keep = state[key] || '';
    render(el, options(facets[key] || [], map, allText));
    el.value = keep;
  };
  fill('platform', PLATFORM, 'платформа: все');
  fill('join_type', JOIN_TYPE, 'тип входа: все');
  fill('join_status', JOIN_STATUS, 'вступление: все');
  fill('read_outcome', READ_OUTCOME, 'чтение: все');
  fill('source', SOURCE, 'источник: все');
}

function sync(params) {
  state = toObject(params);
  for (const el of ctx.body.querySelectorAll('[data-f]')) {
    const v = state[el.dataset.f] || '';
    if (document.activeElement !== el && el.value !== v) el.value = v || (el.tagName === 'SELECT' ? el.options[0].value : '');
  }
}

export async function update(params) {
  sync(params);
  await refresh();
}

export async function refresh() {
  const page = Number(state.page || 1);
  const { open, page: _page, ...filters } = state;
  const data = await api('/api/dash/communities', { params: { ...filters, page, limit: LIMIT } });
  ctx.setUpdated();
  draw(data, page);
}

function icpCell(r) {
  const score = r.icp_score === null || r.icp_score === undefined ? '—' : Math.round(r.icp_score);
  if (r.fit) return html`${pill('подходит', 'good')} <span class="muted small">${score}</span>`;
  if (r.icp_flag) return html`${pill('ICP вне воронки', 'neutral', 'помечено ICP, но не Circle или подписка истекла')} <span class="muted small">${score}</span>`;
  return html`<span class="muted small">${score}</span>`;
}

function draw(data, page) {
  render($('[data-table]', ctx.body), data.rows.length ? html`
    <div class="table-wrap"><table class="table">
      <thead><tr>
        <th>Сообщество</th><th>Платформа</th><th>ICP</th><th>Тип входа</th><th>Вступление</th>
        <th>Чтение</th><th>Опрос</th><th class="num">Лидов</th><th>Найдено</th>
      </tr></thead>
      <tbody>${data.rows.map((r) => html`
        <tr class="row-link" data-cid="${r.id}">
          <td><div class="strong">${r.name}</div><div class="muted small">${r.host || r.url}</div></td>
          <td class="small">${label(PLATFORM, r.platform)}</td>
          <td>${icpCell(r)}</td>
          <td class="small">${label(JOIN_TYPE, r.join_type)}${r.price_label ? html`<div class="muted">${r.price_label}</div>` : ''}</td>
          <td class="small">${r.join_status === 'joined' ? pill('вступили', 'good') : label(JOIN_STATUS, r.join_status)}${r.has_session ? html`<div class="muted">есть сессия</div>` : ''}</td>
          <td class="small">${label(READ_OUTCOME, r.read_outcome)}</td>
          <td class="small">${r.watch_mode ? label(WATCH_MODE, r.watch_mode) : raw('<span class="muted">—</span>')}</td>
          <td class="num">${r.leads ? num(r.leads) : html`<span class="muted">0</span>`}</td>
          <td class="small nowrap">${when(r.discovered_at)}</td>
        </tr>`)}</tbody>
    </table></div>` : empty('Ничего не найдено.'));
  $('[data-pager]', ctx.body).replaceChildren(pager(page, LIMIT, data.total, (p) => {
    navigate('communities', { ...state, page: String(p) }, { replace: true });
  }));
}
