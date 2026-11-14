import { useCallback, useState } from 'react'
import { Archive, BookOpen, Plus } from 'lucide-react'
import { api, companyPath, fullDate } from './api'
import type { Company, Source } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

const categories = {
  business: 'О компании',
  style: 'Стиль и правила',
  faq: 'Частые вопросы',
  decision: 'Решения редакции',
}
const statuses = { draft: 'Черновик', active: 'Используется в задачах', archived: 'В архиве' }
type Entry = {
  id: number
  title: string
  text: string
  category: keyof typeof categories
  status: keyof typeof statuses
  origin_url: string | null
  updated_at: string
}
type EntryFormMode = 'text' | 'material' | 'website'

export function Knowledge({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/knowledge'
  const load = useCallback(() => api<Entry[]>(path), [path])
  const entries = useData(load)
  const [archived, setArchived] = useState(false)
  const [entry, setEntry] = useState<Entry | null>()
  const writable = company.role !== 'approver'
  const visible = entries.data?.filter((item) => (item.status === 'archived') === archived)
  function save(item: Entry) {
    entries.setData((current) => [item, ...(current ?? []).filter(({ id }) => id !== item.id)])
  }
  return <>
    <PageHeader title="База знаний" action={writable && <button className="button primary" onClick={() => setEntry(null)}>
      <Plus size={18} />Добавить запись
    </button>} />
    <ErrorMessage message={entries.error} retry={entries.retry} />
    {!entries.data ? !entries.error && <Loading /> : <section className="surface">
      <div className="editor-tabs" aria-label="Разделы базы знаний">
        <button className={archived ? 'tab' : 'tab active'} aria-pressed={!archived} onClick={() => setArchived(false)}>Записи</button>
        <button className={archived ? 'tab active' : 'tab'} aria-pressed={archived} onClick={() => setArchived(true)}>Архив</button>
      </div>
      {visible?.length ? <ul className="data-list">{visible.map((item) => <li className="data-row" key={item.id}>
        <span className="row-icon">{archived ? <Archive size={18} /> : <BookOpen size={18} />}</span>
        <button className="data-copy text-button" onClick={() => setEntry(item)}>
          <strong>{item.title}</strong><small>{categories[item.category]} · {statuses[item.status]}</small>
        </button>
        <time className="muted" dateTime={item.updated_at}>{fullDate(item.updated_at, company.timezone)}</time>
      </li>)}</ul> : <Empty icon={archived ? <Archive size={25} /> : <BookOpen size={25} />}
        title={archived ? 'Архив пуст' : 'Записей пока нет'} />}
    </section>}
    {entry !== undefined && <KnowledgeEntry path={path} entry={entry} writable={writable} onClose={() => setEntry(undefined)}
      onSaved={(item, open) => {
        save(item)
        setArchived(item.status === 'archived')
        setEntry(open ? item : undefined)
      }} />}
  </>
}

function KnowledgeEntry({ path, entry, writable, onClose, onSaved }: {
  path: string
  entry: Entry | null
  writable: boolean
  onClose: () => void
  onSaved: (entry: Entry, open?: boolean) => void
}) {
  const action = useAction()
  const itemPath = path + '/' + entry?.id
  if (entry && (!writable || entry.status === 'archived')) return <Modal title={entry.title} wide busy={action.busy} onClose={onClose}>
    <div className="form-stack modal-body">
      <small className="muted">{categories[entry.category]} · {statuses[entry.status]}</small>
      <p className="material-text">{entry.text}</p>
      {entry.origin_url && <a className="text-link" href={entry.origin_url} target="_blank" rel="noreferrer">Открыть исходную страницу</a>}
      <ErrorMessage message={action.error} />
      {writable && <div className="modal-actions">
        <button className="button primary" disabled={action.busy} onClick={() => void action.run(async () => {
          onSaved(await api<Entry>(itemPath, 'PATCH', { status: 'draft' }), true)
        })}>Восстановить как черновик</button>
      </div>}
    </div>
  </Modal>
  return <KnowledgeForm key={entry?.id ?? 'new'} path={path} entry={entry} onClose={onClose} onSaved={onSaved} />
}

function KnowledgeForm({ path, entry, onClose, onSaved }: {
  path: string
  entry: Entry | null
  onClose: () => void
  onSaved: (entry: Entry, open?: boolean) => void
}) {
  const [mode, setMode] = useState<EntryFormMode>('text')
  const action = useAction()
  const itemPath = entry ? path + '/' + entry.id : path
  return <Modal title={entry ? 'Редактировать запись' : 'Новая запись'} wide onClose={onClose} busy={action.busy}>
    {!entry && <div className="editor-tabs modal-tabs" aria-label="Откуда взять текст">
      {([['text', 'Ввести текст'], ['material', 'Из материалов'], ['website', 'Со страницы сайта']] as const).map(([id, label]) =>
        <button key={id} className={mode === id ? 'tab active' : 'tab'} disabled={action.busy} aria-pressed={mode === id}
          onClick={() => { setMode(id); action.setError('') }}>{label}</button>
      )}
    </div>}
    <form key={mode} className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      if (mode === 'website') {
        onSaved(await api<Entry>(path + '/import-url', 'POST', { url: formText(form, 'url') }), true)
        return
      }
      const source = formText(form, 'source_id')
      onSaved(await api<Entry>(itemPath, entry ? 'PATCH' : 'POST', {
        title: formText(form, 'title'), text: formText(form, 'text'),
        category: formText(form, 'category'), status: formText(form, 'status'),
        ...(source ? { source_id: Number(source) } : {}),
      }))
    }))}>
      {mode === 'website' ? <label>Адрес страницы
        <input name="url" type="url" placeholder="https://" maxLength={2048} required />
      </label> : <>
        {mode === 'material' ? <MaterialText path={path} /> : <>
          <label>Название<input name="title" required maxLength={240} defaultValue={entry?.title} /></label>
          <label>Текст<textarea name="text" rows={12} required maxLength={120000} defaultValue={entry?.text} /></label>
        </>}
        <div className="form-grid">
          <label>Раздел<select name="category" defaultValue={entry?.category ?? 'business'}>
            {Object.entries(categories).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select></label>
          <label>Статус<select name="status" aria-label="Статус" defaultValue={entry?.status ?? 'draft'}>
            <option value="draft">Черновик</option>
            <option value="active">Использовать в задачах</option>
          </select></label>
        </div>
        {entry?.origin_url && <a className="text-link" href={entry.origin_url} target="_blank" rel="noreferrer">Открыть исходную страницу</a>}
      </>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        {entry && <button className="button secondary" type="button" disabled={action.busy} onClick={() => void action.run(async () => {
          await api(itemPath, 'DELETE')
          onSaved({ ...entry, status: 'archived', updated_at: new Date().toISOString() })
        })}><Archive size={16} />В архив</button>}
        <button className="button primary" disabled={action.busy}>{action.busy ? 'Сохраняем…' : mode === 'website' ? 'Загрузить текст' : 'Сохранить'}</button>
      </div>
    </form>
  </Modal>
}

function MaterialText({ path }: { path: string }) {
  const sourcePath = path.slice(0, -'/knowledge'.length) + '/sources'
  const load = useCallback(() => api<Source[]>(sourcePath), [sourcePath])
  const sources = useData(load)
  const [source, setSource] = useState<Source>()
  const choices = sources.data?.filter((item) => item.kind !== 'knowledge' && item.text.trim())
  return <>
    <label>Материал<select name="source_id" required value={source?.id ?? ''} onChange={(event) =>
      setSource(choices?.find((item) => item.id === Number(event.target.value)))}>
      <option value="">Выберите материал</option>
      {choices?.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
    </select></label>
    <ErrorMessage message={sources.error} retry={sources.retry} />
    {!sources.data && !sources.error && <Loading />}
    {choices?.length === 0 && <p className="muted">Добавьте заметку или документ в разделе «Материалы».</p>}
    {source && <div className="form-stack" key={source.id}>
      <label>Название<input name="title" required maxLength={240} defaultValue={source.title} /></label>
      <label>Текст<textarea name="text" rows={12} required maxLength={120000} defaultValue={source.text} /></label>
    </div>}
  </>
}
