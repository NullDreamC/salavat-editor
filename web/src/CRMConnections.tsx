import { useEffect, useState } from 'react'
import { api, companyPath, errorText } from './api'

type Provider = 'bitrix24' | 'amocrm'
type Stats = Record<'records' | 'lead' | 'sale' | 'duplicates' | 'unmatched' |
  'unsupported_currency' | 'unknown_stage' | 'invalid_data' | 'skipped', number>
type Connection = {
  provider: Provider
  version: number
  domain: string
  configured: boolean
  enabled: boolean
  interval_minutes: 0 | 15 | 60
  lead_field: string
  deal_field: string
  amo_field_id: number | null
  amo_rub: boolean
  running: boolean
  last_state: 'completed' | 'partial' | 'failed' | null
  last_error: string | null
  last_finished_at: string | null
  last_counts: Stats | null
}

const names: Record<Provider, string> = { bitrix24: 'Битрикс24', amocrm: 'amoCRM' }
const syncStates = { completed: 'Завершён', partial: 'Есть следующие страницы', failed: 'Прерван' }
const countsLabel: Record<keyof Stats, string> = {
  records: 'Записей прочитано',
  lead: 'Новых заявок',
  sale: 'Новых успешных сделок',
  duplicates: 'Повторных событий',
  unmatched: 'Без подходящей кампании',
  unsupported_currency: 'Продаж без подтверждения рублей',
  unknown_stage: 'С неизвестным этапом',
  invalid_data: 'С отсутствующей датой или суммой',
  skipped: 'Удалённых записей',
}

export function CRMConnections({ companyId }: { companyId: number }) {
  const [connections, setConnections] = useState<Connection[] | null>(null)
  const [error, setError] = useState('')
  const path = companyPath(companyId) + '/crm'

  useEffect(() => {
    let active = true
    api<Connection[]>(path).then(
      (result) => { if (active) setConnections(result) },
      (failure) => { if (active) setError(errorText(failure)) },
    )
    return () => { active = false }
  }, [path])

  return <section className="crm-connections" aria-label="Подключения CRM">
    <h3>Подключения CRM</h3>
    <p className="muted">Передавайте код <code>p_…</code> из метки <code>utm_campaign</code> в поле записи CRM. Успешные сделки учитываются по сумме в CRM. Для синхронизации включите учёт заявок и продаж.</p>
    {error && <p role="alert">{error}</p>}
    {!connections && !error && <p className="muted">Загрузка подключений…</p>}
    {connections?.map((connection) => <ConnectionForm key={`${companyId}-${connection.provider}`} initial={connection} path={path} />)}
  </section>
}

