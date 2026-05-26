import { useMemo, useState } from 'react'
import DictTable from './DictTable'

const TYPES = ['термин', 'предложение', 'число', 'код', 'не латиница']
const DEFAULT_ENABLED = new Set(['термин', 'предложение'])

function detectType(text) {
  const s = text.trim()
  if (!s) return 'не латиница'
  const latin = [...s].filter(c => /[a-zA-Z]/.test(c)).length
  if (!latin) return 'не латиница'
  const digits = [...s].filter(c => /\d/.test(c)).length
  if (digits > s.length * 0.5 && latin < 4) return 'число'
  const words = s.split(/\s+/)
  if (words.length <= 2) {
    const j = words.join('')
    if (/^[A-Za-z0-9\-_/\.]+$/.test(j) && /\d/.test(j) && /[a-zA-Z]/.test(j)) return 'код'
  }
  return words.length <= 3 ? 'термин' : 'предложение'
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

const EMPTY_FILTERS = { search: '', type: 'all', ru: 'all', enabled: 'all' }

export default function DictSection({ dict, onUpdateCell, onAddRow, onClearDict, onMergeItems, onReplaceDict }) {
  const [pendingFilters, setPendingFilters] = useState(EMPTY_FILTERS)
  const [appliedFilters, setAppliedFilters] = useState(EMPTY_FILTERS)
  const [loading, setLoading] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [newOrig, setNewOrig] = useState('')
  const [newRu, setNewRu] = useState('')
  const [msg, setMsg] = useState(null) // {text, kind}

  const total   = dict.length
  const withRu  = dict.filter(r => r.russian.trim()).length
  const needRu  = dict.filter(r => r.enabled && !r.russian.trim()).length

  const filtered = useMemo(() => dict.filter(row => {
    const { search, type, ru, enabled } = appliedFilters
    if (search && !row.original.toLowerCase().includes(search.toLowerCase())
        && !row.russian.toLowerCase().includes(search.toLowerCase())) return false
    if (type !== 'all' && row.type !== type) return false
    if (ru === 'with'    && !row.russian.trim()) return false
    if (ru === 'without' &&  row.russian.trim()) return false
    if (enabled === 'yes' && !row.enabled) return false
    if (enabled === 'no'  &&  row.enabled) return false
    return true
  }), [dict, appliedFilters])

  function setFilter(k, v) { setPendingFilters(f => ({ ...f, [k]: v })) }
  function applyFilters()  { setAppliedFilters(pendingFilters) }
  function resetFilters()  { setPendingFilters(EMPTY_FILTERS); setAppliedFilters(EMPTY_FILTERS) }

  const filtersActive = Object.values(appliedFilters).some(v => v !== 'all' && v !== '')

  function flash(text, kind = 'info') {
    setMsg({ text, kind })
    setTimeout(() => setMsg(null), 3000)
  }

  async function handleExtractPDF(e) {
    const file = e.target.files[0]
    if (!file) return
    e.target.value = ''
    setLoading('extract')
    try {
      const fd = new FormData()
      fd.append('file', file)
      const res = await fetch('/api/extract-pdf', { method: 'POST', body: fd })
      const data = await res.json()
      onMergeItems(data.items)
      flash(`Извлечено: ${data.items.length} строк`)
    } catch (err) {
      flash('Ошибка извлечения: ' + err.message, 'error')
    }
    setLoading('')
  }

  async function handleLoadCSV(e) {
    const file = e.target.files[0]
    if (!file) return
    e.target.value = ''
    try {
      const fd = new FormData()
      fd.append('file', file)
      const res = await fetch('/api/parse-csv', { method: 'POST', body: fd })
      const data = await res.json()
      onReplaceDict(data.items)
      flash(`Загружено: ${data.items.length} строк`)
    } catch (err) {
      flash('Ошибка загрузки CSV: ' + err.message, 'error')
    }
  }

  async function handleExportCSV() {
    try {
      const res = await fetch('/api/export-csv', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: dict }),
      })
      downloadBlob(await res.blob(), 'mapping.csv')
    } catch (err) {
      flash('Ошибка экспорта: ' + err.message, 'error')
    }
  }

  async function handleAutoTranslate() {
    const texts = dict
      .filter(r => r.enabled && !r.russian.trim() && !r.original.includes('*'))
      .map(r => r.original)
    if (!texts.length) { flash('Нет строк для перевода', 'info'); return }
    setLoading('auto')
    try {
      const res = await fetch('/api/auto-translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts }),
      })
      const data = await res.json()
      if (data.error) { flash(data.error, 'error'); return }
      dict.forEach(row => {
        if (data.results[row.original]) onUpdateCell(row.id, 'russian', data.results[row.original])
      })
      flash(`Переведено: ${Object.keys(data.results).length} строк`)
    } catch (err) {
      flash('Ошибка авто-перевода: ' + err.message, 'error')
    }
    setLoading('')
  }

  function handleAddRow() {
    if (!newOrig.trim()) return
    const type = detectType(newOrig)
    onAddRow({ original: newOrig.trim(), type, russian: newRu.trim(), enabled: DEFAULT_ENABLED.has(type) })
    setNewOrig(''); setNewRu(''); setShowAdd(false)
  }

  return (
    <section className="card">
      <h2>Шаг 1 — Словарь переводов</h2>

      {/* Toolbar */}
      <div className="toolbar">
        <div className="toolbar-group">
          <label className="btn file-label">
            {loading === 'extract' ? '⏳ Извлекаю...' : '📄 Извлечь из PDF'}
            <input type="file" accept=".pdf" onChange={handleExtractPDF} disabled={!!loading} />
          </label>
          <label className="btn file-label">
            📂 Загрузить CSV
            <input type="file" accept=".csv" onChange={handleLoadCSV} />
          </label>
          <button className="btn" onClick={handleExportCSV}>💾 Скачать CSV</button>
        </div>

        <div className="toolbar-group">
          <button className="btn" onClick={() => setShowAdd(v => !v)}>＋ Добавить строку</button>
          <button className="btn btn-danger" onClick={() => { if (window.confirm('Очистить словарь?')) onClearDict() }}>
            🗑 Очистить
          </button>
          <button className="btn" onClick={handleAutoTranslate} disabled={!!loading}>
            {loading === 'auto' ? '⏳ Перевожу...' : '🌐 Авто-перевод'}
          </button>
        </div>

        <div className="stats">
          <span>Всего: <strong>{total}</strong></span>
          <span>С переводом: <strong>{withRu}</strong></span>
          <span className={needRu > 0 ? 'stat-warn' : ''}>Нужен перевод: <strong>{needRu}</strong></span>
        </div>
      </div>

      {msg && <div className={`flash flash-${msg.kind}`}>{msg.text}</div>}

      {/* Add row form */}
      {showAdd && (
        <div className="add-form">
          <input placeholder="Оригинал (EN)" value={newOrig} onChange={e => setNewOrig(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleAddRow()} autoFocus />
          <input placeholder="Перевод (RU)" value={newRu} onChange={e => setNewRu(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleAddRow()} />
          <button className="btn btn-primary" onClick={handleAddRow}>Добавить</button>
          <button className="btn" onClick={() => setShowAdd(false)}>Отмена</button>
        </div>
      )}

      {/* Filters */}
      <div className="filters">
        <input placeholder="🔍 Поиск..." value={pendingFilters.search}
          onChange={e => setFilter('search', e.target.value)}
          onKeyDown={e => e.key === 'Enter' && applyFilters()} />
        <select value={pendingFilters.type} onChange={e => setFilter('type', e.target.value)}>
          <option value="all">Все типы</option>
          {TYPES.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={pendingFilters.ru} onChange={e => setFilter('ru', e.target.value)}>
          <option value="all">Все</option>
          <option value="without">Без перевода</option>
          <option value="with">С переводом</option>
        </select>
        <select value={pendingFilters.enabled} onChange={e => setFilter('enabled', e.target.value)}>
          <option value="all">Все</option>
          <option value="yes">Только ✓</option>
          <option value="no">Только —</option>
        </select>
        <button className="btn btn-primary" onClick={applyFilters}>Найти</button>
        <button className="btn" onClick={resetFilters}>Сброс</button>
      </div>

      {filtersActive && filtered.length < total && (
        <p className="filter-caption">
          Показано <strong>{filtered.length}</strong> из {total} &nbsp;|&nbsp;
          Совет: используйте <code>*</code> для переменных частей — <code>Creation: *</code> → <code>Создание: *</code>
        </p>
      )}

      <DictTable rows={filtered} onUpdateCell={onUpdateCell} />
    </section>
  )
}
