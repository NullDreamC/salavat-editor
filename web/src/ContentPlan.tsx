import { useCallback, useState } from 'react'
import { CalendarDays, ListRestart, Plus } from 'lucide-react'
import { api, companyPath, shortDate } from './api'
import type { Company, Post } from './api'
import { selectedSources, TaskDialog, TaskKindField, TaskSources } from './AssistantTasks'
import type { TaskKind } from './AssistantTasks'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

const formats = { post: 'Публикация', video: 'Видео' }
const weekdays = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
type PlanItem = {
  id: number
  title: string
  planned_on: string | null
  format: keyof typeof formats
  brief: string
  source_ids: number[]
  post_id: number | null
}
type Routine = {
  id: number
  name: string
  kind: TaskKind
  prompt: string
  source_ids: number[]
  frequency: 'daily' | 'weekly'
  time: string
  weekday: number | null
  enabled: boolean
  notify_telegram: boolean
}

export function ContentPlan({ company, openPost }: { company: Company; openPost?: (id: number) => void }) {
  const [tab, setTab] = useState<'plan' | 'routines'>('plan')
  const [adding, setAdding] = useState(false)
  const base = companyPath(company.id)
  const writable = company.role !== 'approver'
  return <>
    <PageHeader title="План публикаций" action={writable && <button className="button primary" onClick={() => setAdding(true)}>
      <Plus size={18} />{tab === 'plan' ? 'Добавить в план' : 'Добавить регулярную задачу'}
    </button>} />
    <div className="surface">
      <div className="editor-tabs" aria-label="Разделы плана">
        <button className={tab === 'plan' ? 'tab active' : 'tab'} aria-pressed={tab === 'plan'}
          onClick={() => { setAdding(false); setTab('plan') }}>Публикации</button>
        <button className={tab === 'routines' ? 'tab active' : 'tab'} aria-pressed={tab === 'routines'}
          onClick={() => { setAdding(false); setTab('routines') }}>Регулярные задачи</button>
      </div>
      {tab === 'plan' ? <PlanItems base={base} adding={adding} writable={writable} openPost={openPost} endAdding={() => setAdding(false)} />
        : <Routines base={base} adding={adding} writable={writable} timezone={company.timezone} endAdding={() => setAdding(false)} />}
    </div>
  </>
}

function PlanItems({ base, adding, writable, endAdding, openPost }: {
  base: string; adding: boolean; writable: boolean; endAdding: () => void; openPost?: (id: number) => void
}) {
  const path = base + '/plan'
  const load = useCallback(() => api<PlanItem[]>(path), [path])
  const plan = useData(load)
  const [selected, setSelected] = useState<number>()
  const selection = plan.data?.find((item) => item.id === selected)
  const visible = [...(plan.data ?? [])].sort((first, second) =>
    (first.planned_on ?? '9999').localeCompare(second.planned_on ?? '9999') || first.id - second.id)
  function close() {
    setSelected(undefined)
    endAdding()
  }
  function save(item: PlanItem) {
    plan.setData((current) => [item, ...(current ?? []).filter(({ id }) => id !== item.id)])
  }
  return <>
    <ErrorMessage message={plan.error} retry={plan.retry} />
    {!plan.data ? !plan.error && <Loading /> : visible.length ?
      <ul className="data-list">{visible.map((item) => <li className="data-row" key={item.id}>
        <span className="row-icon"><CalendarDays size={18} /></span>
        <button className="data-copy text-button" onClick={() => setSelected(item.id)}>
          <strong>{item.title}</strong><small>{formats[item.format]} · {item.planned_on ? shortDate(item.planned_on) : 'Без даты'}</small>
        </button>
        <span className={'status ' + (item.post_id ? 'status-approved' : 'status-draft')}>{item.post_id ? 'Есть черновик' : 'В плане'}</span>
      </li>)}</ul> : <Empty icon={<CalendarDays size={25} />} title="В плане пока нет публикаций" />}
    {(adding || selection) && <PlanEditor key={selection?.id ?? 'new'} base={base} item={selection} writable={writable}
      openPost={openPost} onClose={close} onSaved={save}
      onPostCreated={(item, post) => save({ ...item, post_id: post.id })}
      onDeleted={(id) => {
        plan.setData((current) => current?.filter((item) => item.id !== id))
        close()
      }} />}
  </>
}