function ConnectionForm({ initial, path }: { initial: Connection; path: string }) {
  const [saved, setSaved] = useState(initial)
  const [form, setForm] = useState(initial)
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [feedback, setFeedback] = useState('')
  const bitrix = saved.provider === 'bitrix24'
  const endpoint = path + '/' + saved.provider
  const dirty = Boolean(secret) || form.enabled !== saved.enabled || form.domain !== saved.domain ||
    form.interval_minutes !== saved.interval_minutes || form.lead_field !== saved.lead_field ||
    form.deal_field !== saved.deal_field || form.amo_field_id !== saved.amo_field_id || form.amo_rub !== saved.amo_rub

  async function request(action: 'save' | 'remove' | 'sync' | 'refresh') {
    setBusy(true)
    setError('')
    setFeedback('')
    try {
      let connection
      if (action === 'refresh') {
        const result = await api<Connection[]>(path)
        connection = result.find((item) => item.provider === saved.provider) ?? saved
      } else if (action === 'sync') {
        connection = await api<Connection>(endpoint + '/sync', 'POST')
      } else {
        const remove = action === 'remove'
        connection = await api<Connection>(endpoint, 'PUT', {
          version: saved.version,
          enabled: !remove && form.enabled,
          interval_minutes: remove ? 0 : form.interval_minutes,
          remove_credential: remove,
          ...(bitrix ? {
            lead_field: form.lead_field.trim(),
            deal_field: form.deal_field.trim(),
            ...(secret && !remove ? { webhook_url: secret.trim() } : {}),
          } : {
            ...(!remove && form.domain !== saved.domain ? { domain: form.domain.trim() } : {}),
            amo_field_id: form.amo_field_id,
            amo_rub: form.amo_rub,
            ...(secret && !remove ? { token: secret.trim() } : {}),
          }),
        })
      }
      setSaved(connection)
      if (action !== 'refresh' || !dirty || connection.version !== saved.version) {
        setForm(connection)
        setSecret('')
      }
      if (action === 'save') setFeedback('Настройки сохранены.')
      if (action === 'remove') setFeedback('Ключ удалён.')
    } catch (failure) {
      setError(errorText(failure))
    } finally {
      setBusy(false)
    }
  }

  return <details>
    <summary>{names[saved.provider]} · {saved.enabled ? 'включено' : saved.configured ? 'ключ сохранён' : 'ключ не задан'}</summary>
    <div className="form-stack">
      {bitrix ? <>
        {saved.domain && <p>Портал {saved.domain}</p>}
        <label>Адрес входящего вебхука
          <input type="password" autoComplete="new-password" value={secret} onChange={(event) => setSecret(event.target.value)} maxLength={300} placeholder={saved.configured ? 'Заменить сохранённый адрес' : 'https://портал.bitrix24.ru/rest/…'} />
        </label>
        <label>Поле кампании в лидах
          <input value={form.lead_field} onChange={(event) => setForm({ ...form, lead_field: event.target.value })} placeholder="UTM_CAMPAIGN" maxLength={80} />
        </label>
        <label>Поле кампании в сделках
          <input value={form.deal_field} onChange={(event) => setForm({ ...form, deal_field: event.target.value })} placeholder="UTM_CAMPAIGN" maxLength={80} />
        </label>
      </> : <>
        <label>Поддомен аккаунта
          <input value={form.domain} onChange={(event) => setForm({ ...form, domain: event.target.value })} placeholder="компания.amocrm.ru" maxLength={100} />
        </label>
        <label>Долгосрочный токен приватной интеграции
          <input type="password" autoComplete="new-password" value={secret} onChange={(event) => setSecret(event.target.value)} maxLength={8192} placeholder={saved.configured ? 'Заменить сохранённый токен' : ''} />
        </label>
        <label>ID дополнительного поля кампании
          <input type="number" min="1" max="2147483647" step="1" value={form.amo_field_id ?? ''} onChange={(event) => setForm({ ...form, amo_field_id: event.target.value === '' ? null : event.target.valueAsNumber })} placeholder="UTM_CAMPAIGN" />
        </label>
        <label className="toggle-row"><input type="checkbox" checked={form.amo_rub} onChange={(event) => setForm({ ...form, amo_rub: event.target.checked })} /> Валюта сделок в аккаунте — рубли</label>
      </>}
      <label className="toggle-row"><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} /> Включить подключение</label>
      <label>Автосинхронизация
        <select value={form.interval_minutes} onChange={(event) => setForm({ ...form, interval_minutes: Number(event.target.value) as Connection['interval_minutes'] })}>
          <option value="0">Выключена</option><option value="15">Каждые 15 минут</option><option value="60">Раз в час</option>
        </select>
      </label>
      <div className="button-group">
        <button className="button primary" type="button" disabled={busy || !dirty} onClick={() => void request('save')}>Сохранить подключение</button>
        {saved.enabled && <button className="button secondary" type="button" disabled={busy || dirty || saved.running} onClick={() => void request('sync')}>{saved.last_state === 'partial' ? 'Продолжить синхронизацию' : 'Синхронизировать'}</button>}
        {saved.configured && <button className="button secondary" type="button" disabled={busy} onClick={() => void request('refresh')}>Обновить статус</button>}
        {saved.configured && <button className="button secondary" type="button" disabled={busy} onClick={() => void request('remove')}>Отключить и удалить ключ</button>}
      </div>
      {error && <p role="alert">{error}</p>}
      {feedback && <p role="status">{feedback}</p>}
      {saved.running && <p role="status">Синхронизация выполняется.</p>}
      {saved.last_state && saved.last_finished_at && <p>{new Date(saved.last_finished_at).toLocaleString('ru-RU')} · {syncStates[saved.last_state]}</p>}
      {saved.last_error && <p role="alert">{saved.last_error}</p>}
      {saved.last_counts && <dl>{Object.entries(countsLabel).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{saved.last_counts?.[key as keyof Stats]}</dd></div>)}</dl>}
    </div>
  </details>
}
