// Section 6: the schedules and lead rules the worker reads (editable), and how
// the services are doing (read-only). A save changes only what the form shows
// and starts nothing.

import { api } from '../api.js';
import { $, ago, empty, html, num, pill, render, toast, when } from '../dom.js';
import { JOB_STATE, JOB_STATE_TONE, SCHEDULE, STAGE, STAGE_NOTE, label } from '../labels.js';

export const title = 'Config';
export const subtitle = 'Schedules and lead rules the worker reads, and how the services are doing.';

let ctx = null;

const NUMBER_FIELDS = [
  ['minimum_confidence', 'Minimum model confidence (0–1)', 0.05],
  ['max_post_age_days', 'Max post age, days (0 = no limit)', 1],
  ['llm_escalation_threshold', 'Rules threshold for asking the model (leads)', 1],
  ['icp_escalation_threshold', 'Rules threshold for asking the model (ICP)', 1],
  ['max_search_niches', 'Niches to search per run (0 = all)', 1],
  ['max_expanded_niches', 'Max niches after model expansion', 1],
];
const SCORING = [
  ['hiring_intent', 'Hiring intent'],
  ['target_role_match', 'Role match'],
  ['target_skill_match', 'Skill match'],
  ['budget_mentioned', 'Budget mentioned'],
  ['company_identified', 'Company named'],
  ['recent_post', 'Recent post'],
  ['recency_days', 'Recent post: max age, days'],
];

export function mount(context) {
  ctx = context;
  render(ctx.body, html`
    <div class="grid-2">
      <section class="panel" data-schedules>${empty('Loading…')}</section>
      <section class="panel" data-actions>
        <h2>Actions</h2>
        <p class="muted small">Each button queues a job for the worker; it picks the job up as soon as it is free.</p>
        <div class="row wrap">
          <button class="btn" data-job="harvest">Run harvest now</button>
          <button class="btn btn-ghost" data-job="scan_all">Scan all sessions</button>
        </div>
      </section>
    </div>
    <section class="panel" data-services>${empty('Loading…')}</section>
    <section class="panel" data-rules>${empty('Loading…')}</section>
    <section class="panel" data-jobs></section>`);
  ctx.body.addEventListener('click', async (e) => {
    const job = e.target.closest('[data-job]');
    if (!job) return;
    try {
      const res = await api('/api/dash/jobs', { method: 'POST', body: { kind: job.dataset.job } });
      toast(`Job #${res.job_id} queued`);
      loadRuntime();
    } catch (err) { toast(err.message, 'error'); }
  });
}

export async function update() {
  await refresh();
}

export async function refresh() {
  await Promise.all([loadSchedules(), loadRules(), loadRuntime()]);
  ctx.setUpdated();
}

// --- schedules ---------------------------------------------------------------------

async function loadSchedules() {
  const data = await api('/api/dash/schedule');
  const host = $('[data-schedules]', ctx.body);
  render(host, html`
    <h2>Schedules</h2>
    <form class="stack" data-schedule-form>
      ${Object.entries(data).map(([key, s]) => html`
        <label class="field">${STAGE[key] || key}
          <select name="${key}">
            ${s.options.map((o) => html`<option value="${o}" ${o === s.value ? 'selected' : ''}>${label(SCHEDULE, o)}</option>`)}
            ${s.options.includes(s.value) ? '' : html`<option value="${s.value}" selected>${s.value}</option>`}
          </select>
          ${STAGE_NOTE[key] ? html`<span class="muted small">${STAGE_NOTE[key]}</span>` : ''}
        </label>`)}
      <div class="row"><button class="btn" type="submit">Save schedules</button></div>
    </form>`);
  $('[data-schedule-form]', host).addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    const changed = Object.fromEntries(Object.entries(body).filter(([k, v]) => data[k]?.value !== v));
    if (!Object.keys(changed).length) { toast('Nothing changed'); return; }
    try {
      await api('/api/dash/schedule', { method: 'POST', body: changed });
      toast('Schedules saved');
      await loadSchedules();
      await loadRuntime();
    } catch (err) { toast(err.message, 'error'); }
  });
}

// --- lead rules --------------------------------------------------------------------

const lines = (list) => (list || []).join('\n');
const parseLines = (text) => text.split('\n').map((x) => x.trim()).filter(Boolean);

