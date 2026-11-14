import { useCallback } from 'react'
import { RefreshCw } from 'lucide-react'
import { api, companyPath, fullDate, platforms, shortDate } from './api'
import type { Company } from './api'
import { ErrorMessage, Loading, useAction, useData } from './ui'

type Evidence = {
  metric: 'views'; window_start: string; window_end: string
  age_days: { min: number; max: number }
  groups: { format: string; publications: number; views_coverage: number; median_views: number | null
    observations: { metric_id: number; publication_id: string; published_on: string; observed_on: string; views: number; source: string }[] }[]
}
type Recommendation = {
  id: number; version: number; status: 'proposed' | 'applied' | 'superseded'
  platform: keyof typeof platforms; planned_on: string
  title: string; brief: string; evidence: Evidence; plan_id: number | null
}
type StrategyOverview = {
  assessment: { status: 'insufficient' | 'recommendations'; reason: string; checked_at: string | null } | null
  recommendations: Recommendation[]
}
const number = new Intl.NumberFormat('ru-RU')
const statusNames = { proposed: 'Предложение', applied: 'В плане', superseded: 'Устарело' }

export function Strategy({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/strategy'
  const writable = company.role === 'admin' || company.role === 'editor'
  const load = useCallback(() => api<StrategyOverview>(path), [path])
  const strategy = useData(load)
  const action = useAction()
  return <section className="modal-body form-stack">
    <div className="button-group">
      <h2>Предложения для контент-плана</h2>
      {writable && <button className="button secondary" disabled={action.busy}
        onClick={() => void action.run(async () => strategy.setData(await api<StrategyOverview>(path + '/refresh', 'POST')))}>
        <RefreshCw size={16} />Обновить рекомендации
      </button>}
    </div>
    <ErrorMessage message={strategy.error} retry={strategy.retry} />
    <ErrorMessage message={action.error} />
    {!strategy.data && !strategy.error && <Loading />}
    {strategy.data && <>
      {strategy.data.assessment ? <div className="data-copy">
        {strategy.data.assessment.reason && <p>{strategy.data.assessment.reason}</p>}
        {strategy.data.assessment.checked_at && <small>Проверено {fullDate(strategy.data.assessment.checked_at, company.timezone)}</small>}
      </div> : <p className="muted">Рекомендации ещё не рассчитаны.</p>}
      <ul className="data-list">{strategy.data.recommendations.map((item) => <li className="data-row" key={item.id}>
        <div className="data-copy">
          <strong>{item.title}</strong>
          <small>{platforms[item.platform] ?? item.platform} · {shortDate(item.planned_on)} · {statusNames[item.status]}</small>
          <p className="material-text">{item.brief}</p>
          <details>
            <summary>Замеры для сравнения</summary>
            <p className="muted">Публикации с {shortDate(item.evidence.window_start)} по {shortDate(item.evidence.window_end)}.
              Замеры через {item.evidence.age_days.min}–{item.evidence.age_days.max} дн. после выхода.</p>
            <ul>{item.evidence.groups.map((group) => <li key={group.format}>
              <strong>{group.format}</strong> · Медиана просмотров {group.median_views == null ? '—' : number.format(group.median_views)}
              {' · '}замеров {group.views_coverage} из {group.publications}
              <ul>{group.observations.map((observation) => <li key={observation.metric_id}>
                {observation.publication_id} · {number.format(observation.views)} просм. · {shortDate(observation.observed_on)} · {observation.source}
              </li>)}</ul>
            </li>)}</ul>
          </details>
          {writable && item.status === 'proposed' && <div className="button-group">
            <button className="button primary" disabled={action.busy} onClick={() => void action.run(async () => {
              strategy.setData(await api<StrategyOverview>(`${path}/${item.id}/apply`, 'POST', { version: item.version }))
            })}>Добавить эксперимент в план</button>
          </div>}
        </div>
      </li>)}</ul>
    </>}
  </section>
}
