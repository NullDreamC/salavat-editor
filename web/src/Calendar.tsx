import { useCallback, useState } from 'react'
import { Bell, CalendarDays, ChevronLeft, ChevronRight, Pencil, Plus, Trash2 } from 'lucide-react'
import { api, companyPath, localDateTime, shortDate } from './api'
import type { CalendarEvent, Company, Source } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

const weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
const monthLabel = new Intl.DateTimeFormat('ru-RU', { month: 'long', year: 'numeric', timeZone: 'UTC' })
const iso = (year: number, month: number, day: number) => new Date(Date.UTC(year, month, day)).toISOString().slice(0, 10)

export function Calendar({ company }: { company: Company }) {
  const today = localDateTime(new Date().toISOString(), company.timezone).slice(0, 10)
  const [month, setMonth] = useState(today.slice(0, 7))
  const [day, setDay] = useState<string>()
  const [dialog, setDialog] = useState<CalendarEvent | null>()
  const path = companyPath(company.id)
  const load = useCallback(() => api<CalendarEvent[]>(path + '/events'), [path])
  const events = useData(load)
  const writable = company.role !== 'approver'
  const [year, monthNumber] = month.split('-').map(Number)
  const firstDay = new Date(Date.UTC(year, monthNumber - 1, 1))
  const offset = (firstDay.getUTCDay() + 6) % 7
  const days = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate()
  const monthEvents = events.data?.filter((event) => event.event_date.startsWith(month)).sort((a, b) => a.event_date.localeCompare(b.event_date)) ?? []
  const shown = day ? monthEvents.filter((event) => event.event_date === day) : monthEvents
  function navigate(offsetMonths: number) {
    setMonth(iso(year, monthNumber - 1 + offsetMonths, 1).slice(0, 7))
    setDay(undefined)
  }
  return <>
    <PageHeader title="Календарь" action={writable && <button className="button primary" onClick={() => setDialog(null)}><Plus size={18} />Новое событие</button>} />
    <ErrorMessage message={events.error} retry={events.retry} />
    {!events.data ? !events.error && <Loading /> : <div className="calendar-layout">
      <section className="surface month">
        <header className="month-header">
          <h2>{monthLabel.format(firstDay)}</h2>
          <div className="button-group">
            <button className="icon-button" aria-label="Предыдущий месяц" onClick={() => navigate(-1)}><ChevronLeft size={20} /></button>
            <button className="icon-button" aria-label="Следующий месяц" onClick={() => navigate(1)}><ChevronRight size={20} /></button>
          </div>
        </header>
        <div className="month-grid">
          {weekdays.map((label) => <span className="weekday" key={label}>{label}</span>)}
          {Array.from({ length: offset }, (_, index) => <span key={'blank' + index} />)}
          {Array.from({ length: days }, (_, index) => {
            const date = iso(year, monthNumber - 1, index + 1)
            const count = monthEvents.filter((event) => event.event_date === date).length
            return <button key={date} className={`day ${date === today ? 'today' : ''} ${date === day ? 'selected' : ''}`}
              aria-label={shortDate(date) + (count ? ` — события: ${count}` : '')} aria-pressed={date === day}
              onClick={() => setDay(day === date ? undefined : date)}>{index + 1}{count > 0 && <span className="event-dot" />}</button>
          })}
        </div>
      </section>
      <section className="surface agenda">
        <header className="agenda-header">
          <h2>{day ? shortDate(day) : 'События месяца'}</h2>
          {day && <button className="text-button" onClick={() => setDay(undefined)}>Весь месяц</button>}
        </header>
        {!shown.length ? <Empty icon={<CalendarDays size={24} />} title="Нет событий" /> :
          <ul className="data-list">{shown.map((event) => <li key={event.id} className="data-row">
            <div className="data-copy"><strong>{event.title}</strong><small>{shortDate(event.event_date)}</small>
              {event.description && <p>{event.description}</p>}
              {event.reminded_at && <small className="inline-label"><Bell size={13} />Напоминание отправлено</small>}
            </div>
            {writable && <button className="icon-button" aria-label={`Изменить «${event.title}»`}
              onClick={() => setDialog(event)}><Pencil size={17} /></button>}
          </li>)}</ul>}
      </section>
    </div>}
    {dialog !== undefined && <EventForm path={path} value={dialog} initialDate={day ?? today} onClose={() => setDialog(undefined)}
      onSaved={(event) => {
        events.setData((current) => [...(current ?? []).filter((item) => item.id !== event.id), event])
        setMonth(event.event_date.slice(0, 7))
        setDay(event.event_date)
        setDialog(undefined)
      }} onDeleted={(id) => {
        events.setData((current) => current?.filter((event) => event.id !== id))
        setDialog(undefined)
      }} />}
  </>
}

function EventForm({ path, value, initialDate, onClose, onSaved, onDeleted }: {
  path: string; value: CalendarEvent | null; initialDate: string; onClose: () => void
  onSaved: (event: CalendarEvent) => void; onDeleted: (id: number) => void
}) {
  const action = useAction()
  const load = useCallback(() => api<Source[]>(path + '/sources'), [path])
  const sources = useData(load)
  const eventPath = path + '/events' + (value ? '/' + value.id : '')
  return <Modal title={value ? 'Изменить событие' : 'Новое событие'} onClose={onClose} busy={action.busy}>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const source = formText(form, 'source_id')
      onSaved(await api<CalendarEvent>(eventPath, value ? 'PATCH' : 'POST', {
        title: formText(form, 'title'), event_date: formText(form, 'event_date'), description: formText(form, 'description'),
        source_id: sources.data ? source ? Number(source) : null : value?.source_id ?? null,
      }))
    }))}>
      <label>Название<input name="title" autoFocus required maxLength={240} defaultValue={value?.title} /></label>
      <label>Дата<input name="event_date" type="date" required defaultValue={value?.event_date ?? initialDate} /></label>
      <label>Описание<textarea name="description" rows={4} maxLength={8000} defaultValue={value?.description} /></label>
      {sources.data && <label>Материал<select name="source_id" defaultValue={value?.source_id ?? ''}>
        <option value="">Без материала</option>
        {sources.data.map((source) => <option key={source.id} value={source.id}>{source.title}</option>)}
      </select></label>}
      <ErrorMessage message={sources.error} retry={sources.retry} />
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        {value && <button className="button danger" type="button" disabled={action.busy} onClick={() => {
          if (window.confirm(`Удалить событие «${value.title}»?`)) void action.run(async () => {
            await api(eventPath, 'DELETE')
            onDeleted(value.id)
          })
        }}><Trash2 size={16} />Удалить</button>}
        <button className="button primary" disabled={action.busy}>Сохранить</button>
      </div>
    </form>
  </Modal>
}