async function loadRules() {
  const { config } = await api('/api/dash/config');
  const host = $('[data-rules]', ctx.body);
  render(host, html`
    <h2>Lead rules</h2>
    <p class="muted small">Only the fields the worker actually reads. Saving changes only these and starts nothing; the worker picks up the changes within a minute.</p>
    <form class="stack" data-rules-form>
      <div class="grid-2">
        <label class="field">Target roles (one per line)
          <textarea name="target_roles" rows="8">${lines(config.target_roles)}</textarea></label>
        <label class="field">Target skills (one per line)
          <textarea name="target_skills" rows="8">${lines(config.target_skills)}</textarea></label>
      </div>
      <label class="field">Exclude words: a post with any of them is not a lead (one per line)
        <textarea name="keywords.exclude" rows="5">${lines(config.keywords?.exclude)}</textarea></label>
      <div class="grid-3">
        ${NUMBER_FIELDS.map(([name, text, step]) => html`
          <label class="field">${text}<input type="number" name="${name}" step="${step}" value="${config[name]}"></label>`)}
        <label class="check"><input type="checkbox" name="expand_search_with_ai" ${config.expand_search_with_ai ? 'checked' : ''}> expand the niche search with the model</label>
      </div>
      <h3>Lead scoring</h3>
      <div class="grid-4">
        ${SCORING.map(([name, text]) => html`
          <label class="field">${text}<input type="number" name="scoring.${name}" step="1" value="${config.scoring?.[name]}"></label>`)}
        <label class="field">High priority: min score<input type="number" name="priority_thresholds.high" value="${config.priority_thresholds?.high}"></label>
        <label class="field">Medium priority: min score<input type="number" name="priority_thresholds.medium" value="${config.priority_thresholds?.medium}"></label>
      </div>
      <div class="row"><button class="btn" type="submit">Save rules</button></div>
    </form>`);
  $('[data-rules-form]', host).addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const payload = {
      target_roles: parseLines(form.elements.target_roles.value),
      target_skills: parseLines(form.elements.target_skills.value),
      keywords: { exclude: parseLines(form.elements['keywords.exclude'].value) },
      expand_search_with_ai: form.elements.expand_search_with_ai.checked,
      scoring: {},
      priority_thresholds: {},
    };
    for (const [name] of NUMBER_FIELDS) payload[name] = Number(form.elements[name].value);
    for (const [name] of SCORING) payload.scoring[name] = Number(form.elements[`scoring.${name}`].value);
    for (const name of ['high', 'medium']) payload.priority_thresholds[name] = Number(form.elements[`priority_thresholds.${name}`].value);
    try {
      const res = await api('/api/dash/config', { method: 'POST', body: payload });
      toast(res.changed.length ? `Saved: ${res.changed.join(', ')}` : 'Nothing changed');
    } catch (err) { toast(err.message, 'error'); }
  });
}

// --- services, stages, jobs ---------------------------------------------------------

function heartbeat(name, hb) {
  const tone = hb.state === 'ok' ? 'good' : 'critical';
  const text = hb.state === 'ok' ? `alive · ${ago(hb.at)}` : hb.state === 'never' ? 'never reported' : `silent · ${ago(hb.at)}`;
  return html`<div class="kpi ${hb.state === 'ok' ? '' : 'kpi-warn'}"><b>${name}</b><span>${pill(text, tone)}</span></div>`;
}

function snapshot(name, snap) {
  if (!snap) return html`<div class="card"><h3>${name}</h3>${empty('No snapshot from this service yet: it runs an old version or is not running.')}</div>`;
  const env = snap.env || {};
  const gov = snap.governor || {};
  const stage = snap.stage?.stage;
  return html`
    <div class="card">
      <h3>${name}</h3>
      <dl class="facts">
        <dt>Host</dt><dd>${snap.host} · pid ${snap.pid}</dd>
        <dt>Code version</dt><dd>${snap.code_version}</dd>
        <dt>Started</dt><dd>${when(snap.started_at)} · snapshot ${ago(snap.beat_at)}</dd>
        <dt>Now</dt><dd>${stage ? html`${label(STAGE, stage)} <span class="muted small">since ${when(snap.stage.since)}</span>` : 'idle'}</dd>
        <dt>Model</dt><dd>${snap.llm?.backend ? `${snap.llm.backend} · ${snap.llm.model || '—'}${snap.llm.base_host ? ` · ${snap.llm.base_host}` : ''}` : pill('no model', 'warning')}</dd>
        <dt>Circle limit</dt><dd>${gov.enabled === false ? 'off' : gov.limits
          ? html`${num(gov.usedLastHour)} of ${num(gov.limits.maxPerHour)} requests in the last hour · ${num(gov.limits.requestsPerMinute)}/min${gov.cooldownUntil ? html` · ${pill('paused', 'warning', gov.cooldownReason || '')}` : ''}`
          : '—'}</dd>
        <dt>Keys</dt><dd class="keys">${Object.entries(env).map(([k, v]) => pill(k, v ? 'good' : 'muted', v ? 'set' : 'not set'))}</dd>
      </dl>
    </div>`;
}