function PlanEditor({ base, item, writable, openPost, onClose, onSaved, onPostCreated, onDeleted }: {
  base: string; item?: PlanItem; writable: boolean; openPost?: (id: number) => void; onClose: () => void
  onSaved: (item: PlanItem) => void; onPostCreated: (item: PlanItem, post: Post) => void; onDeleted: (id: number) => void
}) {
  const action = useAction()
  const path = base + '/plan' + (item ? '/' + item.id : '')
  const postLink = item?.post_id ? openPost ? <button className="button secondary" type="button"
    onClick={() => { onClose(); openPost(item.post_id!) }}>Открыть публикацию</button> :
    <p className="saved" role="status">Черновик сохранён в разделе «Публикации».</p> : null
  if (item && !writable) return <Modal title={item.title} wide onClose={onClose}>
    <div className="form-stack modal-body">
      <small className="muted">{formats[item.format]} · {item.planned_on ? shortDate(item.planned_on) : 'Без даты'}</small>
      {item.brief && <p className="material-text">{item.brief}</p>}
      {postLink}
    </div>
  </Modal>

  return <Modal title={item ? 'Публикация в плане' : 'Добавить в план'} wide onClose={onClose} busy={action.busy}>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      onSaved(await api<PlanItem>(path, item ? 'PATCH' : 'POST', payload(form)))
      onClose()
    }))}>
      <label>Название<input name="title" required maxLength={240} defaultValue={item?.title} /></label>
      <div className="form-grid">
        <label>Формат<select name="format" defaultValue={item?.format ?? 'post'}>
          {Object.entries(formats).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        <label>Дата выхода<input name="planned_on" type="date" defaultValue={item?.planned_on ?? ''} /></label>
      </div>
      <label>Что подготовить<textarea name="brief" rows={8} maxLength={12000} defaultValue={item?.brief} /></label>
      <TaskSources base={base} initial={item?.source_ids} />
      {postLink}
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        {item && <button className="button danger" type="button" disabled={action.busy} onClick={() => {
          if (window.confirm(`Убрать из плана «${item.title}»?`)) void action.run(async () => {
            await api(path, 'DELETE')
            onDeleted(item.id)
          })
        }}>Убрать из плана</button>}
        {item && !item.post_id && <button className="button secondary" type="button" disabled={action.busy}
          onClick={(event) => {
            const form = event.currentTarget.form
            if (!form?.reportValidity()) return
            const data = new FormData(form)
            void action.run(async () => {
              const saved = await api<PlanItem>(path, 'PATCH', payload(data))
              onSaved(saved)
              const post = await api<Post>(path + '/post', 'POST')
              onPostCreated(saved, post)
            })
          }}>Создать черновик</button>}
        <button className="button primary" disabled={action.busy}>Сохранить</button>
      </div>
    </form>
  </Modal>
}

function payload(form: FormData) {
  return {
    title: formText(form, 'title'), planned_on: formText(form, 'planned_on') || null,
    format: formText(form, 'format'), brief: formText(form, 'brief'), source_ids: selectedSources(form),
  }
}

function Routines({ base, adding, writable, timezone, endAdding }: {
  base: string; adding: boolean; writable: boolean; timezone: string; endAdding: () => void
}) {
  const path = base + '/routines'
  const load = useCallback(() => api<Routine[]>(path), [path])
  const routines = useData(load)
  const [selected, setSelected] = useState<Routine>()
  const [task, setTask] = useState<number>()
  function close() {
    setSelected(undefined)
    endAdding()
  }
  return <>
    <ErrorMessage message={routines.error} retry={routines.retry} />
    {!routines.data ? !routines.error && <Loading /> : routines.data.length ?
      <ul className="data-list">{routines.data.map((item) => <li className="data-row" key={item.id}>
        <span className="row-icon"><ListRestart size={18} /></span>
        <button className="data-copy text-button" onClick={() => setSelected(item)}>
          <strong>{item.name}</strong><small>{routineSchedule(item)}</small>
        </button>
        <span className={'status ' + (item.enabled ? 'status-approved' : 'status-draft')}>{item.enabled ? 'Включена' : 'Выключена'}</span>
      </li>)}</ul> : <Empty icon={<ListRestart size={25} />} title="Регулярных задач пока нет" />}
    {(adding || selected) && <RoutineEditor base={base} item={selected} timezone={timezone} writable={writable} onClose={close}
      onSaved={(item) => {
        routines.setData((current) => [item, ...(current ?? []).filter(({ id }) => id !== item.id)])
        close()
      }}
      onDeleted={(id) => {
        routines.setData((current) => current?.filter((item) => item.id !== id))
        close()
      }}
      onRun={(id) => { close(); setTask(id) }} />}
    {task !== undefined && <TaskDialog path={base + '/tasks/' + task} timezone={timezone} writable={writable}
      onClose={() => setTask(undefined)} />}
  </>
}

