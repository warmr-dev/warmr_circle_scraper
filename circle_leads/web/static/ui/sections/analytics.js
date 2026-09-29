// Section 1: the funnel. Communities: found -> fit -> paid / free / closed ->
// inside. Leads: found -> sent to Vini -> what Vini said. Every number has its
// "today" beside it, and every tile opens the list it counts.

import { api } from '../api.js';
import { empty, html, num, pct, raw, render, utcOffsetLabel } from '../dom.js';
import { SEGMENT, SEGMENT_TONE } from '../labels.js';
import { navigate } from '../router.js';

export const title = 'Аналитика';
export const subtitle = 'Воронка сообществ и лидов: за всё время и за сегодня.';
export const autoRefresh = true;

let ctx = null;

export function mount(context) {
  ctx = context;
  render(ctx.body, empty('Загрузка…'));
  const follow = (e) => {
    const el = e.target.closest('[data-link]');
    if (!el) return;
    const { section, ...params } = JSON.parse(el.dataset.link);
    navigate(section, params);
  };
  ctx.body.addEventListener('click', follow);
  ctx.body.addEventListener('keydown', (e) => { if (e.key === 'Enter') follow(e); });
}

export async function update() {
  await refresh();
}

export async function refresh() {
  const data = await api('/api/dash/analytics');
  render(ctx.body, view(data));
  ctx.setUpdated(data.generated_at);
}

function today(n) {
  return n ? html`<span class="today up">+${num(n)} сегодня</span>` : html`<span class="today">сегодня 0</span>`;
}

function tile({ name, all, todayCount, note, link, hero = false }) {
  const attrs = link ? raw(` data-link="${escapeAttr(JSON.stringify(link))}" role="link" tabindex="0"`) : raw('');
  return html`
    <div class="tile${hero ? ' tile-hero' : ''}${link ? ' tile-link' : ''}"${attrs}>
      <div class="tile-name">${name}</div>
      <div class="tile-value">${num(all)}</div>
      <div class="tile-foot">${todayCount === undefined ? '' : today(todayCount)}${note ? html`<span class="muted">${note}</span>` : ''}</div>
    </div>`;
}

