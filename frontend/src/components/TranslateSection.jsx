import { useState } from 'react'

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

function downloadAll(results) {
  results.forEach((r, i) => {
    setTimeout(() => {
      const a = document.createElement('a')
      a.href = r.url; a.download = r.name; a.click()
    }, i * 300)
  })
}

export default function TranslateSection({ dict }) {
  const [files, setFiles]       = useState([])
  const [results, setResults]   = useState([]) // [{name, url}]
  const [progress, setProgress] = useState(null) // {fileIdx, fileCount, page, pages}
  const [error, setError]       = useState('')

  const activeTerms = dict
    .filter(r => r.enabled && r.russian.trim())
    .map(r => ({ original: r.original, russian: r.russian }))
    .sort((a, b) => b.original.length - a.original.length)

  function addFiles(e) {
    const incoming = Array.from(e.target.files)
    setFiles(f => {
      const names = new Set(f.map(x => x.name))
      return [...f, ...incoming.filter(x => !names.has(x.name))]
    })
    e.target.value = ''
  }

  function removeFile(idx) {
    setFiles(f => f.filter((_, i) => i !== idx))
  }

  async function handleTranslate() {
    if (!files.length || !activeTerms.length) return
    setResults([])
    setError('')
    const out = []

    for (let i = 0; i < files.length; i++) {
      const file = files[i]
      setProgress({ fileIdx: i + 1, fileCount: files.length, page: 0, pages: 0 })

      const fd = new FormData()
      fd.append('file', file)
      fd.append('terms', JSON.stringify(activeTerms))

      try {
        const response = await fetch('/api/translate-pdf', { method: 'POST', body: fd })
        if (!response.ok) throw new Error(await response.text())

        const reader  = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        outer: while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop()
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const ev = JSON.parse(line.slice(6))
            if (ev.error) throw new Error(ev.error)
            if (ev.done) {
              const bytes = Uint8Array.from(atob(ev.pdf), c => c.charCodeAt(0))
              const blob  = new Blob([bytes], { type: 'application/octet-stream' })
              out.push({ name: file.name.replace(/\.pdf$/i, '_ru.pdf'), url: URL.createObjectURL(blob) })
              break outer
            }
            if (ev.page !== undefined) {
              setProgress(p => ({ ...p, page: ev.page, pages: ev.total }))
            }
          }
        }
      } catch (err) {
        setError(`Ошибка: ${file.name} — ${err.message}`)
      }
    }

    setResults(out)
    setProgress(null)
  }

  const busy = !!progress

  return (
    <section className="card">
      <h2>Шаг 2 — Перевод PDF</h2>

      {!activeTerms.length
        ? <div className="flash flash-warn">Словарь пуст — добавьте переводы выше</div>
        : <p className="hint">Активных записей в словаре: <strong>{activeTerms.length}</strong></p>
      }

      {/* File picker */}
      <div className="upload-area">
        <label className="btn file-label file-label-lg">
          📁 Выбрать PDF файлы
          <input type="file" accept=".pdf" multiple onChange={addFiles} />
        </label>

        {files.length > 0 && (
          <ul className="file-list">
            {files.map((f, i) => (
              <li key={i}>
                <span>{f.name}</span>
                <button className="btn btn-sm" onClick={() => removeFile(i)}>✕</button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <button
        className="btn btn-primary btn-lg"
        onClick={handleTranslate}
        disabled={!files.length || !activeTerms.length || busy}
      >
        {busy
          ? `Файл ${progress.fileIdx} / ${progress.fileCount}…`
          : '▶ Перевести'}
      </button>

      {busy && progress.pages > 0 && (
        <div className="progress-wrap">
          <div className="progress-bar">
            <div className="progress-fill" style={{ width: `${progress.page / progress.pages * 100}%` }} />
          </div>
          <span className="progress-label">Страница {progress.page} / {progress.pages}</span>
        </div>
      )}

      {error && <div className="flash flash-error">{error}</div>}

      {results.length > 0 && (
        <div className="results">
          <div className="results-header">
            <p className="hint" style={{ margin: 0 }}>Готово — скачать:</p>
            {results.length > 1 && (
              <button className="btn btn-primary btn-sm" onClick={() => downloadAll(results)}>
                ⬇ Скачать все ({results.length})
              </button>
            )}
          </div>
          <ul className="results-list">
            {results.map(r => (
              <li key={r.name}>
                <span className="results-name">📄 {r.name}</span>
                <a href={r.url} download={r.name} className="btn btn-success btn-sm">⬇ Скачать</a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
