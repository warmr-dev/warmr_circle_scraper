// The side panels: one community, one lead. Both sections that list
// communities (monitoring, the database) open the same community panel, and
// the session actions of the old "Connector" tab live in it.

import { api } from './api.js';
import {
  $, ago, empty, extLink, html, num, openDrawer, pill, raw, render, toast, when,
} from './dom.js';
import {
  CONN_BUCKET, JOIN_STATUS, JOIN_TYPE, KIND, LEVEL, LEVEL_TONE, NOT_SENT, PLATFORM,
  PRIORITY, READ_OUTCOME, SEGMENT, SEGMENT_TONE, WATCH_MODE, WATCH_TIER, label,
  viniReason, watchStatus,
} from './labels.js';

function facts(rows) {
  return html`<dl class="facts">${rows.filter(Boolean).map(([k, v]) => html`
    <dt>${k}</dt><dd>${v === null || v === undefined || v === '' ? '—' : v}</dd>`)}</dl>`;
}

function segmentPill(seg, reason, notSent) {
  const text = label(SEGMENT, seg);
  const why = seg === 'not_sent' ? label(NOT_SENT, notSent) : viniReason(reason);
  return html`${pill(text, SEGMENT_TONE[seg] || 'neutral', reason || '')}${why ? html` <span class="muted small" title="${reason || ''}">${why}</span>` : ''}`;
}

export function segmentCell(row) {
  return segmentPill(row.segment, row.vini_reason, row.not_sent_reason);
}

// --- community -------------------------------------------------------------------

function sessionForm(host) {
  return html`
    <form class="stack" data-session-form>
      <label class="field">Экспорт кук (JSON-массив)
        <textarea name="cookies" rows="3" placeholder='[{"name":"_circle_session","value":"…"}, …]'></textarea>
      </label>
      <p class="muted small">или два значения по отдельности:</p>
      <div class="row">
        <input name="session_cookie" placeholder="_circle_session" autocomplete="off">
        <input name="user_session_identifier" placeholder="user_session_identifier" autocomplete="off">
      </div>
      <div class="row"><button class="btn" type="submit">Сохранить сессию для ${host}</button></div>
    </form>`;
}

// Which of our accounts is the member: the one whose cookies can be
// refreshed here. Free text, with the join bot's own keys suggested.
function accountForm(account, sessionLabel) {
  const note = sessionLabel && sessionLabel !== account
    ? html`<div class="muted small">в сессии записано: ${sessionLabel}</div>` : '';
  return html`
    <form class="row" data-account-form>
      <input name="account" value="${account || ''}" placeholder="не записан" maxlength="32" list="accounts" autocomplete="off">
      <datalist id="accounts"><option value="main"><option value="test"><option value="4"><option value="client"></datalist>
      <button class="btn btn-ghost" type="submit">Сохранить</button>
    </form>${note}
    <div class="muted small">Под этим аккаунтом обновлять куки: другой аккаунт здесь не участник.</div>`;
}

