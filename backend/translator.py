"""PDF translation logic — no UI dependencies."""

import io
import re
import sys
from pathlib import Path

try:
    from deep_translator import GoogleTranslator
    _translator_available = True
except ImportError:
    _translator_available = False

import fitz

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_LINUX_FONT_MAP = {
    "arial.ttf":   ["LiberationSans-Regular.ttf",   "DejaVuSans.ttf"],
    "arialbd.ttf": ["LiberationSans-Bold.ttf",       "DejaVuSans-Bold.ttf"],
    "ariali.ttf":  ["LiberationSans-Italic.ttf",     "DejaVuSans-Oblique.ttf"],
    "arialbi.ttf": ["LiberationSans-BoldItalic.ttf", "DejaVuSans-BoldOblique.ttf"],
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
    else:
        candidates = _LINUX_FONT_MAP.get(win, []) + [win]
        for d in _LINUX_FONT_DIRS:
            for name in candidates:
                p = d / name
                if p.exists():
                    return str(p)
    return None


_FONT_PATH        = _find_font("arial.ttf",   ["Arial.ttf"])
_FONT_BOLD_PATH   = _find_font("arialbd.ttf", ["Arial Bold.ttf", "ArialBd.ttf"])
_FONT_ITALIC_PATH = _find_font("ariali.ttf",  ["Arial Italic.ttf", "ArialI.ttf"])
_FONT_BI_PATH     = _find_font("arialbi.ttf", ["Arial Bold Italic.ttf", "ArialBI.ttf"])


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

DEFAULT_ENABLED_TYPES = {"термин", "предложение"}

# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

def detect_text_type(text: str) -> str:
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


def _apply_dict(text: str, sorted_terms: list) -> tuple[str, float | None]:
    """Returns (translated_text, font_size_override_or_None)."""
    stripped = text.strip()
    for en, ru, size_override in sorted_terms:
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
                return result, size_override
        elif stripped.lower() == en.strip().lower():
            return ru.strip(), size_override
    return text, None


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
            return ((1 - c[0]) * (1 - k), (1 - c[1]) * (1 - k), (1 - c[2]) * (1 - k))
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
            translated, size_override = _apply_dict(line_text, sorted_terms)
            if translated.strip() == line_text.strip() or not translated.strip():
                continue
            rect = fitz.Rect(line["bbox"])
            best_len, size, color, bold, italic = -1, 10.0, (0.0, 0.0, 0.0), False, False
            for sp in spans:
                ln = len(sp.get("text", ""))
                if ln > best_len:
                    best_len = ln
                    size   = float(sp.get("size", 10.0))
                    flags  = sp.get("flags", 0)
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
            final_size = float(size_override) if size_override is not None else size
            replacements.append((rect, translated.strip(), final_size, color, bool(bold), bool(italic), align))

    if not replacements:
        return

    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []

    raw_bgs = [_bg_color(rect, drawings) for rect, *_ in replacements]

    fill_colors = []
    for (_, _, _, color, *_), bg in zip(replacements, raw_bgs):
        bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        fg_lum = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
                                  progress_cb=None) -> list[tuple[str, str, float]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    seen: dict[str, tuple[str, float]] = {}
    total = len(doc)
    for i, page in enumerate(doc):
        if progress_cb:
            progress_cb(i + 1, total)
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(sp.get("text", "") for sp in spans).strip()
                if text and text not in seen:
                    best_len, size = -1, 10.0
                    for sp in spans:
                        ln = len(sp.get("text", ""))
                        if ln > best_len:
                            best_len = ln
                            size = float(sp.get("size", 10.0))
                    seen[text] = (detect_text_type(text), size)
    doc.close()
    return [(text, typ, size) for text, (typ, size) in seen.items()]


def auto_translate_texts(texts: list[str], progress_cb=None) -> dict[str, str]:
    if not _translator_available:
        raise RuntimeError("deep-translator not installed")
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
