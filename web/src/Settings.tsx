import { useCallback, useState } from 'react'
import { Activity, Check, ChevronRight, Plus, RefreshCw, UserRound, Users } from 'lucide-react'
import { api, companyPath, fullDate, fundingNames, roles } from './api'
import type { Audit, Company, FundingMode, Role, Settings, Usage, User } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'
import { PasswordDialog } from './PasswordDialog'
import { Invitations, RoleOptions } from './Access'
import { BrandSettings } from './BrandSettings'
import { SocialConnections } from './SocialConnections'
import { ReviewPolicy } from './ReviewPolicy'

export function CompanySettings({ company, onUpdate, onRemove }: {
  company: Company; onUpdate: (value: Company) => void; onRemove?: () => void
}) {
  const path = companyPath(company.id)
  const load = useCallback(() => api<Settings>(path + '/settings'), [path])
  const settings = useData(load)
  const [confirmRemoval, setConfirmRemoval] = useState(false)
  const removal = useAction()
  return <>
    <PageHeader title="Настройки" action={onRemove && <button className="button danger" onClick={() => setConfirmRemoval(true)}>Убрать компанию</button>} />
    <ErrorMessage message={settings.error} retry={settings.retry} />
    {settings.data ? <SettingsForm value={settings.data} path={path} onSaved={(value) => {
      settings.setData(value)
      onUpdate(value)
    }} /> : !settings.error && <Loading />}
    {settings.data && <BrandSettings path={path} />}
    {settings.data && <SocialConnections company={company} />}
    {settings.data && <ReviewPolicy company={company} />}
    {confirmRemoval && <Modal title="Убрать компанию" busy={removal.busy} onClose={() => {
      removal.setError('')
      setConfirmRemoval(false)
    }}>
      <div className="modal-body form-stack">
        <p>Компания «{company.name}» исчезнет из списка. Материалы и публикации сохранятся в базе.</p>
        <ErrorMessage message={removal.error} />
        <div className="modal-actions">
          <button className="button secondary" disabled={removal.busy} onClick={() => setConfirmRemoval(false)}>Отмена</button>
          <button className="button danger" disabled={removal.busy} onClick={() => void removal.run(async () => {
            await api(path, 'DELETE')
            setConfirmRemoval(false)
            onRemove?.()
          })}>Убрать компанию</button>
        </div>
      </div>
    </Modal>}
  </>
}

