import { useCallback, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { api, companyPath } from './api'
import type { Company } from './api'
import { ErrorMessage, Loading, Modal, PageHeader, formAction, formText, useAction, useData } from './ui'

const money = new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB' })
const number = new Intl.NumberFormat('ru-RU')
const rubles = (kopecks: number | null | undefined) => kopecks == null ? '—' : money.format(kopecks / 100)
const kinds = { text: 'Текст', image: 'Изображение', layout: 'Размещение логотипа', search: 'Поиск' } as const
const priceKinds = ['text', 'image', 'layout', 'search'] as const
type PriceKind = typeof priceKinds[number]
type BudgetTotals = {
  month: string; enabled: boolean; monthly_limit: number
  metered_kopecks: number; reserved_kopecks: number; unpriced_requests: number; remaining_kopecks: number | null
  opening_kopecks: number | null; covered_requests: number
}
type Budget = BudgetTotals & {
  models: Record<PriceKind, string>; missing_tariffs: string[]
  quotes_kopecks: Record<PriceKind, number | null>
  limit_editable: boolean
}
type Tariff = {
  provider: 'openai' | 'yandex'; kind: keyof typeof kinds; model: string; currency: 'RUB' | 'USD' | 'EUR'
  rub_per_unit: string; input_per_million?: string; output_per_million?: string; max_input_tokens?: number
  image_model?: string; image_input_per_million?: string; image_output_per_million?: string
  max_image_input_tokens?: number; max_image_output_tokens?: number; per_request?: string
  image_bounds_source?: string | null
}
type Catalog = { version: string; tariffs: Tariff[] }

export function Billing({ company, global }: { company?: Company; global: boolean }) {
  const [tab, setTab] = useState<'budget' | 'overall' | 'tariffs' | 'calculator'>(company ? 'budget' : global ? 'overall' : 'calculator')
  const tabs = { budget: 'Бюджет компании', overall: 'Общий бюджет', calculator: 'Калькулятор', tariffs: 'Тарифы провайдеров' } as const
  const visibleTabs = (Object.keys(tabs) as Array<keyof typeof tabs>).filter((key) => {
    if (key === 'budget') return !!company
    if (key === 'overall' || key === 'tariffs') return global
    return true
  })
  const path = company ? companyPath(company.id) + '/billing' : ''
  const load = useCallback(() => path ? api<Budget>(path) : Promise.resolve(null), [path])
  const summary = useData(load)
  return <>
    <PageHeader title="Расходы" />
    <div className="surface editor-tabs button-group" aria-label="Управление расходами">
      {visibleTabs.map((key) => <button key={key} className={tab === key ? 'tab active' : 'tab'}
        aria-pressed={tab === key} onClick={() => setTab(key)}>{tabs[key]}</button>)}
    </div>
    {tab === 'budget' && company && <>
      <ErrorMessage message={summary.error} retry={summary.retry} />
      {!summary.data && !summary.error && <Loading />}
      {summary.data && <BudgetPanel value={summary.data} details={summary.data} path={path}
        writable={summary.data.limit_editable} onSaved={summary.setData} reload={summary.retry} />}
    </>}
    {tab === 'calculator' && <Calculator quotes={summary.data?.quotes_kopecks} />}
    {tab === 'tariffs' && global && <Tariffs onUpdated={summary.retry} />}
    {tab === 'overall' && global && <OverallBudget />}
  </>
}

function OverallBudget() {
  const path = '/billing/settings'
  const load = useCallback(() => api<BudgetTotals>(path), [])
  const summary = useData(load)
  return <>
    <ErrorMessage message={summary.error} retry={summary.retry} />
    {!summary.data && !summary.error && <Loading />}
    {summary.data && <BudgetPanel path={path} value={summary.data} writable onSaved={summary.setData} reload={summary.retry} />}
  </>
}

function BudgetPanel<T extends BudgetTotals>({ value, details, path, writable, onSaved, reload }: {
  value: T; details?: Budget; path: string; writable: boolean; onSaved: (budget: T) => void; reload: () => void
}) {
  const action = useAction()
  const [saved, setSaved] = useState(false)
  const uncovered = Math.max(0, value.unpriced_requests - value.covered_requests)
  return <section className="surface modal-body usage form-stack">
    <div className="button-group">
      <h2>Расходы за {value.month} (UTC)</h2>
      <button className="button secondary" disabled={action.busy} onClick={() => { setSaved(false); reload() }}>
        <RefreshCw size={16} />Обновить
      </button>
    </div>
    <dl>
      <div><dt>Учтено по тарифам</dt><dd>{rubles(value.metered_kopecks)}</dd></div>
      <div><dt>Зарезервировано</dt><dd>{rubles(value.reserved_kopecks)}</dd></div>
      {value.opening_kopecks != null && <div><dt>Сверено отдельно</dt><dd>{rubles(value.opening_kopecks)}</dd></div>}
      <div><dt>Запросы без цены</dt><dd>{number.format(uncovered)}</dd></div>
      <div><dt>Остаток лимита</dt><dd>{rubles(value.remaining_kopecks)}</dd></div>
    </dl>
    <p className="muted">Месячный лимит {value.enabled ? money.format(value.monthly_limit) : 'выключен'}.
      {uncovered > 0 && ' Общая стоимость неизвестна из-за запросов без цены.'}</p>
    {details && <div className="form-stack">
      <h2>Модели и верхние оценки запросов</h2>
      <ul className="data-list">{priceKinds.map((key) => <li className="data-row" key={key}>
        <div className="data-copy"><strong>{kinds[key]}</strong><small>{details.models[key] || 'Модель не определена'}</small></div>
        <span>{rubles(details.quotes_kopecks?.[key])}</span>
      </li>)}</ul>
      {!!details.missing_tariffs.length && <div>
        <strong>Нет подходящих тарифов</strong>
        <ul>{details.missing_tariffs.map((item) => <li key={item}>{item}</li>)}</ul>
      </div>}
    </div>}
    {writable ? <form key={`${value.month}/${value.enabled}/${value.monthly_limit}/${value.opening_kopecks}`} className="form-stack"
      onChange={() => setSaved(false)} onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
        setSaved(false)
        const opening = formText(form, 'opening')
        onSaved(await api<T>(path, 'PUT', {
          enabled: form.has('enabled'), monthly_limit: Number(formText(form, 'monthly_limit')),
          expected_month: value.month,
          ...(opening && { opening_kopecks: Math.round(Number(opening) * 100) }),
        }))
        setSaved(true)
      }))}>
      <fieldset className="form-grid" disabled={action.busy}>
        <label>Лимит в месяц, ₽<input type="number" name="monthly_limit" min={0} max={1000000000} step={1} required defaultValue={value.monthly_limit} /></label>
        <label className="toggle-row"><input type="checkbox" name="enabled" defaultChecked={value.enabled} />Контролировать лимит</label>
        {value.unpriced_requests > 0 && <label>Сверенная сумма за запросы без цены, ₽<input type="number" name="opening"
          min={0} max={1000000000} step={0.01} />
          <small className="muted">Если нужно обновить сверку, укажите общую сумму за все {value.unpriced_requests} запросов без цены за текущий месяц.</small></label>}
      </fieldset>
      <ErrorMessage message={action.error} />
      <div className="button-group"><button className="button primary" disabled={action.busy}>Сохранить лимит</button>
        {saved && <span className="saved" role="status">Сохранено</span>}</div>
    </form> : <p className="muted">Лимит для общих ключей назначает главный администратор.</p>}
  </section>
}

