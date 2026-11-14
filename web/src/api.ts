export type Role = 'super_admin' | 'admin' | 'editor' | 'approver'
export type FundingMode = 'own' | 'salavat' | 'shared'
export const fundingNames: Record<FundingMode, string> = {
  own: 'Ключи компании',
  salavat: 'Ключи Салаватстекло',
  shared: 'Общие ключи',
}
export type User = { id: number; name: string; email: string; role: Role; telegram_id?: string | null; active?: boolean; can_approve?: boolean }
export type Company = {
  id: number
  name: string
  role: Role
  can_approve?: boolean
  timezone: string
  calendar_enabled: boolean
  provider: 'openai' | 'yandex'
  model: string
  editorial_rules?: string
  channel_id: string
  chat_id: string
  bot_username: string
  yandex_folder_id: string
  monthly_limit: number
  requests_limit: number
  funding_mode?: FundingMode
}
export type Settings = Company & {
  webapp_url?: string
  openai_configured: boolean
  yandex_configured: boolean
  telegram_configured: boolean
  provider_credentials_editable?: boolean
}
export type Source = {
  id: number
  title: string
  text: string
  kind: string
  created_at: string
  file_url?: string | null
  mime_type?: string | null
}
export type Status = 'draft' | 'needs_info' | 'review' | 'approved' | 'publishing' | 'published' | 'rejected' | 'failed'
export type Post = {
  platform: 'telegram' | 'vk' | 'max'
  id: number
  title: string
  body: string
  kind: string
  status: Status
  version: number
  source_ids: number[]
  questions: string[]
  created_at: string
  scheduled_at: string | null
  channel_id: string
  template: string | null
  card_url?: string | null
  photo_url?: string | null
  logo_editable?: boolean
  logo_pending?: boolean
  logo?: { variant: string; corner: string; width: number } | null
  publication_url?: string | null
  publication_uncertain?: boolean
  feedback?: string | null
}
export type CalendarEvent = { id: number; title: string; event_date: string; description: string; source_id?: number | null; reminded_at?: string | null }
export type Audit = { id: number; action: string; detail: string; created_at: string }
export type Usage = { requests: number; input_tokens: number; output_tokens: number; monthly_limit: number; requests_limit: number }

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

export async function api<T>(path: string, method = 'GET', payload?: unknown): Promise<T> {
  const upload = payload instanceof FormData
  let response: Response
  try {
    response = await fetch('/api' + path, {
      method,
      headers: payload !== undefined && !upload ? { 'Content-Type': 'application/json' } : undefined,
      body: upload ? payload : JSON.stringify(payload),
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch {
    throw new ApiError('Нет связи с сервером. Проверьте, запущено ли приложение.', 0)
  }

  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => undefined)
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/auth/')) {
      window.dispatchEvent(new Event('editor-session-expired'))
    }
    const message = data?.detail
    throw new ApiError(typeof message === 'string' ? message : (
      response.status === 422 ? 'Проверьте заполнение полей.' : 'Не удалось выполнить действие.'
    ), response.status)
  }
  return data as T
}

export const companyPath = (companyId: number) => '/companies/' + companyId
export const errorText = (error: unknown) => error instanceof Error ? error.message : 'Не удалось выполнить действие.'

export const roles: Record<Role, string> = { super_admin: 'Главный администратор', admin: 'Администратор компании', editor: 'Редактор', approver: 'Согласующий' }
export const statuses: Record<Status, string> = {
  draft: 'Черновик', needs_info: 'Нужно уточнение', review: 'На согласовании', approved: 'Согласовано',
  publishing: 'Отправляется', published: 'Опубликовано', rejected: 'На доработке', failed: 'Ошибка отправки',
}
export const kinds: Record<string, string> = { general: 'Публикация', announcement: 'Анонс', person: 'Сотрудник', results: 'Итоги' }
export const platforms = { telegram: 'Telegram', vk: 'ВКонтакте', max: 'MAX' }
export const sourceKinds: Record<string, string> = { text: 'Заметка', document: 'Документ', image: 'Фото', video: 'Видео' }
export const shortDate = (value: string) => new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(value))
export const fullDate = (value: string, timeZone: string) => new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short', timeZone }).format(new Date(value))

export function localDateTime(value: string, timeZone: string) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(value))
  const field = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value
  return `${field('year')}-${field('month')}-${field('day')}T${field('hour')}:${field('minute')}`
}