function SettingsForm({ value, path, onSaved }: { value: Settings; path: string; onSaved: (settings: Settings) => void }) {
  const [provider, setProvider] = useState(value.provider)
  const [model, setModel] = useState(value.model)
  const [saved, setSaved] = useState(false)
  const [modelsRevision, setModelsRevision] = useState(0)
  const [funding, setFunding] = useState<FundingMode>(value.funding_mode ?? 'own')
  const [requestLimit, setRequestLimit] = useState(String(value.requests_limit))
  const isSuper = value.role === 'super_admin'
  const canEditLimit = isSuper || funding === 'own'
  const ownKeys = funding === 'own' && (isSuper || value.provider_credentials_editable !== false)
  const action = useAction()
  return <form className="surface settings-form" onChange={() => setSaved(false)}
    onSubmit={(event) => {
      const element = event.currentTarget
      formAction(event, (form) => void action.run(async () => {
        setSaved(false)
        const keyFields = ['openai_key', 'yandex_key', 'telegram_token']
        const secrets = Object.fromEntries(keyFields.map((name) => [name, formText(form, name)]).filter(([, secret]) => secret))
        const folder = formText(form, 'yandex_folder_id')
        const updated = await api<Settings>(path, 'PATCH', {
          name: formText(form, 'name'), timezone: formText(form, 'timezone'), calendar_enabled: form.has('calendar_enabled'),
          provider, model: model.trim(),
          ...(ownKeys && folder !== value.yandex_folder_id && { yandex_folder_id: folder }),
          ...(isSuper && { funding_mode: funding }),
          editorial_rules: formText(form, 'editorial_rules'),
          channel_id: formText(form, 'channel_id'), chat_id: formText(form, 'chat_id'), bot_username: formText(form, 'bot_username'),
          monthly_limit: Number(formText(form, 'monthly_limit')),
          ...(canEditLimit && { requests_limit: Number(requestLimit) }),
          ...secrets,
        })
        for (const field of keyFields) {
          const input = element.elements.namedItem(field)
          if (input instanceof HTMLInputElement) input.value = ''
        }
        onSaved(updated)
        if (secrets.openai_key || secrets.yandex_key || updated.yandex_folder_id !== value.yandex_folder_id || updated.funding_mode !== value.funding_mode) {
          setModelsRevision((revision) => revision + 1)
        }
        setSaved(true)
      }))
    }}>
    <fieldset disabled={action.busy} className="settings-section">
      <h2>Компания</h2>
      <div className="form-grid">
        <label>Название<input name="name" required maxLength={150} defaultValue={value.name} /></label>
        <label>Часовой пояс<input name="timezone" required defaultValue={value.timezone} placeholder="Europe/Moscow" /></label>
        <label className="toggle-row"><input type="checkbox" name="calendar_enabled" defaultChecked={value.calendar_enabled} />Включить календарь</label>
      </div>
    </fieldset>
    <fieldset disabled={action.busy} className="settings-section">
      <h2>Telegram</h2>
      <div className="form-grid">
        <label>Канал публикаций<input name="channel_id" placeholder="@название_канала" defaultValue={value.channel_id} /></label>
        <label>ID чата<input name="chat_id" defaultValue={value.chat_id} placeholder="-100…" /></label>
        <label>Имя бота<input name="bot_username" defaultValue={value.bot_username} placeholder="@имя_бота" /></label>
        <Secret label="Токен бота" name="telegram_token" configured={value.telegram_configured} />
        {value.webapp_url && <a href={value.webapp_url} target="_blank" rel="noreferrer" className="form-wide">Открыть WebApp</a>}
      </div>
    </fieldset>
    <fieldset disabled={action.busy} className="settings-section">
      <h2>Подготовка текста</h2>
      <div className="form-grid">
        <label>Сервис<select aria-label="Сервис" value={provider} onChange={(event) => {
          const next = event.target.value as Settings['provider']
          setProvider(next)
          setModel(next === value.provider ? value.model : next === 'openai' ? 'gpt-4.1-mini' : 'qwen3.6-35b-a3b')
        }}>
          <option value="openai">OpenAI</option><option value="yandex">Yandex AI Studio</option>
        </select></label>
        <ModelSelect key={provider} path={path} provider={provider} model={model} onChange={setModel} revision={modelsRevision} />
        {ownKeys && <>
          <Secret label="Ключ OpenAI" name="openai_key" configured={value.openai_configured} />
          <Secret label="Ключ Yandex" name="yandex_key" configured={value.yandex_configured} />
          <label>ID каталога Yandex<input name="yandex_folder_id" defaultValue={value.yandex_folder_id} /></label>
        </>}
        <label className="form-wide">Правила текстов<textarea name="editorial_rules" rows={4} maxLength={12000}
          defaultValue={value.editorial_rules ?? ''} placeholder="Тон, рубрики, принятые обращения" /></label>
      </div>
    </fieldset>
    <fieldset disabled={action.busy} className="settings-section">
      <h2>Месячные лимиты</h2>
      <div className="form-grid">
        <label className="form-wide">Источник ключей<select aria-label="Источник ключей" value={funding} disabled={!isSuper}
          onChange={(event) => setFunding(event.target.value as FundingMode)}>
          {Object.entries(fundingNames).filter(([mode]) => mode !== 'salavat' || value.id !== 1)
            .map(([mode, label]) => <option value={mode} key={mode}>{label}</option>)}
        </select></label>
        <label>Запросы<input type="number" name="requests_limit" min={funding === 'own' ? 0 : 1} max={1_000_000_000}
          step={1} value={requestLimit} disabled={!canEditLimit} required={funding !== 'own'}
          onChange={(event) => setRequestLimit(event.target.value)} /></label>
        <label>Бюджет, ₽<input type="number" name="monthly_limit" min={0} step={1} defaultValue={value.monthly_limit} /></label>
        <small className="muted">{funding === 'own' ? '0 — без ограничения запросов' : 'Лимит для компании'}</small>
        <small className="muted">Фактические списания — у провайдера.</small>
      </div>
    </fieldset>
    <div className="settings-footer">
      <ErrorMessage message={action.error} />
      {saved && <span className="inline-label saved" role="status"><Check size={17} />Настройки сохранены</span>}
      <button className="button primary" disabled={action.busy}>{action.busy ? 'Сохраняем…' : 'Сохранить настройки'}</button>
    </div>
  </form>
}

