"""
streamlit_app.py — PDF Translator (Web)
"""

import csv
import io
import re
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from deep_translator import GoogleTranslator
    _translator_available = True
except ImportError:
    _translator_available = False

try:
    import fitz
except ImportError:
    st.error("PyMuPDF не установлен. Выполните: pip install pymupdf")
    st.stop()


# ---------------------------------------------------------------------------
# Шрифты (кросс-платформенно)
# ---------------------------------------------------------------------------

_LINUX_FONT_MAP = {
    "arial.ttf":   ["LiberationSans-Regular.ttf",    "DejaVuSans.ttf"],
    "arialbd.ttf": ["LiberationSans-Bold.ttf",        "DejaVuSans-Bold.ttf"],
    "ariali.ttf":  ["LiberationSans-Italic.ttf",      "DejaVuSans-Oblique.ttf"],
    "arialbi.ttf": ["LiberationSans-BoldItalic.ttf",  "DejaVuSans-BoldOblique.ttf"],
}

_LINUX_FONT_DIRS = [
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/msttcorefonts"),
    Path("/usr/share/fonts"),
]


def _find_font(win: str, mac_names: list) -> str | None:
    if sys.platform == "win32":
        p = Path("C:/Windows/Fonts") / win
        return str(p) if p.exists() else None
    if sys.platform == "darwin":
        for d in [Path("/Library/Fonts"),
                  Path("/System/Library/Fonts/Supplemental"),
                  Path.home() / "Library/Fonts"]:
            for name in mac_names:
                p = d / name
                if p.exists():
                    return str(p)
    else:  # Linux
        candidates = _LINUX_FONT_MAP.get(win, []) + [win]
        for d in _LINUX_FONT_DIRS:
            for name in candidates:
                p = d / name
                if p.exists():
                    return str(p)
    return None


_FONT_PATH        = _find_font("arial.ttf",   ["Arial.ttf"])
_FONT_BOLD_PATH   = _find_font("arialbd.ttf",  ["Arial Bold.ttf", "ArialBd.ttf"])
_FONT_ITALIC_PATH = _find_font("ariali.ttf",   ["Arial Italic.ttf", "ArialI.ttf"])
_FONT_BI_PATH     = _find_font("arialbi.ttf",  ["Arial Bold Italic.ttf", "ArialBI.ttf"])


def _make_fobj(path: str | None) -> fitz.Font:
    if path:
        try:
            return fitz.Font(fontfile=path)
        except Exception:
            pass
    return fitz.Font("helv")


_FOBJ    = _make_fobj(_FONT_PATH)
_FOBJ_B  = _make_fobj(_FONT_BOLD_PATH)
_FOBJ_I  = _make_fobj(_FONT_ITALIC_PATH)
_FOBJ_BI = _make_fobj(_FONT_BI_PATH)

_FONT_MAP = {
    (False, False): ("arial-cy",    _FONT_PATH,        _FOBJ),
    (True,  False): ("arial-cy-b",  _FONT_BOLD_PATH,   _FOBJ_B),
    (False, True):  ("arial-cy-i",  _FONT_ITALIC_PATH, _FOBJ_I),
    (True,  True):  ("arial-cy-bi", _FONT_BI_PATH,     _FOBJ_BI),
}

_DEFAULT_ENABLED_TYPES = {"термин", "предложение"}


# ---------------------------------------------------------------------------
# PDF-логика
# ---------------------------------------------------------------------------

