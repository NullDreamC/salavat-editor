import { useCallback, useEffect, useState } from 'react'
import { LogoControls } from './LogoControls'
import { ArrowUpRight, CalendarClock, Check, ChevronRight, FileText, Image, Layers2, MessageCircle, Newspaper, Plus, RefreshCw, Search, Send, X } from 'lucide-react'
import { api, ApiError, companyPath, fullDate, kinds, localDateTime, platforms, sourceKinds, statuses } from './api'
import type { Company, Post, Source, Status } from './api'
import { Empty, ErrorMessage, formAction, formText, Loading, Modal, PageHeader, StatusBadge, useAction, useData } from './ui'

export function Posts({ company, newPost, clearNewPost }: {
  company: Company; newPost: number[] | null; clearNewPost: () => void
}) {
  const path = companyPath(company.id)
  const loadPosts = useCallback(() => api<Post[]>(path + '/posts'), [path])
  const loadSources = useCallback(() => api<Source[]>(path + '/sources'), [path])
  const posts = useData(loadPosts)
  const sources = useData(loadSources)
  const [filter, setFilter] = useState<'all' | Status>('all')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<number>()
  const [createOpen, setCreateOpen] = useState(false)
  const [hasChanges, setHasChanges] = useState(false)
  const writable = company.role !== 'approver'
  const update = (post: Post) => posts.setData((current) => {
    const list = current ?? []
    return list.some((item) => item.id === post.id) ? list.map((item) => item.id === post.id ? post : item) : [post, ...list]
  })
  const normalized = search.trim().toLocaleLowerCase('ru')
  const visible = posts.data?.filter((post) => (filter === 'all' || post.status === filter) &&
    (!normalized || [post.title, post.body, kinds[post.kind]].some((value) => value?.toLocaleLowerCase('ru').includes(normalized))))
  const selectedPost = posts.data?.find((post) => post.id === selected)
  const newButton = <button className="button primary" onClick={() => setCreateOpen(true)}><Plus size={18} />Новая публикация</button>

  function closeCreate() {
    setCreateOpen(false)
    clearNewPost()
  }

  return <>
    <PageHeader title="Публикации" action={writable && newButton} />
    <ErrorMessage message={posts.error} retry={posts.retry} />
    {!posts.data ? !posts.error && <Loading /> :
      <div className={selectedPost ? 'post-layout with-detail' : 'post-layout'}>
        <section className="post-index" aria-label="Список публикаций">
          <div className="list-tools">
            <div className="search"><Search size={17} /><input type="search" aria-label="Поиск публикаций" placeholder="Поиск по публикациям"
              value={search} onChange={(event) => setSearch(event.target.value)} /></div>
            <select aria-label="Статус публикации" value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}>
              <option value="all">Все статусы</option>
              {Object.entries(statuses).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
            <button className="icon-button" aria-label="Обновить список публикаций" title="Обновить список"
              onClick={() => { posts.retry(); sources.retry() }}><RefreshCw size={17} /></button>
          </div>
          {!visible?.length ? <Empty icon={<Newspaper size={26} />}
            title={posts.data.length ? 'Публикации не найдены' : 'Публикаций пока нет'}
            action={posts.data.length ? <button className="button secondary" onClick={() => { setSearch(''); setFilter('all') }}>Сбросить фильтры</button> :
              writable && <button className="button secondary" onClick={() => setCreateOpen(true)}>Создать публикацию</button>} /> :
            <ul className="post-list">
              {visible.map((post) => <li key={post.id}>
                <button className={selected === post.id ? 'post-row selected' : 'post-row'} aria-pressed={selected === post.id}
                  onClick={() => {
                    if (selected === post.id || !hasChanges || window.confirm('Открыть другую публикацию без сохранения изменений?')) setSelected(post.id)
                  }}>
                  <span className="row-icon"><FileText size={18} /></span>
                  <span className="post-row-copy"><strong>{post.title || 'Без названия'}</strong>
                    <small>{kinds[post.kind] ?? post.kind} <span aria-hidden="true">/</span> {fullDate(post.created_at, company.timezone)}</small>
                  </span>
                  <StatusBadge status={post.status} /><ChevronRight className="row-chevron" size={17} aria-hidden="true" />
                </button>
              </li>)}
            </ul>}
        </section>
        {selectedPost && <PostEditor key={selectedPost.id} post={selectedPost} company={company}
          sources={sources.data ?? []} sourceError={sources.error} retrySources={sources.retry}
          onUpdate={update} onDirty={setHasChanges} onClose={() => setSelected(undefined)} />}
      </div>}
    {writable && (createOpen || newPost !== null) &&
      <CreatePost path={path} sources={sources.data} sourceError={sources.error} retrySources={sources.retry}
        initialSources={newPost ?? []} onClose={closeCreate} onCreated={(post) => {
          update(post)
          setSearch('')
          setFilter('all')
          setSelected(post.id)
          closeCreate()
        }} />}
  </>
}

