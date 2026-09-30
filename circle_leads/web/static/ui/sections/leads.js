// Section 3: every lead we hold, split by what Vini said about it, with the
// reason when it did not take one -- in Vini's words, or ours when the lead
// never went out.
//
// Each lead is the first dashboard's card (restored 2026-09-30 at the user's
// request): author, priority, what the model pulled out, the post, where it
// came from -- plus what Vini said. A click opens the full panel.

import { api } from '../api.js';
import { $, debounce, empty, html, num, pager, render } from '../dom.js';
import { openLead, segmentCell } from '../drawers.js';
import { icon } from '../icons.js';
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
    <div class="leads-count" data-count></div>
    <div class="lc-grid" data-cards>${empty('Загрузка…')}</div>
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
    const more = e.target.closest('.lc-more');
    if (more) {
      const text = more.previousElementSibling;
      const open = text.classList.toggle('open');
      more.setAttribute('aria-expanded', String(open));
      more.textContent = open ? 'Свернуть' : 'Показать ещё';
      return;
    }
    const card = e.target.closest('[data-lead]');
    // Selecting a phrase to copy is not a click on the card.
    if (card && !e.target.closest('a') && !String(window.getSelection() || '')) {
      openLead(Number(card.dataset.lead));
    }
  });
  ctx.body.addEventListener('keydown', (e) => {
    const card = e.target.closest('[data-lead]');
    if (card && e.target === card && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      openLead(Number(card.dataset.lead));
    }
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

// --- the lead card ------------------------------------------------------------------

const PRIORITY_WORD = { HIGH: 'Высокий приоритет', MEDIUM: 'Средний приоритет', LOW: 'Низкий приоритет' };
const AVATAR_COLORS = ['#c026d3', '#0891b2', '#7a5af8', '#b54708', '#067647', '#2970ff', '#e11d48'];
const TEXT_CLAMP = 240;  // a longer post gets "Показать ещё"

function initials(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  const first = (w) => Array.from(w)[0] || '';
  return (first(words[0]) + (words.length > 1 ? first(words[words.length - 1]) : '')).toUpperCase() || '?';
}

function avatarColor(name) {
  let h = 0;
  for (const ch of String(name || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function dateWord(iso, withTime) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const opts = { day: 'numeric', month: 'short', year: 'numeric' };
  return withTime
    ? d.toLocaleString('ru-RU', { ...opts, hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString('ru-RU', opts);
}

function spaceOf(url) {
  const m = String(url || '').match(/\/c\/([^/?#]+)/);
  if (!m) return '';
  try { return decodeURIComponent(m[1]); } catch { return m[1]; }
}

function leadCard(r) {
  let content = String(r.content ?? r.snippet ?? '').trim();
  const firstLine = (content.split('\n').find((l) => l.trim()) || '').trim();
  const rawTitle = r.job_title || r.title || firstLine;
  // The title came from the post's first line: do not repeat it in the text.
  if (rawTitle === firstLine && content.length > firstLine.length) {
    content = content.slice(content.indexOf(firstLine) + firstLine.length).trim();
  }
  const cut = rawTitle.length > 110 ? `${rawTitle.slice(0, 110)}…` : rawTitle;
  const title = cut ? cut.charAt(0).toUpperCase() + cut.slice(1) : '—';
  const author = r.author || 'Автор неизвестен';
  const isLead = r.classification === 'LEAD';
  const priority = ['HIGH', 'MEDIUM', 'LOW'].includes(r.priority) ? r.priority : 'LOW';
  const isLlm = r.decided_by === 'llm';
  const skills = (r.skills || []).slice(0, 3);
  const moreSkills = (r.skills || []).length - skills.length;
  const employment = r.employment_type && r.employment_type !== 'Unknown' ? r.employment_type : '';
  const community = r.community.name || r.community.slug;
  const space = spaceOf(r.post_url);

  const chips = [
    !isLead && html`<span class="lc-chip verdict">${r.classification === 'NOT_LEAD' ? 'не лид' : 'не уверен'}</span>`,
    html`<span class="lc-chip ${isLlm ? 'ai' : 'rules'}" title="${r.reason || ''}">${icon(isLlm ? 'bot' : 'rules')} ${isLlm ? 'AI' : 'Правила'}</span>`,
    r.lead_score !== null && r.lead_score !== undefined
      && html`<span class="lc-chip score" title="Уверенность: ${r.confidence || '—'}">${icon('gauge')} Оценка ${r.lead_score}</span>`,
    employment && html`<span class="lc-chip">${icon('briefcase')} ${employment}</span>`,
    r.hire_target && html`<span class="lc-chip">${r.hire_target}</span>`,
    r.budget && html`<span class="lc-chip money">${icon('money')} ${r.budget}</span>`,
    r.urgency === 'High' && html`<span class="lc-chip urgent">Срочно</span>`,
    ...skills.map((s) => html`<span class="lc-chip">${s}</span>`),
    moreSkills > 0 && html`<span class="lc-chip">+${moreSkills}</span>`,
  ].filter(Boolean);

  const source = [
    html`<a class="src" href="${r.community.url || '#'}" target="_blank" rel="noopener noreferrer"
      title="${community} · ${r.community.slug}">${icon('users')}<b>${community}</b></a>`,
    space && html`<span>${icon('hash', 13)}${space}</span>`,
    r.company && html`<span>${icon('building', 13)}${r.company}</span>`,
    r.location && html`<span>${icon('pin', 13)}${r.location}</span>`,
  ].filter(Boolean);

  const titleHtml = r.post_url
    ? html`<a href="${r.post_url}" target="_blank" rel="noopener noreferrer">${title}${icon('ext', 13)}</a>`
    : title;

  return html`<article class="lc ${isLead ? priority : 'not-lead'}" data-lead="${r.id}" tabindex="0">
    <div class="lc-head">
      <span class="lc-avatar" style="--av:${avatarColor(author)}">${initials(r.author)}</span>
      <div class="lc-name" title="${author}">${author}</div>
      ${isLead ? html`<span class="lc-badge">${icon('flame')} ${PRIORITY_WORD[priority]}</span>` : ''}
    </div>
    <div class="lc-chips">${chips}</div>
    <h3 class="lc-title">${titleHtml}</h3>
    <p class="lc-text">${content || '—'}</p>
    ${content.length > TEXT_CLAMP ? html`<button class="lc-more" type="button" aria-expanded="false">Показать ещё</button>` : ''}
    <div class="lc-source">${source}</div>
    <div class="lc-vini"><span class="muted">Vini:</span> ${segmentCell(r)}</div>
    <div class="lc-foot">
      <span title="Когда мы нашли лид">${icon('found')} Найден ${dateWord(r.created_at, false)}</span>
      <span title="Номер лида">#${r.id}</span>
      <span title="Когда опубликован пост">${icon('clock')} ${dateWord(r.published_at, true)}</span>
    </div>
  </article>`;
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

  render($('[data-count]', ctx.body), data.rows.length
    ? `Показано ${num(data.rows.length)} из ${num(data.filtered)}` : '');
  render($('[data-cards]', ctx.body), data.rows.length
    ? data.rows.map(leadCard) : empty('Ничего не найдено.'));

  const pagerHost = $('[data-pager]', ctx.body);
  pagerHost.replaceChildren(pager(page, LIMIT, data.filtered, (p) => {
    navigate('leads', { ...state, page: String(p) }, { replace: true });
  }));
}

