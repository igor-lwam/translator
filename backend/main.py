import asyncio
import base64
import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from translator import (
    DEFAULT_ENABLED_TYPES,
    _translator_available,
    auto_translate_texts,
    detect_text_type,
    extract_lines_from_pdf_bytes,
    translate_pdf_bytes,
)

app = FastAPI(title="PDF Translator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/extract-pdf")
async def extract_pdf(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    lines = extract_lines_from_pdf_bytes(pdf_bytes)
    items = [
        {
            "original": text,
            "type": typ,
            "fontSize": round(size, 1),
            "page": page,
            "russian": "",
            "enabled": typ in DEFAULT_ENABLED_TYPES,
        }
        for text, typ, size, page in lines
    ]
    return {"items": items}


@app.post("/api/translate-pdf")
async def translate_pdf(file: UploadFile = File(...), terms: str = Form(...)):
    pdf_bytes = await file.read()
    raw_terms = json.loads(terms)
    sorted_terms = sorted(
        [
            (t["original"], t["russian"], t.get("fontSize"), t.get("fontSizeRu") or None)
            for t in raw_terms if t.get("russian", "").strip()
        ],
        key=lambda x: (-len(x[0]), x[2] is None),
    )

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def progress_cb(page: int, total: int) -> None:
        asyncio.run_coroutine_threadsafe(
            queue.put({"page": page, "total": total}), loop
        )

    async def run_translation():
        try:
            result = await loop.run_in_executor(
                None, lambda: translate_pdf_bytes(pdf_bytes, sorted_terms, progress_cb)
            )
            await queue.put({"done": True, "pdf": base64.b64encode(result).decode()})
        except Exception as e:
            await queue.put({"error": str(e)})

    asyncio.create_task(run_translation())

    async def event_stream():
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("done") or event.get("error"):
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/auto-translate")
async def auto_translate(data: dict):
    if not _translator_available:
        return Response(
            content=json.dumps({"error": "deep-translator не установлен"}),
            status_code=400,
            media_type="application/json",
        )
    texts = data.get("texts", [])
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, lambda: auto_translate_texts(texts))
    return {"results": results}


@app.post("/api/parse-csv")
async def parse_csv(file: UploadFile = File(...), delimiter: str = Form(",")):
    if len(delimiter) != 1:
        delimiter = ","
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    items = []
    header_skipped = False
    for row in reader:
        if not header_skipped and row and row[0].lower() in (
            "original", "english", "en", "оригинал"
        ):
            header_skipped = True
            continue
        if not row or not row[0].strip():
            continue
        orig = row[0]
        if len(row) == 2:
            typ, ru = detect_text_type(orig), row[1]
        else:
            typ = row[1] if len(row) > 1 else detect_text_type(orig)
            ru  = row[2] if len(row) > 2 else ""
        if len(row) > 3:
            enabled = row[3] in ("✓", "True", "true", "1")
        else:
            enabled = typ in DEFAULT_ENABLED_TYPES
        def _float(v):
            try: return float(v)
            except: return None
        font_size    = _float(row[4]) if len(row) > 4 else None
        font_size_ru = _float(row[5]) if len(row) > 5 else None
        page         = int(row[6]) if len(row) > 6 and row[6].strip().isdigit() else None
        items.append({"original": orig, "type": typ, "russian": ru, "enabled": enabled,
                      "fontSize": font_size, "fontSizeRu": font_size_ru, "page": page})
    return {"items": items}


@app.post("/api/export-csv")
async def export_csv(data: dict):
    items = data.get("items", [])
    delimiter = data.get("delimiter", ",")
    if len(delimiter) != 1:
        delimiter = ","
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(["original", "type", "russian", "enabled", "fontSize", "fontSizeRu", "page"])
    for item in items:
        writer.writerow([
            item["original"], item["type"], item["russian"],
            "✓" if item.get("enabled") else "—",
            item.get("fontSize") if item.get("fontSize") is not None else "",
            item.get("fontSizeRu") if item.get("fontSizeRu") is not None else "",
            item.get("page") if item.get("page") is not None else "",
        ])
    csv_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="mapping.csv"'},
    )


# Serve React build in production
_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")