type Forecast = { items: Record<PriceKind | 'server', number | null>; limit: number }

function Calculator({ quotes }: { quotes?: Budget['quotes_kopecks'] }) {
  const [prices, setPrices] = useState<Record<PriceKind, string>>({ text: '', image: '', layout: '', search: '' })
  const [forecast, setForecast] = useState<Forecast>()
  const costs = forecast && Object.values(forecast.items)
  const total = costs?.reduce<number | null>((sum, cost) => sum === null || cost === null ? null : sum + cost, 0)
  return <section className="surface modal-body usage form-stack">
    <h2>Прогноз расходов в месяц</h2>
    <form className="form-stack" onChange={() => setForecast(undefined)} onSubmit={(event) => formAction(event, (form) => {
      const volume = (name: string) => Number(formText(form, name))
      const cost = (name: string, count: number) => {
        const value = formText(form, name)
        return count === 0 ? 0 : value === '' ? null : Number(value) * count
      }
      const posts = volume('posts')
      const images = posts * volume('image_calls')
      setForecast({
        limit: volume('limit'),
        items: {
          text: cost('text', posts * volume('text_calls') + volume('text_edits') + volume('tasks')),
          image: cost('image', images + volume('image_edits')),
          layout: cost('layout', images + volume('image_edits')),
          search: cost('search', volume('search_calls')),
          server: cost('server', 1),
        },
      })
    })}>
      <div className="form-grid">
        <label>Публикаций в месяц<input name="posts" type="number" min={0} max={1000000} step={1} defaultValue={180} required />
          <small className="muted">3 компании × 2 публикации × 30 дней</small></label>
        <label>Запросов текста на публикацию<input name="text_calls" type="number" min={0} max={1000} step={1} defaultValue={1} required /></label>
        <label>Генераций изображения на публикацию<input name="image_calls" type="number" min={0} max={1000} step={1} defaultValue={0} required /></label>
        <label>Дополнительных правок текста в месяц<input name="text_edits" type="number" min={0} max={1000000} step={1} defaultValue={0} required /></label>
        <label>Дополнительных правок изображения в месяц<input name="image_edits" type="number" min={0} max={1000000} step={1} defaultValue={0} required /></label>
        <label>Запросов текста для задач и чата в месяц<input name="tasks" type="number" min={0} max={1000000} step={1} defaultValue={0} required /></label>
        <label>Платных поисковых запросов в месяц<input name="search_calls" type="number" min={0} max={1000000} step={1} defaultValue={0} required /></label>
        <label>Сервер и прочие фиксированные расходы в месяц, ₽<input name="server" type="number" min={0} max={100000000} step="any" /></label>
        <label>Бюджет в месяц, ₽<input name="limit" type="number" min={0} max={100000000} step="any" defaultValue={40000} required /></label>
        {priceKinds.map((key) => <label key={key}>{kinds[key]} за запрос, ₽<input name={key}
          type="number" min={0} max={1000000} step="any" value={prices[key]}
          onChange={(event) => setPrices({ ...prices, [key]: event.target.value })} /></label>)}
      </div>
      <p className="muted">Каждая генерация и правка изображения включает один запрос размещения логотипа. Пустая цена означает, что расходы по этой статье неизвестны.</p>
      <div className="button-group">
        {quotes && priceKinds.some((key) => quotes[key] != null) && <button className="button secondary" type="button"
          onClick={() => {
            setForecast(undefined)
            setPrices((current) => {
              const next = { ...current }
              for (const key of priceKinds) {
                const estimate = quotes[key]
                if (estimate != null) next[key] = String(estimate / 100)
              }
              return next
            })
          }}>Подставить верхние оценки по тарифам</button>}
        <button className="button primary">Рассчитать</button>
      </div>
    </form>
    {forecast && <dl aria-live="polite">
      {([...priceKinds, 'server'] as const).map((key) => <div key={key}>
        <dt>{key === 'server' ? 'Фиксированные расходы' : kinds[key]}</dt>
        <dd>{forecast.items[key] == null ? '—' : money.format(forecast.items[key]!)}</dd>
      </div>)}
      <div><dt>Всего</dt><dd>{total == null ? 'Нет полной оценки' : money.format(total)}</dd></div>
      {total != null && <div><dt>{total > forecast.limit ? 'Сверх бюджета' : 'Остаток бюджета'}</dt>
        <dd>{money.format(Math.abs(forecast.limit - total))}</dd></div>}
    </dl>}
  </section>
}

