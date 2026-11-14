import { useCallback, useState } from 'react'
import { ChartNoAxesCombined, Plus, RefreshCw, Upload } from 'lucide-react'
import { api, companyPath, shortDate } from './api'
import type { Company } from './api'
import { Empty, ErrorMessage, Loading, Modal, PageHeader, formAction, formText, useAction, useData } from './ui'

const metrics = {
  views: 'Просмотры', reach: 'Охват', reactions: 'Реакции', comments: 'Комментарии', shares: 'Репосты',
} as const
const countFields = Object.keys(metrics) as (keyof typeof metrics)[]
const platforms: Record<string, string> = { telegram: 'Telegram', vk: 'ВКонтакте', max: 'MAX' }
const origins: Record<string, string> = { manual: 'Ручной ввод', csv: 'CSV', json: 'JSON', api: 'API' }
const number = new Intl.NumberFormat('ru-RU')
type Counts = Record<keyof typeof metrics, number | null>
type Rollup = {
  publications: number; totals: Counts; coverage: Counts
  er: { percent: number | null; reach: number | null; publications: number }
}
type Snapshot = Counts & {
  platform: string; publication_id: string; title: string; format: string
  published_on: string; observed_on: string; source: string; origin: string
}
type Report = {
  items: Snapshot[]; summary: Rollup; formats: Array<Rollup & { format: string }>; er_formula: string
  report: { findings: string[]; hypotheses: string[] }
}
type Mode = 'manual' | 'import' | 'sync'