def _detect_text_type(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "не латиница"
    latin_count = sum(1 for c in stripped if c.isascii() and c.isalpha())
    if latin_count == 0:
        return "не латиница"
    digit_count = sum(1 for c in stripped if c.isdigit())
    if digit_count > len(stripped) * 0.5 and latin_count < 4:
        return "число"
    words = stripped.split()
    if len(words) <= 2:
        joined = "".join(words)
        if re.fullmatch(r'[A-Za-z0-9\-_/\.]+', joined):
            if any(c.isdigit() for c in joined) and any(c.isalpha() for c in joined):
                return "код"
    if len(words) <= 3:
        return "термин"
    return "предложение"


def _apply_dict(text: str, sorted_terms: list) -> str:
    stripped = text.strip()
    for en, ru in sorted_terms:
        if not ru.strip():
            continue
        if '*' in en:
            parts = en.split('*')
            pattern_parts = []
            for i, part in enumerate(parts):
                if i > 0:
                    pattern_parts.append(r'(.*)' if i == len(parts) - 1 else r'(.*?)')
                pattern_parts.append(re.escape(part))
            pattern = ''.join(pattern_parts)
            m = re.fullmatch(pattern, stripped, re.IGNORECASE | re.DOTALL)
            if m:
                result = ru
                for cap in m.groups():
                    result = result.replace('*', cap, 1)
                return result
        elif stripped.lower() == en.strip().lower():
            return ru.strip()
    result = text
    for en, ru in sorted_terms:
        if ru.strip() and '*' not in en:
            result = re.sub(r'(?i)\b' + re.escape(en) + r'\b', ru, result)
    return result


def _block_align(block: dict) -> int:
    bx0, _, bx1, _ = block["bbox"]
    bw = bx1 - bx0
    if bw < 1:
        return 0
    lines = block.get("lines", [])
    if not lines:
        return 0
    spans = lines[0].get("spans", [])
    if not spans:
        return 0
    sx0 = spans[0]["bbox"][0]
    sx1 = spans[-1]["bbox"][2]
    left_gap  = sx0 - bx0
    right_gap = bx1 - sx1
    if right_gap < 8 and left_gap > bw * 0.15:
        return 2
    if left_gap > bw * 0.25 and abs(left_gap - right_gap) < bw * 0.15:
        return 1
    return 0


def _ensure_contrast(bg: tuple, fg: tuple) -> tuple:
    bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    fg_lum = 0.299 * fg[0] + 0.587 * fg[1] + 0.114 * fg[2]
    if bg_lum < 0.4 and fg_lum < 0.4:
        return (1.0, 1.0, 1.0)
    return fg


def _bg_color(rect: fitz.Rect, drawings: list) -> tuple:
    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    hits = []
    for d in drawings:
        fill = d.get("fill")
        if fill is None:
            continue
        dr = fitz.Rect(d["rect"])
        if dr.x0 <= cx <= dr.x1 and dr.y0 <= cy <= dr.y1:
            hits.append((dr.get_area(), fill))
    if hits:
        hits.sort()
        c = hits[0][1]
        if len(c) == 1:
            return (c[0], c[0], c[0])
        if len(c) == 3:
            return (c[0], c[1], c[2])
        if len(c) == 4:
            k = c[3]
            return ((1-c[0])*(1-k), (1-c[1])*(1-k), (1-c[2])*(1-k))
    return (1.0, 1.0, 1.0)


def _translate_page(page: fitz.Page, sorted_terms: list) -> None:
    blocks_data = page.get_text("dict")["blocks"]
    replacements = []

    for block in blocks_data:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(sp.get("text", "") for sp in spans)
            translated = _apply_dict(line_text, sorted_terms)
            if translated.strip() == line_text.strip() or not translated.strip():
                continue
            rect = fitz.Rect(line["bbox"])
            best_len, size, color, bold, italic = -1, 10.0, (0.0, 0.0, 0.0), False, False
            for sp in spans:
                ln = len(sp.get("text", ""))
                if ln > best_len:
                    best_len = ln
                    size  = float(sp.get("size", 10.0))
                    flags = sp.get("flags", 0)
                    bold   = bool(flags & 16)
                    italic = bool(flags & 2)
                    raw = sp.get("color", 0)
                    if isinstance(raw, int):
                        color = (
                            ((raw >> 16) & 0xFF) / 255.0,
                            ((raw >> 8)  & 0xFF) / 255.0,
                            (raw & 0xFF) / 255.0,
                        )
            align = _block_align({"bbox": line["bbox"], "lines": [line]})
            replacements.append((rect, translated.strip(), size, color, bool(bold), bool(italic), align))

    if not replacements:
        return

    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []

    raw_bgs = [_bg_color(rect, drawings) for rect, *_ in replacements]

    fill_colors = []
    for (_, _, _, color, *_), bg in zip(replacements, raw_bgs):
        bg_lum = 0.299*bg[0] + 0.587*bg[1] + 0.114*bg[2]
        fg_lum = 0.299*color[0] + 0.587*color[1] + 0.114*color[2]
        fill_colors.append(bg if (bg_lum < 0.4 and fg_lum >= 0.4) else None)

    for (rect, *_), fill in zip(replacements, fill_colors):
        page.add_redact_annot(rect, fill=fill)
    page.apply_redactions(graphics=0)

    registered: set[str] = set()
    for _, _, _, _, bold, italic, _ in replacements:
        fname, ffile, _ = _FONT_MAP[(bold, italic)]
        if fname not in registered and ffile:
            page.insert_font(fontname=fname, fontfile=ffile)
            registered.add(fname)

    for (rect, text, size, color, bold, italic, align), fill, bg in zip(
            replacements, fill_colors, raw_bgs):
        fname, ffile, fobj = _FONT_MAP[(bold, italic)]
        fg = _ensure_contrast(fill, color) if fill is not None else color
        tw = fobj.text_length(text, fontsize=size)
        if align == 2:
            x = rect.x1 - tw
        elif align == 1:
            x = rect.x0 + (rect.width - tw) / 2
        else:
            x = rect.x0
        mid_y      = (rect.y0 + rect.y1) / 2
        baseline_y = mid_y + (fobj.ascender + fobj.descender) / 2 * size
        page.insert_text(fitz.Point(x, baseline_y), text,
                         fontname=fname if ffile else "helv",
                         fontsize=size, color=fg)


def translate_pdf_bytes(pdf_bytes: bytes, sorted_terms: list,
                        progress_cb=None) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = len(doc)
    for idx in range(n):
        if progress_cb:
            progress_cb(idx + 1, n)
        _translate_page(doc[idx], sorted_terms)
    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()


def extract_lines_from_pdf_bytes(pdf_bytes: bytes,
                                  progress_cb=None) -> list[tuple[str, str]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    seen: dict[str, str] = {}
    total = len(doc)
    for i, page in enumerate(doc):
        if progress_cb:
            progress_cb(i + 1, total)
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(
                    sp.get("text", "") for sp in line.get("spans", [])
                ).strip()
                if text and text not in seen:
                    seen[text] = _detect_text_type(text)
    doc.close()
    return list(seen.items())


def auto_translate_texts(texts: list[str], progress_cb=None) -> dict[str, str]:
    if not _translator_available:
        raise RuntimeError("Установите: pip install deep-translator")
    translator = GoogleTranslator(source="en", target="ru")
    results: dict[str, str] = {}
    batch_size = 30
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        joined = "\n---\n".join(batch)
        try:
            translated = translator.translate(joined)
            parts = [p.strip() for p in translated.split("\n---\n")]
            if len(parts) == len(batch):
                for orig, ru in zip(batch, parts):
                    results[orig] = ru
            else:
                for text in batch:
                    try:
                        results[text] = translator.translate(text)
                    except Exception:
                        results[text] = ""
        except Exception:
            for text in batch:
                try:
                    results[text] = translator.translate(text)
                except Exception:
                    results[text] = ""
        if progress_cb:
            progress_cb(min(i + batch_size, len(texts)), len(texts))
    return results


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def df_from_csv_bytes(content: bytes) -> pd.DataFrame:
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    records = []
    header_skipped = False
    for row in rows:
        if not header_skipped and row and row[0].lower() in (
                "original", "english", "en", "оригинал"):
            header_skipped = True
            continue
        if not row or not row[0].strip():
            continue
        orig = row[0]
        if len(row) == 2:
            typ, ru = _detect_text_type(orig), row[1]
        else:
            typ = row[1] if len(row) > 1 else _detect_text_type(orig)
            ru  = row[2] if len(row) > 2 else ""
        enabled_default = typ in _DEFAULT_ENABLED_TYPES
        if len(row) > 3:
            enabled = row[3] in ("✓", "True", "true", "1")
        else:
            enabled = enabled_default
        records.append({"original": orig, "type": typ, "russian": ru,
                         "enabled": enabled})
    return pd.DataFrame(records) if records else _empty_df()


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["original", "type", "russian", "enabled"])
    for _, row in df.iterrows():
        writer.writerow([row["original"], row["type"], row["russian"],
                         "✓" if row["enabled"] else "—"])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["original", "type", "russian", "enabled"])