function tariffLabel(tariff: Tariff) {
  return `${kinds[tariff.kind]} · ${tariff.provider === 'openai' ? 'OpenAI' : 'Yandex'} · ${tariff.model}`
}

function Tariffs({ onUpdated }: { onUpdated: () => void }) {
  const path = '/billing/tariffs'
  const load = useCallback(() => api<Catalog>(path), [])
  const catalog = useData(load)
  const [editing, setEditing] = useState<number | 'new'>()
  return <section className="surface modal-body form-stack">
    <div className="button-group">
      <h2>Тарифы провайдеров</h2>
      {catalog.data && <>
        <button className="button secondary" onClick={() => setEditing('new')}><Plus size={16} />Добавить тариф</button>
        <button className="button secondary" onClick={catalog.retry}><RefreshCw size={16} />Обновить тарифы</button>
      </>}
    </div>
    <ErrorMessage message={catalog.error} retry={catalog.retry} />
    {!catalog.data && !catalog.error && <Loading />}
    {catalog.data && <>
      {!catalog.data.tariffs.length && <p className="muted">Тарифы ещё не заведены.</p>}
      <ul className="data-list">{catalog.data.tariffs.map((tariff, index) => <li key={tariffLabel(tariff)} className="data-row">
        <div className="data-copy">
          <strong>{tariffLabel(tariff)}</strong>
          <small>{tariff.kind === 'search' ? `${tariff.per_request} ${tariff.currency} за запрос` :
            `${tariff.input_per_million} / ${tariff.output_per_million} ${tariff.currency} за 1 млн входных / выходных токенов`}</small>
        </div>
        <button className="button secondary" onClick={() => setEditing(index)}>Изменить</button>
      </li>)}</ul>
    </>}
    {catalog.data && editing !== undefined && <TariffEditor path={path} catalog={catalog.data} editing={editing}
      onClose={() => setEditing(undefined)} onSaved={(value) => {
        catalog.setData(value)
        setEditing(undefined)
        onUpdated()
      }} />}
  </section>
}

