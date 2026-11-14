import { useCallback, useState } from 'react'
import { KeyRound, Link, Plus } from 'lucide-react'
import { api, companyPath, roles } from './api'
import type { Company, Role, User } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, useAction, useData } from './ui'

type Access = { id: number; company_id: number; role: Role; active: boolean }
type Invitation = { id: number; role: Role; expires_at: number; status: 'active' | 'used' | 'revoked' | 'expired' }
const inviteStatuses = { active: 'Действует', used: 'Принято', revoked: 'Отозвано', expired: 'Истекло' }
export function RoleOptions() {
  return <>{Object.entries(roles).filter(([role]) => role !== 'super_admin').map(([role, name]) => <option key={role} value={role}>{name}</option>)}</>
}

export function Invitations({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/invitations'
  const load = useCallback(() => api<Invitation[]>(path), [path])
  const invites = useData(load)
  const action = useAction()
  const [open, setOpen] = useState(false)
  const [url, setUrl] = useState('')
  return <section className="invitation-section">
    <div className="page-header"><h2>Приглашения</h2><button className="button secondary" onClick={() => { setUrl(''); action.setError(''); setOpen(true) }}><Plus size={17} />Пригласить</button></div>
    <ErrorMessage message={invites.error || (!open ? action.error : '')} retry={invites.retry} />
    {!!invites.data?.length && <ul className="surface data-list">{invites.data.map(invite => <li className="data-row" key={invite.id}>
      <Link size={17} /><div className="data-copy"><strong>{roles[invite.role]}</strong><small>{inviteStatuses[invite.status]}</small></div>
      {invite.status === 'active' && <button className="button secondary" disabled={action.busy} onClick={() => void action.run(async () => { await api(path + '/' + invite.id, 'DELETE'); invites.retry() })}>Отозвать</button>}
    </li>)}</ul>}
    {open && <Modal title="Пригласить сотрудника" busy={action.busy} onClose={() => setOpen(false)}>
      {url ? <div className="modal-body form-stack"><label>Ссылка на 7 дней<input value={url} readOnly onFocus={e => e.target.select()} /></label>
        <p className="muted">Передайте ссылку получателю. Он сам укажет имя, почту и пароль.</p>
        <div className="modal-actions"><button className="button primary" onClick={() => setOpen(false)}>Готово</button></div></div> :
        <form className="modal-body form-stack" onSubmit={event => formAction(event, form => void action.run(async () => {
          const invite = await api<{ token: string }>(path, 'POST', { role: formText(form, 'role') })
          setUrl(window.location.origin + '/#invite=' + invite.token)
          invites.retry()
        }))}>
          <label>Роль<select name="role" defaultValue="admin"><RoleOptions /></select></label>
          <ErrorMessage message={action.error} />
          <div className="modal-actions"><button className="button primary" disabled={action.busy}>Создать ссылку</button></div>
        </form>}
    </Modal>}
  </section>
}

export function AccessPage({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/access'
  const load = useCallback(() => api<Access[]>(path), [path])
  const access = useData(load)
  const [selected, setSelected] = useState<Access>()
  const [confirmRemoval, setConfirmRemoval] = useState(false)
  const action = useAction()
  return <>
    <PageHeader title="Доступы" />
    <ErrorMessage message={access.error} retry={access.retry} />
    {!access.data ? !access.error && <Loading /> : access.data.length ? <ul className="surface data-list">{access.data.map(profile =>
      <li key={profile.id}><button className="post-row employee-row" onClick={() => { action.setError(''); setConfirmRemoval(false); setSelected(profile) }}>
        <span className="row-icon"><KeyRound size={17} /></span><span className="post-row-copy"><strong>Профиль #{profile.id}</strong><small>{company.name}</small></span>
        <span className="muted">{roles[profile.role]} · {profile.active ? 'Активен' : 'Отключён'}</span>
      </button></li>)}</ul> : <div className="surface"><Empty icon={<KeyRound size={26} />} title="Профилей пока нет" /></div>}
    <Invitations company={company} />
    {selected && <Modal title={confirmRemoval ? `Удалить профиль #${selected.id}?` : `Профиль #${selected.id}`} busy={action.busy} onClose={() => setSelected(undefined)}>
      {confirmRemoval ? <div className="modal-body form-stack">
        <p>Профиль исчезнет из доступов компании «{company.name}». Материалы и доступ к другим компаниям сохранятся.</p>
        <ErrorMessage message={action.error} />
        <div className="modal-actions">
          <button className="button secondary" disabled={action.busy} onClick={() => { action.setError(''); setConfirmRemoval(false) }}>Отмена</button>
          <button className="button danger" disabled={action.busy} onClick={() => void action.run(async () => {
            await api(path + '/' + selected.id, 'DELETE')
            access.setData(current => current?.filter(profile => profile.id !== selected.id))
            setSelected(undefined); setConfirmRemoval(false)
          })}>Удалить профиль</button>
        </div>
      </div> : <form className="modal-body form-stack" onSubmit={event => formAction(event, form => void action.run(async () => {
        await api(path + '/' + selected.id, 'PATCH', { role: formText(form, 'role'), active: form.has('active') })
        setSelected(undefined); access.retry()
      }))}>
        <label>Роль<select name="role" defaultValue={selected.role}><RoleOptions /></select></label>
        <label className="toggle-row"><input type="checkbox" name="active" defaultChecked={selected.active} />Доступ к компании</label>
        <ErrorMessage message={action.error} />
        <div className="modal-actions"><button type="button" className="button danger" disabled={action.busy} onClick={() => { action.setError(''); setConfirmRemoval(true) }}>Удалить профиль</button><button className="button primary" disabled={action.busy}>Сохранить</button></div>
      </form>}
    </Modal>}
  </>
}

export function AcceptInvitation({ token, onAuth }: { token: string; onAuth: (user: User) => void }) {
  const load = useCallback(() => api<{ company_name: string; role: Role }>('/invitations/' + encodeURIComponent(token)), [token])
  const invite = useData(load)
  const action = useAction()
  return <main className="auth"><section className="auth-card">
    <h1>Приглашение</h1>
    <ErrorMessage message={invite.error} />
    {invite.error && <a href="/">Войти в кабинет</a>}
    {invite.data && <form className="form-stack" onSubmit={event => formAction(event, form => void action.run(async () => {
      const user = await api<User>('/invitations/' + encodeURIComponent(token) + '/accept', 'POST', {
        name: formText(form, 'name'), email: formText(form, 'email'), password: String(form.get('password') ?? ''),
      })
      window.history.replaceState(null, '', '/')
      onAuth(user)
    }))}>
      <p>{invite.data.company_name}<br />{roles[invite.data.role]}</p>
      <label>Имя<input name="name" required maxLength={150} autoComplete="name" /></label>
      <label>Почта<input name="email" type="email" required autoComplete="username" /></label>
      <label>Пароль<input name="password" type="password" required minLength={10} maxLength={256} autoComplete="current-password" /></label>
      <small className="muted">От 10 символов. Если аккаунт уже есть, используйте его пароль.</small>
      <ErrorMessage message={action.error} />
      <button className="button primary" disabled={action.busy}>Принять приглашение</button>
    </form>}
  </section></main>
}
