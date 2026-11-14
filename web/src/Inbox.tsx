import { useCallback, useState } from 'react'
import { Inbox as InboxIcon, Plus, RefreshCw } from 'lucide-react'
import { api, companyPath, fullDate } from './api'
import type { Company } from './api'
import { Empty, ErrorMessage, Loading, Modal, PageHeader, formAction, formText, useAction, useData } from './ui'

const platforms: Record<string, string> = { telegram: 'Telegram', vk: 'ВКонтакте', max: 'MAX' }
const statuses = { new: 'Новое', draft: 'Черновик', approved: 'Согласовано', sent: 'Ответ отправлен', closed: 'Закрыто' }
type Item = {
  id: number; version: number; status: keyof typeof statuses; platform: string; kind: 'comment' | 'question'
  body: string; author: string; reference: string; created_at: string; reply: string
  questions: string[]; evidence: { source_id: number; title: string; quote: string }[]
  can_send: boolean; generating: boolean; sending: boolean; send_uncertain: boolean; send_error: string | null
}
type Settings = { auto_replies_enabled: boolean }
type Faq = { source_id: number; question: string; answer: string; selected: boolean }
type Monitor = { enabled: boolean; last_sync: string | null; last_error: string | null }
type InboxData = { items: Item[]; settings: Settings; auto_faq: Faq[]; monitor: Monitor }
type Mode = 'intake' | 'sync'

export function Inbox({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/inbox'
  const writable = company.role === 'admin' || company.role === 'editor'
  const [mode, setMode] = useState<Mode>()
  const [onlyOpen, setOnlyOpen] = useState(false)
  const [selected, setSelected] = useState<number>()
  const inbox = useData(useCallback(() => api<InboxData>(path), [path]))
  const item = inbox.data?.items.find((entry) => entry.id === selected)
  const displayed = inbox.data?.items.filter((entry) => !onlyOpen || entry.status !== 'closed') ?? []

  function refreshItem(updated: Item) {
    if (inbox.data) inbox.setData({ ...inbox.data, items: inbox.data.items.map((entry) => entry.id === updated.id ? updated : entry) })
  }

  return <>
    <PageHeader title="Обращения" action={writable && <div className="button-group">
      <button className="button secondary" onClick={() => setMode('sync')}><RefreshCw size={16} />Загрузить комментарии</button>
      <button className="button primary" onClick={() => setMode('intake')}><Plus size={18} />Добавить обращение</button>
    </div>} />
    <ErrorMessage message={inbox.error} retry={inbox.retry} />
    {!inbox.data && !inbox.error && <Loading />}
    {inbox.data && <div className="form-stack">
      {!!inbox.data.items.length && <div className="button-group">
        <button className={onlyOpen ? 'tab' : 'tab active'} aria-pressed={!onlyOpen} onClick={() => setOnlyOpen(false)}>Все</button>
        <button className={onlyOpen ? 'tab active' : 'tab'} aria-pressed={onlyOpen} onClick={() => setOnlyOpen(true)}>Открытые</button>
      </div>}
      {displayed.length ? <ul className="surface data-list">{displayed.map((entry) => <li className="data-row" key={entry.id}>
        <button className="data-copy text-button" onClick={() => setSelected(entry.id)}>
          <strong>{entry.body.length > 120 ? entry.body.slice(0, 117) + '…' : entry.body}</strong>
          <small>{platforms[entry.platform]} · {entry.kind === 'comment' ? 'Комментарий' : 'Вопрос'}{entry.author && ` · ${entry.author}`}</small>
          <small>{fullDate(entry.created_at, company.timezone)}</small>
        </button>
        <span className={`status status-${entry.status === 'sent' ? 'published' : entry.status}`}>
          {entry.sending ? 'Отправляется' : entry.generating ? 'Готовится ответ' : entry.send_uncertain && entry.status !== 'closed' ? 'Проверьте доставку' : statuses[entry.status]}
        </span>
      </li>)}</ul> : <div className="surface"><Empty icon={<InboxIcon size={26} />} title={onlyOpen ? 'Открытых обращений нет' : 'Обращений пока нет'} /></div>}
      {company.role === 'admin' && <>
        <SocialMonitor path={path} value={inbox.data.monitor} timezone={company.timezone} onSaved={inbox.retry} />
        <AutoReplies path={path} value={inbox.data} onSaved={inbox.retry} />
      </>}
    </div>}
    {mode && <IntakeForm mode={mode} path={path} onClose={() => setMode(undefined)} onSaved={() => {
      setMode(undefined)
      inbox.retry()
    }} />}
    {item && <ItemDialog key={item.id} item={item} company={company} path={`${path}/${item.id}`}
      onClose={() => setSelected(undefined)} onSaved={refreshItem} onReload={inbox.retry} />}
  </>
}

function SocialMonitor({ path, value, timezone, onSaved }: { path: string; value: Monitor; timezone: string; onSaved: () => void }) {
  const action = useAction()
  return <section className="surface modal-body form-stack">
    <h2>Мониторинг площадок</h2>
    <small className="muted">Раз в 15 минут редакция проверяет метрики и недавние комментарии к пяти последним публикациям в ВКонтакте и MAX за 30 дней.</small>
    <label className="inline-label"><input type="checkbox" checked={value.enabled} disabled={action.busy} onChange={(event) => {
      const enabled = event.target.checked
      void action.run(async () => {
        await api(path + '/monitor', 'PUT', { enabled })
        onSaved()
      })
    }} />Обновлять замеры и комментарии автоматически</label>
    {value.last_sync && <small className="muted">Последнее обновление {fullDate(value.last_sync, timezone)}</small>}
    <ErrorMessage message={action.error || value.last_error || ''} />
  </section>
}

function AutoReplies({ path, value, onSaved }: { path: string; value: InboxData; onSaved: () => void }) {
  const action = useAction()
  const [saved, setSaved] = useState(false)
  const formKey = JSON.stringify([value.settings.auto_replies_enabled, value.auto_faq])
  return <section className="surface modal-body form-stack">
    <h2>Автоответы</h2>
    <small className="muted">При точном совпадении нового комментария с вопросом редакция отправит выбранный готовый ответ. Чтобы добавить правило, опубликуйте запись категории «Вопросы и ответы» в базе знаний.</small>
    <form key={formKey} className="form-stack" onChange={() => setSaved(false)} onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      await api<Settings>(path + '/settings', 'PUT', { auto_replies_enabled: form.has('enabled'), faq_source_ids: form.getAll('source_id').map(Number) })
      setSaved(true)
      onSaved()
    }))}>
      <label className="inline-label"><input type="checkbox" name="enabled" defaultChecked={value.settings.auto_replies_enabled} disabled={action.busy} />Разрешить автоответы для компании</label>
      {value.auto_faq.map((rule) => <div className="data-copy" key={rule.source_id}>
        <label className="inline-label"><input type="checkbox" name="source_id" value={rule.source_id} defaultChecked={rule.selected} disabled={action.busy} />{rule.question}</label>
        <small>{rule.answer}</small>
      </div>)}
      <ErrorMessage message={action.error} />
      <div className="button-group">
        <button className="button secondary" disabled={action.busy}>Сохранить настройки</button>
        {saved && <small role="status">Настройки сохранены</small>}
      </div>
    </form>
  </section>
}

