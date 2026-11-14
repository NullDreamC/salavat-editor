import { useCallback, useState } from 'react'
import { Clipboard, Link2, Settings2 } from 'lucide-react'
import { api, companyPath, fullDate, platforms, shortDate, statuses } from './api'
import type { Company, Status } from './api'
import { CRMConnections } from './CRMConnections'
import { ErrorMessage, Loading, Modal, PageHeader, formAction, formText, useAction, useData } from './ui'

type Campaign = {
  post_id: number; title: string; platform: keyof typeof platforms; url: string
  cost_kopecks: number | null; revenue_cost_ratio: number | null; last_occurred_at: string | null; last_received_at: string | null
  events: number; leads: number; sales: number; refunds: number; net_kopecks: number | null
  sources: EventSource[]
}
type Overview = {
  enabled: boolean; start: string; end: string
  summary: Pick<Campaign, 'events' | 'leads' | 'sales' | 'refunds' | 'net_kopecks'>
  items: Campaign[]; posts: Publication[]; prepared: Array<Publication & { url: string }>; source: EventSource | null
}
type Publication = { id: number; title: string; platform: keyof typeof platforms; status: Status }
type Settings = { enabled: boolean; key_configured: boolean; target_url: string; webhook_url: string }
const sourceNames = { bitrix24: 'Битрикс24', amocrm: 'amoCRM', crm_webhook: 'Приём событий по ссылке' }
type EventSource = keyof typeof sourceNames
const rubles = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB' })
const ratio = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 })
const money = (kopecks: number | null) => kopecks === null ? '—' : rubles.format(kopecks / 100)

