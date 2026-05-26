import { useState } from 'react'
import DictSection from './components/DictSection'
import TranslateSection from './components/TranslateSection'

let _nextId = 1
function makeId() { return _nextId++ }

export default function App() {
  const [dict, setDict] = useState([])

  function updateCell(id, field, value) {
    setDict(d => d.map(r => r.id === id ? { ...r, [field]: value } : r))
  }

  function addRow(row) {
    setDict(d => [...d, { ...row, id: makeId() }])
  }

  function clearDict() { setDict([]) }

  function mergeItems(items) {
    setDict(d => {
      const existing = new Set(d.map(r => r.original))
      const fresh = items
        .filter(item => !existing.has(item.original))
        .map(item => ({ ...item, id: makeId() }))
      return [...d, ...fresh]
    })
  }

  function replaceDict(items) {
    setDict(items.map(item => ({ ...item, id: makeId() })))
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>PDF Translator</h1>
      </header>
      <main>
        <DictSection
          dict={dict}
          onUpdateCell={updateCell}
          onAddRow={addRow}
          onClearDict={clearDict}
          onMergeItems={mergeItems}
          onReplaceDict={replaceDict}
        />
        <div className="section-divider" />
        <TranslateSection dict={dict} />
      </main>
    </div>
  )
}