function IntakeForm({ mode, path, onClose, onSaved }: { mode: Mode; path: string; onClose: () => void; onSaved: () => void }) {
  const action = useAction()
  return <Modal title={mode === 'intake' ? 'Добавить обращение' : 'Загрузить комментарии'} onClose={onClose} busy={action.busy}>
    <form className="modal-body form-stack" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      if (mode === 'sync') {
        await api(path + '/sync', 'POST', { platform: formText(form, 'platform'), publication_id: formText(form, 'publication_id') })
      } else {
        const fields = ['platform', 'kind', 'body', 'author', 'reference']
        await api(path, 'POST', Object.fromEntries(fields.map((field) => [field, formText(form, field)])))
      }
      onSaved()
    }))}>
      <label>Площадка<select name="platform">
        {Object.entries(platforms).filter(([key]) => mode !== 'sync' || key !== 'telegram').map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      {mode === 'intake' ? <>
        <label>Тип<select name="kind"><option value="comment">Комментарий</option><option value="question">Вопрос</option></select></label>
        <label>Текст обращения<textarea name="body" rows={4} maxLength={4000} required /></label>
        <label>Автор <input name="author" maxLength={200} /></label>
        <label>Ссылка или номер обращения <input name="reference" maxLength={200} /></label>
      </> : <>
        <label>ID публикации <input name="publication_id" maxLength={200} required /></label>
        <small className="muted">Для ВКонтакте укажите ID в формате −сообщество_пост, например −123_456, с обычным знаком минуса. Для MAX укажите ID сообщения.</small>
      </>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button className="button primary" disabled={action.busy}>{mode === 'intake' ? 'Добавить' : 'Загрузить'}</button></div>
    </form>
  </Modal>
}

function ItemDialog({ item, company, path, onClose, onSaved, onReload }: {
  item: Item; company: Company; path: string; onClose: () => void; onSaved: (item: Item) => void; onReload: () => void
}) {
  const action = useAction()
  const [confirmSend, setConfirmSend] = useState(false)
  const editor = company.role === 'admin' || company.role === 'editor'
  const approver = company.role === 'admin' || company.role === 'approver'
  const editable = editor && !item.sending && !item.generating && !item.send_uncertain && ['new', 'draft', 'approved'].includes(item.status)
  function submit(name: string, method = 'POST', text?: string) {
    void action.run(async () => {
      try {
        onSaved(await api<Item>(path + '/' + name, method, { version: item.version, ...(text === undefined ? {} : { text }) }))
      } catch (error) {
        onReload()
        throw error
      } finally {
        setConfirmSend(false)
      }
    })
  }

  return <Modal title={item.kind === 'comment' ? 'Комментарий' : 'Вопрос'} onClose={onClose} busy={action.busy}>
    <div className="modal-body form-stack">
      <div className="data-copy">
        <strong>{platforms[item.platform]}{item.author && ` · ${item.author}`}</strong>
        <small>{fullDate(item.created_at, company.timezone)} · {item.sending ? 'Отправляется' : item.generating ? 'Готовится ответ' : statuses[item.status]}</small>
        {item.reference && <small>{item.platform === 'telegram' && /^https:\/\/t\.me\//i.test(item.reference)
          ? <a href={item.reference} target="_blank" rel="noreferrer">Открыть комментарий в Telegram</a>
          : item.reference}</small>}
      </div>
      <p className="material-text">{item.body}</p>
      {!!item.questions.length && <section><strong>Что нужно уточнить</strong>
        <ul>{item.questions.map((question, index) => <li key={index}>{question}</li>)}</ul>
      </section>}
      {!!item.evidence.length && <section><strong>Источники из базы знаний</strong>
        <ul>{item.evidence.map((source, index) => <li key={`${source.source_id}/${index}`}><strong>{source.title}</strong> · {source.quote}</li>)}</ul>
      </section>}
      {editable ? <form key={item.version} className="form-stack" onSubmit={(event) => formAction(event, (form) => submit('draft', 'PUT', formText(form, 'text')))}>
        <label>Ответ<textarea name="text" defaultValue={item.reply} maxLength={4000} rows={5} required disabled={action.busy} /></label>
        <div className="button-group">
          <button className="button secondary" type="button" onClick={() => submit('generate')} disabled={action.busy}>Подготовить с ИИ</button>
          <button className="button secondary" disabled={action.busy}>Сохранить черновик</button>
        </div>
      </form> : item.reply && <section><strong>Ответ</strong><p className="material-text">{item.reply}</p></section>}
      {item.status === 'approved' && !item.can_send && <small className="muted">{item.platform === 'telegram'
        ? 'Ответ на комментарий можно отправить в Telegram.'
        : 'Отправка из редакции доступна для комментариев, загруженных с подключённой площадки.'}</small>}
      <ErrorMessage message={action.error || item.send_error || ''} />
      {confirmSend && <p>Ответ будет опубликован под комментарием на площадке {platforms[item.platform]}.</p>}
      <div className="modal-actions">
        {item.status !== 'closed' && !item.sending && !item.generating && !confirmSend && <button className="button secondary" disabled={action.busy} onClick={() => submit('close')}>Закрыть обращение</button>}
        {approver && item.status === 'draft' && item.reply && !item.questions.length && !item.generating && <button className="button primary" disabled={action.busy} onClick={() => submit('approve')}>Согласовать сохранённый ответ</button>}
        {editor && item.status === 'approved' && item.can_send && !item.sending && !item.generating && !item.send_uncertain && (confirmSend ? <>
          <button className="button secondary" disabled={action.busy} onClick={() => setConfirmSend(false)}>Отмена</button>
          <button className="button primary" disabled={action.busy} onClick={() => submit('send')}>Опубликовать ответ</button>
        </> : <button className="button primary" disabled={action.busy} onClick={() => setConfirmSend(true)}>Отправить согласованный ответ</button>)}
      </div>
    </div>
  </Modal>
}