function ModelSelect({ path, provider, model, onChange, revision }: {
  path: string; provider: Settings['provider']; model: string; onChange: (model: string) => void; revision: number
}) {
  const load = useCallback(() => api<string[]>(`${path}/models?provider=${provider}`), [path, provider, revision])
  const models = useData(load)
  const selected = provider === 'yandex' ? model.replace(/^gpt:\/\/[^/]+\//, '') : model
  const missing = !models.data?.includes(selected)
  return <div className="model-field">
    <label htmlFor="company-model">Модель</label>
    <div className="model-control">
      <select id="company-model" name="model" value={selected} onChange={(event) => onChange(event.target.value)}
        disabled={!models.data?.length} aria-describedby={models.error ? 'model-error' : undefined}>
        {missing && <option value={selected} disabled>{selected || 'Выберите модель'}{models.data ? ' — нет в списке' : ''}</option>}
        {models.data?.map((id) => <option key={id} value={id}>{id}</option>)}
      </select>
      <button type="button" className="icon-button" aria-label="Обновить модели" title="Обновить модели"
        disabled={!models.data && !models.error} onClick={models.retry}><RefreshCw size={18} /></button>
    </div>
    {!models.data && !models.error && <small className="muted" role="status">Загружаем модели…</small>}
    {models.error && <small id="model-error" className="model-error" role="alert">{models.error}</small>}
    {models.data?.length === 0 && <small className="muted">Доступных моделей нет.</small>}
  </div>
}

function Secret({ label, name, configured }: { label: string; name: string; configured: boolean }) {
  return <label>{label}
    <input type="password" name={name} aria-label={label} autoComplete="new-password" placeholder={configured ? '••••••••' : 'Введите для подключения'} />
    {configured && <small className="muted">Подключено</small>}
  </label>
}

type SharedApi = Pick<Settings, 'openai_configured' | 'yandex_configured' | 'yandex_folder_id'>

export function SharedApiSettings() {
  const load = useCallback(() => api<SharedApi>('/shared-api'), [])
  const settings = useData(load)
  const action = useAction()
  const [saved, setSaved] = useState(false)
  const value = settings.data
  return <>
    <PageHeader title="Общие ключи" />
    <ErrorMessage message={settings.error} retry={settings.retry} />
    {value ? <form className="surface settings-form" onChange={() => setSaved(false)} onSubmit={(event) => {
      const element = event.currentTarget
      formAction(event, (form) => void action.run(async () => {
        const openai = formText(form, 'openai_key')
        const yandex = formText(form, 'yandex_key')
        settings.setData(await api<SharedApi>('/shared-api', 'PATCH', {
          ...(openai && { openai_key: openai }), ...(yandex && { yandex_key: yandex }),
          yandex_folder_id: formText(form, 'yandex_folder_id'),
        }))
        for (const name of ['openai_key', 'yandex_key']) {
          const field = element.elements.namedItem(name)
          if (field instanceof HTMLInputElement) field.value = ''
        }
        setSaved(true)
      }))
    }}>
      <fieldset className="settings-section" disabled={action.busy}>
        <h2>Подключения</h2>
        <div className="form-grid">
          <Secret label="Ключ OpenAI" name="openai_key" configured={value.openai_configured} />
          <Secret label="Ключ Yandex" name="yandex_key" configured={value.yandex_configured} />
          <label>ID каталога Yandex<input name="yandex_folder_id" defaultValue={value.yandex_folder_id} /></label>
        </div>
      </fieldset>
      <div className="settings-footer">
        <ErrorMessage message={action.error} />
        {saved && <span className="inline-label saved" role="status"><Check size={17} />Ключи сохранены</span>}
        <button className="button primary" disabled={action.busy}>Сохранить</button>
      </div>
    </form> : !settings.error && <Loading />}
  </>
}

export function Employees({ company, currentUserId }: { company: Company; currentUserId: number }) {
  const path = companyPath(company.id) + '/users'
  const load = useCallback(() => api<User[]>(path), [path])
  const users = useData(load)
  const [selected, setSelected] = useState<User | null>()
  return <>
    <PageHeader title="Сотрудники" action={<button className="button primary" onClick={() => setSelected(null)}><Plus size={18} />Добавить сотрудника</button>} />
    <ErrorMessage message={users.error} retry={users.retry} />
    {!users.data ? !users.error && <Loading /> : !users.data.length ?
      <div className="surface"><Empty icon={<Users size={26} />} title="Нет сотрудников" /></div> :
      <ul className="surface data-list">{users.data.map((user) => <li key={user.id}>
        <button className="post-row employee-row" onClick={() => setSelected(user)}>
          <span className="row-icon"><UserRound size={17} /></span>
          <span className="post-row-copy"><strong>{user.name}</strong><small>{user.email}</small></span>
          <span className="muted">{user.active === false ? 'Доступ отключён' : roles[user.role]}</span>
          <ChevronRight size={17} className="row-chevron" />
        </button>
      </li>)}</ul>}
    <Invitations company={company} />
    {selected !== undefined && <EmployeeForm path={path} user={selected} currentUserId={currentUserId} onClose={() => setSelected(undefined)}
      onSaved={(user) => {
        users.setData((current) => selected ? current?.map((item) => item.id === user.id ? user : item) : [user, ...(current ?? [])])
        setSelected(undefined)
      }} />}
  </>
}

function EmployeeForm({ path, user, currentUserId, onClose, onSaved }: {
  path: string; user: User | null; currentUserId: number; onClose: () => void; onSaved: (user: User) => void
}) {
  const action = useAction()
  const [resetPassword, setResetPassword] = useState(false)
  if (resetPassword && user) return <PasswordDialog reset={{ path: path + '/' + user.id + '/password', name: user.name }} onClose={onClose} />
  return <Modal title={user ? user.name : 'Новый сотрудник'} busy={action.busy} onClose={onClose}>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const telegram = formText(form, 'telegram_id')
      const fields = { role: formText(form, 'role') as Role, telegram_id: telegram || null }
      onSaved(user ? await api<User>(path + '/' + user.id, 'PATCH', { ...fields, active: form.has('active') }) :
        await api<User>(path, 'POST', {
          ...fields, name: formText(form, 'name'), email: formText(form, 'email'), password: String(form.get('password') ?? ''),
        }))
    }))}>
      {!user && <>
        <label>Имя<input name="name" required maxLength={150} autoFocus /></label>
        <label>Почта<input name="email" type="email" required /></label>
        <label>Пароль<input name="password" type="password" autoComplete="new-password" minLength={10} maxLength={256} required /></label>
      </>}
      <label>Роль<select name="role" defaultValue={user?.role ?? 'editor'}>
        <RoleOptions />
      </select></label>
      <small className="muted">Администратор компании может отправлять и согласовывать публикации.</small>
      <label>ID пользователя Telegram<input name="telegram_id" inputMode="numeric" pattern="[1-9][0-9]{0,18}" defaultValue={user?.telegram_id ?? ''} /></label>
      {user && <label className="toggle-row"><input type="checkbox" name="active" defaultChecked={user.active !== false} />Доступ к компании</label>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        {user && user.id !== currentUserId && <button type="button" className="button secondary" disabled={action.busy} onClick={() => setResetPassword(true)}>Сбросить пароль</button>}
        <button className="button primary" disabled={action.busy}>{user ? 'Сохранить' : 'Добавить'}</button>
      </div>
    </form>
  </Modal>
}

