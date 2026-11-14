import { useCallback, useState } from 'react'
import { api, companyPath, roles } from './api'
import type { Company, Role } from './api'
import { ErrorMessage, Loading, useAction, useData } from './ui'

type ReviewPolicyData = {
  reviewer_ids: number[]
  options: { id: number; role: Role; active: boolean }[]
}

type Selection = { mode: 'all' | 'assigned'; ids: number[] }

export function ReviewPolicy({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/review-policy'
  const load = useCallback(() => api<ReviewPolicyData>(path), [path])
  const policy = useData(load)
  const action = useAction()
  const [draft, setDraft] = useState<Selection>()
  const [saved, setSaved] = useState(false)
  const value = policy.data
  const selection: Selection = draft ?? {
    mode: value?.reviewer_ids.length ? 'assigned' : 'all',
    ids: value?.reviewer_ids ?? [],
  }
  const unavailable = selection.ids.filter(id => !value?.options.some(option => option.id === id && option.active))

  function update(selection: Selection) {
    setSaved(false)
    action.setError('')
    setDraft(selection)
  }

  return <section className="surface settings-form separate-section">
    <ErrorMessage message={policy.error} retry={policy.retry} />
    {!value ? !policy.error && <Loading /> :
      <form onSubmit={event => {
        event.preventDefault()
        void action.run(async () => {
          policy.setData(await api<ReviewPolicyData>(path, 'PATCH', {
            reviewer_ids: selection.mode === 'all' ? [] : selection.ids,
          }))
          setDraft(undefined)
          setSaved(true)
        })
      }}>
        <fieldset className="settings-section" disabled={action.busy}>
          <h2>Согласование</h2>
          <div className="form-stack">
            <label>Кто может согласовывать посты
              <select value={selection.mode} onChange={event => {
                update({ ...selection, mode: event.currentTarget.value === 'assigned' ? 'assigned' : 'all' })
              }}>
                <option value="all">Все сотрудники с правом согласования</option>
                <option value="assigned">Только выбранные профили</option>
              </select>
            </label>
            {selection.mode === 'assigned' && <>
              {!value.options.length && <p className="muted">Нет доступных профилей для назначения.</p>}
              {value.options.map(option => <label className="toggle-row" key={option.id}>
                <input type="checkbox" checked={selection.ids.includes(option.id)}
                  onChange={event => update({
                    ...selection,
                    ids: event.currentTarget.checked
                      ? [...selection.ids, option.id]
                      : selection.ids.filter(id => id !== option.id),
                  })} />
                Профиль #{option.id} · {option.role === 'editor' ? 'Редактор с правом согласования' : roles[option.role]}
              </label>)}
              {unavailable.map(id => <label className="toggle-row" key={id}>
                <input type="checkbox" checked onChange={() => update({
                  ...selection, ids: selection.ids.filter(selectedId => selectedId !== id),
                })} />
                Профиль #{id} · Недоступен для согласования
              </label>)}
            </>}
            <ErrorMessage message={action.error} />
            <div className="modal-actions">
              {saved && <span className="muted" role="status">Настройки сохранены</span>}
              <button className="button secondary"
                disabled={!draft || (selection.mode === 'assigned' && (!selection.ids.length || !!unavailable.length))}>
                Сохранить
              </button>
            </div>
          </div>
        </fieldset>
      </form>}
  </section>
}