export function Attribution({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/attribution'
  const editor = company.role === 'admin' || company.role === 'editor'
  const [period, setPeriod] = useState({ start: '', end: '', source: '' })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [spend, setSpend] = useState<Campaign>()
  const [link, setLink] = useState('')
  const action = useAction()
  const load = useCallback(() => {
    const params = new URLSearchParams()
    if (period.start) params.set('start', period.start)
    if (period.end) params.set('end', period.end)
    if (period.source) params.set('source', period.source)
    return api<Overview>(path + (params.size ? `?${params}` : ''))
  }, [path, period])
  const report = useData(load)
  const data = report.data

  return <>
    <PageHeader title="Заявки и продажи" action={company.role === 'admin' && <button type="button" className="button secondary"
      onClick={() => setSettingsOpen(true)}><Settings2 size={16} />Подключение CRM</button>} />
    <ErrorMessage message={report.error} retry={report.retry} />
    <ErrorMessage message={action.error} />
    {!report.error && !data && <Loading />}
    {data && <div className="form-stack">
      {!data.enabled && <div className="surface modal-body"><strong>Учёт заявок и продаж выключен</strong></div>}
      {(data.enabled || data.items.length > 0) && <section className="surface modal-body form-stack">
        <h2>Данные из CRM</h2>
        <form key={`${data.start}/${data.end}/${data.source}`} className="button-group" aria-label="Период событий CRM" onSubmit={(event) => formAction(event, (form) => {
          setPeriod({ start: formText(form, 'start'), end: formText(form, 'end'), source: formText(form, 'source') })
        })}>
          <label>С <input type="date" name="start" defaultValue={data.start} required /></label>
          <label>По <input type="date" name="end" defaultValue={data.end} required /></label>
          <label>Источник <select name="source" defaultValue={data.source ?? ''}>
            <option value="">Все источники</option>
            {Object.entries(sourceNames).map(([source, label]) => <option key={source} value={source}>{label}</option>)}
          </select></label>
          <button className="button secondary">Показать</button>
        </form>
        {!report.error && <div className="button-group">
          <div className="data-copy"><strong>{data.summary.leads}</strong><small>Заявки</small></div>
          <div className="data-copy"><strong>{data.summary.sales}</strong><small>Продажи по данным CRM</small></div>
          <div className="data-copy"><strong>{data.summary.refunds}</strong><small>Возвраты</small></div>
          <div className="data-copy"><strong>{money(data.summary.net_kopecks)}</strong><small>Сумма продаж за вычетом возвратов</small></div>
        </div>}
        {!report.error && <small className="muted">{shortDate(data.start)} — {shortDate(data.end)} по дате события. Связь с постом определяется по метке в ссылке. Для оценки вклада публикаций нужны дополнительные данные.</small>}
        {!report.error && !!data.summary.sales && <small className="muted">Битрикс24 и amoCRM передают суммы успешно закрытых сделок. Суммы платежей и возвратов показываются по отдельным событиям. При загрузке одних и тех же сделок из нескольких источников выберите один источник.</small>}
        {!report.error && data.summary.events === 0 && <p className="muted">За этот период CRM не передала событий по ссылкам публикаций.</p>}
      </section>}
      {data.enabled && editor && <section className="surface modal-body form-stack">
        <h2>Ссылки для постов</h2>
        {data.posts.length > 0 ? <form className="form-stack" onSubmit={(event) => formAction(event, (form) => {
          void action.run(async () => {
            const created = await api<Campaign>(path + '/campaigns', 'POST', { post_id: Number(formText(form, 'post_id')) })
            setLink(created.url)
            report.retry()
          })
        })}>
          <label>Публикация <select name="post_id" defaultValue="" required><option value="" disabled>Выберите пост</option>
            {data.posts.map((post) => <option key={post.id} value={post.id}>{post.title || `Пост №${post.id}`} · {platforms[post.platform]} · {statuses[post.status]}</option>)}
          </select></label>
          <div><button className="button primary" disabled={action.busy}><Link2 size={16} />Создать ссылку</button></div>
        </form> : <small className="muted">Нет постов, для которых можно создать ссылку.</small>}
        {data.prepared.length > 0 && <ul className="data-list">{data.prepared.map((post) => <li className="data-row" key={post.id}>
          <div className="data-copy"><strong>{post.title || `Пост №${post.id}`}</strong><small>{platforms[post.platform]} · {statuses[post.status]}</small></div>
          <button type="button" className="button secondary" onClick={() => setLink(post.url)}><Link2 size={16} />Ссылка</button>
        </li>)}</ul>}
      </section>}
      {!report.error && data.items.length > 0 && <section className="surface modal-body form-stack">
        <h2>Результаты публикаций</h2>
        <ul className="data-list">{data.items.map((item) => <li className="data-row" key={item.post_id}>
          <div className="data-copy">
            <strong>{item.title || `Пост №${item.post_id}`}</strong>
            <small>{platforms[item.platform]} · Заявки {item.leads} · Продажи {item.sales} · Возвраты {item.refunds}</small>
            <small>Сумма продаж за вычетом возвратов за период {money(item.net_kopecks)}</small>
            <small>Расходы за всё время {money(item.cost_kopecks)} · Сумма продаж к расходам за всё время {item.revenue_cost_ratio === null ? '—' : `${ratio.format(item.revenue_cost_ratio)} ×`}</small>
            {item.sources.length > 0 && <small>{item.sources.map((source) => sourceNames[source]).join(' · ')}</small>}
            {item.last_occurred_at && <small>Последнее событие за период {fullDate(item.last_occurred_at, company.timezone)}</small>}
            {item.last_received_at && <small>Последнее поступление данных {fullDate(item.last_received_at, company.timezone)}</small>}
          </div>
          <div className="button-group">
            <button type="button" className="button secondary" onClick={() => setLink(item.url)}><Link2 size={16} />Ссылка</button>
            {editor && data.enabled && <button type="button" className="button secondary" onClick={() => setSpend(item)}>Расходы</button>}
          </div>
        </li>)}</ul>
      </section>}
    </div>}
    {link && <Modal title="Ссылка с меткой" onClose={() => setLink('')}>
      <div className="modal-body form-stack">
        <p>Вставьте ссылку в текст поста перед согласованием. Для опубликованного поста добавьте её на площадке, если публикация доступна для редактирования.</p>
        <CopyField label="Ссылка для публикации" value={link} />
      </div>
    </Modal>}
    {spend && <SpendForm path={path} item={spend} onClose={() => setSpend(undefined)} onSaved={() => {
      setSpend(undefined)
      report.retry()
    }} />}
    {settingsOpen && <AttributionSettings companyId={company.id} path={path} onClose={() => {
      setSettingsOpen(false)
      report.retry()
    }} onSaved={report.retry} />}
  </>
}

function CopyField({ label, value }: { label: string; value: string }) {
  const [message, setMessage] = useState('')
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setMessage('Скопировано.')
    } catch {
      setMessage('Выделите и скопируйте текст из поля.')
    }
  }
  return <div className="form-stack">
    <label>{label} <input readOnly value={value} onFocus={(event) => event.currentTarget.select()} /></label>
    <div className="button-group"><button type="button" className="button secondary" onClick={() => void copy()}>
      <Clipboard size={16} />Скопировать</button><small role="status">{message}</small></div>
  </div>
}