function communityView(d) {
  const c = d.community;
  const host = c.host || '';
  const watch = d.watch;
  const conn = d.conn;
  const session = d.session;
  const pills = [
    d.fit ? pill('подходит (ICP)', 'good') : pill(c.icp_flag ? 'ICP, но вне воронки' : 'не подходит', 'muted'),
    pill(label(JOIN_TYPE, c.join_type), 'neutral', c.join_type_detail || ''),
    pill(label(JOIN_STATUS, c.join_status), c.join_status === 'joined' ? 'good' : 'neutral'),
    watch ? pill(`опрос: ${label(WATCH_MODE, watch.mode)}`, watch.mode === 'off' ? 'muted' : 'neutral') : '',
  ];
  return html`
    <div class="stack">
      <div class="row wrap">${pills}</div>
      <p class="muted">${c.url ? extLink(c.url, c.url) : ''}</p>
      ${c.description ? html`<p class="clamp" title="${c.description}">${c.description}</p>` : ''}

      <h3>Карточка</h3>
      ${facts([
        ['Платформа', label(PLATFORM, c.platform)],
        ['Найдено', html`${when(c.discovered_at)} · ${c.discovery_source || '—'}`],
        ['ICP', html`${c.icp_score === null ? '—' : Math.round(c.icp_score)} · ${c.icp_decided_by || '—'} · проверено ${ago(c.icp_checked_at)}`],
        ['Тип входа', html`${label(JOIN_TYPE, c.join_type)}${c.price_label ? ` · ${c.price_label}` : ''}<div class="muted small">${c.join_type_detail || ''}</div>`],
        ['Вступление', html`${label(JOIN_STATUS, c.join_status)} · попыток ${num(c.join_attempts || 0)}${c.joined_at ? ` · вступили ${when(c.joined_at)}` : ''}<div class="muted small">${c.join_status_detail || ''}</div>`],
        ['Аккаунт', accountForm(c.join_account, session?.member_label)],
        ['Чтение', html`${label(READ_OUTCOME, c.read_outcome)} · последнее ${ago(c.last_synced_at)}`],
        ['Заметки', c.notes],
      ])}

      <h3>Опрос ленты</h3>
      ${watch ? facts([
        ['Режим', html`${label(WATCH_MODE, watch.mode)} · ${label(WATCH_TIER, watch.tier)}`],
        ['Последний ответ', html`${watchStatus(watch.last_status, watch.mode, conn?.bucket === 'working' && !!session)} · ${ago(watch.last_checked_at)}${watch.consecutive_errors ? html` · <b>${watch.consecutive_errors} ошибок подряд</b>` : ''}<div class="muted small">${watch.last_detail || ''}</div>`],
        ['Следующая проверка', when(watch.next_check_at)],
        ['Новые посты', html`последний ${ago(watch.last_new_at)} · всего увидено ${num(watch.posts_seen)}`],
      ]) : empty('Это сообщество не опрашивается.')}

      <h3>Сессия</h3>
      ${conn || session ? facts([
        conn ? ['Состояние', html`${pill(label(CONN_BUCKET, conn.bucket), conn.bucket === 'working' ? 'good' : 'warning')}<div class="muted small">${conn.detail || ''}</div>`] : null,
        conn ? ['Приоритет', html`<select data-priority>${Object.entries(PRIORITY).map(([v, t]) => html`<option value="${v}" ${v === conn.priority ? raw('selected') : ''}>${t}</option>`)}</select>`] : null,
        conn ? ['Разделы', `${num(conn.spaces_readable)} из ${num(conn.spaces_total)} читаются · прочитано ${ago(conn.last_sync_at)}`] : null,
        session ? ['Куки', html`${num(session.cookie_count)} шт. · ${session.source} · ${session.member_label || '—'} · сохранены ${when(session.created_at)}${session.plaintext ? html` · ${pill('без шифрования', 'warning')}` : ''}`] : null,
        conn?.notes ? ['Заметка', conn.notes] : null,
      ]) : empty('Сессии нет.')}
      ${host ? html`
        <div class="row wrap">
          ${session ? html`<button class="btn" data-scan>Прочитать сейчас</button>` : ''}
          ${session ? html`<button class="btn btn-ghost" data-clear>Удалить сессию</button>` : ''}
        </div>
        <details><summary>Вставить сессию вручную</summary>${sessionForm(host)}</details>` : ''}

      <h3>Лиды (${num(d.leads.length)}${d.leads.length === 50 ? '+' : ''})</h3>
      ${d.leads.length ? html`<ul class="list">${d.leads.map((l) => html`
        <li><a href="#" data-lead="${l.id}">${l.job_title || l.snippet || `лид #${l.id}`}</a>
          <div class="small">${when(l.created_at)} · ${l.classification === 'LEAD' ? 'лид' : 'не лид'} · ${segmentPill(l.segment, l.vini_reason, l.not_sent_reason)}</div></li>`)}</ul>`
        : empty('Лидов нет.')}

      <h3>Журнал</h3>
      ${d.activity.length ? html`<ul class="list">${d.activity.map((a) => html`
        <li><span class="muted small">${when(a.created_at)} · ${label(KIND, a.kind)}</span>
          ${pill(label(LEVEL, a.level), LEVEL_TONE[a.level] || 'neutral')} ${a.summary}</li>`)}</ul>`
        : empty('Записей нет.')}

      ${d.form_fills.length ? html`<h3>Анкета вступления</h3><ul class="list">${d.form_fills.map((f) => html`
        <li>${f.label} — ${f.outcome === 'needs_human' ? pill('нужен человек', 'warning') : pill('отвечено', 'good')} <span class="muted small">${f.account} · ${when(f.created_at)}</span></li>`)}</ul>` : ''}

      <details><summary>Все поля</summary><pre class="json">${JSON.stringify(c, null, 2)}</pre></details>
    </div>`;
}

export async function openCommunity(id, { onClose } = {}) {
  const body = openDrawer('Сообщество', empty('Загрузка…'), { onClose });
  let d;
  try {
    d = await api(`/api/dash/communities/${id}`);
  } catch (err) {
    render(body, empty(err.message));
    return;
  }
  render($('#drawer-title'), d.community.name || d.community.slug);
  render(body, communityView(d));
  wireCommunity(body, d, id);
}

function wireCommunity(body, d, id) {
  const host = d.community.host;
  const reload = () => openCommunity(id);
  body.addEventListener('click', (e) => {
    const lead = e.target.closest('[data-lead]');
    if (lead) {
      e.preventDefault();
      openLead(Number(lead.dataset.lead));
    }
  });
  $('[data-scan]', body)?.addEventListener('click', async () => {
    try {
      await api(`/api/connections/${encodeURIComponent(host)}/scan`, { method: 'POST', body: {} });
      toast('Чтение поставлено в очередь воркера');
    } catch (err) { toast(err.message, 'error'); }
  });
  $('[data-clear]', body)?.addEventListener('click', async () => {
    if (!confirm(`Удалить сохранённую сессию ${host}?`)) return;
    try {
      await api(`/api/connections/${encodeURIComponent(host)}/session/clear`, { method: 'POST', body: {} });
      toast('Сессия удалена');
      reload();
    } catch (err) { toast(err.message, 'error'); }
  });
  $('[data-priority]', body)?.addEventListener('change', async (e) => {
    try {
      await api(`/api/connections/${encodeURIComponent(host)}/priority`, { method: 'POST', body: { priority: e.target.value } });
      toast('Приоритет сохранён');
    } catch (err) { toast(err.message, 'error'); }
  });
  $('[data-account-form]', body)?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const account = new FormData(e.target).get('account');
    try {
      await api(`/api/dash/communities/${id}/account`, { method: 'POST', body: { account } });
      toast('Аккаунт сохранён');
      reload();
    } catch (err) { toast(err.message, 'error'); }
  });
  $('[data-session-form]', body)?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    const payload = Object.fromEntries([...form.entries()].filter(([, v]) => String(v).trim()));
    try {
      const res = await api(`/api/connections/${encodeURIComponent(host)}/session`, { method: 'POST', body: payload });
      toast(res.missing?.length ? `Сохранено, но не хватает: ${res.missing.join(', ')}` : 'Сессия сохранена', res.missing?.length ? 'warn' : 'ok');
      reload();
    } catch (err) { toast(err.message, 'error'); }
  });
}

// --- lead -----------------------------------------------------------------------------

function leadView(l) {
  const described = l.score_breakdown?.described;
  return html`
    <div class="stack">
      <div class="row wrap">
        ${pill(l.classification === 'LEAD' ? 'лид' : 'не лид', l.classification === 'LEAD' ? 'good' : 'muted')}
        ${l.priority ? pill(l.priority, 'neutral') : ''}
        ${pill(`оценка ${num(l.lead_score)}`, 'neutral')}
        ${segmentPill(l.segment, l.vini_reason, l.not_sent_reason)}
      </div>

      <h3>Vini</h3>
      ${facts([
        ['Ответ', l.vini_status ? label(SEGMENT, l.vini_status) : (l.external_synced_at ? 'отправлен до того, как ответы стали сохраняться' : 'не отправлялся')],
        ['Причина', l.vini_reason ? html`${viniReason(l.vini_reason)} <span class="muted small">${l.vini_reason}</span>` : null],
        ['Номер у Vini', l.vini_ref],
        ['Попыток', l.vini_attempts ? `${num(l.vini_attempts)} · последняя ${when(l.vini_last_attempt_at)}` : null],
        ['Отмечен отправленным', when(l.external_synced_at)],
      ])}

      <h3>Наш вердикт</h3>
      ${facts([
        ['Кто решил', `${l.decided_by || '—'} · уверенность ${l.confidence === null ? '—' : Math.round(l.confidence * 100)}%`],
        ['Почему', l.reason],
        ['Цитата', l.evidence_quote ? html`<blockquote>${l.evidence_quote}</blockquote>` : null],
        ['Разбор', described ? Object.entries(described).map(([k, v]) => `${k}: ${v}`).join(' · ') : null],
        ['Роль / навыки', [l.job_title, (l.skills || []).join(', ')].filter(Boolean).join(' · ')],
        ['Компания / бюджет', [l.company, l.budget, l.location, l.urgency && `срочность: ${l.urgency}`].filter(Boolean).join(' · ')],
        ['Версия', l.classifier_version],
      ])}

      <h3>Пост</h3>
      ${facts([
        ['Автор', l.author],
        ['Сообщество', html`<a href="#" data-community="${l.community.id}">${l.community.name}</a>`],
        ['Опубликован', when(l.published_at)],
        ['Найден', when(l.created_at)],
        ['Ссылка', extLink(l.post_url, l.post_url)],
      ])}
      <div class="post">${l.content || ''}</div>
      ${l.duplicates?.length ? html`<p class="muted small">Дубли этого лида: ${l.duplicates.map((x) => html`<a href="#" data-lead="${x.id}">#${x.id}</a> `)}</p>` : ''}
      ${l.duplicate_of_id ? html`<p class="muted small">Это дубль лида <a href="#" data-lead="${l.duplicate_of_id}">#${l.duplicate_of_id}</a></p>` : ''}
    </div>`;
}

export async function openLead(id, { onClose } = {}) {
  const body = openDrawer(`Лид #${id}`, empty('Загрузка…'), { onClose });
  let l;
  try {
    l = await api(`/api/dash/leads/${id}`);
  } catch (err) {
    render(body, empty(err.message));
    return;
  }
  render($('#drawer-title'), l.job_title || l.title || `Лид #${l.id}`);
  render(body, leadView(l));
  body.addEventListener('click', (e) => {
    const c = e.target.closest('[data-community]');
    const other = e.target.closest('[data-lead]');
    if (c) { e.preventDefault(); openCommunity(Number(c.dataset.community)); }
    if (other) { e.preventDefault(); openLead(Number(other.dataset.lead)); }
  });
}
