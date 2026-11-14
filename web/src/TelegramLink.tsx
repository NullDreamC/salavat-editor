import { useCallback, useState } from 'react'
import { ArrowUpRight, Check, RefreshCw } from 'lucide-react'
import { api, companyPath, fullDate } from './api'
import type { Company } from './api'
import { ErrorMessage, Loading, Modal, useAction, useData } from './ui'

type LinkStatus = { linked: boolean; configured: boolean }
type Link = { command: string; expires_at: string; url: string }

export function TelegramLink({ company, onClose }: { company: Company; onClose: () => void }) {
  const path = companyPath(company.id) + '/telegram-link'
  const load = useCallback(() => api<LinkStatus>(path), [path])
  const status = useData(load)
  const action = useAction()
  const [link, setLink] = useState<Link>()
  const [copied, setCopied] = useState(false)
  return <Modal title="Привязка Telegram" onClose={onClose} busy={action.busy}>
    <div className="modal-body form-stack">
      <strong>{company.name}</strong>
      <ErrorMessage message={status.error} retry={status.retry} />
      {!status.data && !status.error && <Loading />}
      {status.data && (status.data.linked ? <p className="inline-label saved"><Check size={17} />Telegram привязан</p> :
        !status.data.configured ? <p className="muted">Бот компании ещё не подключён.</p> :
        link ? <>
          <p>Откройте бота и нажмите «Запустить». Ссылка действует до {fullDate(link.expires_at, company.timezone)}.</p>
          <label>Одноразовая команда<input value={link.command} readOnly onFocus={(event) => event.target.select()} /></label>
          <div className="button-group">
            <button className="button secondary" onClick={() => void action.run(async () => {
              await navigator.clipboard.writeText(link.command)
              setCopied(true)
            })} disabled={action.busy}>{copied ? 'Скопировано' : 'Скопировать'}</button>
            <button className="button secondary" onClick={status.retry}><RefreshCw size={16} />Проверить привязку</button>
          </div>
          <div className="modal-actions">
            <button className="button secondary" disabled={action.busy} onClick={() => void action.run(async () => {
              setCopied(false)
              setLink(await api<Link>(path, 'POST'))
            })}>Новая ссылка</button>
            <a className="button primary" href={link.url} target="_blank" rel="noreferrer">Открыть бота<ArrowUpRight size={16} /></a>
          </div>
        </> : <div className="modal-actions">
          <button className="button primary" disabled={action.busy} onClick={() => void action.run(async () => {
            setCopied(false)
            setLink(await api<Link>(path, 'POST'))
          })}>Создать ссылку</button>
        </div>)}
      <ErrorMessage message={action.error} />
    </div>
  </Modal>
}