function SpendForm({ path, item, onClose, onSaved }: { path: string; item: Campaign; onClose: () => void; onSaved: () => void }) {
  const action = useAction()
  return <Modal title="Расходы на публикацию" busy={action.busy} onClose={onClose}>
    <form className="modal-body form-stack" onSubmit={(event) => formAction(event, (form) => {
      const amount = formText(form, 'spend')
      void action.run(async () => {
        await api(path + `/campaigns/${item.post_id}`, 'PATCH', { cost_kopecks: amount ? Math.round(Number(amount) * 100) : null })
        onSaved()
      })
    })}>
      <label>Сумма за всё время, ₽ <input name="spend" type="number" min="0" max="100000000" step="0.01"
        defaultValue={item.cost_kopecks === null ? '' : item.cost_kopecks / 100} /></label>
      <small className="muted">Оставьте поле пустым, если сумма неизвестна.</small>
      <ErrorMessage message={action.error} />
      <div><button className="button primary" disabled={action.busy}>Сохранить</button></div>
    </form>
  </Modal>
}

function AttributionSettings({ companyId, path, onClose, onSaved }: { companyId: number; path: string; onClose: () => void; onSaved: () => void }) {
  const load = useCallback(() => api<Settings>(path + '/settings'), [path])
  const settings = useData(load)
  const action = useAction()
  const [key, setKey] = useState('')
  const [replaceKey, setReplaceKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const data = settings.data

  async function rotate() {
    await action.run(async () => {
      const result = await api<{ token: string }>(path + '/key', 'POST')
      setKey(result.token)
      setReplaceKey(false)
      settings.setData((old) => old && { ...old, key_configured: true })
      onSaved()
    })
  }

  return <Modal title="Подключение CRM" busy={action.busy} onClose={onClose}>
    <div className="modal-body form-stack">
      <ErrorMessage message={settings.error} retry={settings.retry} />
      <ErrorMessage message={action.error} />
      {!settings.error && !data && <Loading />}
      {!settings.error && data && <>
        <form className="form-stack" onSubmit={(event) => formAction(event, (form) => {
          void action.run(async () => {
            const next = await api<Omit<Settings, 'webhook_url'>>(path + '/settings', 'PATCH', { enabled: form.has('enabled'), target_url: formText(form, 'target_url') })
            settings.setData({ ...data, ...next })
            setSaved(true)
            onSaved()
          })
        })} onChange={() => setSaved(false)}>
          <label className="toggle-row"><input type="checkbox" name="enabled" defaultChecked={data.enabled} />Включить учёт заявок и продаж</label>
          <label>HTTPS-ссылка на страницу заявки <input type="url" name="target_url" defaultValue={data.target_url} maxLength={1500} /></label>
          <div className="button-group"><button className="button primary" disabled={action.busy}>Сохранить</button>
            {saved && <small role="status">Настройки сохранены.</small>}</div>
        </form>
        <CRMConnections companyId={companyId} />
        <details>
          <summary>Приём событий из другой системы</summary>
          <div className="form-stack">
            <CopyField label="Адрес приёма событий" value={data.webhook_url} />
            <small className="muted">Техническому специалисту понадобятся адрес и ключ. Для отчёта достаточно идентификаторов, дат, метки ссылки и сумм.</small>
            <p>POST с JSON. Заголовок <code>Authorization: Bearer &lt;ключ&gt;</code>.</p>
            <p>Передайте <code>event_id</code> (уникальный ID события), <code>campaign_code</code> (значение <code>utm_campaign</code>),
              {' '}<code>kind</code> (<code>lead</code>, <code>sale</code> или <code>refund</code>), <code>reference</code> (ID заявки или заказа),
              {' '}<code>occurred_at</code> (дата и время ISO 8601 с часовым поясом). Для оплаты и возврата укажите <code>amount_kopecks</code> целым положительным числом и <code>currency</code> со значением <code>RUB</code>.</p>
            <p>При повторной отправке используйте исходный <code>event_id</code>. Для каждого возврата укажите новый идентификатор события и ID исходного заказа.</p>
            {key && <><small>Ключ доступен до закрытия окна.</small><CopyField key={key} label="Новый ключ" value={key} /></>}
            {replaceKey && <p>После выпуска нового ключа настройте отправку событий с ним. Старый ключ перестанет действовать сразу.</p>}
            <div className="button-group">
              <button type="button" className="button secondary" disabled={action.busy} onClick={() => {
                if (!data.key_configured || replaceKey) void rotate()
                else setReplaceKey(true)
              }}>{replaceKey ? 'Выпустить новый ключ' : data.key_configured ? 'Заменить ключ' : 'Выпустить ключ'}</button>
              {replaceKey && <button type="button" className="button secondary" disabled={action.busy} onClick={() => setReplaceKey(false)}>Отмена</button>}
            </div>
          </div>
        </details>
      </>}
    </div>
  </Modal>
}
