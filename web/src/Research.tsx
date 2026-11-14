import { useCallback, useState } from 'react'
import { Plus, RefreshCw, Search } from 'lucide-react'
import { api, companyPath, fullDate, shortDate } from './api'
import type { Company } from './api'
import { Empty, ErrorMessage, Loading, Modal, PageHeader, formAction, formText, useAction, useData } from './ui'

type ResearchSettings = {
  version: number; query: string; folder_id: string; configured: boolean; enabled: boolean
  last_discovery_at: string | null; last_error: string | null
}
type Candidate = {
  id: number; url: string; title: string; snippet: string; found_at: string; state: 'unverified' | 'selected'
}
type Competitor = {
  id: number; name: string; url: string; feed_url: string | null; active: boolean; version: number
  last_checked_at: string | null; last_success_at: string | null; last_error: string | null
}
type Overview = {
  settings: ResearchSettings; candidates: Candidate[]; competitors: Competitor[]
  publications: { id: number; competitor_id: number; url: string; title: string; excerpt: string
    platform: string; format: string; published_on: string | null; observed_at: string; views: number | null }[]
  comparison: { start: string; end: string; competitors: {
    competitor_id: number; observed_publications: number; undated_publications: number
    formats: Record<string, number>; views_total: number | null; views_coverage: number
  }[] }
}
type Edit = { competitor?: Competitor; candidate?: Candidate }
const formatNames: Record<string, string> = { text: 'Текст', photo: 'Фото', video: 'Видео', article: 'Статья', unknown: 'Неизвестен' }
const number = new Intl.NumberFormat('ru-RU')

