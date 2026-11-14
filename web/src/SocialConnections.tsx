import { useCallback, useState } from 'react'
import { api, companyPath } from './api'
import type { Company } from './api'
import { ErrorMessage, formAction, formText, Loading, useAction, useData } from './ui'

const platforms = {
  vk: { name: 'ВКонтакте', destination: 'Числовой ID сообщества', token: 'Токен пользователя с правами публикации' },
  max: { name: 'MAX', destination: 'Числовой ID канала или чата', token: 'Токен бота' },
}
type Platform = keyof typeof platforms
type Connection = {
  destination_id: string
  label: string
  configured: boolean
  token_configured: boolean
  enabled: boolean
}

export function SocialConnections({ company }: { company: Company }) {
  const base = companyPath(company.id)
  const writable = company.role === 'admin' || company.role === 'super_admin'
  return <>
    <h2 className="section-title">ВКонтакте и MAX</h2>
    <PlatformConnection base={base} platform="vk" writable={writable} />
    <PlatformConnection base={base} platform="max" writable={writable} />
  </>
}

function PlatformConnection({ base, platform, writable }: { base: string; platform: Platform; writable: boolean }) {
  const path = base + '/social/' + platform
  const load = useCallback(() => api<Connection>(path), [path])
  const connection = useData(load)
  const labels = platforms[platform]
  return <section className="surface settings-form separate-section">
    <header className="agenda-header"><h3>{labels.name}</h3>
      {connection.data && <span className={'status ' + (connection.data.enabled ? 'status-review' : 'status-draft')}>
        {connection.data.enabled ? 'Использование включено' : 'Использование выключено'}
      </span>}
    </header>
    <ErrorMessage message={connection.error} retry={connection.retry} />
    {!connection.data ? !connection.error && <Loading /> : <ConnectionForm labels={labels} path={path} value={connection.data}
      writable={writable} onSaved={connection.setData} />}
  </section>
}

function ConnectionForm({ labels, path, value, writable, onSaved }: {
  labels: typeof platforms[Platform]; path: string; value: Connection; writable: boolean; onSaved: (value: Connection) => void
}) {
  const [saved, setSaved] = useState(false)
  const [revision, setRevision] = useState(0)
  const action = useAction()
  const tokenStatus = value.token_configured ? 'Токен сохранён.' : 'Добавьте токен для использования площадки.'
  if (!writable) return <div className="form-stack modal-body">
    {value.label && <p>{value.label}</p>}
    {value.destination_id && <p>{labels.destination} — {value.destination_id}</p>}
    <p className="muted">{value.token_configured ? 'Токен сохранён.' : 'Токен не добавлен.'}</p>
  </div>
  return <form key={revision} className="form-stack modal-body" autoComplete="off" onChange={() => setSaved(false)}
    onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const token = formText(form, 'token')
      const savedValue = await api<Connection>(path, 'PATCH', {
        destination_id: formText(form, 'destination_id'), label: formText(form, 'label'), enabled: form.has('enabled'),
        ...(token ? { token } : {}),
      })
      onSaved(savedValue)
      setRevision((value) => value + 1)
      setSaved(true)
    }))}>
    <div className="form-grid">
      <label>{labels.destination}<input name="destination_id" maxLength={20} pattern="-?[1-9][0-9]*" defaultValue={value.destination_id} /></label>
      <label>Название<input name="label" maxLength={150} defaultValue={value.label} /></label>
      <label className="form-wide">{labels.token}<input name="token" type="password" maxLength={2048} autoComplete="new-password"
        placeholder={value.token_configured ? 'Новый токен' : undefined} /></label>
    </div>
    <small className="muted">{tokenStatus}</small>
    <label className="toggle-row"><input name="enabled" type="checkbox" defaultChecked={value.enabled} />Использовать площадку</label>
    <ErrorMessage message={action.error} />
    <div className="modal-actions">
      {saved && <small className="saved" role="status">Настройки сохранены</small>}
      {value.token_configured && <button className="button secondary" type="button" disabled={action.busy} onClick={() => {
        if (window.confirm(`Удалить токен ${labels.name}?`)) void action.run(async () => {
          const savedValue = await api<Connection>(path, 'PATCH', { token: null, enabled: false })
          onSaved(savedValue)
          setRevision((value) => value + 1)
          setSaved(true)
        })
      }}>Удалить токен</button>}
      <button className="button primary" disabled={action.busy}>Сохранить</button>
    </div>
  </form>
}
