import { useCallback, useState } from 'react'
import { Check } from 'lucide-react'
import { api } from './api'
import { ErrorMessage, formAction, formText, Loading, useAction, useData } from './ui'

type Brand = {
  light_logo_configured: boolean
  dark_logo_configured: boolean
  primary_logo_configured: boolean
  light_background: string
  dark_background: string
  accents: string[]
  theme: 'auto' | 'light' | 'dark'
}

export function BrandSettings({ path }: { path: string }) {
  const brandPath = path + '/brand'
  const load = useCallback(() => api<Brand>(brandPath), [brandPath])
  const brand = useData(load)
  const action = useAction()
  const [saved, setSaved] = useState('')
  const value = brand.data
  return <section className="surface settings-form separate-section">
    <ErrorMessage message={brand.error} retry={brand.retry} />
    {!value ? !brand.error && <Loading /> : <>
      <form onChange={() => setSaved('')} onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
        const accents = formText(form, 'accents').split(/[\s,]+/).filter(Boolean)
        if (accents.some((accent) => !/^#[0-9a-f]{6}$/i.test(accent))) {
          action.setError('Укажите цвета в формате #008D9A.')
          return
        }
        brand.setData(await api<Brand>(brandPath, 'PATCH', {
          light_background: formText(form, 'light_background'), dark_background: formText(form, 'dark_background'),
          accents, theme: formText(form, 'theme'),
        }))
        setSaved('Цвета сохранены')
      }))}>
        <fieldset disabled={action.busy} className="settings-section">
          <h2>Фирменный стиль</h2>
          <div className="form-grid">
            <label>Светлый фон<input type="color" name="light_background" defaultValue={value.light_background} /></label>
            <label>Тёмный фон<input type="color" name="dark_background" defaultValue={value.dark_background} /></label>
            <label>Акцентные цвета<input name="accents" placeholder="#008D9A, #2679A7" defaultValue={value.accents.join(', ')} /></label>
            <label>Фон для оформления<select name="theme" defaultValue={value.theme}>
              <option value="auto">Автоматически</option><option value="light">Светлый</option><option value="dark">Тёмный</option>
            </select></label>
            <button className="button secondary" type="submit">Сохранить цвета</button>
          </div>
        </fieldset>
      </form>
      <div className="settings-section brand-logos">
        <h2>Логотипы</h2>
        <div className="form-stack">
          {(['primary', 'dark', 'light'] as const).map((variant) => <form key={variant} className="logo-upload"
            onSubmit={(event) => {
              const element = event.currentTarget
              formAction(event, (form) => void action.run(async () => {
                setSaved('')
                brand.setData(await api<Brand>(brandPath + '/logos/' + variant, 'POST', form))
                element.reset()
                setSaved('Логотип сохранён')
              }))
            }}>
            <label>{variant === 'primary' ? 'Основной цвет' : variant === 'dark' ? 'Тёмный' : 'Светлый'}
              {value[variant + '_logo_configured' as 'dark_logo_configured' | 'light_logo_configured' | 'primary_logo_configured'] && <small className="muted">Логотип загружен</small>}
              <input type="file" name="file" className="file-input" accept="image/png" required disabled={action.busy} />
            </label>
            <button className="button secondary" disabled={action.busy}>Загрузить PNG</button>
          </form>)}
          <small className="muted">PNG с прозрачным фоном.</small>
        </div>
      </div>
      {(action.error || saved) && <div className="settings-footer">
        <ErrorMessage message={action.error} />
        {saved && <span className="inline-label saved" role="status"><Check size={17} />{saved}</span>}
      </div>}
    </>}
  </section>
}
