import { useCallback, useState } from 'react'
import { ArrowUpRight, FileText, Files, Image, Plus, Video } from 'lucide-react'
import { api, companyPath, fullDate, sourceKinds } from './api'
import type { Company, Source } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

export function Materials({ company, createPost }: { company: Company; createPost: (sources: number[]) => void }) {
  const path = companyPath(company.id)
  const load = useCallback(() => api<Source[]>(path + '/sources'), [path])
  const sources = useData(load)
  const [adding, setAdding] = useState(false)
  const [preview, setPreview] = useState<Source>()
  const [selected, setSelected] = useState<number[]>([])
  const writable = company.role !== 'approver'
  const addButton = <button className="button primary" onClick={() => setAdding(true)}><Plus size={18} />Добавить материал</button>
  return <>
    <PageHeader title="Материалы" action={writable && <div className="button-group">
      {!!selected.length && <button className="button secondary" onClick={() => createPost(selected)}>Новая публикация ({selected.length})</button>}
      {addButton}
    </div>} />
    <ErrorMessage message={sources.error} retry={sources.retry} />
    {!sources.data ? !sources.error && <Loading /> : sources.data.length === 0 ?
      <div className="surface"><Empty icon={<Files size={26} />} title="Материалов пока нет" /></div> :
      <ul className="surface data-list">
        {sources.data.map((source) => {
          const Icon = source.kind === 'image' ? Image : source.kind === 'video' ? Video : FileText
          return <li className="data-row" key={source.id}>
            {writable && <input type="checkbox" aria-label={`Выбрать «${source.title}»`} checked={selected.includes(source.id)}
              onChange={(event) => setSelected(event.target.checked ? [...selected, source.id] : selected.filter((id) => id !== source.id))} />}
            <span className="row-icon">
              {source.kind === 'image' && source.file_url ? <img src={source.file_url} loading="lazy" alt="" /> : <Icon size={18} />}
            </span>
            <button className="data-copy text-button" onClick={() => setPreview(source)}><strong>{source.title}</strong>
              <small>{sourceKinds[source.kind] ?? source.kind}</small></button>
            <time className="muted" dateTime={source.created_at}>{fullDate(source.created_at, company.timezone)}</time>
          </li>
        })}
      </ul>}
    {adding && <AddMaterial path={path} onClose={() => setAdding(false)} onCreated={(source) => {
      sources.setData((current) => [source, ...(current ?? [])])
      setAdding(false)
    }} />}
    {preview && <Modal title={preview.title} wide onClose={() => setPreview(undefined)}>
      <div className="modal-body form-stack">
        {preview.kind === 'image' && preview.file_url && <img className="preview-card" src={preview.file_url} alt={preview.title} />}
        {preview.kind === 'video' && preview.file_url && <video className="preview-card" src={preview.file_url} controls aria-label={preview.title} />}
        {preview.text && <p className="material-text">{preview.text}</p>}
        {preview.file_url && <a className="text-link" href={preview.file_url} target="_blank" rel="noreferrer">Открыть файл <ArrowUpRight size={16} /></a>}
        {writable && <div className="modal-actions">
          <button className="button primary" onClick={() => createPost([preview.id])}>Создать публикацию</button>
        </div>}
      </div>
    </Modal>}
  </>
}

function AddMaterial({ path, onClose, onCreated }: { path: string; onClose: () => void; onCreated: (source: Source) => void }) {
  const [mode, setMode] = useState<'note' | 'file'>('note')
  const action = useAction()
  return <Modal title="Новый материал" onClose={onClose} busy={action.busy}>
    <div className="editor-tabs modal-tabs" aria-label="Вид материала">
      <button className={mode === 'note' ? 'tab active' : 'tab'} disabled={action.busy} aria-pressed={mode === 'note'}
        onClick={() => { setMode('note'); action.setError('') }}>Заметка</button>
      <button className={mode === 'file' ? 'tab active' : 'tab'} disabled={action.busy} aria-pressed={mode === 'file'}
        onClick={() => { setMode('file'); action.setError('') }}>Файл</button>
    </div>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      onCreated(mode === 'note' ? await api<Source>(path + '/sources', 'POST', {
        title: formText(form, 'title'), text: formText(form, 'text'),
      }) : await api<Source>(path + '/uploads', 'POST', form))
    }))}>
      {mode === 'note' ? <>
        <label>Название<input name="title" required autoFocus maxLength={240} /></label>
        <label>Текст<textarea name="text" rows={7} required maxLength={120000} /></label>
      </> : <label>Документ, фото или видео<input className="file-input" name="file" type="file" required /></label>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button className="button primary" disabled={action.busy}>
        {action.busy ? 'Добавляем…' : 'Добавить'}
      </button></div>
    </form>
  </Modal>
}
