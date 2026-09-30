// The shell: the menu, the current section, the drawer from the address, the
// attention count beside the menu item, and a quiet refresh while the tab is
// in front.

import { api, takeReturnHash } from './api.js';
import { setBadge } from './badge.js';
import { $, $$, ago, html, initDrawer, render, toast } from './dom.js';
import { openCommunity, openLead } from './drawers.js';
import { emit, onRoute, rewrite, toObject } from './router.js';
import * as analytics from './sections/analytics.js';
import * as attention from './sections/attention.js';
import * as communities from './sections/communities.js';
import * as config from './sections/config.js';
import * as leads from './sections/leads.js';
import * as monitoring from './sections/monitoring.js';

const SECTIONS = { analytics, monitoring, leads, communities, attention, config };
const DEFAULT = 'analytics';
const REFRESH_MS = 60_000;
const BADGE_MS = 120_000;

let current = null;   // { name, module, ctx }
let openedFrom = '';  // the drawer param the current drawer was opened by

function makeContext(module) {
  const view = $('#view');
  render(view, html`
    <header class="page-head">
      <div>
        <h1>${module.title}</h1>
        <p class="muted">${module.subtitle}</p>
      </div>
      <div class="page-meta">
        <span class="muted" data-updated></span>
        <button class="btn btn-ghost" data-refresh title="Refresh">↻ Refresh</button>
      </div>
    </header>
    <div class="page-body"></div>`);
  const updated = $('[data-updated]', view);
  let stamp = null;
  const ctx = {
    body: $('.page-body', view),
    setUpdated(iso) {
      stamp = iso || new Date().toISOString();
      updated.textContent = `updated ${ago(stamp)}`;
    },
    tick() {
      if (stamp) updated.textContent = `updated ${ago(stamp)}`;
    },
  };
  $('[data-refresh]', view).addEventListener('click', () => module.refresh?.());
  return ctx;
}

function highlight(name) {
  for (const a of $$('nav a[data-section]')) {
    a.setAttribute('aria-current', a.dataset.section === name ? 'page' : 'false');
  }
}

async function show(route) {
  const name = SECTIONS[route.name] ? route.name : DEFAULT;
  const module = SECTIONS[name];
  if (!current || current.name !== name) {
    current = { name, module, ctx: makeContext(module) };
    highlight(name);
    document.title = `${module.title} — Warmr Circle`;
    try {
      module.mount(current.ctx);
    } catch (err) {
      toast(`Could not open the section: ${err.message}`, 'error');
    }
  }
  try {
    await module.update(route.params);
  } catch (err) {
    if (err.status !== 401) toast(err.message || String(err), 'error');
  }
  openFromAddress(name, route.params);
}

function openFromAddress(name, params) {
  const open = params.get('open') || '';
  if (!open || open === openedFrom) return;
  openedFrom = open;
  const onClose = () => {
    openedFrom = '';
    const rest = toObject(params);
    delete rest.open;
    rewrite(name, rest);
  };
  const [kind, id] = open.split(':');
  if (kind === 'c' && id) openCommunity(Number(id), { onClose });
  else if (kind === 'l' && id) openLead(Number(id), { onClose });
}

async function refreshBadge() {
  try {
    const data = await api('/api/dash/attention');
    setBadge(data);
  } catch { /* the section itself reports errors */ }
}

function boot() {
  initDrawer();
  $('#logout').addEventListener('click', async () => {
    await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
    location.href = '/login';
  });
  const back = takeReturnHash();
  if (back && (!location.hash || location.hash === '#')) history.replaceState(null, '', back);
  onRoute(show);
  emit();
  refreshBadge();
  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    current?.ctx.tick();
  }, 15_000);
  setInterval(() => {
    if (document.visibilityState !== 'visible' || !current?.module.autoRefresh) return;
    if (!document.body.classList.contains('drawer-open')) current.module.refresh?.();
  }, REFRESH_MS);
  setInterval(() => {
    if (document.visibilityState === 'visible') refreshBadge();
  }, BADGE_MS);
}

boot();