// The harvest's own summary, in words; any other stage's result as it came.
function stageResult(key, result) {
  if (!result) return '';
  if (key === 'harvest') {
    return html`leads with a session: <b>${num(result.private_leads ?? 0)}</b> · from open spaces: <b>${num(result.public_leads ?? 0)}</b>
      <div class="muted">open communities read: ${num(result.communities_read ?? 0)} · new ones found: ${num(result.new_communities ?? 0)}${result.searched ? ' · discovery ran' : ''}</div>`;
  }
  return html`<code class="compact">${JSON.stringify(result)}</code>`;
}

function stageRow(key, s) {
  const err = s.last_error;
  const failedLast = err && (!s.last_finish || new Date(err.at) > new Date(s.last_finish));
  return html`
    <tr>
      <td>${STAGE[key] || key}${STAGE_NOTE[key] ? html`<div class="muted small">${STAGE_NOTE[key]}</div>` : ''}</td>
      <td>${label(SCHEDULE, s.schedule)}</td>
      <td class="small">${s.last_run ? html`${when(s.last_run)}<div class="muted">${ago(s.last_run)}</div>` : '—'}</td>
      <td class="small">${s.last_finish ? html`${when(s.last_finish)}<div class="muted">${ago(s.last_finish)}</div>` : '—'}</td>
      <td class="small">${err ? html`${pill(failedLast ? 'failed' : 'had an error', failedLast ? 'critical' : 'muted')} <span class="muted">${when(err.at)}</span><div class="detail">${err.error}</div>` : '—'}</td>
      <td class="small">${stageResult(key, s.last_result)}${key === 'icp_classification' && s.enrichment_moved_at ? html`<div class="muted">enrichment: cursor ${s.enrichment_cursor}, moved ${ago(s.enrichment_moved_at)}</div>` : ''}</td>
    </tr>`;
}

async function loadRuntime() {
  const r = await api('/api/dash/runtime');
  render($('[data-services]', ctx.body), html`
    <h2>Services</h2>
    <div class="kpis">
      ${heartbeat('Worker', r.heartbeats.worker)}
      ${heartbeat('Watcher', r.heartbeats.watcher)}
      <div class="kpi"><b>Dashboard</b><span class="muted small">${r.dashboard.commit || 'local'}${r.dashboard.region ? ` · ${r.dashboard.region}` : ''}</span></div>
    </div>
    <div class="grid-2">${snapshot('Worker', r.runtime.worker)}${snapshot('Watcher', r.runtime.watcher)}</div>
    <h3>Worker stages</h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Stage</th><th>Schedule</th><th>Last run</th><th>Finished</th><th>Error</th><th>Result</th></tr></thead>
      <tbody>${Object.entries(r.stages).map(([k, s]) => stageRow(k, s))}</tbody>
    </table></div>`);
  render($('[data-jobs]', ctx.body), html`
    <h2>Recent worker jobs</h2>
    ${r.jobs.length ? html`<div class="table-wrap"><table class="table">
      <thead><tr><th>#</th><th>Job</th><th>State</th><th>Created</th><th>Finished</th><th>Result</th></tr></thead>
      <tbody>${r.jobs.map((j) => html`<tr>
        <td class="small">${j.id}</td><td>${j.kind}${j.host ? html` <span class="muted small">${j.host}</span>` : ''}</td>
        <td>${pill(label(JOB_STATE, j.state), JOB_STATE_TONE[j.state] || 'neutral')}</td>
        <td class="small">${when(j.created_at)}</td><td class="small">${when(j.finished_at)}</td>
        <td class="small detail">${j.detail || ''}</td></tr>`)}</tbody>
    </table></div>` : empty('No jobs yet.')}`);
}
