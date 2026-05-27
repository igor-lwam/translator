import { useEffect, useRef, useState } from 'react'

const TYPES = ['термин', 'предложение', 'число', 'код', 'не латиница']

function EditableCell({ value, onCommit, placeholder = 'введите перевод...' }) {
  const [draft, setDraft] = useState(value)
  const escaping = useRef(false)

  useEffect(() => { setDraft(value) }, [value])

  function handleKeyDown(e) {
    if (e.key === 'Enter')  { onCommit(draft); e.target.blur() }
    if (e.key === 'Escape') { escaping.current = true; e.target.blur() }
  }

  function handleBlur() {
    escaping.current = false
    setDraft(value)
  }

  return (
    <input
      className="input-ru"
      value={draft}
      placeholder={placeholder}
      onChange={e => setDraft(e.target.value)}
      onKeyDown={handleKeyDown}
      onBlur={handleBlur}
    />
  )
}

export default function DictTable({ rows, onUpdateCell }) {
  if (!rows.length) {
    return (
      <p className="empty-msg">
        Словарь пуст. Загрузите CSV или извлеките термины из PDF.
      </p>
    )
  }

  return (
    <div className="table-wrap">
      <table className="dict-table">
        <thead>
          <tr>
            <th style={{ width: '30%' }}>Оригинал</th>
            <th style={{ width: '10%' }}>Тип</th>
            <th>Перевод</th>
            <th style={{ width: '64px', textAlign: 'center' }}>Размер</th>
            <th style={{ width: '72px', textAlign: 'center' }}>Размер (пер.)</th>
            <th style={{ width: '48px', textAlign: 'center' }}>✓</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => (
            <tr key={row.id} className={row.enabled ? '' : 'row-dim'}>
              <td className="cell-orig">
                <EditableCell
                  value={row.original}
                  onCommit={v => onUpdateCell(row.id, 'original', v)}
                  placeholder="оригинал..."
                />
              </td>
              <td>
                <select
                  value={row.type}
                  onChange={e => onUpdateCell(row.id, 'type', e.target.value)}
                >
                  {TYPES.map(t => <option key={t}>{t}</option>)}
                </select>
              </td>
              <td>
                <EditableCell
                  value={row.russian}
                  onCommit={v => onUpdateCell(row.id, 'russian', v)}
                />
              </td>
              <td style={{ textAlign: 'center' }}>
                <input
                  type="number"
                  className="input-fontsize"
                  value={row.fontSize ?? ''}
                  min="4" max="72" step="0.5"
                  onChange={e => onUpdateCell(row.id, 'fontSize', e.target.value ? parseFloat(e.target.value) : null)}
                />
              </td>
              <td style={{ textAlign: 'center' }}>
                <input
                  type="number"
                  className="input-fontsize"
                  value={row.fontSizeRu ?? row.fontSize ?? ''}
                  min="4" max="72" step="0.5"
                  onChange={e => onUpdateCell(row.id, 'fontSizeRu', e.target.value ? parseFloat(e.target.value) : null)}
                />
              </td>
              <td style={{ textAlign: 'center' }}>
                <input
                  type="checkbox"
                  checked={row.enabled}
                  onChange={e => onUpdateCell(row.id, 'enabled', e.target.checked)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
