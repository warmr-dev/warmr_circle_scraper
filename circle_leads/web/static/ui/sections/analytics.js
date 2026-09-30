// Section 1: the funnel. Communities: found -> fit -> paid / free / closed ->
// inside. Leads: found -> sent to Vini -> what Vini said. Every number has its
// "today" beside it, and every tile opens the list it counts.

import { api } from '../api.js';
import { empty, html, num, pct, raw, render, utcOffsetLabel } from '../dom.js';
import { SEGMENT, SEGMENT_TONE } from '../labels.js';
import { navigate } from '../router.js';

export const title = 'Analytics';
export const subtitle = 'The community and lead funnel, all time and today.';
export const autoRefresh = true;

let ctx = null;

export function mount(context) {
  ctx = context;
  render(ctx.body, empty('Loading…'));
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
  return n ? html`<span class="today up">+${num(n)} today</span>` : html`<span class="today">0 today</span>`;
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
      <thead><tr><th></th><th class="num">total</th><th class="num">share</th><th class="num">today</th></tr></thead>
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
  const r = c.reading;
  const l = d.leads;
  const seg = l.segments;
  const fitParts = [
    { name: 'paid', value: c.paid.all, today: c.paid.today, cls: 'cat-1',
      hint: c.paid.with_session ? `${num(c.paid.with_session)} with a bought session` : '',
      link: { section: 'communities', icp: 'fit', join_type: 'paid' } },
    { name: 'free', value: c.free.all, today: c.free.today, cls: 'cat-2',
      hint: c.free.no_address ? `${num(c.free.no_address)} with no known address` : '',
      link: { section: 'communities', icp: 'fit', join_type: 'free_join' } },
    { name: 'closed or unclear', value: c.other.all, today: c.other.today, cls: 'cat-3',
      hint: 'invite-only, closed, or type unknown',
      link: { section: 'communities', icp: 'fit' } },
  ];
  const segParts = ['accepted', 'duplicate', 'held', 'rejected', 'error', 'no_data', 'not_sent'].map((key) => ({
    name: SEGMENT[key],
    value: seg[key].all,
    today: seg[key].today,
    cls: `tone-${SEGMENT_TONE[key]}`,
    link: { section: 'leads', segment: key, classification: 'LEAD' },
    hint: key === 'no_data' ? 'sent before we started saving answers' : '',
  }));
  const notAccepted = seg.held.all + seg.rejected.all + seg.error.all;
  const notAcceptedToday = seg.held.today + seg.rejected.today + seg.error.today;

  return html`
    <p class="chip">Today counts from 00:00 your time (${utcOffsetLabel()})</p>

    <section class="panel">
      <h2>Communities</h2>
      <div class="funnel">
        ${tile({ name: 'Found', all: c.found.all, todayCount: c.found.today,
                 note: `${num(c.found.circle_all)} on Circle with a known address`,
                 link: { section: 'communities' } })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'ICP fit', all: c.fit.all, todayCount: c.fit.today,
                 note: `${pct(c.fit.all, c.found.all)} of those found`,
                 link: { section: 'communities', icp: 'fit' }, hero: true })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Got in', all: c.access.all, todayCount: c.access.today,
                 note: `${num(c.access.fit_all)} ICP fit · ${num(c.access.joined)} via the join bot · ${num(c.access.with_session)} with a session`,
                 link: { section: 'communities', access: '1' } })}
      </div>
      <h3>Of the ICP fit: paid, free, closed</h3>
      ${splitBar(fitParts, c.fit.all)}
      ${legend(fitParts, c.fit.all)}
      ${c.other.all ? html`<p class="muted small">Closed ICP-fit communities are rechecked weekly, in case they opened up or started allowing joins.</p>` : ''}
    </section>

    <section class="panel">
      <h2>Reading</h2>
      <div class="funnel">
        ${tile({ name: 'Reading now', all: r.all,
                 note: `${num(r.anonymous)} anonymously · ${num(r.with_session)} with a session · ${num(r.fit)} ICP fit`,
                 link: { section: 'monitoring', status: 'ok' }, hero: true })}
        ${tile({ name: 'Feed every 2 min', all: r.feed_fast,
                 note: 'recently active, or we are members',
                 link: { section: 'monitoring', status: 'ok', tier: 'fast' } })}
        ${tile({ name: 'Feed every 15 min', all: r.feed_slow,
                 note: 'no new posts for 14+ days',
                 link: { section: 'monitoring', status: 'ok', tier: 'slow' } })}
        ${tile({ name: 'New posts today', all: r.today, note: 'communities whose feed brought new posts' })}
      </div>
      <p class="muted small">“Got in” means we are members: the join bot joined, or a session is saved. “Reading now” means the feed answers or the session works. Open communities are read anonymously, without joining, so “Reading now” is larger than “Got in”. With a session, the cookie scan reads the whole community every 6 hours, including closed spaces and comments.</p>
    </section>

    <section class="panel">
      <h2>Leads</h2>
      <div class="funnel">
        ${tile({ name: 'Leads found', all: l.found.all, todayCount: l.found.today,
                 link: { section: 'leads', classification: 'LEAD' } })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Sent to Vini', all: l.sent.all, todayCount: l.sent.today,
                 note: `${pct(l.sent.all, l.found.all)} of those found`,
                 link: { section: 'leads', classification: 'LEAD', segment: 'accepted,duplicate,held,rejected,error,no_data' } })}
        <span class="arrow" aria-hidden="true">→</span>
        ${tile({ name: 'Accepted by Vini', all: seg.accepted.all, todayCount: seg.accepted.today,
                 note: seg.duplicate.all ? `${num(seg.duplicate.all)} more Vini already had` : '',
                 link: { section: 'leads', classification: 'LEAD', segment: 'accepted' }, hero: true })}
        ${tile({ name: 'Not accepted', all: notAccepted, todayCount: notAcceptedToday,
                 note: 'parked, rejected, or not delivered',
                 link: { section: 'leads', classification: 'LEAD', segment: 'held,rejected,error' } })}
      </div>
      <h3>What Vini said</h3>
      ${seg.no_data.all ? html`<p class="muted small">“No data”: leads sent before the dashboard started saving Vini's answer, so what Vini said about them is unknown.</p>` : ''}
      ${splitBar(segParts, l.found.all)}
      ${legend(segParts, l.found.all)}
    </section>`;
}
