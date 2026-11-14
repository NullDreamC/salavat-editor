import { useCallback, useEffect, useState } from 'react'
import { Activity, BookOpen, Building2, CalendarDays, ChartNoAxesCombined, ChevronDown, Files, Inbox as InboxIcon, KeyRound, Link2, ListTodo, LogOut, MessageSquare, Newspaper, Plus, Search, Settings2, Users, Wallet } from 'lucide-react'
import { api, ApiError, errorText, roles } from './api'
import type { Company, User } from './api'
import { Brand, Empty, ErrorMessage, formAction, formText, Loading, Modal, useAction, useData } from './ui'
import { Posts } from './Posts'
import { Materials } from './Materials'
import { Calendar } from './Calendar'
import { ActivityPage, CompanySettings, Employees, SharedApiSettings } from './Settings'
import { PasswordDialog } from './PasswordDialog'
import { AcceptInvitation, AccessPage } from './Access'
import { TelegramLink } from './TelegramLink'
import { Knowledge } from './Knowledge'
import { AssistantTasks } from './AssistantTasks'
import { ContentPlan } from './ContentPlan'
import { Inbox } from './Inbox'
import { Analytics } from './Analytics'
import { Reports } from './Reports'
import { Research } from './Research'
import { Billing } from './Billing'

const routes = [
  { id: 'access', label: 'Доступы', icon: KeyRound },
  { id: 'posts', label: 'Публикации', icon: Newspaper },
  { id: 'tasks', label: 'Задачи', icon: MessageSquare },
  { id: 'plan', label: 'Контент-план', icon: ListTodo },
  { id: 'research', label: 'Исследования', icon: Search },
  { id: 'materials', label: 'Материалы', icon: Files },
  { id: 'knowledge', label: 'База знаний', icon: BookOpen },
  { id: 'inbox', label: 'Обращения', icon: InboxIcon },
  { id: 'analytics', label: 'Статистика', icon: ChartNoAxesCombined },
  { id: 'reports', label: 'Отчёты', icon: Files },
  { id: 'calendar', label: 'Календарь', icon: CalendarDays },
  { id: 'employees', label: 'Сотрудники', icon: Users },
  { id: 'activity', label: 'Журнал', icon: Activity },
  { id: 'settings', label: 'Настройки', icon: Settings2 },
  { id: 'billing', label: 'Расходы', icon: Wallet },
  { id: 'shared', label: 'Общие ключи', icon: Link2 },
] as const
type Route = typeof routes[number]['id']

export function App() {
  const [user, setUser] = useState<User | null>()
  const [setup, setSetup] = useState(false)
  const [notice, setNotice] = useState('')
  const [inviteToken, setInviteToken] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('invite'))
  const [failure, setFailure] = useState('')
  const [attempt, setAttempt] = useState(0)
  const retry = () => setAttempt((current) => current + 1)
  useEffect(() => {
    if (inviteToken) return
    let active = true
    setFailure('')
    async function checkSession() {
      try {
        const account = await api<User>('/auth/me')
        if (active) setUser(account)
      } catch (cause) {
        if (!(cause instanceof ApiError && cause.status === 401)) throw cause
        const initial = await api<{ required: boolean }>('/auth/setup')
        if (active) {
          setSetup(initial.required)
          setUser(null)
        }
      }
    }
    checkSession().catch((cause) => { if (active) setFailure(errorText(cause)) })
    return () => { active = false }
  }, [attempt, inviteToken])
  if (inviteToken) return <AcceptInvitation token={inviteToken} onAuth={account => { setInviteToken(null); setUser(account) }} />
  if (user) return <Workspace user={user} signOut={() => { setSetup(false); setUser(null) }}
    passwordChanged={() => { setNotice('Пароль изменён. Войдите снова.'); setSetup(false); setUser(null) }} />
  if (user === null) return <Auth setup={setup} notice={notice} onAuth={(account) => { setNotice(''); setUser(account) }} />
  return <main className="auth"><div className="auth-card"><Brand />
    {failure ? <ErrorMessage message={failure} retry={retry} /> : <Loading />}
  </div></main>
}

