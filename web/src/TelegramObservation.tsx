import { useCallback, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api, companyPath, fullDate } from './api'
import type { Company } from './api'
import { ErrorMessage, Loading, PageHeader, formAction, useAction, useData } from './ui'

const statuses = {
  pending: 'Ожидает проверки',
  active: 'Подключено',
  disconnected: 'Нет подключения',
  disabled: 'Отключено',
  reactions_only: 'Доступны только реакции',
}
type Status = keyof typeof statuses
type Observation = {
  analytics_enabled: boolean; comments_enabled: boolean
  channel_id: string | null; discussion_chat_id: string | null
  comments_status: Status; analytics_status: Status
  last_stats_sync: string | null; last_reaction_at: string | null; last_comment_at: string | null
  last_error: string | null
}

export function TelegramObservation({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/telegram-observation'
  const load = useCallback(() => api<Observation>(path), [path])
  const observation = useData(load)
  const action = useAction()
  const [saved, setSaved] = useState(false)
  const value = observation.data
  const writable = company.role === 'admin'
  const formatted = (date: string | null) => date ? fullDate(date, company.timezone) : 'Нет данных'

  return <>
    <PageHeader title="Наблюдение за Telegram" action={writable && value && <button className="button secondary"
      disabled={action.busy || !(value.analytics_enabled || value.comments_enabled)} onClick={() => void action.run(async () => {
        setSaved(false)
        observation.setData(await api<Observation>(path + '/sync', 'POST'))
      })}><RefreshCw size={16} />Проверить подключение</button>} />
    <ErrorMessage message={observation.error} retry={observation.retry} />
    {!value && !observation.error && <Loading />}
    {value && <section className="surface modal-body usage form-stack">
      <dl>
        <div><dt>Канал</dt><dd>{value.channel_id || 'Не указан'}</dd></div>
        <div><dt>Группа обсуждений</dt><dd>{value.discussion_chat_id || 'Не подключена'}</dd></div>
        <div><dt>Статистика</dt><dd>{statuses[value.analytics_status]}</dd></div>
        <div><dt>Комментарии</dt><dd>{statuses[value.comments_status]}</dd></div>
        <div><dt>Последний замер статистики</dt><dd>{formatted(value.last_stats_sync)}</dd></div>
        <div><dt>Последняя реакция</dt><dd>{formatted(value.last_reaction_at)}</dd></div>
        <div><dt>Последний комментарий</dt><dd>{formatted(value.last_comment_at)}</dd></div>
      </dl>
      {value.analytics_status === 'reactions_only' && <p className="muted">Для просмотров, репостов и счётчика комментариев нужно подключить полный сбор статистики на сервере.</p>}
      {value.last_error && <ErrorMessage message={value.last_error} />}
      {writable && <form className="form-stack"
        onChange={() => setSaved(false)} onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
          setSaved(false)
          observation.setData(await api<Observation>(path, 'PATCH', {
            analytics_enabled: form.has('analytics_enabled'),
            comments_enabled: form.has('comments_enabled'),
          }))
          setSaved(true)
        }))}>
        <fieldset className="form-stack" disabled={action.busy}
          key={`${value.analytics_enabled}/${value.comments_enabled}`}>
          <label className="toggle-row"><input type="checkbox" name="analytics_enabled" defaultChecked={value.analytics_enabled} />Собирать замеры публикаций</label>
          <label className="toggle-row"><input type="checkbox" name="comments_enabled" defaultChecked={value.comments_enabled} />Передавать комментарии в «Обращения»</label>
        </fieldset>
        <small className="muted">Для комментариев добавьте бота в связанную с каналом группу обсуждений. Ответить можно в Telegram по ссылке из обращения.</small>
        <div className="button-group">
          <button className="button primary" disabled={action.busy}>Сохранить</button>
          {saved && <span className="saved" role="status">Сохранено</span>}
        </div>
      </form>}
      <ErrorMessage message={action.error} />
    </section>}
  </>
}
