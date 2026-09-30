// Every stored word, as the page says it. An unknown word is shown as is,
// so a new status on the server is visible before anyone names it here.

export const label = (map, key) => (key === null || key === undefined || key === ''
  ? '—' : (map[key] ?? key));

export const JOIN_STATUS = {
  not_attempted: 'not attempted',
  queued: 'queued',
  pending_approval: 'pending approval',
  joined: 'joined',
  paid_skip: 'paid — skipped',
  invite_skip: 'invite only — skipped',
  subscription_expired_skip: 'community subscription expired',
  failed: 'failed',
  dead_host: 'dead address',
  profile_pending: 'profile not filled in',
  external_login: 'sign-in on its own site',
  needs_human: 'needs a human',
};

export const JOIN_TYPE = {
  free_join: 'free',
  paid: 'paid',
  invite_only: 'invite only',
  locked_unknown: 'closed, unclear how to join',
  unknown: 'unknown',
  subscription_expired: 'subscription expired',
};

export const READ_OUTCOME = {
  public: 'public',
  private: 'members only',
  gone: 'deleted',
  error: 'read error',
};

export const PLATFORM = {
  circle: 'Circle',
  discover: 'Circle directory',
  other: 'not Circle',
  circle_infra: 'Circle internal',
  facebook: 'Facebook',
};

export const SOURCE = { directory: 'Circle directory', dns: 'DNS dump', other: 'other' };

export const WATCH_MODE = { anon: 'anonymously', cookie: 'with a session', off: 'not read' };
export const WATCH_TIER = { fast: 'every 2 min', slow: 'every 15 min' };

const WATCH_STATUS_WORDS = {
  ok: 'OK',
  not_modified: 'unchanged',
  ratelimited: 'rate limited (429)',
  challenge: 'Cloudflare',
  notfound: 'not found (404)',
  moved: 'moved',
  tls_error: 'domain no longer served (SSL)',
  error: 'error',
};

// A 401 means different things. Read anonymously, the community is private:
// it shows nothing to a visitor without a login. Read with our session, the
// session stopped working -- unless the cookie scan still reads with it, and
// then it is the feed request that fails (seen 2026-09-29 on four hosts).
export function watchStatus(status, mode, sessionWorks = false) {
  if (!status) return 'not checked yet';
  if (status === 'unauthorized') {
    if (mode !== 'cookie') return 'private (401)';
    return sessionWorks ? 'feed refused (401), session alive' : 'session refused (401)';
  }
  if (status === 'http_301' || status === 'http_302') return `moved (${status.slice(5)})`;
  if (status.startsWith('http_')) return `HTTP ${status.slice(5)}`;
  return WATCH_STATUS_WORDS[status] ?? status;
}

export const CONN_BUCKET = {
  working: 'session works',
  cloudflare_blocked: 'Cloudflare blocks the server',
  session_expired: 'session expired',
  error: 'read error',
  not_connected: 'not connected',
};

export const PRIORITY = { vip: 'VIP', normal: 'normal', low: 'low', paused: 'paused' };

export const SEGMENT = {
  accepted: 'accepted',
  duplicate: 'already at Vini',
  held: 'held',
  rejected: 'rejected',
  error: 'not delivered',
  not_sent: 'not sent',
  no_data: 'no data',
};

export const SEGMENT_TONE = {
  accepted: 'good',
  duplicate: 'neutral',
  held: 'warning',
  rejected: 'critical',
  error: 'serious',
  not_sent: 'muted',
  no_data: 'muted',
};

export const NOT_SENT = {
  not_lead: 'not a lead — our verdict',
  duplicate_of: 'duplicate of our lead',
  held_for_review: 'held back — the model gave no verdict',
  no_author: 'post has no author',
  pending: 'waiting to be sent',
};

// Vini's reason codes, in words. The raw code stays in the tooltip.
const VINI_WORDS = [
  ['missing_source_author_identity', 'no author ID'],
  ['missing_or_invalid_fetched_at', 'no post fetch time'],
  ['missing_or_invalid_classified_at', 'no classification time'],
  ['missing_root_message_identity', 'no message ID'],
  ['lead_duplicate', 'duplicate at Vini'],
  ['historical_expired', 'post older than 48 hours'],
  ['discarded', 'discarded by Vini'],
  ['no_item_result', 'no answer from Vini for this lead — counted as accepted'],
];

export function viniReason(reason) {
  if (!reason) return '';
  const hit = VINI_WORDS.find(([code]) => reason.includes(code));
  return hit ? hit[1] : reason;
}

export const LEVEL = { info: 'info', success: 'success', warning: 'warning', error: 'error' };
export const LEVEL_TONE = { info: 'neutral', success: 'good', warning: 'warning', error: 'critical' };

export const KIND = {
  read: 'reading',
  triage: 'triage',
  classify: 'classification',
  ingest: 'sessions',
  export: 'Vini',
  join: 'join',
  discover: 'discovery',
  harvest: 'harvest',
  review: 'actions',
};

export const SEVERITY = { crit: 'critical', warn: 'warning', info: 'info' };
export const SEVERITY_TONE = { crit: 'critical', warn: 'warning', info: 'neutral' };

export const SCHEDULE = {
  off: 'off',
  every_run: 'every worker run',
  every_5min: 'every 5 min',
  every_15min: 'every 15 min',
  hourly: 'every hour',
  every_6h: 'every 6 h',
  every_12h: 'every 12 h',
  twice_daily: 'twice a day',
  daily: 'once a day',
  weekly: 'once a week',
};

export const STAGE = {
  harvest: 'Cookie scan and public spaces',
  discovery: 'Finding new communities',
  icp_classification: 'ICP and enrichment',
  join_type: 'Join type check',
};

// What each stage is for, now that the feed watcher reads new posts every 2
// or 15 minutes (the user asked on 2026-09-29 why the 6-hour pass exists).
export const STAGE_NOTE = {
  harvest: "With a session, reads everything a member sees: all spaces and comments — the watcher's feed does not see these. Reads new communities in full the first time. Re-reads public spaces just in case: the watcher already catches their new posts every 2–15 min.",
  discovery: 'Finds new communities; runs inside the cookie scan when it is due.',
  icp_classification: 'Fills in community details and decides whether a community is an ICP fit.',
  join_type: 'Finds out how to join: free, paid or invite only. Re-checks closed ICP-fit communities once a week.',
};

export const JOB_STATE = { queued: 'queued', running: 'running', done: 'done', error: 'error' };
export const JOB_STATE_TONE = { queued: 'neutral', running: 'warning', done: 'good', error: 'critical' };