def _get_sorted_terms(df: pd.DataFrame) -> list[tuple[str, str]]:
    terms = []
    for _, row in df.iterrows():
        orig = str(row["original"]).strip()
        ru   = str(row["russian"]).strip()
        if orig and ru and row["enabled"]:
            terms.append((orig, ru))
    return sorted(terms, key=lambda x: len(x[0]), reverse=True)


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="PDF Translator", layout="wide", page_icon="📄")
    st.title("PDF Translator")

    if "df" not in st.session_state:
        st.session_state.df = _empty_df()
    if "translated" not in st.session_state:
        st.session_state.translated = {}

    tab_dict, tab_translate = st.tabs(["Словарь переводов", "Перевод PDF"])

    with tab_dict:
        _dict_tab()

    with tab_translate:
        _translate_tab()


def _dict_tab():
    # ---- Тулбар ----
    col_extract, col_load, col_save, col_auto, col_add, col_del = st.columns(6)

    with col_extract:
        extract_file = st.file_uploader("Извлечь из PDF",
                                         type="pdf", key="extract_pdf",
                                         label_visibility="collapsed")
        if extract_file:
            _do_extract(extract_file)

        if st.button("📄 Извлечь из PDF...", use_container_width=True):
            st.info("Загрузите PDF-файл через поле выше")

    with col_load:
        csv_file = st.file_uploader("Загрузить CSV",
                                     type="csv", key="load_csv",
                                     label_visibility="collapsed")
        if csv_file:
            st.session_state.df = df_from_csv_bytes(csv_file.read())
            st.success(f"Загружено: {len(st.session_state.df)} строк")
            st.rerun()
        if st.button("📂 Загрузить CSV", use_container_width=True):
            st.info("Загрузите CSV-файл через поле выше")

    with col_save:
        csv_bytes = df_to_csv_bytes(st.session_state.df)
        st.download_button("💾 Сохранить CSV", data=csv_bytes,
                           file_name="mapping.csv", mime="text/csv",
                           use_container_width=True)

    with col_auto:
        if st.button("🌐 Авто-перевод (Google)", use_container_width=True):
            _do_auto_translate()

    with col_add:
        if st.button("＋ Строка", use_container_width=True):
            st.session_state.show_add_form = True

    with col_del:
        if st.button("✕ Очистить всё", use_container_width=True):
            if st.session_state.get("confirm_clear"):
                st.session_state.df = _empty_df()
                st.session_state.confirm_clear = False
                st.rerun()
            else:
                st.session_state.confirm_clear = True

    if st.session_state.get("confirm_clear"):
        st.warning("Нажмите ещё раз '✕ Очистить всё' для подтверждения, или обновите страницу для отмены.")

    # ---- Форма добавления строки ----
    if st.session_state.get("show_add_form"):
        with st.form("add_row_form"):
            c1, c2 = st.columns(2)
            new_orig = c1.text_input("Оригинал")
            new_ru   = c2.text_input("Перевод")
            submitted = st.form_submit_button("Добавить")
            if submitted and new_orig.strip():
                typ = _detect_text_type(new_orig)
                enabled = typ in _DEFAULT_ENABLED_TYPES
                new_row = pd.DataFrame([{
                    "original": new_orig.strip(),
                    "type": typ,
                    "russian": new_ru.strip(),
                    "enabled": enabled,
                }])
                st.session_state.df = pd.concat(
                    [st.session_state.df, new_row], ignore_index=True)
                st.session_state.show_add_form = False
                st.rerun()

    # ---- Фильтры ----
    df = st.session_state.df

    with st.expander("🔍 Фильтры", expanded=len(df) > 20):
        fc1, fc2, fc3, fc4, fc5 = st.columns([3, 2, 2, 2, 1])
        search    = fc1.text_input("Поиск", placeholder="текст в оригинале или переводе...")
        type_f    = fc2.selectbox("Тип", ["Все типы", "термин", "предложение",
                                           "число", "код", "не латиница"])
        ru_f      = fc3.selectbox("Перевод", ["Все", "Без перевода", "С переводом"])
        en_f      = fc4.selectbox("Включено", ["Все", "Только ✓", "Только —"])
        if fc5.button("Сброс", use_container_width=True):
            st.rerun()

    # ---- Применяем фильтры ----
    mask = pd.Series([True] * len(df), index=df.index)
    if search:
        mask &= (df["original"].str.contains(search, case=False, na=False) |
                 df["russian"].str.contains(search, case=False, na=False))
    if type_f != "Все типы":
        mask &= df["type"] == type_f
    if ru_f == "Без перевода":
        mask &= df["russian"].str.strip().eq("")
    elif ru_f == "С переводом":
        mask &= df["russian"].str.strip().ne("")
    if en_f == "Только ✓":
        mask &= df["enabled"] == True
    elif en_f == "Только —":
        mask &= df["enabled"] == False

    filtered = df[mask].copy()

    # ---- Счётчик ----
    total   = len(df)
    shown   = len(filtered)
    enabled = int(df["enabled"].sum()) if len(df) else 0
    with_ru = int(df["russian"].str.strip().ne("").sum()) if len(df) else 0
    st.caption(
        f"Показано: **{shown}** / {total}  |  Включено: **{enabled}**  |  "
        f"С переводом: **{with_ru}**  |  "
        "Совет: используйте `*` для переменных частей — `Creation: *` → `Создание: *`"
    )

    # ---- Таблица ----
    edited = st.data_editor(
        filtered,
        column_config={
            "original": st.column_config.TextColumn("Оригинал", width="large"),
            "type": st.column_config.SelectboxColumn(
                "Тип", width="small",
                options=["термин", "предложение", "число", "код", "не латиница"]),
            "russian": st.column_config.TextColumn("Перевод", width="large"),
            "enabled": st.column_config.CheckboxColumn("✓", width="small"),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key="dict_editor",
    )

    # Сохраняем правки обратно в полный датафрейм
    st.session_state.df.update(edited)


def _do_extract(uploaded_file):
    pdf_bytes = uploaded_file.read()
    progress_bar = st.progress(0, text="Читаю PDF...")

    def on_progress(page, total):
        progress_bar.progress(page / total, text=f"Страница {page}/{total}...")

    lines = extract_lines_from_pdf_bytes(pdf_bytes, progress_cb=on_progress)
    progress_bar.empty()

    existing = set(st.session_state.df["original"].tolist())
    new_rows = []
    for text, typ in lines:
        if text not in existing:
            new_rows.append({
                "original": text,
                "type": typ,
                "russian": "",
                "enabled": typ in _DEFAULT_ENABLED_TYPES,
            })
    if new_rows:
        st.session_state.df = pd.concat(
            [st.session_state.df, pd.DataFrame(new_rows)],
            ignore_index=True)
    st.success(f"Извлечено строк: {len(lines)}, добавлено новых: {len(new_rows)}")
    st.rerun()


def _do_auto_translate():
    if not _translator_available:
        st.error("Установите: `pip install deep-translator`")
        return

    df = st.session_state.df
    to_translate = [
        (i, str(row["original"]).strip())
        for i, row in df.iterrows()
        if row["enabled"] and not str(row["russian"]).strip()
        and "*" not in str(row["original"])
    ]

    if not to_translate:
        st.info("Нет строк для перевода (включены ✓, без перевода, без `*`)")
        return

    indices, texts = zip(*to_translate)
    progress_bar = st.progress(0, text=f"Переводим 0/{len(texts)}...")

    def on_progress(done, total):
        progress_bar.progress(done / total, text=f"Переводим {done}/{total}...")

    results = auto_translate_texts(list(texts), progress_cb=on_progress)
    progress_bar.empty()

    for idx, text in zip(indices, texts):
        ru = results.get(text, "").strip()
        if ru:
            st.session_state.df.at[idx, "russian"] = ru

    done = sum(1 for t in texts if results.get(t, "").strip())
    st.success(f"Авто-перевод завершён: переведено {done}/{len(texts)}")
    st.rerun()


def _translate_tab():
    st.subheader("Перевод PDF файлов")

    sorted_terms = _get_sorted_terms(st.session_state.df)
    if not sorted_terms:
        st.warning("Словарь пуст. Добавьте переводы на вкладке «Словарь переводов».")
        return

    st.caption(f"Активных записей в словаре: **{len(sorted_terms)}**")

    pdf_files = st.file_uploader(
        "Загрузите PDF для перевода",
        type="pdf",
        accept_multiple_files=True,
        key="translate_pdfs",
    )

    if st.button("▶ ПЕРЕВЕСТИ", type="primary", disabled=not pdf_files):
        st.session_state.translated = {}
        progress = st.progress(0)
        status   = st.empty()

        for i, pdf_file in enumerate(pdf_files):
            status.text(f"Перевожу {i+1}/{len(pdf_files)}: {pdf_file.name}...")
            pdf_bytes = pdf_file.read()

            page_bar = st.progress(0)

            def on_page(page, total, _bar=page_bar):
                _bar.progress(page / total)

            try:
                out_bytes = translate_pdf_bytes(pdf_bytes, sorted_terms,
                                                progress_cb=on_page)
                stem = Path(pdf_file.name).stem
                st.session_state.translated[stem + "_ru.pdf"] = out_bytes
            except Exception as e:
                st.error(f"Ошибка при переводе {pdf_file.name}: {e}")

            page_bar.empty()
            progress.progress((i + 1) / len(pdf_files))

        status.empty()
        st.success(f"Готово! Переведено файлов: {len(st.session_state.translated)}")

    # ---- Кнопки скачивания ----
    if st.session_state.get("translated"):
        st.divider()
        st.subheader("Скачать результаты")
        for filename, data in st.session_state.translated.items():
            st.download_button(
                label=f"⬇ {filename}",
                data=data,
                file_name=filename,
                mime="application/pdf",
                key=f"dl_{filename}",
            )


if __name__ == "__main__":
    main()