function Auth({ setup, notice, onAuth }: { setup: boolean; notice: string; onAuth: (user: User) => void }) {
  const action = useAction()
  return <main className="auth">
    <section className="auth-card">
      <Brand />
      <h1>{setup ? 'Первый запуск' : 'Вход в редакцию'}</h1>
      {setup && <p className="auth-notice muted">Главный администратор</p>}
      {notice && <p className="auth-notice saved" role="status">{notice}</p>}
      <form className="form-stack" onSubmit={(event) => formAction(event, (form) => {
        void action.run(async () => {
          const credentials = { email: formText(form, 'email'), password: String(form.get('password') ?? '') }
          onAuth(await api<User>(setup ? '/auth/setup' : '/auth/login', 'POST',
            setup ? { ...credentials, name: formText(form, 'name') } : credentials))
        })
      })}>
        {setup && <label>Имя<input name="name" autoComplete="name" required maxLength={150} /></label>}
        <label>Почта<input name="email" type="email" autoComplete="username" required /></label>
        <label>Пароль<input name="password" type="password" autoComplete={setup ? 'new-password' : 'current-password'}
          minLength={setup ? 10 : undefined} maxLength={256} required /></label>
        {setup && <small className="muted">Пароль должен содержать не менее 10 символов.</small>}
        <ErrorMessage message={action.error} />
        <button className="button primary" disabled={action.busy}>
          {action.busy ? 'Подождите…' : setup ? 'Создать аккаунт' : 'Войти'}
        </button>
      </form>
    </section>
    <aside className="auth-glass" aria-hidden="true"><span /><span /><span /></aside>
  </main>
}