function TariffEditor({ path, catalog, editing, onClose, onSaved }: {
  path: string; catalog: Catalog; editing: number | 'new'; onClose: () => void; onSaved: (value: Catalog) => void
}) {
  const item = editing === 'new' ? undefined : catalog.tariffs[editing]
  const [kind, setKind] = useState<Tariff['kind']>(item?.kind ?? 'text')
  const [currency, setCurrency] = useState<Tariff['currency']>(item?.currency ?? 'RUB')
  const action = useAction()
  const search = kind === 'search'
  async function save(tariffs: Tariff[]) {
    onSaved(await api<Catalog>(path, 'PUT', {
      version: new Date().toISOString(), expected_version: catalog.version, tariffs,
    }))
  }
  return <Modal title={item ? 'Изменить тариф' : 'Новый тариф'} busy={action.busy} onClose={onClose} wide>
    <form className="modal-body form-stack" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const next: Tariff = {
        provider: search ? 'yandex' : kind === 'text' ? formText(form, 'provider') as Tariff['provider'] : 'openai',
        kind, currency, model: search ? 'yandex-web-sync' : formText(form, 'model'),
        rub_per_unit: currency === 'RUB' ? '1' : formText(form, 'rub_per_unit'),
        ...(!search && {
          input_per_million: formText(form, 'input_per_million'), output_per_million: formText(form, 'output_per_million'),
          max_input_tokens: Number(formText(form, 'max_input_tokens')),
        }),
        per_request: formText(form, 'per_request') || '0',
        ...(kind === 'image' && {
          image_model: formText(form, 'image_model'), image_input_per_million: formText(form, 'image_input_per_million'),
          image_output_per_million: formText(form, 'image_output_per_million'),
          max_image_input_tokens: Number(formText(form, 'max_image_input_tokens')),
          max_image_output_tokens: Number(formText(form, 'max_image_output_tokens')),
          image_bounds_source: formText(form, 'image_bounds_source'),
        }),
      }
      const tariffs = [...catalog.tariffs]
      if (editing === 'new') tariffs.push(next)
      else tariffs[editing] = next
      await save(tariffs)
    }))}>
      <fieldset className="form-grid" disabled={action.busy}>
        <label>Операция<select value={kind} onChange={(event) => setKind(event.target.value as Tariff['kind'])}>
          {Object.entries(kinds).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        <label>Провайдер{kind === 'text' ? <select name="provider" defaultValue={item?.provider ?? 'openai'}>
          <option value="openai">OpenAI</option><option value="yandex">Yandex</option>
        </select> : <input value={search ? 'Yandex' : 'OpenAI'} readOnly />}</label>
        <label className="form-wide">Точный идентификатор модели{search ? <input value="yandex-web-sync" readOnly /> :
          <input name="model" defaultValue={item?.kind === 'search' ? '' : item?.model} required maxLength={300} />}</label>
        <label>Валюта<select value={currency} onChange={(event) => setCurrency(event.target.value as Tariff['currency'])}>
          <option>RUB</option><option>USD</option><option>EUR</option>
        </select></label>
        {currency !== 'RUB' && <label>Курс, ₽ за единицу<input name="rub_per_unit" type="number" min="0.00000001" max={10000} step="any" defaultValue={item?.rub_per_unit} required /></label>}
        <label>{search ? 'Цена за запрос' : 'Доплата за запрос'}, {currency}<input name="per_request" type="number"
          min={search ? '0.00000001' : 0} max={100000} step="any" defaultValue={item?.per_request} required={search} /></label>
        {!search && <>
          <label>Цена 1 млн входных токенов<input name="input_per_million" type="number" min={0} max={100000} step="any" defaultValue={item?.input_per_million} required /></label>
          <label>Цена 1 млн выходных токенов<input name="output_per_million" type="number" min={0} max={100000} step="any" defaultValue={item?.output_per_million} required /></label>
          <label>Максимум входных токенов за запрос<input name="max_input_tokens" type="number" min={1} max={10000000} step={1} defaultValue={item?.max_input_tokens} required /></label>
        </>}
        {kind === 'image' && <>
          <label className="form-wide">Точный идентификатор модели генерации<input name="image_model" defaultValue={item?.image_model} required maxLength={300} /></label>
          <label>Цена 1 млн входных токенов генерации<input name="image_input_per_million" type="number" min="0.00000001" max={100000} step="any" defaultValue={item?.image_input_per_million} required /></label>
          <label>Цена 1 млн выходных токенов генерации<input name="image_output_per_million" type="number" min="0.00000001" max={100000} step="any" defaultValue={item?.image_output_per_million} required /></label>
          <label>Максимум входных токенов генерации<input name="max_image_input_tokens" type="number" min={1} max={10000000} step={1} defaultValue={item?.max_image_input_tokens} required /></label>
          <label>Максимум выходных токенов генерации<input name="max_image_output_tokens" type="number" min={1} max={10000000} step={1} defaultValue={item?.max_image_output_tokens} required /></label>
          <label className="form-wide">Ссылка на подтверждение верхних границ провайдером<input name="image_bounds_source"
            type="url" pattern="https://.+" defaultValue={item?.image_bounds_source ?? ''} required />
            <small className="muted">Один вызов и один PNG 1024×1024 (medium), низкая сложность, до шести референсов размером до 2048 px по каждой стороне, переработанный запрос и чтение результата базовой моделью.</small>
          </label>
        </>}
      </fieldset>
      <ErrorMessage message={action.error} />
      <div className="modal-actions">
        {item && <button type="button" className="button danger" disabled={action.busy} onClick={() => {
          if (window.confirm(`Удалить тариф «${tariffLabel(item)}»?`)) {
            void action.run(() => save(catalog.tariffs.filter((_, index) => index !== editing)))
          }
        }}>Удалить</button>}
        <button className="button primary" disabled={action.busy}>Сохранить тариф</button>
      </div>
    </form>
  </Modal>
}
