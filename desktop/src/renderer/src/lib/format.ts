import type { StageName } from '@shared/types'

export const STAGE_TITLES: Record<StageName, string> = {
  discover: 'Поиск',
  verify: 'Проверка и ICP',
  join: 'Вступление',
  scrape: 'Чтение постов'
}

export const STAGE_HINTS: Record<StageName, string> = {
  discover: 'Каталог discover.circle.so и ваши списки. Для подходящих карточек ищет настоящий адрес сообщества.',
  verify: 'Один запрос к API Circle доказывает, что сообщество существует и как в него вступить. Затем LLM решает, подходит ли оно вам.',
  join: 'Вступает в подходящие бесплатные сообщества в вашем браузере ego lite. Не больше заданного числа визитов в день.',
  scrape: 'Читает все посты и комментарии сообществ, где есть ваша сессия, и публичные разделы подходящих сообществ.'
}

export const EXISTS_LABELS: Record<string, string> = {
  alive: 'существует',
  locked_or_absent: 'закрыто или нет',
  unreachable: 'не отвечает',
  not_circle: 'не Circle'
}

export const JOIN_TYPE_LABELS: Record<string, string> = {
  free_join: 'бесплатно',
  paid: 'платно',
  invite_only: 'по приглашению',
  locked_unknown: 'закрыто',
  unknown: 'неизвестно',
  subscription_expired: 'подписка владельца истекла'
}

export const JOIN_STATUS_LABELS: Record<string, string> = {
  not_attempted: 'не пробовали',
  joined: 'вступили',
  paid_skip: 'платное — пропуск',
  pending_approval: 'ждёт одобрения',
  invite_skip: 'только по приглашению',
  subscription_expired_skip: 'сообщество не оплачено',
  needs_login: 'нужен вход',
  challenge_stop: 'проверка Cloudflare',
  application_form_detected: 'анкета',
  unclear: 'непонятная страница',
  failed: 'ошибка'
}

export const PLATFORM_LABELS: Record<string, string> = {
  circle: 'Circle',
  discover: 'каталог (адрес не найден)',
  other: 'не Circle',
  circle_infra: 'служебный адрес Circle'
}

export function label(map: Record<string, string>, value: string | null | undefined, empty = '—'): string {
  if (!value) return empty
  return map[value] ?? value
}

const nf = new Intl.NumberFormat('ru-RU')
export function num(value: number | null | undefined): string {
  return value == null ? '—' : nf.format(value)
}

export function usd(value: number): string {
  return `$${value.toFixed(value < 1 ? 3 : 2)}`
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const future = diff < 0
  const s = Math.abs(diff) / 1000
  const phrase =
    s < 60
      ? 'меньше минуты'
      : s < 3600
        ? `${Math.round(s / 60)} мин`
        : s < 86400
          ? `${Math.round(s / 3600)} ч`
          : `${Math.round(s / 86400)} дн`
  return future ? `через ${phrase}` : `${phrase} назад`
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}
