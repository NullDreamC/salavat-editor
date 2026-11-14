import { useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { api, companyPath } from './api'
import type { Company } from './api'
import { ErrorMessage, Modal, useAction } from './ui'

export function DeleteCompanyDialog({ company, onClose, onRemoved }: {
  company: Company; onClose: () => void; onRemoved: () => void
}) {
  const action = useAction()
  return <Modal title="Удалить компанию?" busy={action.busy} onClose={onClose}>
    <div className="modal-body form-stack">
      <p>Компания «{company.name}» исчезнет из списка. Доступ сотрудников и фоновые задачи остановятся. Материалы и публикации сохранятся в архиве.</p>
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        <button className="button secondary" disabled={action.busy} onClick={onClose}>Отмена</button>
        <button className="button danger" disabled={action.busy} onClick={() => void action.run(async () => {
          await api(companyPath(company.id), 'DELETE')
          onRemoved()
        })}>Удалить компанию</button>
      </div>
    </div>
  </Modal>
}

export function CompanyManagement({ companies, onClose, onPick, onCreate, onRemove }: {
  companies: Company[]; onClose: () => void; onPick: (id: number) => void; onCreate: () => void; onRemove: (id: number) => void
}) {
  const [selected, setSelected] = useState<Company>()
  if (selected) return <DeleteCompanyDialog company={selected} onClose={() => setSelected(undefined)} onRemoved={() => {
    onRemove(selected.id); setSelected(undefined)
  }} />
  return <Modal title="Компании" onClose={onClose}>
    <div className="modal-body form-stack">
      <ul className="data-list">{companies.map(company => <li className="data-row" key={company.id}>
        <button className="text-button data-copy" onClick={() => onPick(company.id)}>{company.name}</button>
        <button className="button danger" aria-label={`Удалить компанию «${company.name}»`} onClick={() => setSelected(company)}><Trash2 size={16} />Удалить</button>
      </li>)}</ul>
      {!companies.length && <p className="muted">Компаний пока нет.</p>}
      <div className="modal-actions"><button className="button primary" onClick={onCreate}><Plus size={17} />Добавить компанию</button></div>
    </div>
  </Modal>
}