export function Analytics({ company }: { company: Company }) {
  const path = companyPath(company.id) + '/analytics'
  const writable = company.role === 'admin' || company.role === 'editor'
  const [period, setPeriod] = useState({ start: '', end: '' })
  const [mode, setMode] = useState<Mode>()
  const load = useCallback(() => {
    const query = new URLSearchParams()
    if (period.start) query.set('start', period.start)
    if (period.end) query.set('end', period.end)
    return api<Report>(path + (query.size ? `?${query}` : ''))
  }, [path, period])
  const report = useData(load)

  return <>
    <PageHeader title="Аналитика" action={writable && <div className="button-group">
      <button className="button secondary" onClick={() => setMode('sync')}><RefreshCw size={16} />Получить замер</button>
      <button className="button secondary" onClick={() => setMode('import')}><Upload size={16} />Импорт</button>
      <button className="button primary" onClick={() => setMode('manual')}><Plus size={18} />Новый замер</button>
    </div>} />
    <form className="button-group" aria-label="Период публикаций" onSubmit={(event) => formAction(event, (form) => {
      report.setData(undefined)
      setPeriod({ start: formText(form, 'start'), end: formText(form, 'end') })
    })}>
      <label>Публикации с <input type="date" name="start" /></label>
      <label>по <input type="date" name="end" /></label>
      <button className="button secondary">Показать</button>
    </form>
    <ErrorMessage message={report.error} retry={report.retry} />
    {!report.error && !report.data && <Loading />}
    {!report.error && report.data && (report.data.items.length === 0 ?
      <div className="surface"><Empty icon={<ChartNoAxesCombined size={26} />} title="За выбранный период нет замеров" /></div> :
      <div className="form-stack">
        <section className="surface modal-body form-stack" aria-label="Сводка">
          <h2>Период в цифрах</h2>
          <RollupFigures rollup={report.data.summary} />
          <small className="muted">{report.data.er_formula} Для сводки суммируются взаимодействия и охваты только по публикациям с полным замером. Охваты разных постов могут пересекаться.</small>
          <ul>{report.data.report.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul>
          {!!report.data.report.hypotheses.length && <div><strong>Гипотезы для проверки</strong>
            <ul>{report.data.report.hypotheses.map((hypothesis) => <li key={hypothesis}>{hypothesis}</li>)}</ul>
          </div>}
        </section>
        <section className="surface modal-body form-stack">
          <h2>По форматам</h2>
          {report.data.formats.map((format) => <div key={format.format} className="data-copy">
            <strong>{format.format} · {number.format(format.publications)} публ.</strong>
            <RollupFigures rollup={format} />
          </div>)}
        </section>
        <section className="surface modal-body form-stack">
          <h2>Последние замеры публикаций</h2>
          <ul className="data-list">{report.data.items.map((item) => <li className="data-row" key={`${item.platform}/${item.publication_id}`}>
            <div className="data-copy">
              <strong>{item.title}</strong>
              <small>{platforms[item.platform]} · {item.format} · опубликовано {shortDate(item.published_on)}</small>
              <small>Замер от {shortDate(item.observed_on)} · {origins[item.origin]} · {item.source}</small>
              <small>{countFields.filter((field) => item[field] != null).map((field) => `${metrics[field]} ${number.format(item[field]!)}`).join(' · ')}</small>
            </div>
          </li>)}</ul>
        </section>
      </div>)}
    {mode && <MetricsForm mode={mode} path={path} onClose={() => setMode(undefined)} onSaved={() => {
      setMode(undefined)
      report.retry()
    }} />}
  </>
}

function RollupFigures({ rollup }: { rollup: Rollup }) {
  return <div className="button-group">
    {countFields.map((field) => <div className="data-copy" key={field}>
      <strong>{rollup.totals[field] == null ? '—' : number.format(rollup.totals[field]!)}</strong>
      <small>{metrics[field]} · {rollup.coverage[field]} из {rollup.publications} публ.</small>
    </div>)}
    <div className="data-copy">
      <strong>{rollup.er.percent == null ? '—' : `${number.format(rollup.er.percent)}%`}</strong>
      <small>ER · {rollup.er.publications} из {rollup.publications} публ.</small>
      {rollup.er.reach != null && <small>Сумма охватов {number.format(rollup.er.reach)}</small>}
    </div>
  </div>
}

function MetricsForm({ mode, path, onClose, onSaved }: { mode: Mode; path: string; onClose: () => void; onSaved: () => void }) {
  const action = useAction()
  const title = { manual: 'Новый замер', import: 'Импорт замеров', sync: 'Получить замер площадки' }[mode]
  return <Modal title={title} onClose={onClose} busy={action.busy}>
    <form className="modal-body form-stack" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      if (mode === 'import') {
        const file = form.get('file')
        if (!(file instanceof File) || !file.size || file.size > 1_000_000) throw new Error('Выберите файл размером до 1 МБ.')
        await api(path + '/import', 'POST', { format: formText(form, 'import_format'), content: await file.text() })
      } else {
        const fields = ['platform', 'publication_id', 'title', 'format', 'published_on']
        if (mode === 'manual') fields.push('observed_on', 'source')
        const snapshot: Record<string, string | number> = Object.fromEntries(fields.map((field) => [field, formText(form, field)]))
        for (const field of countFields) {
          const value = formText(form, field)
          if (value !== '') snapshot[field] = Number(value)
        }
        await api(`${path}/${mode === 'sync' ? 'sync' : 'metrics'}`, 'POST', snapshot)
      }
      onSaved()
    }))}>
      {mode === 'import' ? <>
        <label>Формат файла<select name="import_format"><option value="csv">CSV</option><option value="json">JSON</option></select></label>
        <label>Файл <input name="file" type="file" className="file-input" accept=".csv,.json" required /></label>
        <small className="muted">CSV с запятыми и заголовками или JSON с массивом записей. Обязательные поля: platform (telegram, vk, max), publication_id, title, format, published_on, observed_on, source. Даты в формате ГГГГ-ММ-ДД. Добавьте хотя бы одну метрику из views, reach, reactions, comments, shares.</small>
      </> : <>
        <label>Площадка<select name="platform">
          {Object.entries(platforms).filter(([key]) => mode !== 'sync' || key !== 'telegram').map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select></label>
        <label>ID публикации <input name="publication_id" maxLength={200} required /></label>
        <small className="muted">Для ВКонтакте используйте ID вида -123_456, для MAX — ID сообщения.{mode === 'manual' && ' Для Telegram укажите ссылку на пост или ID вида @канал:123.'}</small>
        <label>Название <input name="title" maxLength={200} required /></label>
        <label>Формат публикации <input name="format" maxLength={200} required /></label>
        <label>Дата публикации <input name="published_on" type="date" required /></label>
        {mode === 'manual' && <>
          <label>Дата замера <input name="observed_on" type="date" required /></label>
          <label>Источник данных <input name="source" maxLength={200} required /></label>
          <small className="muted">Введите доступные значения. Пустое поле означает, что данных по метрике нет.</small>
          {countFields.map((field) => <label key={field}>{metrics[field]}<input name={field} type="number" min={0} max={1_000_000_000_000} step={1} /></label>)}
        </>}
      </>}
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button className="button primary" disabled={action.busy}>{mode === 'manual' ? 'Сохранить' : mode === 'sync' ? 'Получить' : 'Импортировать'}</button></div>
    </form>
  </Modal>
}
