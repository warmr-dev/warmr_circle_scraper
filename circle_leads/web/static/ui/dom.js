// Small DOM helpers: an escaping template tag, formatting, a drawer, toasts.
//
// Everything that reaches innerHTML goes through `html`, which escapes every
// interpolated value unless it is itself the output of `html` (or `raw`).
// The page renders posts written by strangers; nothing of theirs is markup.

class Safe {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
}

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function escape(value) {
  if (value instanceof Safe) return value.text;
  if (Array.isArray(value)) return value.map(escape).join('');
  if (value === null || value === undefined || value === false) return '';
  return String(value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i += 1) out += escape(values[i]) + strings[i + 1];
  return new Safe(out);
}

export const raw = (text) => new Safe(text);

export function render(el, content) {
  el.innerHTML = escape(content);
  return el;
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// --- formatting ---------------------------------------------------------------

const NUMBER = new Intl.NumberFormat('ru-RU');
export const num = (n) => (n === null || n === undefined ? '—' : NUMBER.format(n));

export function pct(part, whole) {
  if (!whole) return '';
  const p = (100 * part) / whole;
  return p < 1 && p > 0 ? '<1%' : `${Math.round(p)}%`;
}

const pad = (n) => String(n).padStart(2, '0');

export function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const now = new Date();
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return `сегодня ${time}`;
  const date = `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
  return d.getFullYear() === now.getFullYear() ? `${date} ${time}` : `${date}.${d.getFullYear()}`;
}

export function ago(iso) {
  if (!iso) return 'никогда';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 0) return `через ${Math.round(-s / 60)} мин`;
  if (s < 60) return 'только что';
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 48 * 3600) return `${Math.floor(s / 3600)} ч назад`;
  return `${Math.floor(s / 86400)} дн назад`;
}

export function utcOffsetLabel() {
  const minutes = -new Date().getTimezoneOffset();
  const sign = minutes >= 0 ? '+' : '−';
  const h = Math.floor(Math.abs(minutes) / 60);
  const m = Math.abs(minutes) % 60;
  return `UTC${sign}${h}${m ? `:${pad(m)}` : ''}`;
}

export function hostOf(url) {
  try { return new URL(url).host; } catch { return url || ''; }
}

// --- pieces -------------------------------------------------------------------

// A state as a word in a pill; the colour only repeats what the word says.
export function pill(text, tone = 'neutral', title = '') {
  return html`<span class="pill pill-${tone}" title="${title}">${text}</span>`;
}

export function extLink(url, text = '↗') {
  if (!url) return '';
  return html`<a class="ext" href="${url}" target="_blank" rel="noopener noreferrer" title="${url}">${text}</a>`;
}

export function empty(text) {
  return html`<div class="empty">${text}</div>`;
}

export function pager(page, limit, total, onGo) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const el = document.createElement('div');
  el.className = 'pager';
  render(el, html`
    <button data-go="${page - 1}" ${page <= 1 ? raw('disabled') : ''}>← Назад</button>
    <span>страница ${num(page)} из ${num(pages)} · всего ${num(total)}</span>
    <button data-go="${page + 1}" ${page >= pages ? raw('disabled') : ''}>Дальше →</button>`);
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-go]');
    if (b && !b.disabled) onGo(Number(b.dataset.go));
  });
  return el;
}

// --- toasts -------------------------------------------------------------------

export function toast(text, tone = 'ok') {
  const host = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast toast-${tone}`;
  el.textContent = text;
  host.append(el);
  setTimeout(() => el.remove(), tone === 'error' ? 8000 : 4000);
}

// --- drawer -------------------------------------------------------------------

let onDrawerClose = null;

export function openDrawer(title, content, { onClose } = {}) {
  const drawer = $('#drawer');
  const wasOpen = !drawer.hidden;
  render($('#drawer-title'), title);
  // A fresh body each time, so listeners of the previous panel go with it.
  const old = $('#drawer-body');
  const body = old.cloneNode(false);
  old.replaceWith(body);
  render(body, content);
  drawer.hidden = false;
  document.body.classList.add('drawer-open');
  // A panel opened from inside another keeps the first one's close action.
  onDrawerClose = onClose || (wasOpen ? onDrawerClose : null);
  $('#drawer-close').focus();
  return body;
}

export function closeDrawer() {
  const drawer = $('#drawer');
  if (drawer.hidden) return;
  drawer.hidden = true;
  document.body.classList.remove('drawer-open');
  const cb = onDrawerClose;
  onDrawerClose = null;
  if (cb) cb();
}

export function initDrawer() {
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-backdrop').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
}

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
