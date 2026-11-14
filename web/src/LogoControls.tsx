import { useCallback, useEffect, useState } from 'react'
import { api, type Post } from './api'
import { ErrorMessage, Loading, useData } from './ui'

type Variant = 'primary' | 'light' | 'dark'
const colours = { primary: 'Основной цвет', light: 'Белый', dark: 'Чёрный' }
const corners = { top_left: 'Слева сверху', top_right: 'Справа сверху', bottom_left: 'Слева снизу', bottom_right: 'Справа снизу' }

export function LogoControls({ path, post, disabled, onApply }: {
  path: string; post: Post; disabled: boolean
  onApply: (selection: { variant: string; corner: string }) => void
}) {
  const load = useCallback(() => api<{ variants: Variant[] }>(path + '/logo'), [path, post.version])
  const options = useData(load)
  const [variant, setVariant] = useState(post.logo?.variant ?? '')
  const [corner, setCorner] = useState(post.logo?.corner ?? '')
  useEffect(() => {
    setVariant(post.logo?.variant ?? '')
    setCorner(post.logo?.corner ?? '')
  }, [post.version, post.logo])
  return <section className="editor-section">
    <div className="section-inner form-stack">
      <h3>Логотип</h3>
      <ErrorMessage message={options.error} retry={options.retry} />
      {!options.data ? !options.error && <Loading /> : <form onSubmit={(event) => {
        event.preventDefault()
        onApply({ variant, corner })
      }}>
        <fieldset disabled={disabled} className="form-grid">
          <label>Цвет логотипа<select aria-label="Цвет логотипа" required value={variant} onChange={(event) => setVariant(event.target.value)}>
            <option value="" disabled>Выберите цвет</option>
            {options.data.variants.map((value) => <option value={value} key={value}>{colours[value]}</option>)}
          </select></label>
          <label>Угол фотографии<select aria-label="Угол фотографии" required value={corner} onChange={(event) => setCorner(event.target.value)}>
            <option value="" disabled>Выберите угол</option>
            {Object.entries(corners).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
          </select></label>
          <button className="button secondary" disabled={disabled || !variant || !corner}>Применить логотип</button>
        </fieldset>
      </form>}
    </div>
  </section>
}