function SourceSelection({ sources, selected, onChange }: { sources: Source[]; selected: number[]; onChange: (ids: number[]) => void }) {
  if (!sources.length) return <p className="muted">Добавьте заметку или файл в разделе «Материалы».</p>
  return <div className="source-choices">{sources.map((source) => <label key={source.id} className="choice">
    <input type="checkbox" checked={selected.includes(source.id)} onChange={(event) => onChange(
      event.target.checked ? [...selected, source.id] : selected.filter((id) => id !== source.id)
    )} />
    <span>{source.title}</span><small>{sourceKinds[source.kind] ?? source.kind}</small>
  </label>)}</div>
}

function CreatePost({ path, sources, sourceError, retrySources, initialSources, onClose, onCreated }: {
  path: string; sources?: Source[]; sourceError: string; retrySources: () => void; initialSources: number[]
  onClose: () => void; onCreated: (post: Post) => void
}) {
  const [selected, setSelected] = useState(initialSources)
  const action = useAction()
  return <Modal title="Новая публикация" onClose={onClose} busy={action.busy}>
    <form className="form-stack modal-body" onSubmit={(event) => formAction(event, (form) => void action.run(async () => {
      const post = await api<Post>(path + '/posts', 'POST', {
        title: formText(form, 'title') || undefined, kind: formText(form, 'kind'), source_ids: selected, platform: formText(form, 'platform'),
      })
      onCreated(post)
    }))}>
      <label>Заголовок<input name="title" placeholder="По материалу или вручную" maxLength={240} autoFocus /></label>
      <label>Тип<select name="kind">{Object.entries(kinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Площадка<select name="platform">{Object.entries(platforms).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <fieldset>
        <legend>Материалы</legend>
        {sourceError ? <ErrorMessage message={sourceError} retry={retrySources} /> : !sources ? <Loading /> :
          <SourceSelection sources={sources} selected={selected} onChange={setSelected} />}
      </fieldset>
      <ErrorMessage message={action.error} />
      <div className="modal-actions"><button type="submit" className="button primary" disabled={action.busy}>
        {action.busy ? 'Создаём…' : 'Создать черновик'}
      </button></div>
    </form>
  </Modal>
}

type Panel = 'write' | 'preview'
type Prompt = 'reject' | 'publish' | 'reconcile' | null
type PhotoMode = 'original' | 'create' | 'edit'

function PostEditor({ post, company, sources, sourceError, retrySources, onUpdate, onDirty, onClose }: {
  post: Post; company: Company; sources: Source[]; sourceError: string; retrySources: () => void
  onUpdate: (post: Post) => void; onDirty: (changed: boolean) => void; onClose: () => void
}) {
  const path = companyPath(company.id) + '/posts/' + post.id
  const [panel, setPanel] = useState<Panel>('write')
  const [title, setTitle] = useState(post.title)
  const [body, setBody] = useState(post.body)
  const [channel, setChannel] = useState(post.channel_id ?? '')
  const [platform, setPlatform] = useState(post.platform ?? 'telegram')
  const originalSchedule = post.scheduled_at ? localDateTime(post.scheduled_at, company.timezone) : ''
  const [schedule, setSchedule] = useState(originalSchedule)
  const [template, setTemplate] = useState(post.template ?? 'announcement')
  const [sourceIds, setSourceIds] = useState(post.source_ids)
  const [answer, setAnswer] = useState('')
  const [photoMode, setPhotoMode] = useState<PhotoMode>('original')
  const [photoInstruction, setPhotoInstruction] = useState('')
  const [photoSource, setPhotoSource] = useState('')
  const [prompt, setPrompt] = useState<Prompt>(null)
  const [conflict, setConflict] = useState(false)
  const [running, setRunning] = useState('')
  const action = useAction()
  const [base, setBase] = useState(post)
  const editable = company.role !== 'approver' && !['published', 'publishing'].includes(post.status) && !post.publication_uncertain
  const reviewer = company.role === 'admin' || company.role === 'approver' || (company.role === 'editor' && company.can_approve === true)
  const changedSources = sourceIds.length !== base.source_ids.length || sourceIds.some((id) => !base.source_ids.includes(id))
  const changed = platform !== (base.platform ?? 'telegram') || title !== base.title || body !== base.body || channel !== (base.channel_id ?? '') ||
    schedule !== (base.scheduled_at ? localDateTime(base.scheduled_at, company.timezone) : '') ||
    template !== (base.template ?? 'announcement') || changedSources
  const linkedSources = sources.filter((source) => sourceIds.includes(source.id))
  const attachments = linkedSources.filter((source) => source.file_url && ['image', 'video'].includes(source.kind))
  const originalPhotos = attachments.filter((source) => source.kind === 'image')
  const photoReferences = originalPhotos.filter((source) =>
    ['image/jpeg', 'image/png', 'image/webp'].includes(source.mime_type ?? ''))
  const singlePhoto = photoReferences.find((source) => String(source.id) === photoSource)
  const allPhotosUsable = photoReferences.length > 0 && photoReferences.length === originalPhotos.length && photoReferences.length <= 6
  const scheduledForLater = post.scheduled_at && new Date(post.scheduled_at).getTime() > Date.now()

  useEffect(() => {
    if (window.matchMedia('(max-width: 860px)').matches) {
      document.getElementById('post-editor')?.scrollIntoView({ block: 'start' })
    }
  }, [])

  useEffect(() => {
    onDirty(changed)
    if (!changed) return
    const confirmExit = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', confirmExit)
    return () => {
      window.removeEventListener('beforeunload', confirmExit)
      onDirty(false)
    }
  }, [changed, onDirty])

  function applyPost(next: Post) {
    setBase(next)
    setTitle(next.title)
    setBody(next.body)
    setChannel(next.channel_id)
    setPlatform(next.platform ?? 'telegram')
    setSchedule(next.scheduled_at ? localDateTime(next.scheduled_at, company.timezone) : '')
    setTemplate(next.template ?? 'announcement')
    setSourceIds(next.source_ids)
    setAnswer('')
    setConflict(false)
  }

  if (post.version !== base.version && !conflict) {
    if (changed) setConflict(true)
    else applyPost(post)
  }

  async function save(): Promise<Post> {
    if (!changed) return post
    return api<Post>(path, 'PATCH', {
      version: post.version,
      ...(title !== post.title && { title: title.trim() }),
      ...(body !== post.body && { body }),
      ...(platform !== post.platform && { platform }),
      ...(channel !== (post.channel_id ?? '') && { channel_id: channel.trim() }),
      ...(schedule !== originalSchedule && { scheduled_at: schedule || null }),
      ...(template !== (post.template ?? 'announcement') && { template }),
      ...(changedSources && { source_ids: sourceIds }),
    })
  }

  function perform(operation: string, extra = {}, saveFirst = false) {
    void action.run(async () => {
      setRunning(operation)
      let saved = post
      try {
        if (saveFirst) saved = await save()
        const result = operation === 'save' ? saved :
          await api<Post>(path + '/' + operation, 'POST', { version: saved.version, ...extra })
        setPrompt(null)
        applyPost(result)
        onUpdate(result)
        if (operation === 'answer' || operation === 'card') retrySources()
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 409 && cause.message.includes('Версия')) setConflict(true)
        if (saved.version !== post.version || operation === 'publish' || operation === 'answer') {
          const current = await api<Post>(path).catch(() => saved)
          if (current.version !== post.version || current.status !== post.status) {
            setPrompt(null)
            applyPost(current)
            onUpdate(current)
            if (operation === 'answer') retrySources()
          }
        }
        throw cause
      } finally {
        setRunning('')
      }
    })
  }

  const locked = action.busy || conflict
  const canSubmit = editable && (changed || ['draft', 'rejected', 'failed'].includes(post.status)) && !post.questions.length && !!body.trim()
  const publishChannel = post.channel_id

  return <article className="post-editor" id="post-editor" aria-label="Публикация">
    <header className="editor-header">
      <div><StatusBadge status={post.status} /><h2>{post.title || 'Без названия'}</h2></div>
      <button className="icon-button" type="button" disabled={action.busy} aria-label="Закрыть публикацию" onClick={() => {
        if (!changed || window.confirm('Закрыть публикацию без сохранения изменений?')) onClose()
      }}><X size={19} /></button>
    </header>
    <div className="editor-tabs" aria-label="Вид публикации">
      <button className={panel === 'write' ? 'tab active' : 'tab'} aria-pressed={panel === 'write'} onClick={() => setPanel('write')}>Редактор</button>
      <button className={panel === 'preview' ? 'tab active' : 'tab'} aria-pressed={panel === 'preview'} onClick={() => setPanel('preview')}>Предпросмотр</button>
    </div>
    <div className="editor-body">
      <ErrorMessage message={prompt ? '' : action.error || (conflict ? 'Публикация изменилась. Загрузите текущую версию.' : '')} />
      {conflict && <button className="button secondary" onClick={() => {
        if (!changed || window.confirm('Загрузить текущую версию без сохранения изменений?')) void action.run(async () => {
          const current = await api<Post>(path)
          applyPost(current)
          onUpdate(current)
        })
      }}>Загрузить текущую версию</button>}
      {post.feedback && <div className="notice"><MessageCircle size={17} /><p>{post.feedback}</p></div>}
      {panel === 'preview' ? <div className="message-preview">
        <div className="preview-post">
          <strong>{company.name}</strong>
          {post.card_url && <img className="preview-card" src={post.card_url} alt="Карточка публикации" />}
          {post.photo_url && <img className="preview-card" src={post.photo_url} alt="Оформленное изображение" />}
          {attachments.filter((source) => !post.photo_url || source.kind !== 'image').map((source) => source.kind === 'image'
            ? <img className="preview-card" key={source.id} src={source.file_url!} alt={source.title} />
            : <video className="preview-card" key={source.id} src={source.file_url!} controls preload="metadata" aria-label={source.title} />)}
          <p>{body.trim() || 'Текст публикации пока пуст.'}</p>
        </div>
      </div> : <>
        <fieldset disabled={locked || !editable} className="form-stack editor-fields">
          <label>Заголовок<input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={240} /></label>
          <label>Текст<textarea rows={9} value={body} maxLength={20000} onChange={(event) => setBody(event.target.value)} /></label>
        </fieldset>
        {!!post.questions.length && <form className="form-stack question-block" onSubmit={(event) => {
          event.preventDefault()
          if (answer.trim()) perform('answer', { text: answer.trim() }, true)
        }}>
          <h3>Нужно уточнение</h3>
          <ul>{post.questions.map((question, index) => <li key={index}>{question}</li>)}</ul>
          {editable && <><label>Ответ<textarea rows={3} required maxLength={120000} value={answer} disabled={locked}
            onChange={(event) => setAnswer(event.target.value)} /></label>
            <button className="button secondary" disabled={locked || !answer.trim()}>{running === 'answer' ? 'Обновляем…' : 'Ответить и обновить текст'}</button>
          </>}
        </form>}
        <details className="editor-section">
          <summary><Layers2 size={17} />Материалы <span className="summary-count">{sourceIds.length}</span></summary>
          <fieldset disabled={locked || !editable} className="section-inner">
            {sourceError ? <ErrorMessage message={sourceError} retry={retrySources} /> :
              editable ? <SourceSelection sources={sources} selected={sourceIds} onChange={setSourceIds} /> :
              linkedSources.length ? <ul className="plain-list">{linkedSources.map((source) => <li key={source.id}>
                {source.file_url ? <a href={source.file_url} target="_blank" rel="noreferrer">{source.title}</a> : source.title}</li>)}</ul> : <p className="muted">Нет материалов</p>}
          </fieldset>
        </details>
        <details className="editor-section">
          <summary><Image size={17} />Фотографии</summary>
          <div className="section-inner form-stack">
            {editable && <form className="form-stack" onSubmit={(event) => {
              event.preventDefault()
              perform('photo', { mode: photoMode, ...(photoMode !== 'original' && { instruction: photoInstruction.trim() }),
                ...(photoMode === 'edit' && singlePhoto && { source_id: singlePhoto.id }) }, true)
            }}>
              <label>Режим<select aria-label="Режим" value={photoMode} disabled={locked} onChange={(event) => setPhotoMode(event.target.value as PhotoMode)}>
                <option value="original">Исходные фотографии</option>
                <option value="create">Создать изображение</option>
                <option value="edit">Оформить фотографии</option>
              </select></label>
              {photoMode === 'edit' && <label>Исходное фото<select aria-label="Исходное фото" value={singlePhoto ? photoSource : ''} disabled={locked || !photoReferences.length}
                onChange={(event) => setPhotoSource(event.target.value)}>
                <option value="" disabled={!allPhotosUsable}>{allPhotosUsable ? 'Все фотографии публикации' : 'Выберите фото'}</option>
                {photoReferences.map((source) => <option value={source.id} key={source.id}>{source.title}</option>)}
              </select></label>}
              {photoMode === 'edit' && !photoReferences.length && <p className="muted">Добавьте JPEG, PNG или WebP в материалы публикации.</p>}
              {photoMode !== 'original' && <>
                <label>{photoMode === 'edit' ? 'Что изменить' : 'Что должно быть на изображении'}<textarea rows={3} maxLength={4000} required disabled={locked}
                  value={photoInstruction} onChange={(event) => setPhotoInstruction(event.target.value)} /></label>
                <small className="muted">Логотип добавится автоматически. Цвет и угол можно изменить.</small>
              </>}
              {photoMode === 'original' && !originalPhotos.length && <p className="muted">Добавьте фотографии в материалы публикации.</p>}
              <button className="button secondary" disabled={locked || (photoMode !== 'original' && !photoInstruction.trim()) ||
                (photoMode === 'edit' && !allPhotosUsable && !singlePhoto) || (photoMode === 'original' && !originalPhotos.length)}>
                {running === 'photo' ? 'Обрабатываем…' : photoMode === 'original' ? 'Использовать оригиналы' :
                  photoMode === 'create' ? 'Создать изображение' : 'Оформить фотографии'}
              </button>
            </form>}
            {post.photo_url && <img className="preview-card" src={post.photo_url} alt="Оформленное изображение" />}
          </div>
        </details>
        <details className="editor-section">
          <summary><Image size={17} />Карточка</summary>
          <div className="section-inner form-stack">
            {editable && <><label>Шаблон<select value={template} disabled={locked} onChange={(event) => setTemplate(event.target.value)}>
              <option value="announcement">Анонс</option><option value="person">Сотрудник</option><option value="results">Итоги</option>
            </select></label>
            <button className="button secondary" disabled={locked || !title.trim()} onClick={() => perform('card', {}, true)}>
              {running === 'card' ? 'Собираем…' : post.card_url ? 'Обновить карточку' : 'Создать карточку'}
            </button></>}
            {post.card_url ? <img className="preview-card" src={post.card_url} alt="Карточка публикации" /> :
              !editable && <p className="muted">Нет карточки</p>}
          </div>
        </details>
        <details className="editor-section">
          <summary><CalendarClock size={17} />Канал и дата</summary>
          <fieldset disabled={locked || !editable} className="section-inner form-stack">
            <label>Площадка<select value={platform} onChange={event => {
              const next = event.target.value as Post['platform']
              if (next === 'telegram') { setPlatform(next); setChannel(company.channel_id); return }
              void action.run(async () => {
                const connection = await api<{ destination_id: string }>(companyPath(company.id) + '/social/' + next)
                setPlatform(next); setChannel(connection.destination_id)
              })
            }}>
              {Object.entries(platforms).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <label>Адрес публикации<input value={channel} readOnly={platform !== 'telegram'} placeholder={platform === 'telegram' ? company.channel_id || '@название_канала' : 'Задайте адрес в настройках компании'}
              onChange={(event) => setChannel(event.target.value)} /></label>
            {platform !== 'telegram' && <button className="button secondary" type="button" onClick={() => void action.run(async () => {
              const connection = await api<{ destination_id: string }>(companyPath(company.id) + '/social/' + platform)
              setChannel(connection.destination_id)
              if (!connection.destination_id) throw new Error('Укажите адрес площадки в настройках компании.')
            })}>Взять адрес из настроек</button>}
            <label>Дата и время ({company.timezone})<input type="datetime-local" value={schedule} onChange={(event) => setSchedule(event.target.value)} /></label>
          </fieldset>
        </details>
      </>}
      {post.logo_editable && (editable || (reviewer && ['review', 'approved'].includes(post.status))) &&
        <LogoControls path={path} post={post} disabled={locked || changed} onApply={(selection) => perform('logo', selection)} />}
      {post.publication_url && <a className="text-link" href={post.publication_url} target="_blank" rel="noreferrer">Открыть публикацию <ArrowUpRight size={16} /></a>}
      {reviewer && post.publication_uncertain && <button className="button secondary" disabled={locked} onClick={() => setPrompt('reconcile')}>Проверить результат отправки</button>}
    </div>
    <footer className="editor-footer">
      {editable && changed && <button className="button secondary" disabled={locked} onClick={() => perform('save', {}, true)}>
        {running === 'save' ? 'Сохраняем…' : 'Сохранить'}
      </button>}
      {editable && ['draft', 'rejected', 'needs_info', 'failed'].includes(post.status) && !!sourceIds.length &&
        <button className="button secondary" disabled={locked} onClick={() => perform('generate', {}, true)}>
          {running === 'generate' ? 'Готовим текст…' : body.trim() ? 'Обновить текст' : 'Подготовить текст'}
        </button>}
      {canSubmit && <button className="button primary" disabled={locked || post.logo_pending} onClick={() => perform('submit', {}, true)}>На согласование</button>}
      {post.logo_pending && <small className="muted">Выберите цвет и угол логотипа.</small>}
      {reviewer && ['review', 'approved'].includes(post.status) &&
        <button className="button secondary" disabled={locked || changed} onClick={() => setPrompt('reject')}>Вернуть на доработку</button>
      }
      {reviewer && post.status === 'review' &&
        <button className="button primary" disabled={locked || changed} onClick={() => perform('approve')}><Check size={17} />Согласовать</button>
      }
      {reviewer && post.status === 'approved' && (scheduledForLater ?
        <span className="scheduled-note"><CalendarClock size={17} />Запланировано на {fullDate(post.scheduled_at!, company.timezone)}</span> :
        <button className="button primary" disabled={locked || changed || !publishChannel} onClick={() => setPrompt('publish')}><Send size={16} />Опубликовать</button>)}
      {reviewer && post.status === 'approved' && !publishChannel && <p className="muted">Укажите адрес публикации.</p>}
      <small className="version">Версия {post.version}</small>
    </footer>
    {prompt && <Modal title={prompt === 'reject' ? 'Вернуть на доработку' : prompt === 'reconcile' ? 'Результат отправки' : `Опубликовать: ${platforms[post.platform ?? 'telegram']}`} busy={action.busy}
      onClose={() => { action.setError(''); setPrompt(null) }}>
      <form className="modal-body form-stack" onSubmit={(event) => formAction(event, (form) => {
        if (prompt === 'reject') perform('reject', { comment: formText(form, 'comment') })
        else if (prompt === 'reconcile') perform('reconcile', {
          outcome: formText(form, 'outcome'), note: formText(form, 'note'),
          message_id: formText(form, 'message_id') || undefined, publication_url: formText(form, 'publication_url') || undefined,
        })
        else perform('publish')
      })}>
        {prompt === 'reject' ? <label>Что нужно исправить<textarea name="comment" autoFocus required maxLength={4000} rows={4} /></label> : prompt === 'reconcile' ? <>
          <p>Проверьте канал «{post.channel_id}», затем укажите результат.</p>
          <label>Результат<select name="outcome"><option value="published">Публикация найдена</option><option value="not_published">Публикации нет — вернуть в черновики</option></select></label>
          <label>ID сообщения<input name="message_id" maxLength={254} /></label>
          <label>Ссылка<input name="publication_url" type="url" maxLength={2000} /></label>
          <label>Комментарий<textarea name="note" maxLength={2000} rows={2} /></label>
          <label className="choice"><input type="checkbox" required />Канал проверен вручную</label>
        </> :
          <p>Канал <strong>{publishChannel}</strong><br />{post.title || 'Без названия'}, версия {post.version}</p>}
        <ErrorMessage message={action.error} />
        <div className="modal-actions">
          <button className="button secondary" type="button" disabled={action.busy} onClick={() => setPrompt(null)}>Отмена</button>
          <button className="button primary" disabled={locked}>{action.busy ? 'Подождите…' : prompt === 'reject' ? 'Вернуть' : prompt === 'reconcile' ? 'Сохранить результат' : 'Опубликовать'}</button>
        </div>
      </form>
    </Modal>}
  </article>
}
