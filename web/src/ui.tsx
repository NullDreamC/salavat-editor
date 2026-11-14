import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { AlertCircle, LoaderCircle, X } from 'lucide-react'
import { errorText, statuses } from './api'
import type { Status } from './api'

export function Brand() {
  return <div className="brand">
    <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
    <span>Редакция</span>
  </div>
}

export function ErrorMessage({ message, retry }: { message: string; retry?: () => void }) {
  if (!message) return null
  return <div className="error" role="alert"><AlertCircle size={17} /><span>{message}</span>
    {retry && <button className="text-button" onClick={retry}>Повторить</button>}
  </div>
}

export function Loading({ label = 'Загрузка' }: { label?: string }) {
  return <div className="loading" role="status"><LoaderCircle size={19} className="spin" />{label}</div>
}

export function Empty({ icon, title, action }: { icon: ReactNode; title: string; action?: ReactNode }) {
  return <div className="empty">
    <div className="empty-icon" aria-hidden="true">{icon}</div>
    <h2>{title}</h2>
    {action}
  </div>
}

export function PageHeader({ title, action }: { title: string; action?: ReactNode }) {
  return <header className="page-header"><h1>{title}</h1>{action}</header>
}

export function StatusBadge({ status }: { status: Status }) {
  return <span className={`status status-${status}`}>{statuses[status] ?? status}</span>
}

export function Modal({ title, children, onClose, wide = false, busy = false }: {
  title: string; children: ReactNode; onClose: () => void; wide?: boolean; busy?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])
  return <dialog ref={ref} className={wide ? 'modal modal-wide' : 'modal'} aria-label={title}
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }}>
    <header className="modal-header">
      <h2>{title}</h2>
      <button className="icon-button" type="button" disabled={busy} aria-label="Закрыть" onClick={onClose}><X size={20} /></button>
    </header>
    {children}
  </dialog>
}

export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const locked = useRef(false)
  async function run(action: () => Promise<void>) {
    if (locked.current) return
    locked.current = true
    setBusy(true)
    setError('')
    try {
      await action()
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
      locked.current = false
    }
  }
  return { busy, error, setError, run }
}

export function useData<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true
    setError('')
    load().then(
      (value) => { if (active) setData(value) },
      (cause) => { if (active) setError(errorText(cause)) },
    )
    return () => { active = false }
  }, [load, attempt])
  return { data, error, setData, retry: () => setAttempt((count) => count + 1) }
}

export function formAction(event: FormEvent<HTMLFormElement>, action: (form: FormData) => void) {
  event.preventDefault()
  action(new FormData(event.currentTarget))
}

export function formText(form: FormData, name: string) {
  return String(form.get(name) ?? '').trim()
}