const actionNames: Record<string, string> = {
  'member.access': 'Изменён доступ', 'invitation.created': 'Создано приглашение', 'invitation.accepted': 'Приглашение принято',
  'account.password_changed': 'Пароль изменён', 'account.password_reset': 'Пароль сброшен администратором',
  'company.created': 'Создана компания', 'company.settings': 'Изменены настройки',
  'member.created': 'Предоставлен доступ', 'member.edited': 'Изменён доступ', 'member.telegram': 'Изменён доступ к боту',
  'source.created': 'Добавлен материал', 'post.created': 'Создан черновик', 'post.edited': 'Изменена публикация',
  'member.telegram_linked': 'Привязан Telegram', 'post.photo_original': 'Выбраны исходные фотографии',
  'post.photo_generated': 'Оформлено изображение', 'post.photo_failed': 'Не удалось подготовить изображение',
  'post.logo_changed': 'Изменён логотип',
  'post.generated': 'Подготовлен текст', 'post.answered': 'Добавлено уточнение', 'post.card': 'Создана карточка',
  'post.submitted': 'На согласовании', 'post.approved': 'Согласовано', 'post.rejected': 'Возврат на доработку',
  'post.publishing': 'Отправляется', 'post.published': 'Опубликовано', 'post.publish_failed': 'Ошибка отправки',
  'post.publish_interrupted': 'Отправка прервана', 'post.generation_failed': 'Не удалось подготовить текст',
  'event.extracted': 'Событие из материалов', 'event.created': 'Добавлено событие', 'event.edited': 'Изменено событие',
  'event.deleted': 'Удалено событие', bot_error: 'Ошибка Telegram',
}
const number = new Intl.NumberFormat('ru-RU')