function SourceLink({ href, children }: { href: string; children: string }) {
  if (!/^https?:\/\//i.test(href)) return <>{children}</>
  return <a href={href} target="_blank" rel="noreferrer">{children}</a>
}

export function Research({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/research'
  const [days, setDays] = useState(30)
  const [edit, setEdit] = useState<Edit>()
  const [settingsSaved, setSettingsSaved] = useState(false)
  const load = useCallback(() => api<Overview>(`${path}?days=${days}`), [path, days])
  const overview = useData(load)
  const action = useAction()
  const admin = company.role === 'admin'
  const writable = admin || company.role === 'editor'
  const value = overview.data
  const checked = (date: string) => fullDate(date, company.timezone)
  const companyName = (id: number) => value?.competitors.find((competitor) => competitor.id === id)?.name ?? 'Компания'
  async function update(route: string, method: string, payload?: unknown) {
    const result = await api<Overview>(path + route, method, payload)
    overview.setData(days === 30 ? result : await load())
  }
  return <>
    <PageHeader title="Исследования конкурентов" action={writable && <button className="button primary" onClick={() => setEdit({})}>
      <Plus size={18} />Добавить конкурента
    </button>} />
    <ErrorMessage message={overview.error} retry={overview.retry} />
    <ErrorMessage message={action.error} />
    {!value && !overview.error && <Loading />}
    {value && <div className="form-stack">
      <section className="surface modal-body form-stack">
        <h2>Поиск конкурентов</h2>
        <p className="muted">{value.settings.configured ? 'Поиск настроен' : 'Укажите запрос, каталог и ключ Yandex Search API для поиска'}
          {' · '}{value.settings.enabled ? 'Ежедневный запуск включён' : 'Ежедневный запуск выключен'}</p>
        {value.settings.last_discovery_at && <small className="muted">Последний поиск · {checked(value.settings.last_discovery_at)}</small>}
        <ErrorMessage message={value.settings.last_error ?? ''} />
        {admin && <SearchSettings key={value.settings.version} settings={value.settings}
          busy={action.busy} saved={settingsSaved} onChange={() => setSettingsSaved(false)}
          save={(payload) => void action.run(async () => {
            await update('/settings', 'PATCH', payload)
            setSettingsSaved(true)
          })}
          discover={(query) => void action.run(async () => {
            setSettingsSaved(false)
            await update('/discover', 'POST', query ? { query } : {})
          })} />}
        {!!value.candidates.length && <ul className="data-list">{value.candidates.map((item) => <li className="data-row" key={item.id}>
          <div className="data-copy">
            <strong><SourceLink href={item.url}>{item.title || item.url}</SourceLink></strong>
            <small>{item.state === 'selected' ? 'Выбран' : 'Кандидат · проверьте источник'} · {checked(item.found_at)}</small>
            {item.snippet && <p className="material-text">{item.snippet}</p>}
            {writable && item.state === 'unverified' && <div className="button-group">
              <button className="button secondary" onClick={() => setEdit({ candidate: item })}>Добавить в наблюдение</button>
            </div>}
          </div>
        </li>)}</ul>}
      </section>
      <section className="surface modal-body form-stack">
        <div className="button-group">
          <h2>Сравнение публикаций</h2>
          <label>Период<select value={days} disabled={action.busy} onChange={(event) => setDays(Number(event.target.value))}>
            <option value={7}>7 дней</option><option value={30}>30 дней</option><option value={90}>90 дней</option>
          </select></label>
        </div>
        <small className="muted">{shortDate(value.comparison.start)} – {shortDate(value.comparison.end)} · публикации из доступных источников</small>
        {!value.competitors.length && <Empty icon={<Search size={25} />} title="Список конкурентов пуст" />}
        <ul className="data-list">{value.competitors.map((item) => {
          const figures = value.comparison.competitors.find((line) => line.competitor_id === item.id)
          return <li className="data-row" key={item.id}>
            <div className="data-copy">
              <strong><SourceLink href={item.url}>{item.name}</SourceLink></strong>
              <small>{item.active ? 'В наблюдении' : 'Наблюдение приостановлено'} · {item.feed_url ? 'Подключён RSS/Atom' : 'Источник — страница компании'}</small>
              <small>{item.last_success_at ? `Последнее обновление · ${checked(item.last_success_at)}` : 'Ожидает первого обновления'}</small>
              {!!item.last_error && <ErrorMessage message={item.last_error} />}
              {figures && <small>{number.format(figures.observed_publications)} публ. за период · просмотры {figures.views_total == null ? '—' : number.format(figures.views_total)}
                {' · '}просмотры известны для {figures.views_coverage} из {figures.observed_publications}
                {figures.undated_publications > 0 && ` · без даты ${figures.undated_publications}`}</small>}
              {figures && Object.keys(figures.formats).length > 0 && <small>{Object.entries(figures.formats)
                .map(([format, count]) => `${formatNames[format] ?? format} ${number.format(count)}`).join(' · ')}</small>}
              {writable && <div className="button-group">
                <button className="button secondary" disabled={action.busy} onClick={() => setEdit({ competitor: item })}>Настроить</button>
                <button className="button secondary" disabled={action.busy || !item.active}
                  onClick={() => void action.run(() => update(`/competitors/${item.id}/refresh`, 'POST'))}>
                  <RefreshCw size={15} />Обновить данные
                </button>
              </div>}
            </div>
          </li>
        })}</ul>
      </section>
      {!!value.publications.length && <section className="surface modal-body form-stack">
        <h2>Последние наблюдения</h2>
        <ul className="data-list">{value.publications.map((item) => <li className="data-row" key={item.id}>
          <div className="data-copy">
            <strong><SourceLink href={item.url}>{item.title || item.url}</SourceLink></strong>
            <small>{companyName(item.competitor_id)} · {formatNames[item.format] ?? item.format} · {item.published_on ? shortDate(item.published_on) : 'Дата выхода неизвестна'}</small>
            <small>Зафиксировано {checked(item.observed_at)} · просмотры {item.views == null ? '—' : number.format(item.views)}</small>
            {item.excerpt && <p className="material-text">{item.excerpt}</p>}
          </div>
        </li>)}</ul>
      </section>}
    </div>}
    {edit && <CompetitorEditor edit={edit} path={path} close={() => setEdit(undefined)} reload={overview.retry} />}
  </>
}

function SearchSettings({ settings, busy, saved, onChange, save, discover }: {
  settings: ResearchSettings; busy: boolean; saved: boolean; onChange: () => void
  save: (payload: unknown) => void; discover: (query: string) => void
}) {
  const [query, setQuery] = useState(settings.query)
  const [clearKey, setClearKey] = useState(false)
  return <form className="form-stack" onChange={onChange} onSubmit={(event) => formAction(event, (form) => {
    const key = formText(form, 'api_key')
    save({ version: settings.version, query: query.trim(), folder_id: formText(form, 'folder_id'),
      enabled: form.has('enabled'), ...(clearKey ? { api_key: '' } : key ? { api_key: key } : {}) })
  })}>
    <fieldset className="form-stack" disabled={busy}>
      <label>Поисковый запрос<input name="query" maxLength={400} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <div className="form-grid">
        <label>Каталог Yandex Cloud<input name="folder_id" defaultValue={settings.folder_id} maxLength={50} pattern="[a-z0-9]*" /></label>
        <label>Новый ключ Yandex Search API<input name="api_key" type="password" autoComplete="new-password"
          disabled={clearKey} maxLength={1000} /></label>
      </div>
      <label className="toggle-row"><input type="checkbox" name="enabled" defaultChecked={settings.enabled} />
        Ежедневно запускать платный поиск и обновлять выбранные источники</label>
      <label className="toggle-row"><input type="checkbox" checked={clearKey}
        onChange={(event) => setClearKey(event.target.checked)} />Удалить сохранённый ключ</label>
      <div className="button-group">
        <button className="button secondary">Сохранить настройки</button>
        <button type="button" className="button primary" disabled={!settings.configured}
          onClick={() => discover(query.trim())}><Search size={16} />Запустить платный поиск</button>
        {saved && <span className="saved" role="status">Сохранено</span>}
      </div>
    </fieldset>
  </form>
}

function CompetitorEditor({ edit: { competitor, candidate }, path, close, reload }: {
  edit: Edit; path: string; close: () => void; reload: () => void
}) {
  const action = useAction()
  const source = competitor?.url ?? candidate?.url
  return <Modal title={competitor ? 'Настроить наблюдение' : 'Добавить конкурента'} busy={action.busy} onClose={close}>
    <form className="modal-body form-stack" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const name = formText(form, 'name')
      const feed = formText(form, 'feed_url')
      const url = candidate?.url ?? formText(form, 'url')
      await api(path + '/competitors' + (competitor ? '/' + competitor.id : ''), competitor ? 'PATCH' : 'POST',
        competitor ? { name, feed_url: feed || null, version: competitor.version, active: form.has('active') } :
          { name, url, ...(feed && { feed_url: feed }), ...(candidate && { candidate_id: candidate.id }) })
      reload()
      close()
    }))}>
      <label>Название<input name="name" defaultValue={competitor?.name ?? candidate?.title} required maxLength={150} /></label>
      {source ? <p className="material-text"><SourceLink href={source}>{source}</SourceLink></p> :
        <label>Ссылка на публичный источник<input type="url" name="url" required maxLength={2000} /></label>}
      <label>Ссылка на RSS или Atom<input type="url" name="feed_url" defaultValue={competitor?.feed_url ?? ''} maxLength={2000} /></label>
      {competitor && <label className="toggle-row">
        <input type="checkbox" name="active" defaultChecked={competitor.active} />Продолжать наблюдение
      </label>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button className="button primary" disabled={action.busy}>Сохранить</button></div>
    </form>
  </Modal>
}
