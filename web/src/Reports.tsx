import { useCallback, useEffect, useState } from 'react'
import { FileText, Plus, Send } from 'lucide-react'
import { api, companyPath, fullDate, shortDate } from './api'
import type { Company } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

const weekdays = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
const deliveryLabels = {
  ready: 'Сохранён',
  pending: 'В очереди Telegram',
  sending: 'Отправляется',
  sent: 'Доставлен в Telegram',
  failed: 'Ошибка отправки',
  cancelled: 'Отправка отменена',
  uncertain: 'Доставка не подтверждена',
}

type Period = 1 | 7
type Report = { start_on: string; end_on: string; created_at: string; timezone: string; text: string }
type StoredReport = Report & {
  id: number
  schedule_id: number | null
  delivery_status: keyof typeof deliveryLabels
  delivery_error: string | null
  can_send: boolean
}
type Schedule = {
  id: number
  frequency: 'daily' | 'weekly'
  time: string
  weekday: number | null
  recipient_id: number
  notify: boolean
  enabled: boolean
  version: number
}
type ReportsData = {
  actor_id: number
  telegram_linked: boolean
  telegram_configured: boolean
  recipients: { id: number; name: string }[]
  schedules: Schedule[]
  reports: StoredReport[]
}

export function Reports({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/reports'
  const writable = company.role === 'admin' || company.role === 'editor'
  const load = useCallback(() => api<ReportsData>(path), [path])
  const page = useData(load)
  const action = useAction()
  const [days, setDays] = useState<Period>(7)
  const [preview, setPreview] = useState<Report>()
  const [editing, setEditing] = useState<Schedule | 'new'>()
  const [viewing, setViewing] = useState<number>()
  const hasPending = page.data?.reports.some((report) => ['pending', 'sending'].includes(report.delivery_status))

  useEffect(() => {
    setPreview(undefined)
    setViewing(undefined)
    setEditing(undefined)
    page.setData(undefined)
  }, [path, page.setData])

  useEffect(() => {
    if (!hasPending) return
    const interval = window.setInterval(page.retry, 5000)
    return () => window.clearInterval(interval)
  }, [hasPending, page.retry])

  const selection = page.data?.reports.find((report) => report.id === viewing)
  const previewReport = () => action.run(async () => {
    setPreview(await api<Report>(path + '/preview?days=' + days))
  })
  const saveReport = () => action.run(async () => {
    const report = await api<StoredReport>(path, 'POST', { days })
    setViewing(report.id)
    page.retry()
  })
  const send = (report: StoredReport) => action.run(async () => {
    await api(path + '/' + report.id + '/send', 'POST')
    page.retry()
  })

  return <>
    <PageHeader title="Отчёты" />
    <ErrorMessage message={page.error} retry={page.retry} />
    {!editing && !selection && <ErrorMessage message={action.error} />}
    {!page.data && !page.error && <Loading />}
    {page.data && <div className="form-stack">
      <section className="surface modal-body form-stack">
        <h2>Отчёт за период</h2>
        <div className="button-group">
          <label>Период
            <select value={days} disabled={action.busy} onChange={(event) => {
              setDays(Number(event.target.value) as Period)
              setPreview(undefined)
            }}>
              <option value={1}>Предыдущий день</option>
              <option value={7}>Последние семь полных дней</option>
            </select>
          </label>
          <button className="button secondary" disabled={action.busy} onClick={previewReport}>Предпросмотр</button>
          {writable && <button className="button primary" disabled={action.busy} onClick={saveReport}>Сохранить за период</button>}
        </div>
        {writable && <small className="muted">Предпросмотр показывает текущие данные. В истории хранится первый сохранённый вами отчёт за каждый период.</small>}
        {preview && <ReportText report={preview} />}
      </section>

      {writable && <section className="surface modal-body form-stack">
        <div className="button-group">
          <h2>Расписания</h2>
          <button className="button secondary" onClick={() => { action.setError(''); setEditing('new') }}>
            <Plus size={17} />Добавить расписание
          </button>
        </div>
        <small className="muted">Часовой пояс компании — {company.timezone}. Ежедневный отчёт за предыдущий день, еженедельный за предыдущие семь дней.</small>
        {!page.data.schedules.length && <p className="muted">Расписаний пока нет.</p>}
        {!!page.data.schedules.length && <ul className="data-list">
          {page.data.schedules.map((schedule) => {
            const recipient = page.data?.recipients.find(({ id }) => id === schedule.recipient_id)
            return <li className="data-row" key={schedule.id}>
              <div className="data-copy">
                <strong>{schedule.frequency === 'daily' ? 'Ежедневно' : weekdays[schedule.weekday!]} · {schedule.time}</strong>
                <small>{!schedule.enabled ? 'Отключено' : schedule.notify ? 'Отправка в Telegram' : 'Сохранение в истории'}</small>
                {schedule.notify && recipient && <small>{recipient.name}</small>}
              </div>
              <div className="button-group">
                <button className="button secondary" disabled={action.busy} onClick={() => {
                  action.setError('')
                  setEditing(schedule)
                }}>Изменить</button>
                {schedule.enabled && <button className="button secondary" disabled={action.busy} onClick={() => action.run(async () => {
                  await api(path + '/schedules/' + schedule.id + '/disable?version=' + schedule.version, 'POST')
                  page.retry()
                })}>Отключить</button>}
              </div>
            </li>
          })}
        </ul>}
      </section>}

      <section className="surface modal-body form-stack">
        <h2>История отчётов</h2>
        {!page.data.reports.length && <Empty icon={<FileText size={25} />} title="Сохранённых отчётов пока нет" />}
        {!!page.data.reports.length && <ul className="data-list">
          {page.data.reports.map((report) => <li className="data-row" key={report.id}>
            <div className="data-copy">
              <strong>{shortDate(report.start_on)} — {shortDate(report.end_on)}</strong>
              <small>{fullDate(report.created_at, report.timezone)} · {deliveryLabels[report.delivery_status]}</small>
              {report.delivery_error && <small>{report.delivery_error}</small>}
            </div>
            <button className="button secondary" onClick={() => { action.setError(''); setViewing(report.id) }}>Открыть</button>
          </li>)}
        </ul>}
      </section>
    </div>}

    {selection && <Modal title="Сохранённый отчёт" wide busy={action.busy} onClose={() => {
      action.setError('')
      setViewing(undefined)
    }}>
      <div className="modal-body form-stack">
        <ReportText report={selection} />
        <p role="status">{deliveryLabels[selection.delivery_status]}{selection.delivery_error && <> · {selection.delivery_error}</>}</p>
        <ErrorMessage message={action.error} />
      </div>
      {selection.can_send && <footer className="modal-body modal-actions">
        <button className="button primary" disabled={action.busy || (
          selection.delivery_status === 'ready' && (!page.data?.telegram_linked || !page.data.telegram_configured)
        )} onClick={() => send(selection)}>
          <Send size={16} />{selection.delivery_status === 'failed' ? 'Повторить отправку' : 'Отправить себе в Telegram'}
        </button>
        {selection.delivery_status === 'ready' && !page.data?.telegram_linked && <small>Привяжите Telegram в профиле.</small>}
        {selection.delivery_status === 'ready' && !page.data?.telegram_configured && <small>Бот Telegram компании ещё не подключён.</small>}
      </footer>}
    </Modal>}

    {editing && page.data && <ScheduleEditor schedule={editing === 'new' ? undefined : editing} data={page.data}
      path={path} action={action} onClose={() => setEditing(undefined)} onSaved={page.retry} />}
  </>
}

function ReportText({ report }: { report: Report }) {
  return <div className="data-copy">
    {report.text.split('\n\n').map((paragraph) =>
      <p key={paragraph} style={{ whiteSpace: 'pre-wrap' }}>{paragraph}</p>)}
  </div>
}

function ScheduleEditor({ schedule, data, path, action, onClose, onSaved }: {
  schedule?: Schedule
  data: ReportsData
  path: string
  action: ReturnType<typeof useAction>
  onClose: () => void
  onSaved: () => void
}) {
  const [frequency, setFrequency] = useState<Schedule['frequency']>(schedule?.frequency ?? 'weekly')
  const [notify, setNotify] = useState(schedule?.notify ?? false)
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true)
  const scheduleRecipient = schedule?.recipient_id ?? data.actor_id
  const hasRecipients = data.recipients.length > 0
  const needsLink = enabled && notify && (!hasRecipients || !data.telegram_configured)

  return <Modal title={schedule ? 'Изменить расписание' : 'Новое расписание'} busy={action.busy}
    onClose={() => { action.setError(''); onClose() }}>
    <form onSubmit={(event) => formAction(event, (form) => action.run(async () => {
      const payload = {
        frequency,
        time: formText(form, 'time'),
        weekday: frequency === 'weekly' ? Number(formText(form, 'weekday')) : null,
        recipient_id: notify ? Number(formText(form, 'recipient')) || scheduleRecipient : data.actor_id,
        notify,
        enabled,
      }
      if (schedule) {
        await api(path + '/schedules/' + schedule.id, 'PUT', { ...payload, version: schedule.version })
      } else {
        await api(path + '/schedules', 'POST', payload)
      }
      onClose()
      onSaved()
    }))}>
      <div className="modal-body form-stack">
        <ErrorMessage message={action.error} />
        <label>Повтор<select value={frequency} onChange={(event) => setFrequency(event.target.value as Schedule['frequency'])}>
          <option value="daily">Каждый день</option>
          <option value="weekly">Каждую неделю</option>
        </select></label>
        {frequency === 'weekly' && <label>День недели<select name="weekday" defaultValue={schedule?.weekday ?? 0}>
          {weekdays.map((day, index) => <option value={index} key={day}>{day}</option>)}
        </select></label>}
        <label>Время компании<input type="time" name="time" defaultValue={schedule?.time ?? '09:00'} required /></label>
        <label className="toggle-row"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />Расписание включено</label>
        <label className="toggle-row"><input type="checkbox" checked={notify} onChange={(event) => setNotify(event.target.checked)} />Отправлять отчёт в Telegram</label>
        {notify && <label>Получатель<select name="recipient" defaultValue={scheduleRecipient} required={enabled}>
          {!data.recipients.some(({ id }) => id === scheduleRecipient) && <option value="">Выберите сотрудника с привязанным Telegram</option>}
          {data.recipients.map(({ id, name }) => <option value={id} key={id}>{id === data.actor_id ? 'Я' : name}</option>)}
        </select></label>}
        {notify && !hasRecipients && <small className="muted">Укажите привязанный Telegram в профиле.</small>}
        {notify && !data.telegram_configured && <small className="muted">Бот Telegram компании ещё не подключён.</small>}
      </div>
      <footer className="modal-body modal-actions">
        <button className="button primary" disabled={action.busy || needsLink}>Сохранить расписание</button>
      </footer>
    </form>
  </Modal>
}
