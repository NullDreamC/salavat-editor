import { useCallback, useEffect, useState } from 'react'
import { ListChecks, Plus, Send } from 'lucide-react'
import { api, companyPath, errorText, fullDate } from './api'
import type { Company, Source } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

const taskKinds = {
  chat: 'Вопрос',
  strategy: 'Стратегия',
  topics: 'Темы публикаций',
  plan: 'План публикаций',
  video: 'Сценарий видео',
}
export type TaskKind = keyof typeof taskKinds
const taskStatuses = {
  queued: 'В очереди', running: 'Готовится', completed: 'Готово',
  needs_info: 'Нужно уточнение', failed: 'Ошибка', interrupted: 'Прервано',
}
const badgeClasses: Record<keyof typeof taskStatuses, string> = {
  queued: 'draft', running: 'review', completed: 'approved',
  needs_info: 'needs_info', failed: 'failed', interrupted: 'failed',
}
type Task = {
  id: number
  kind: TaskKind
  status: keyof typeof taskStatuses
  prompt: string
  updated_at: string
}
type TaskDetail = Task & {
  reply: string
  questions: string[]
  facts: { source_id: number; evidence: string }[]
  error: string | null
  messages: { id: number; role: 'user' | 'assistant'; text: string; created_at: string }[]
}

const pending = (task: Task) => task.status === 'queued' || task.status === 'running'

export function AssistantTasks({ company }: { company: Company }) {
  const base = companyPath(company.id)
  const path = base + '/tasks'
  const load = useCallback(() => api<Task[]>(path), [path])
  const { data, error, retry, setData } = useData(load)
  const [selected, setSelected] = useState<number | null>()
  const writable = company.role !== 'approver'
  const poll = selected === undefined && !!data?.some(pending)
  const [pollError, setPollError] = useState('')
  useEffect(() => {
    if (!poll) return
    let active = true
    let timer: number
    const schedule = () => {
      timer = window.setTimeout(async () => {
        try {
          const tasks = await load()
          if (active) {
            setData(tasks)
            setPollError('')
          }
        } catch (cause) {
          if (active) setPollError(errorText(cause))
        }
        if (active) schedule()
      }, 2500)
    }
    schedule()
    return () => { active = false; window.clearTimeout(timer) }
  }, [load, poll, setData])

  const update = useCallback((task: Task) => {
    setData((current) => [task, ...(current ?? []).filter(({ id }) => id !== task.id)]
      .sort((first, second) => second.id - first.id))
  }, [setData])
  return <>
    <PageHeader title="Помощник" action={writable && <button className="button primary" onClick={() => setSelected(null)}>
      <Plus size={18} />Новая задача
    </button>} />
    <ErrorMessage message={error} retry={retry} />
    {poll && <ErrorMessage message={pollError} />}
    {!data ? !error && <Loading /> : data.length ? <ul className="surface data-list">
      {data.map((task) => <li className="data-row" key={task.id}>
        <span className="row-icon"><ListChecks size={18} /></span>
        <button className="data-copy text-button" onClick={() => setSelected(task.id)}>
          <strong>{task.prompt.length > 100 ? task.prompt.slice(0, 100).trimEnd() + '…' : task.prompt}</strong>
          <small>{taskKinds[task.kind]} · {fullDate(task.updated_at, company.timezone)}</small>
        </button>
        <TaskBadge task={task} />
      </li>)}
    </ul> : <div className="surface"><Empty icon={<ListChecks size={25} />} title="Задач пока нет" /></div>}
    {selected === null && <NewTask base={base} onClose={() => setSelected(undefined)} onCreated={(task) => {
      update(task)
      setSelected(task.id)
    }} />}
    {typeof selected === 'number' && <TaskDialog key={selected} path={path + '/' + selected} timezone={company.timezone}
      writable={writable} onClose={() => setSelected(undefined)} onUpdate={update} />}
  </>
}

function TaskBadge({ task }: { task: Task }) {
  return <span className={'status status-' + badgeClasses[task.status]} role="status">{taskStatuses[task.status]}</span>
}

