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

export const title = 'Communities';
export const subtitle = 'Every community we have ever found, with search and filters.';

const LIMIT = 50;
const NULL = '__null__';
let ctx = null;
let state = {};
let facets = null;

function options(values, map, allText) {
  return html`<option value="">${allText}</option>${values.map((f) => html`
    <option value="${f.value}">${f.value === NULL ? 'not set' : label(map, f.value)} (${num(f.count)})</option>`)}`;
}

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <form class="toolbar" data-add>
      <input class="grow" name="url" placeholder="Add a community by link: https://…" autocomplete="off">
      <button class="btn" type="submit">Add</button>
    </form>
    <div class="toolbar">
      <input type="search" data-f="q" placeholder="Search: name, URL, description">
      <select data-f="icp">
        <option value="">ICP: all</option>
        <option value="fit">ICP fit (in the funnel)</option>
        <option value="flag">flagged ICP (all)</option>
        <option value="no">not a fit</option>
        <option value="unchecked">not checked yet</option>
      </select>
      <select data-f="platform"><option value="">platform: all</option></select>
      <select data-f="join_type"><option value="">join type: all</option></select>
      <select data-f="join_status"><option value="">join status: all</option></select>
      <select data-f="read_outcome"><option value="">reading: all</option></select>
      <select data-f="monitored">
        <option value="">polling: all</option>
        <option value="1">polled</option>
        <option value="off">polling off</option>
        <option value="0">not polled</option>
      </select>
      <select data-f="access">
        <option value="">access: all</option>
        <option value="1">has access</option>
        <option value="0">no access</option>
      </select>
      <select data-f="source"><option value="">source: all</option></select>
      <select data-f="sort">
        <option value="icp">by ICP</option>
        <option value="leads">by leads</option>
        <option value="discovered">by date found</option>
        <option value="read">by last read</option>
        <option value="joined">by join date</option>
        <option value="name">by name</option>
      </select>
      <select data-f="dir">
        <option value="">descending</option>
        <option value="asc">ascending</option>
      </select>
    </div>
    <div data-table>${empty('Loading…')}</div>
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
      toast(res.note || 'Added');
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
  fill('platform', PLATFORM, 'platform: all');
  fill('join_type', JOIN_TYPE, 'join type: all');
  fill('join_status', JOIN_STATUS, 'join status: all');
  fill('read_outcome', READ_OUTCOME, 'reading: all');
  fill('source', SOURCE, 'source: all');
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
  if (r.fit) return html`${pill('ICP fit', 'good')} <span class="muted small">${score}</span>`;
  if (r.icp_flag) return html`${pill('ICP, not in funnel', 'neutral', 'flagged ICP, but not on Circle, or its subscription expired')} <span class="muted small">${score}</span>`;
  return html`<span class="muted small">${score}</span>`;
}

function draw(data, page) {
  render($('[data-table]', ctx.body), data.rows.length ? html`
    <div class="table-wrap"><table class="table">
      <thead><tr>
        <th>Community</th><th>Platform</th><th>ICP</th><th>Join type</th><th>Join status</th>
        <th>Reading</th><th>Polling</th><th class="num">Leads</th><th>Found</th>
      </tr></thead>
      <tbody>${data.rows.map((r) => html`
        <tr class="row-link" data-cid="${r.id}">
          <td><div class="strong">${r.name}</div><div class="muted small">${r.host || r.url}</div></td>
          <td class="small">${label(PLATFORM, r.platform)}</td>
          <td>${icpCell(r)}</td>
          <td class="small">${label(JOIN_TYPE, r.join_type)}${r.price_label ? html`<div class="muted">${r.price_label}</div>` : ''}</td>
          <td class="small">${r.join_status === 'joined' ? pill('joined', 'good') : label(JOIN_STATUS, r.join_status)}${r.has_session ? html`<div class="muted">has a session</div>` : ''}</td>
          <td class="small">${label(READ_OUTCOME, r.read_outcome)}</td>
          <td class="small">${r.watch_mode ? label(WATCH_MODE, r.watch_mode) : raw('<span class="muted">—</span>')}</td>
          <td class="num">${r.leads ? num(r.leads) : html`<span class="muted">0</span>`}</td>
          <td class="small nowrap">${when(r.discovered_at)}</td>
        </tr>`)}</tbody>
    </table></div>` : empty('Nothing found.'));
  $('[data-pager]', ctx.body).replaceChildren(pager(page, LIMIT, data.total, (p) => {
    navigate('communities', { ...state, page: String(p) }, { replace: true });
  }));
}