const routineSchedule = (item: Routine) => (item.frequency === 'daily' ? 'Ежедневно' : weekdays[item.weekday ?? 0]) + ' · ' + item.time

function RoutineEditor({ base, item, timezone, writable, onClose, onSaved, onDeleted, onRun }: {
  base: string; item?: Routine; timezone: string; writable: boolean; onClose: () => void
  onSaved: (item: Routine) => void; onDeleted: (id: number) => void; onRun: (taskId: number) => void
}) {
  const [frequency, setFrequency] = useState(item?.frequency ?? 'daily')
  const action = useAction()
  const path = base + '/routines' + (item ? '/' + item.id : '')
  if (item && !writable) return <Modal title={item.name} wide onClose={onClose}>
    <div className="form-stack modal-body">
      <small className="muted">{routineSchedule(item)} ({timezone})</small>
      <p className="material-text">{item.prompt}</p>
      {item.notify_telegram && <small className="muted">Уведомления в Telegram включены</small>}
    </div>
  </Modal>

  return <Modal title={item ? 'Регулярная задача' : 'Новая регулярная задача'} wide onClose={onClose} busy={action.busy}>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      onSaved(await api<Routine>(path, item ? 'PATCH' : 'POST', {
        name: formText(form, 'name'), kind: formText(form, 'kind'), prompt: formText(form, 'prompt'),
        source_ids: selectedSources(form), frequency, time: formText(form, 'time'),
        weekday: frequency === 'weekly' ? Number(formText(form, 'weekday')) : null,
        enabled: form.has('enabled'), notify_telegram: form.has('notify_telegram'),
      }))
    }))}>
      <label>Название<input name="name" required maxLength={150} defaultValue={item?.name} /></label>
      <TaskKindField value={item?.kind} />
      <label>Что нужно сделать?<textarea name="prompt" rows={6} required maxLength={12000} defaultValue={item?.prompt} /></label>
      <TaskSources base={base} initial={item?.source_ids} />
      <div className="form-grid">
        <label>Повтор<select value={frequency} onChange={(event) => setFrequency(event.target.value as Routine['frequency'])}>
          <option value="daily">Ежедневно</option><option value="weekly">Раз в неделю</option>
        </select></label>
        <label>Время ({timezone})<input name="time" type="time" required defaultValue={item?.time ?? '09:00'} /></label>
        {frequency === 'weekly' && <label>День недели<select name="weekday" defaultValue={item?.weekday ?? 0}>
          {weekdays.map((label, index) => <option value={index} key={label}>{label}</option>)}
        </select></label>}
      </div>
      <label className="toggle-row"><input name="enabled" type="checkbox" defaultChecked={item?.enabled} />Запускать по расписанию</label>
      <label className="toggle-row">
        <input name="notify_telegram" type="checkbox" defaultChecked={item?.notify_telegram} />Сообщить в Telegram
      </label>
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        {item && <button className="button danger" type="button" disabled={action.busy} onClick={() => {
          if (window.confirm(`Удалить регулярную задачу «${item.name}»?`)) void action.run(async () => {
            await api(path, 'DELETE')
            onDeleted(item.id)
          })
        }}>Удалить</button>}
        {item && <button className="button secondary" type="button" disabled={action.busy} onClick={() => void action.run(async () => {
          const result = await api<{ id: number }>(path + '/run', 'POST')
          onRun(result.id)
        })}>Запустить сохранённую задачу</button>}
        <button className="button primary" disabled={action.busy}>Сохранить</button>
      </div>
    </form>
  </Modal>
}
