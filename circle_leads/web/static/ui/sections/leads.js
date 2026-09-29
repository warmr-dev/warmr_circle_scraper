// Section 3: every lead we hold, split by what Vini said about it, with the
// reason when it did not take one -- in Vini's words, or ours when the lead
// never went out.

import { api } from '../api.js';
import { $, debounce, empty, extLink, html, num, pager, pill, render, when } from '../dom.js';
import { openLead, segmentCell } from '../drawers.js';
import { NOT_SENT, SEGMENT, label, viniReason } from '../labels.js';
import { navigate, toObject } from '../router.js';

export const title = 'Лиды';
export const subtitle = 'Все лиды в базе: что принял Vini, что нет и почему.';

const TABS = [
  { key: '', name: 'Все', segments: null },
  { key: 'accepted', name: 'Принял Vini', segments: ['accepted'] },
  { key: 'held,rejected,error', name: 'Не принял', segments: ['held', 'rejected', 'error'] },
  { key: 'duplicate', name: 'Уже был у Vini', segments: ['duplicate'] },
  { key: 'not_sent', name: 'Не отправлен', segments: ['not_sent'] },
  { key: 'no_data', name: 'Нет данных', segments: ['no_data'] },
];
const LIMIT = 50;

let ctx = null;
let state = {};

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <div class="tabs" data-tabs></div>
    <div class="toolbar">
      <input type="search" data-f="q" placeholder="Поиск: текст поста, автор, компания, сообщество">
      <select data-f="classification">
        <option value="">лиды и не лиды</option>
        <option value="LEAD">только лиды</option>
        <option value="NOT_LEAD">только «не лид»</option>
      </select>
      <label class="field-inline">с <input type="date" data-f="since"></label>
      <label class="field-inline">по <input type="date" data-f="until"></label>
      <select data-f="sort">
        <option value="found">сначала новые</option>
        <option value="published">по дате поста</option>
        <option value="score">по оценке</option>
      </select>
    </div>
    <div class="chips" data-reasons></div>
    <div data-table>${empty('Загрузка…')}</div>
    <div data-pager></div>`);

  const push = (extra = {}) => {
    const next = { ...state, page: '', ...extra };
    for (const el of ctx.body.querySelectorAll('[data-f]')) next[el.dataset.f] = el.value.trim();
    navigate('leads', next, { replace: true });
  };
  const typed = debounce(() => push(), 300);
  ctx.body.addEventListener('input', (e) => { if (e.target.matches('input[type=search]')) typed(); });
  ctx.body.addEventListener('change', (e) => { if (e.target.matches('[data-f]:not([type=search])')) push(); });
  ctx.body.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-tab]');
    if (tab) { push({ segment: tab.dataset.tab, reason: '' }); return; }
    const chip = e.target.closest('[data-reason]');
    if (chip) {
      const reason = chip.dataset.reason;
      push({ reason: state.reason === reason ? '' : reason });
      return;
    }
    const row = e.target.closest('tr[data-lead]');
    if (row && !e.target.closest('a')) openLead(Number(row.dataset.lead));
  });
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
  const data = await api('/api/dash/leads', {
    params: {
      segment: state.segment, reason: state.reason, q: state.q,
      classification: state.classification, since: state.since, until: state.until,
      sort: state.sort, community_id: state.community_id, page, limit: LIMIT,
    },
  });
  ctx.setUpdated();
  draw(data, page);
}

function tabCount(counts, tab) {
  if (!tab.segments) return counts.all;
  return tab.segments.reduce((sum, s) => sum + (counts[s] || 0), 0);
}

function reasonsFor(data) {
  const segs = (state.segment || '').split(',').filter(Boolean);
  const chips = [];
  const showVini = !segs.length || segs.some((s) => ['held', 'rejected', 'error'].includes(s));
  const showOurs = !segs.length || segs.includes('not_sent');
  if (showVini) {
    for (const r of data.reasons.vini) {
      chips.push({ key: r.reason, text: `${label(SEGMENT, r.segment)}: ${viniReason(r.reason) || 'без причины'}`, count: r.count, title: r.reason });
    }
  }
  if (showOurs) {
    for (const r of data.reasons.not_sent) {
      chips.push({ key: r.reason, text: `не отправлен: ${label(NOT_SENT, r.reason)}`, count: r.count, title: r.reason });
    }
  }
  return chips;
}

function verdict(row) {
  const lead = row.classification === 'LEAD';
  return html`${pill(lead ? 'лид' : 'не лид', lead ? 'good' : 'muted')}
    <div class="muted small">${row.priority || ''} ${row.lead_score ? `· ${row.lead_score}` : ''} ${row.decided_by ? `· ${row.decided_by}` : ''}</div>`;
}

function draw(data, page) {
  const current = state.segment || '';
  render($('[data-tabs]', ctx.body), TABS.map((t) => html`
    <button class="tab" data-tab="${t.key}" aria-selected="${t.key === current ? 'true' : 'false'}">
      ${t.name} <span class="tab-count">${num(tabCount(data.counts, t))}</span>
    </button>`));

  const chips = reasonsFor(data);
  render($('[data-reasons]', ctx.body), chips.length ? html`
    <span class="muted small">Причина:</span>
    ${chips.map((c) => html`<button class="chip-btn" data-reason="${c.key}" title="${c.title}"
      aria-pressed="${state.reason === c.key ? 'true' : 'false'}">${c.text} <b>${num(c.count)}</b></button>`)}` : '');

  render($('[data-table]', ctx.body), data.rows.length ? html`
    <div class="table-wrap"><table class="table">
      <thead><tr>
        <th>Найден</th><th>Сообщество</th><th>Автор</th><th>Суть</th><th>Наш вердикт</th><th>Vini</th><th></th>
      </tr></thead>
      <tbody>${data.rows.map((r) => html`
        <tr class="row-link" data-lead="${r.id}">
          <td class="small nowrap">${when(r.created_at)}<div class="muted">#${r.id}</div></td>
          <td class="small">${r.community.name}</td>
          <td class="small">${r.author || html`<span class="muted">—</span>`}</td>
          <td class="gist"><div class="strong">${r.job_title || r.title || ''}</div><div class="muted small clamp">${r.snippet}</div></td>
          <td>${verdict(r)}</td>
          <td class="vini">${segmentCell(r)}</td>
          <td>${extLink(r.post_url)}</td>
        </tr>`)}</tbody>
    </table></div>` : empty('Ничего не найдено.'));

  const pagerHost = $('[data-pager]', ctx.body);
  pagerHost.replaceChildren(pager(page, LIMIT, data.filtered, (p) => {
    navigate('leads', { ...state, page: String(p) }, { replace: true });
  }));
}

