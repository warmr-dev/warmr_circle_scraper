// Every stored word, as the page says it. An unknown word is shown as is,
// so a new status on the server is visible before anyone names it here.

export const label = (map, key) => (key === null || key === undefined || key === ''
  ? '—' : (map[key] ?? key));

export const JOIN_STATUS = {
  not_attempted: 'не пробовали',
  queued: 'в очереди',
  pending_approval: 'ждёт одобрения',
  joined: 'вступили',
  paid_skip: 'платное — пропуск',
  invite_skip: 'по приглашению — пропуск',
  subscription_expired_skip: 'у сообщества истекла подписка',
  failed: 'не удалось',
  dead_host: 'адрес мёртв',
  profile_pending: 'профиль не заполнен',
  external_login: 'вход через свой сайт',
  needs_human: 'нужен человек',
};

export const JOIN_TYPE = {
  free_join: 'бесплатно',
  paid: 'платно',
  invite_only: 'по приглашению',
  locked_unknown: 'закрыто, неясно как',
  unknown: 'неизвестно',
  subscription_expired: 'подписка истекла',
};

export const READ_OUTCOME = {
  public: 'читается открыто',
  private: 'только участникам',
  gone: 'удалено',
  error: 'ошибка чтения',
};

export const PLATFORM = {
  circle: 'Circle',
  discover: 'каталог Circle',
  other: 'не Circle',
  circle_infra: 'служебный Circle',
  facebook: 'Facebook',
};

export const SOURCE = { directory: 'каталог Circle', dns: 'DNS-выгрузка', other: 'другое' };

export const WATCH_MODE = { anon: 'анонимно', cookie: 'по сессии', off: 'выключен' };
export const WATCH_TIER = { fast: 'каждые 2 мин', slow: 'каждые 15 мин' };

const WATCH_STATUS_WORDS = {
  ok: 'ок',
  not_modified: 'без изменений',
  unauthorized: 'нет доступа (401)',
  ratelimited: 'лимит (429)',
  challenge: 'Cloudflare',
  notfound: 'не найдено (404)',
  error: 'ошибка',
};

export function watchStatus(status) {
  if (!status) return 'ещё не проверяли';
  if (status.startsWith('http_')) return `HTTP ${status.slice(5)}`;
  return WATCH_STATUS_WORDS[status] ?? status;
}

export const CONN_BUCKET = {
  working: 'сессия работает',
  cloudflare_blocked: 'Cloudflare не пускает сервер',
  session_expired: 'сессия истекла',
  error: 'ошибка чтения',
  not_connected: 'не подключено',
};

export const PRIORITY = { vip: 'VIP', normal: 'обычный', low: 'низкий', paused: 'на паузе' };

export const SEGMENT = {
  accepted: 'принят',
  duplicate: 'уже был у Vini',
  held: 'запаркован',
  rejected: 'отклонён',
  error: 'не дошёл',
  not_sent: 'не отправлен',
  no_data: 'нет данных',
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
  not_lead: 'не лид — наш вердикт',
  duplicate_of: 'дубль нашего лида',
  held_for_review: 'придержан: модель не дала вердикт',
  no_author: 'у поста нет автора',
  pending: 'ждёт отправки',
};

// Vini's reason codes, in words. The raw code stays in the tooltip.
const VINI_WORDS = [
  ['missing_source_author_identity', 'нет ID автора'],
  ['missing_or_invalid_fetched_at', 'нет времени сбора поста'],
  ['missing_or_invalid_classified_at', 'нет времени оценки'],
  ['missing_root_message_identity', 'нет ID сообщения'],
  ['lead_duplicate', 'дубль у Vini'],
  ['historical_expired', 'пост старше 48 часов'],
  ['discarded', 'Vini отбросил'],
  ['no_item_result', 'Vini не ответил по лиду — засчитан как принят'],
];

export function viniReason(reason) {
  if (!reason) return '';
  const hit = VINI_WORDS.find(([code]) => reason.includes(code));
  return hit ? hit[1] : reason;
}

export const LEVEL = { info: 'инфо', success: 'успех', warning: 'предупреждение', error: 'ошибка' };
export const LEVEL_TONE = { info: 'neutral', success: 'good', warning: 'warning', error: 'critical' };

export const KIND = {
  read: 'чтение',
  triage: 'разбор',
  classify: 'оценка',
  ingest: 'сессии',
  export: 'Vini',
  join: 'вступление',
  discover: 'поиск',
  harvest: 'сбор',
  review: 'действия',
};

export const SEVERITY = { crit: 'критично', warn: 'внимание', info: 'к сведению' };
export const SEVERITY_TONE = { crit: 'critical', warn: 'warning', info: 'neutral' };

export const SCHEDULE = {
  off: 'выключено',
  every_run: 'каждый проход воркера',
  every_5min: 'каждые 5 мин',
  every_15min: 'каждые 15 мин',
  hourly: 'каждый час',
  every_6h: 'каждые 6 ч',
  every_12h: 'каждые 12 ч',
  twice_daily: 'дважды в день',
  daily: 'раз в день',
  weekly: 'раз в неделю',
};

export const STAGE = {
  harvest: 'Сбор (чтение известных сообществ)',
  discovery: 'Поиск новых сообществ',
  icp_classification: 'ICP и обогащение',
  join_type: 'Проверка типа входа',
};

export const JOB_STATE = { queued: 'в очереди', running: 'выполняется', done: 'готово', error: 'ошибка' };
export const JOB_STATE_TONE = { queued: 'neutral', running: 'warning', done: 'good', error: 'critical' };