function escapeAttr(text) {
  return text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// A part-to-whole bar. The numbers are always printed beside it: the colour
// is never the only way to tell the parts apart.
function splitBar(parts, total) {
  if (!total) return '';
  return html`
    <div class="split" role="img" aria-label="${parts.map((p) => `${p.name}: ${p.value}`).join(', ')}">
      ${parts.filter((p) => p.value > 0).map((p) => html`
        <span class="split-part ${p.cls}" style="flex-grow:${p.value}" title="${p.name}: ${num(p.value)} (${pct(p.value, total)})"></span>`)}
    </div>`;
}

function legend(parts, total) {
  return html`
    <table class="legend">
      <thead><tr><th></th><th class="num">всего</th><th class="num">доля</th><th class="num">сегодня</th></tr></thead>
      <tbody>${parts.map((p) => html`
        <tr class="${p.link ? 'row-link' : ''}" ${p.link ? raw(`data-link="${escapeAttr(JSON.stringify(p.link))}"`) : ''}>
          <td><span class="swatch ${p.cls}"></span>${p.name}${p.hint ? html` <span class="muted small">${p.hint}</span>` : ''}</td>
          <td class="num">${num(p.value)}</td>
          <td class="num muted">${pct(p.value, total)}</td>
          <td class="num">${p.today ? html`<span class="up">+${num(p.today)}</span>` : html`<span class="muted">0</span>`}</td>
        </tr>`)}</tbody>
    </table>`;
}

function view(d) {
  const c = d.communities;
  const l = d.leads;
  const seg = l.segments;
  const fitParts = [
    { name: 'платные', value: c.paid.all, today: c.paid.today, cls: 'cat-1',
      hint: c.paid.with_session ? `с купленной сессией: ${num(c.paid.with_session)}` : '',
      link: { section: 'communities', icp: 'fit', join_type: 'paid' } },
    { name: 'бесплатные', value: c.free.all, today: c.free.today, cls: 'cat-2',
      hint: c.free.no_address ? `без адреса: ${num(c.free.no_address)}` : '',
      link: { section: 'communities', icp: 'fit', join_type: 'free_join' } },
    { name: 'закрытые или неясно', value: c.other.all, today: c.other.today, cls: 'cat-3',
      hint: 'по приглашению, закрыто, тип не определён',
      link: { section: 'communities', icp: 'fit' } },
  ];
  const segParts = ['accepted', 'duplicate', 'held', 'rejected', 'error', 'no_data', 'not_sent'].map((key) => ({
    name: SEGMENT[key],
    value: seg[key].all,
    today: seg[key].today,
    cls: `tone-${SEGMENT_TONE[key]}`,
    link: { section: 'leads', segment: key, classification: 'LEAD' },
    hint: key === 'no_data' ? 'отправлены до того, как ответы стали сохраняться' : '',
  }));
  const notAccepted = seg.held.all + seg.rejected.all + seg.error.all;
  const notAcceptedToday = seg.held.today + seg.rejected.today + seg.error.today;

  return html`
    <p class="chip">Сегодня — с 00:00 по вашему времени (${utcOffsetLabel()})</p>

    <section class="panel">
      <h2>Сообщества</h2>
      <div class="funnel">
        ${tile({ name: 'Найдено', all: c.found.all, todayCount: c.found.today,
                 note: `на Circle с адресом: ${num(c.found.circle_all)}`,
                 link: { section: 'communities' } })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Подходят (ICP)', all: c.fit.all, todayCount: c.fit.today,
                 note: `${pct(c.fit.all, c.found.all)} от найденных`,
                 link: { section: 'communities', icp: 'fit' }, hero: true })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Смогли зайти', all: c.access.all, todayCount: c.access.today,
                 note: `подходящих ${num(c.access.fit_all)} · бот вступил ${num(c.access.joined)} · сессий ${num(c.access.with_session)}`,
                 link: { section: 'communities', access: '1' } })}
      </div>
      <h3>Из подходящих: платные, бесплатные, закрытые</h3>
      ${splitBar(fitParts, c.fit.all)}
      ${legend(fitParts, c.fit.all)}
    </section>

    <section class="panel">
      <h2>Лиды</h2>
      <div class="funnel">
        ${tile({ name: 'Найдено лидов', all: l.found.all, todayCount: l.found.today,
                 link: { section: 'leads', classification: 'LEAD' } })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Отправлено в Vini', all: l.sent.all, todayCount: l.sent.today,
                 note: `${pct(l.sent.all, l.found.all)} от найденных`,
                 link: { section: 'leads', classification: 'LEAD', segment: 'accepted,duplicate,held,rejected,error,no_data' } })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Принял Vini', all: seg.accepted.all, todayCount: seg.accepted.today,
                 note: seg.duplicate.all ? `ещё ${num(seg.duplicate.all)} у Vini уже были` : '',
                 link: { section: 'leads', classification: 'LEAD', segment: 'accepted' }, hero: true })}
        ${tile({ name: 'Не принял', all: notAccepted, todayCount: notAcceptedToday,
                 note: 'запаркован, отклонён или не дошёл',
                 link: { section: 'leads', classification: 'LEAD', segment: 'held,rejected,error' } })}
      </div>
      <h3>Что сказал Vini</h3>
      ${seg.no_data.all ? html`<p class="muted small">«Нет данных» — лиды, отправленные до того, как дашборд начал сохранять ответ Vini. Что он о них сказал, неизвестно.</p>` : ''}
      ${splitBar(segParts, l.found.all)}
      ${legend(segParts, l.found.all)}
    </section>`;
}