function Workspace({ user, signOut, passwordChanged }: { user: User; signOut: () => void; passwordChanged: () => void }) {
  const loadCompanies = useCallback(() => api<Company[]>('/companies'), [])
  const companies = useData(loadCompanies)
  const [companyId, setCompanyId] = useState<number>()
  const [route, setRoute] = useState<Route>(user.role === 'super_admin' ? 'access' : 'posts')
  const [showCreate, setShowCreate] = useState(false)
  const [newPost, setNewPost] = useState<number[] | null>(null)
  const [sessionExpired, setSessionExpired] = useState(false)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const [telegramOpen, setTelegramOpen] = useState(false)
  const action = useAction()
  const company = companies.data?.find((item) => item.id === companyId) ?? companies.data?.[0]
  const isSuper = user.role === 'super_admin'
  const isAdmin = company?.role === 'admin'
  const visibleRoutes = routes.filter(({ id }) => {
    if (isSuper) return id === 'shared' || id === 'billing' || !!company && ['access', 'settings'].includes(id)
    if (id === 'access' || id === 'shared') return false
    if (id === 'calendar') return !!company?.calendar_enabled
    if (id === 'employees' || id === 'settings' || id === 'billing') return isAdmin
    return true
  })
  const screen = visibleRoutes.some(({ id }) => id === route) ? route : isSuper ? 'access' : 'posts'
  const initials = user.name.trim().split(/\s+/).slice(0, 2).map((word) => word[0]).join('').toUpperCase()
  const logo = <div className="sidebar-heading"><Brand /></div>
  const pickCompany = (id: number) => { setCompanyId(id); setNewPost(null) }
  const updateCompany = (updated: Company) => companies.setData((current) => current?.map(
    (item) => item.id === updated.id ? { ...item, ...updated } : item
  ))
  function removeCompany(id: number) {
    companies.setData((current) => current?.filter((item) => item.id !== id))
    setCompanyId(undefined)
    setRoute('access')
  }

  useEffect(() => {
    const expire = () => setSessionExpired(true)
    window.addEventListener('editor-session-expired', expire)
    return () => window.removeEventListener('editor-session-expired', expire)
  }, [])

  return <div className="workspace">
    <aside className="sidebar">
      {logo}
      <div className="company-picker">
        <Building2 size={17} aria-hidden="true" />
        <select aria-label="Компания" value={company?.id ?? ''} onChange={(event) => {
          if (event.target.value === 'new') setShowCreate(true)
          else pickCompany(Number(event.target.value))
        }}>
          {!company && <option value="">Компания</option>}
          {companies.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          {isSuper && companies.data && <option value="new">Новая компания…</option>}
        </select>
        <ChevronDown size={15} aria-hidden="true" />
      </div>
      {(company || isSuper) && <nav className="navigation" aria-label="Разделы редакции">
        {visibleRoutes
          .map(({ id, label, icon: Icon }) => <button key={id} className={screen === id ? 'nav-link active' : 'nav-link'}
            aria-current={screen === id ? 'page' : undefined} onClick={() => { setRoute(id); setNewPost(null) }}>
            <Icon size={18} strokeWidth={1.7} aria-hidden="true" /><span>{label}</span>
          </button>)}
      </nav>}
      <div className="account">
        <span className="avatar" aria-hidden="true">{initials}</span>
        <div className="account-name"><strong>{user.name}</strong><small>{roles[company?.role ?? user.role]}</small></div>
        {!isSuper && company && <button className="icon-button" type="button" aria-label="Привязать Telegram" title="Привязать Telegram" onClick={() => setTelegramOpen(true)}>
          <Link2 size={18} />
        </button>}
        <button className="icon-button" type="button" aria-label="Сменить пароль" title="Сменить пароль" onClick={() => setPasswordOpen(true)}>
          <KeyRound size={18} />
        </button>
        <button className="icon-button" type="button" disabled={action.busy} aria-label="Выйти" title="Выйти"
          onClick={() => void action.run(async () => { await api('/auth/logout', 'POST'); signOut() })}>
          <LogOut size={18} />
        </button>
      </div>
    </aside>
    <main className="content" key={company?.id}>
      <ErrorMessage message={!showCreate ? action.error : ''} />
      {screen === 'shared' ? <SharedApiSettings /> : screen === 'billing' && (company || isSuper) ?
        <Billing company={company} global={isSuper} /> :
        companies.error ? <ErrorMessage message={companies.error} retry={companies.retry} /> : !companies.data ? <Loading /> :
        !company ? <Empty icon={<Building2 size={27} />} title="Нет доступных компаний"
          action={isSuper && <button className="button primary" onClick={() => setShowCreate(true)}><Plus size={17} />Добавить компанию</button>} /> :
        screen === 'access' ? <AccessPage company={company} /> :
        screen === 'posts' ? <Posts company={company} newPost={newPost} clearNewPost={() => setNewPost(null)} /> :
        screen === 'materials' ? <Materials company={company} createPost={(ids) => { setNewPost(ids); setRoute('posts') }} /> :
        screen === 'tasks' ? <AssistantTasks company={company} /> :
        screen === 'plan' ? <ContentPlan company={company} /> :
        screen === 'research' ? <Research company={company} /> :
        screen === 'knowledge' ? <Knowledge company={company} /> :
        screen === 'inbox' ? <Inbox company={company} /> :
        screen === 'analytics' ? <Analytics company={company} /> :
        screen === 'reports' ? <Reports company={company} /> :
        screen === 'calendar' ? <Calendar company={company} /> :
        screen === 'employees' ? <Employees company={company} currentUserId={user.id} /> :
        screen === 'activity' ? <ActivityPage company={company} /> :
        <CompanySettings company={company} onUpdate={updateCompany} onRemove={isSuper ? () => removeCompany(company.id) : undefined} />}
    </main>
    {passwordOpen && <PasswordDialog onClose={() => setPasswordOpen(false)} onChanged={passwordChanged} />}
    {telegramOpen && company && !isSuper && <TelegramLink company={company} onClose={() => setTelegramOpen(false)} />}
    {showCreate && <Modal title="Новая компания" busy={action.busy} onClose={() => { action.setError(''); setShowCreate(false) }}>
      <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
        const next = await api<Company>('/companies', 'POST', { name: formText(form, 'name') })
        companies.setData((current) => [...(current ?? []), next])
        pickCompany(next.id)
        setRoute('settings')
        setShowCreate(false)
      }))}>
        <label>Название<input name="name" autoFocus required maxLength={150} /></label>
        <ErrorMessage message={action.error} />
        <div className="modal-actions"><button className="button primary" disabled={action.busy}>Создать</button></div>
      </form>
    </Modal>}
    {sessionExpired && <Modal title="Войдите снова" onClose={signOut}>
      <div className="modal-body"><p>Срок сеанса истёк.</p>
        <div className="modal-actions"><button className="button primary" onClick={signOut}>Войти</button></div>
      </div>
    </Modal>}
  </div>
}
