import { useState } from 'react'
import { api } from './api'
import { ErrorMessage, formAction, Modal, useAction } from './ui'

export function PasswordDialog({ reset, onClose, onChanged }: {
  reset?: { path: string; name: string }
  onClose: () => void
  onChanged?: () => void
}) {
  const action = useAction()
  const [done, setDone] = useState(false)
  return <Modal title={reset ? 'Сбросить пароль' : 'Сменить пароль'} busy={action.busy} onClose={onClose}>
    {done ? <div className="modal-body form-stack">
      <p role="status">Пароль изменён. Сеансы сотрудника завершены.</p>
      <div className="modal-actions"><button className="button primary" onClick={onClose}>Готово</button></div>
    </div> : <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const password = String(form.get('new_password') ?? '')
      if (password !== String(form.get('confirm_password') ?? '')) throw new Error('Пароли не совпадают.')
      await api(reset ? reset.path : '/auth/password', 'POST', {
        new_password: password,
        ...(!reset && { current_password: String(form.get('current_password') ?? '') }),
      })
      if (reset) setDone(true)
      else onChanged?.()
    }))}>
      {reset && <p>{reset.name}</p>}
      {!reset && <label>Текущий пароль<input name="current_password" type="password" autoComplete="current-password" required maxLength={256} autoFocus /></label>}
      <label>Новый пароль<input name="new_password" type="password" autoComplete="new-password" required minLength={10} maxLength={256} autoFocus={!!reset} /></label>
      <label>Повторите пароль<input name="confirm_password" type="password" autoComplete="new-password" required minLength={10} maxLength={256} /></label>
      <small className="muted">От 10 символов. Все сеансы будут завершены.</small>
      {reset && <small className="muted">Пароль один для всех компаний сотрудника.</small>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button className="button primary" disabled={action.busy}>
        {action.busy ? 'Сохраняем…' : reset ? 'Сбросить пароль' : 'Сменить пароль'}
      </button></div>
    </form>}
  </Modal>
}