export function ActivityPage({ company }: { company: Company }) {
  const path = companyPath(company.id)
  const loadAudit = useCallback(() => api<Audit[]>(path + '/activity'), [path])
  const loadUsage = useCallback(() => api<Usage>(path + '/usage'), [path])
  const history = useData(loadAudit)
  const usage = useData(loadUsage)
  const volume = usage.data
  return <>
    <PageHeader title="Журнал" />
    <section className="surface usage">
      <h2>Использование за месяц</h2>
      <ErrorMessage message={usage.error} retry={usage.retry} />
      {volume ? <dl>
        <div><dt>Запросы</dt><dd>{number.format(volume.requests)}{volume.requests_limit > 0 && <small> / {number.format(volume.requests_limit)}</small>}</dd></div>
        <div><dt>Токены</dt><dd>{number.format(volume.input_tokens + volume.output_tokens)}</dd></div>
        {volume.monthly_limit > 0 && <div><dt>Бюджет, ₽</dt><dd>{number.format(volume.monthly_limit)}</dd></div>}
      </dl> : !usage.error && <Loading />}
    </section>
    <h2 className="section-title">Действия</h2>
    <ErrorMessage message={history.error} retry={history.retry} />
    {!history.data ? !history.error && <Loading /> :
      !history.data.length ? <div className="surface"><Empty icon={<Activity size={26} />} title="Действий пока нет" /></div> :
      <ul className="surface data-list">{history.data.map((entry) => <li className="data-row" key={entry.id}>
        <span className="row-icon"><Activity size={17} /></span>
        <span className="data-copy"><strong>{actionNames[entry.action] ?? (entry.detail || 'Действие')}</strong>
          {actionNames[entry.action] && entry.detail && <small>{entry.detail}</small>}</span>
        <time className="muted" dateTime={entry.created_at}>{fullDate(entry.created_at, company.timezone)}</time>
      </li>)}</ul>}
  </>
}