export function TaskKindField({ value }: { value?: TaskKind }) {
  return <label>Тип задачи<select name="kind" defaultValue={value ?? 'chat'}>
    {Object.entries(taskKinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
  </select></label>
}

export function TaskSources({ base, initial = [] }: { base: string; initial?: number[] }) {
  const load = useCallback(() => api<Source[]>(base + '/sources'), [base])
  const sources = useData(load)
  const [selected, setSelected] = useState(initial)
  const visible = sources.data?.filter((item) => item.kind !== 'knowledge' || selected.includes(item.id))
  const missing = sources.data ? selected.filter((id) => !sources.data?.some((item) => item.id === id)) : []
  function toggle(id: number, checked: boolean) {
    setSelected((current) => checked ? [...current, id] : current.filter((item) => item !== id))
  }
  return <>
    {selected.map((id) => <input type="hidden" name="source_ids" value={id} key={id} />)}
    <details className="editor-section">
      <summary>Материалы{selected.length > 0 && <span className="summary-count">{selected.length}</span>}</summary>
      <ErrorMessage message={sources.error} retry={sources.retry} />
      {!sources.data ? !sources.error && <Loading /> : (visible?.length || missing.length) ?
        <div className="source-choices section-inner">{visible?.map((item) => <label className="choice" key={item.id}>
            <input type="checkbox" checked={selected.includes(item.id)}
              disabled={selected.length >= 50 && !selected.includes(item.id)} onChange={(event) =>
                toggle(item.id, event.target.checked)} />
            <span>{item.title}</span>
            {item.kind === 'knowledge' && <small>Из базы знаний</small>}
          </label>)}
          {missing.map((id) => <label className="choice" key={id}>
            <input type="checkbox" checked onChange={() => toggle(id, false)} />
            <span>Материал №{id}</span><small>Недоступен</small>
          </label>)}
        </div> : <p className="muted">Материалов пока нет.</p>}
    </details>
  </>
}

export function selectedSources(data: FormData) {
  return data.getAll('source_ids').map(Number)
}

function NewTask({ base, onClose, onCreated }: { base: string; onClose: () => void; onCreated: (task: TaskDetail) => void }) {
  const action = useAction()
  return <Modal title="Новая задача" wide onClose={onClose} busy={action.busy}>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      onCreated(await api<TaskDetail>(base + '/tasks', 'POST', {
        kind: formText(form, 'kind'), prompt: formText(form, 'prompt'), source_ids: selectedSources(form),
        notify_telegram: form.has('notify_telegram'),
      }))
    }))}>
      <TaskKindField />
      <label>Что нужно сделать?<textarea name="prompt" rows={7} required maxLength={12000} /></label>
      <TaskSources base={base} />
      <label className="toggle-row"><input name="notify_telegram" type="checkbox" />Сообщить в Telegram</label>
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button className="button primary" disabled={action.busy}>Отправить задачу</button></div>
    </form>
  </Modal>
}

export function TaskDialog({ path, timezone, writable, onClose, onUpdate }: {
  path: string; timezone: string; writable: boolean; onClose: () => void; onUpdate?: (task: Task) => void
}) {
  const load = useCallback(() => api<TaskDetail>(path), [path])
  const { data: task, error, retry, setData } = useData(load)
  const [refreshError, setRefreshError] = useState('')
  const action = useAction()
  const poll = !!task && pending(task)
  useEffect(() => {
    if (!poll) return
    let active = true
    const timer = window.setTimeout(() => {
      load().then((value) => {
        if (active) {
          setRefreshError('')
          setData(value)
        }
      }, (cause) => {
        if (active) setRefreshError(errorText(cause))
      })
    }, 2000)
    return () => { active = false; window.clearTimeout(timer) }
  }, [load, poll, task, setData])
  useEffect(() => {
    if (task) onUpdate?.(task)
  }, [task, onUpdate])

  const canRetry = task?.status === 'failed' || task?.status === 'interrupted'
  return <Modal title={task ? taskKinds[task.kind] : 'Задача'} wide onClose={onClose} busy={action.busy}>
    <div className="form-stack modal-body">
      <ErrorMessage message={error} retry={retry} />
      {!task ? !error && <Loading /> : <>
        <div><TaskBadge task={task} /></div>
        {task.messages.length ? <ol className="data-list" aria-label="История задачи">
          {task.messages.map((message) => <li key={message.id} className="data-row">
            <div className="data-copy">
              <strong>{message.role === 'user' ? 'Запрос' : 'Помощник'}</strong>
              <small><time dateTime={message.created_at}>{fullDate(message.created_at, timezone)}</time></small>
              <p className="material-text">{message.text}</p>
            </div>
          </li>)}
        </ol> : <>
          <p className="material-text">{task.prompt}</p>
          {task.reply && <p className="material-text">{task.reply}</p>}
        </>}
        {!!task.questions.length && task.messages.length === 0 && <section className="form-stack">
          <h3>Что нужно уточнить</h3>
          <ul>{task.questions.map((question, index) => <li className="material-text" key={index}>{question}</li>)}</ul>
        </section>}
        {!!task.facts.length && <details className="editor-section">
          <summary>На чём основан ответ</summary>
          <ul>{task.facts.map((fact, index) => <li className="material-text" key={index}>
            <strong>Материал №{fact.source_id}</strong> — {fact.evidence}
          </li>)}</ul>
        </details>}
        {task.kind === 'plan' && task.status === 'completed' && <p className="muted">Предложения добавлены в «План публикаций».</p>}
        {poll && <ErrorMessage message={refreshError} retry={() => { setRefreshError(''); retry() }} />}
        <ErrorMessage message={task.error ?? ''} />
        <ErrorMessage message={action.error} />
        {writable && !poll && <form className="form-stack" onSubmit={(event) => {
          const element = event.currentTarget
          formAction(event, (form) => void action.run(async () => {
            const next = await api<TaskDetail>(path + '/messages', 'POST', { text: formText(form, 'text') })
            element.reset()
            setData(next)
          }))
        }}>
          <label>{task.status === 'needs_info' ? 'Уточнение' : 'Сообщение'}<textarea name="text" rows={4} required maxLength={12000} /></label>
          <div className="modal-actions">
            {canRetry && <button className="button secondary" type="button" disabled={action.busy} onClick={() => void action.run(async () =>
              setData(await api<TaskDetail>(path + '/retry', 'POST'))
            )}>Повторить задачу</button>}
            <button className="button primary" disabled={action.busy}><Send size={16} />Отправить</button>
          </div>
        </form>}
      </>}
    </div>
  </Modal>
}
