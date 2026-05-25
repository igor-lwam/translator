"""
app.py — PDF Translator
GUI-приложение для замены английских терминов на русские в PDF-файлах.
"""

import csv
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

try:
    from deep_translator import GoogleTranslator
    _translator_available = True
except ImportError:
    _translator_available = False

try:
    import fitz  # pymupdf
except ImportError:
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    tk.messagebox.showerror(
        "Ошибка",
        "Библиотека PyMuPDF не установлена.\n\nВыполните:\n  pip install pymupdf"
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Шрифты
# ---------------------------------------------------------------------------

_FONT_PATH        = "C:/Windows/Fonts/arial.ttf"
_FONT_BOLD_PATH   = "C:/Windows/Fonts/arialbd.ttf"
_FONT_ITALIC_PATH = "C:/Windows/Fonts/ariali.ttf"
_FONT_BI_PATH     = "C:/Windows/Fonts/arialbi.ttf"

_FOBJ    = fitz.Font(fontfile=_FONT_PATH)
_FOBJ_B  = fitz.Font(fontfile=_FONT_BOLD_PATH)
_FOBJ_I  = fitz.Font(fontfile=_FONT_ITALIC_PATH)
_FOBJ_BI = fitz.Font(fontfile=_FONT_BI_PATH)

_FONT_MAP = {
    (False, False): ("arial-cy",    _FONT_PATH,        _FOBJ),
    (True,  False): ("arial-cy-b",  _FONT_BOLD_PATH,   _FOBJ_B),
    (False, True):  ("arial-cy-i",  _FONT_ITALIC_PATH, _FOBJ_I),
    (True,  True):  ("arial-cy-bi", _FONT_BI_PATH,     _FOBJ_BI),
}


# ---------------------------------------------------------------------------
# Вспомогательные функции PDF
# ---------------------------------------------------------------------------

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
            # Строим паттерн: каждый * → группа захвата, литералы вокруг экранируются.
            # Промежуточные * → (.*?) нежадные, последний * → (.*) жадный.
            # Префикс и суффикс вне * тоже включаются в паттерн.
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
    # Частичная замена слов (без шаблонов)
    result = text
    for en, ru in sorted_terms:
        if ru.strip() and '*' not in en:
            result = re.sub(r'(?i)\b' + re.escape(en) + r'\b', ru, result)
    return result


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
    replacements = []  # (rect, text, size, color, bold, italic, align)

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
        if fname not in registered:
            page.insert_font(fontname=fname, fontfile=ffile)
            registered.add(fname)

    for (rect, text, size, color, bold, italic, align), fill, bg in zip(
            replacements, fill_colors, raw_bgs):
        fname, _, fobj = _FONT_MAP[(bold, italic)]
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
                         fontname=fname, fontsize=size, color=fg)


# ---------------------------------------------------------------------------
# Логика перевода PDF
# ---------------------------------------------------------------------------

def translate_pdf(pdf_path: str, sorted_terms: list, output_path: str,
                  progress_cb=None) -> None:
    doc = fitz.open(pdf_path)
    n = len(doc)
    for idx in range(n):
        if progress_cb:
            progress_cb(idx + 1, n)
        _translate_page(doc[idx], sorted_terms)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


# ---------------------------------------------------------------------------
# Извлечение строк из PDF
# ---------------------------------------------------------------------------

def extract_lines_from_pdf(pdf_path: str, progress_cb=None) -> list[tuple[str, str]]:
    doc = fitz.open(pdf_path)
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


# ---------------------------------------------------------------------------
# Авто-перевод через Google
# ---------------------------------------------------------------------------

def auto_translate_texts(texts: list[str], progress_cb=None,
                         stop_flag: list[bool] | None = None) -> dict[str, str]:
    if not _translator_available:
        raise RuntimeError("Установите: pip install deep-translator")

    translator = GoogleTranslator(source="en", target="ru")
    results: dict[str, str] = {}
    batch_size = 30

    for i in range(0, len(texts), batch_size):
        if stop_flag and stop_flag[0]:
            break
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
                    if stop_flag and stop_flag[0]:
                        break
                    try:
                        results[text] = translator.translate(text)
                    except Exception:
                        results[text] = ""
        except Exception:
            for text in batch:
                if stop_flag and stop_flag[0]:
                    break
                try:
                    results[text] = translator.translate(text)
                except Exception:
                    results[text] = ""

        if progress_cb:
            progress_cb(min(i + batch_size, len(texts)), len(texts))

    return results


# ---------------------------------------------------------------------------
# Диалог редактирования ячейки
# ---------------------------------------------------------------------------

class CellEditor(tk.Toplevel):
    def __init__(self, parent, title, value):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result = None

        tk.Label(self, text=title + ":", anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        self.entry = tk.Entry(self, width=50)
        self.entry.insert(0, value)
        self.entry.pack(padx=10, pady=2)
        self.entry.select_range(0, tk.END)
        self.entry.focus_set()

        btn_frame = tk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        tk.Button(btn_frame, text="OK", command=self._ok, width=10).pack(side="left")
        tk.Button(btn_frame, text="Отмена", command=self.destroy, width=10).pack(side="left", padx=5)

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def _ok(self):
        self.result = self.entry.get()
        self.destroy()


# ---------------------------------------------------------------------------
# Константы таблицы
# ---------------------------------------------------------------------------

_ENABLED  = "✓"
_DISABLED = "—"
_DEFAULT_ENABLED_TYPES = {"термин", "предложение"}

_COL_NAMES = {
    "original": "Оригинал",
    "type":     "Тип",
    "russian":  "Перевод",
    "enabled":  "✓",
}
_COL_IDX = {"original": 0, "type": 1, "russian": 2, "enabled": 3}


# ---------------------------------------------------------------------------
# Главное окно приложения
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Translator")
        self.minsize(960, 560)

        self._stop_flag: list[bool] = [False]
        self._ui_queue: queue.Queue = queue.Queue()

        # Хранилище всех строк: iid -> [orig, type, ru, enabled]
        self._master: dict[str, list] = {}
        self._next_id = 0

        # Сортировка
        self._sort_col: str | None = None
        self._sort_asc: bool = True

        self._build_ui()
        self._poll_queue()

    # ---- Очередь GUI-обновлений ----

    def _poll_queue(self):
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                action = msg.get("action")
                if action == "status":
                    self.status_var.set(msg["text"])
                elif action == "progress":
                    self.progress["value"] = msg["value"]
                    if "maximum" in msg:
                        self.progress["maximum"] = msg["maximum"]
                elif action == "error":
                    messagebox.showerror(msg.get("title", "Ошибка"), msg["text"])
                elif action == "info":
                    messagebox.showinfo(msg.get("title", "Готово"), msg["text"])
                elif action == "reset_auto_btn":
                    self._reset_auto_btn()
                elif action == "lines_loaded":
                    self._on_lines_loaded(msg["lines"])
                elif action == "translations_done":
                    for iid, ru in msg["updates"]:
                        if iid in self._master:
                            self._master[iid][2] = ru
                            if self.tree.exists(iid):
                                self.tree.item(iid, values=self._master[iid])
                    self._q(action="reset_auto_btn")
                    stopped = msg.get("stopped", False)
                    self.status_var.set(
                        f"{'Остановлено' if stopped else 'Авто-перевод завершён'}: "
                        f"переведено {msg['done']}/{msg['total']}."
                    )
                    self._refresh_counter()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _q(self, **kwargs):
        self._ui_queue.put(kwargs)

    def _on_lines_loaded(self, lines: list[tuple[str, str]]):
        existing_texts = {row[0] for row in self._master.values()}
        added = 0
        for text, typ in lines:
            if text not in existing_texts:
                enabled = _ENABLED if typ in _DEFAULT_ENABLED_TYPES else _DISABLED
                self._insert_master(text, typ, "", enabled)
                added += 1
        self._apply_filter()
        self._refresh_counter()
        self.status_var.set(
            f"Извлечено строк: {len(lines)}, добавлено новых: {added}. "
            f"Проверьте переводы, используйте фильтры и 'Авто-перевод'."
        )

    # ---- Управление мастер-данными ----

    def _insert_master(self, orig: str, typ: str, ru: str, enabled: str) -> str:
        """Добавляет строку в _master и дерево (detached). Возвращает iid."""
        iid = str(self._next_id)
        self._next_id += 1
        self._master[iid] = [orig, typ, ru, enabled]
        self.tree.insert("", "end", iid=iid, values=(orig, typ, ru, enabled))
        # Сразу detach — _apply_filter решит, показывать ли
        self.tree.detach(iid)
        return iid

    def _apply_filter(self, *_):
        """Применяет текущие фильтры и сортировку к дереву."""
        text_q  = self._filter_text.get().lower()
        type_q  = self._filter_type.get()
        ru_q    = self._filter_ru.get()
        en_q    = self._filter_en.get()

        # Detach всё видимое
        for iid in list(self.tree.get_children()):
            self.tree.detach(iid)

        # Фильтруем из master
        filtered: list[tuple[str, list]] = []
        for iid, row in self._master.items():
            orig, typ, ru, enabled = row
            if text_q and text_q not in orig.lower() and text_q not in ru.lower():
                continue
            if type_q != "Все типы" and typ != type_q:
                continue
            if ru_q == "Без перевода" and ru.strip():
                continue
            if ru_q == "С переводом" and not ru.strip():
                continue
            if en_q == "Только ✓" and enabled != _ENABLED:
                continue
            if en_q == "Только —" and enabled != _DISABLED:
                continue
            filtered.append((iid, row))

        # Сортируем
        if self._sort_col:
            ci = _COL_IDX[self._sort_col]
            filtered.sort(key=lambda x: x[1][ci].lower(), reverse=not self._sort_asc)

        # Reattach в нужном порядке
        for iid, _ in filtered:
            self.tree.reattach(iid, "", "end")

        self._refresh_counter()

    def _refresh_counter(self):
        total   = len(self._master)
        visible = len(self.tree.get_children())
        enabled = sum(1 for r in self._master.values() if r[3] == _ENABLED)
        with_ru = sum(1 for r in self._master.values() if r[2].strip())
        self._counter_var.set(
            f"Показано: {visible} / {total}  |  Включено: {enabled}  |  С переводом: {with_ru}"
        )

    # ---- Сортировка по заголовку ----

    def _sort_by_col(self, col: str):
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._update_sort_headers()
        self._apply_filter()

    def _update_sort_headers(self):
        for col, name in _COL_NAMES.items():
            if col == self._sort_col:
                arrow = " ▲" if self._sort_asc else " ▼"
                self.tree.heading(col, text=name + arrow)
            else:
                self.tree.heading(col, text=name)

    # ---- Построение интерфейса ----

    def _build_ui(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Загрузить словарь (CSV)...", command=self._load_csv)
        file_menu.add_command(label="Сохранить словарь (CSV)...", command=self._save_csv)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self.destroy)
        menubar.add_cascade(label="Файл", menu=file_menu)
        self.config(menu=menubar)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        # --- Левая панель: словарь ---
        left = ttk.LabelFrame(paned, text="Словарь переводов", padding=4)
        paned.add(left, weight=2)

        # Toolbar действий
        tb = tk.Frame(left)
        tb.pack(fill="x", pady=(0, 4))
        ttk.Button(tb, text="Извлечь из PDF...", command=self._extract_from_pdf).pack(side="left", padx=2)
        ttk.Button(tb, text="Загрузить CSV", command=self._load_csv).pack(side="left", padx=2)
        ttk.Button(tb, text="Сохранить CSV", command=self._save_csv).pack(side="left", padx=2)
        self._auto_btn = ttk.Button(tb, text="Авто-перевод (Google)", command=self._auto_translate)
        self._auto_btn.pack(side="left", padx=2)
        ttk.Button(tb, text="+ Строка", command=self._add_row).pack(side="right", padx=2)
        ttk.Button(tb, text="− Удалить", command=self._del_row).pack(side="right", padx=2)

        # Панель фильтров
        self._build_filter_bar(left)

        # Таблица
        self._build_tree(left)

        # Счётчик строк
        self._counter_var = tk.StringVar(value="")
        tk.Label(left, textvariable=self._counter_var, anchor="w",
                 foreground="#555").pack(fill="x", pady=(2, 0))

        # Подсказка про шаблон *
        tk.Label(left,
                 text="Совет: используйте * для переменных частей — пример: \"Report date: *\" → \"Дата отчёта: *\"",
                 anchor="w", foreground="#888", font=("", 8)).pack(fill="x")

        # --- Правая панель: PDF файлы ---
        right = ttk.LabelFrame(paned, text="PDF Файлы", padding=4)
        paned.add(right, weight=1)

        tb2 = tk.Frame(right)
        tb2.pack(fill="x", pady=(0, 4))
        ttk.Button(tb2, text="Добавить файлы", command=self._add_pdfs).pack(side="left", padx=2)
        ttk.Button(tb2, text="Удалить выбранные", command=self._remove_pdfs).pack(side="left", padx=2)

        self.pdf_list = tk.Listbox(right, selectmode="extended", activestyle="dotbox")
        vsb2 = ttk.Scrollbar(right, orient="vertical", command=self.pdf_list.yview)
        self.pdf_list.configure(yscrollcommand=vsb2.set)
        self.pdf_list.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="left", fill="y")

        bottom = tk.Frame(right)
        bottom.pack(fill="x", side="bottom", pady=4)

        tk.Label(bottom, text="Выходная папка:").grid(row=0, column=0, sticky="w", pady=2)
        self.out_dir = tk.StringVar(value=str(Path.home() / "Downloads"))
        tk.Entry(bottom, textvariable=self.out_dir, width=28).grid(row=0, column=1, padx=4)
        ttk.Button(bottom, text="Выбрать...", command=self._choose_out_dir).grid(row=0, column=2)

        ttk.Button(bottom, text="  ПЕРЕВЕСТИ  ", command=self._run_translation,
                   style="Accent.TButton").grid(row=1, column=0, columnspan=3, pady=6, sticky="ew")

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=2)

        self.status_var = tk.StringVar(value="Готов")
        tk.Label(bottom, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, columnspan=3, sticky="w")

        bottom.columnconfigure(1, weight=1)

    def _build_filter_bar(self, parent):
        fb = ttk.LabelFrame(parent, text="Фильтр", padding=(4, 2))
        fb.pack(fill="x", pady=(0, 4))

        # Поиск
        tk.Label(fb, text="Поиск:").grid(row=0, column=0, sticky="w", padx=(0, 2))
        self._filter_text = tk.StringVar()
        search_entry = ttk.Entry(fb, textvariable=self._filter_text, width=22)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        # Кнопка очистки поиска
        ttk.Button(fb, text="✕", width=2,
                   command=lambda: self._filter_text.set("")).grid(row=0, column=2, padx=(0, 8))

        # Тип
        tk.Label(fb, text="Тип:").grid(row=0, column=3, sticky="w", padx=(0, 2))
        self._filter_type = tk.StringVar(value="Все типы")
        type_cb = ttk.Combobox(fb, textvariable=self._filter_type, width=12,
                               state="readonly",
                               values=["Все типы", "термин", "предложение", "число", "код", "не латиница"])
        type_cb.grid(row=0, column=4, padx=(0, 8))

        # Перевод
        tk.Label(fb, text="Перевод:").grid(row=0, column=5, sticky="w", padx=(0, 2))
        self._filter_ru = tk.StringVar(value="Все")
        ru_cb = ttk.Combobox(fb, textvariable=self._filter_ru, width=12,
                             state="readonly",
                             values=["Все", "Без перевода", "С переводом"])
        ru_cb.grid(row=0, column=6, padx=(0, 8))

        # Включено
        tk.Label(fb, text="Вкл.:").grid(row=0, column=7, sticky="w", padx=(0, 2))
        self._filter_en = tk.StringVar(value="Все")
        en_cb = ttk.Combobox(fb, textvariable=self._filter_en, width=9,
                             state="readonly",
                             values=["Все", "Только ✓", "Только —"])
        en_cb.grid(row=0, column=8, padx=(0, 4))

        # Сброс фильтров
        ttk.Button(fb, text="Сбросить", command=self._reset_filters).grid(row=0, column=9, padx=(4, 0))

        fb.columnconfigure(1, weight=1)

        # Трейсы для авто-обновления
        for var in (self._filter_text, self._filter_type, self._filter_ru, self._filter_en):
            var.trace_add("write", self._apply_filter)

    def _build_tree(self, parent):
        frame = tk.Frame(parent)
        frame.pack(fill="both", expand=True)

        cols = ("original", "type", "russian", "enabled")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")

        for col, name in _COL_NAMES.items():
            self.tree.heading(col, text=name,
                              command=lambda c=col: self._sort_by_col(c))

        self.tree.column("original", width=230, minwidth=120)
        self.tree.column("type",     width=90,  minwidth=70,  anchor="center")
        self.tree.column("russian",  width=230, minwidth=120)
        self.tree.column("enabled",  width=36,  minwidth=36,  anchor="center", stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_single_click)

    def _reset_filters(self):
        self._filter_text.set("")
        self._filter_type.set("Все типы")
        self._filter_ru.set("Все")
        self._filter_en.set("Все")

    # ---- Словарь ----

    def _load_csv(self):
        path = filedialog.askopenfilename(
            title="Загрузить словарь",
            filetypes=[("CSV файлы", "*.csv"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить CSV:\n{e}")
            return

        # Очищаем дерево и мастер
        self.tree.delete(*self.tree.get_children())
        self._master.clear()
        self._next_id = 0

        header_skipped = False
        for row in rows:
            if not header_skipped and row and row[0].lower() in ("original", "english", "en", "оригинал"):
                header_skipped = True
                continue
            if not row or not row[0].strip():
                continue
            orig = row[0]
            if len(row) == 2:
                # Старый формат: original, russian
                typ  = _detect_text_type(orig)
                ru   = row[1]
            else:
                typ  = row[1] if len(row) > 1 else _detect_text_type(orig)
                ru   = row[2] if len(row) > 2 else ""
            enabled_default = _ENABLED if typ in _DEFAULT_ENABLED_TYPES else _DISABLED
            enabled = row[3] if len(row) > 3 else enabled_default
            self._insert_master(orig, typ, ru, enabled)

        self._apply_filter()
        self.status_var.set(
            f"Загружен словарь: {Path(path).name} ({len(self._master)} строк)"
        )

    def _save_csv(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить словарь",
            defaultextension=".csv",
            filetypes=[("CSV файлы", "*.csv")]
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["original", "type", "russian", "enabled"])
                for row in self._master.values():
                    writer.writerow(row)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить CSV:\n{e}")
            return
        self.status_var.set(f"Словарь сохранён: {Path(path).name}")

    def _extract_from_pdf(self):
        path = filedialog.askopenfilename(
            title="Выберите PDF для извлечения текста",
            filetypes=[("PDF файлы", "*.pdf"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        self.status_var.set(f"Читаю PDF: {Path(path).name}...")
        self.update_idletasks()
        threading.Thread(target=self._extract_worker, args=(path,), daemon=True).start()

    def _extract_worker(self, pdf_path: str):
        try:
            def on_progress(page, total):
                self._q(action="status", text=f"Читаю страницу {page}/{total}...")
            lines = extract_lines_from_pdf(pdf_path, progress_cb=on_progress)
        except Exception as e:
            self._q(action="error", title="Ошибка", text=f"Не удалось прочитать PDF:\n{e}")
            self._q(action="status", text="Ошибка извлечения")
            return
        self._q(action="lines_loaded", lines=lines)

    def _auto_translate(self):
        if not _translator_available:
            messagebox.showerror("Ошибка", "Установите библиотеку:\n\nuv add deep-translator")
            return

        to_translate = []
        iids = []
        for iid, row in self._master.items():
            orig, typ, ru, enabled = row
            # Шаблоны с * нельзя авто-переводить — пользователь должен указать перевод вручную
            if str(enabled) == _ENABLED and not str(ru).strip() and '*' not in str(orig):
                to_translate.append(str(orig).strip())
                iids.append(iid)

        if not to_translate:
            messagebox.showinfo("Авто-перевод", "Нет строк для перевода (включены ✓ и без перевода).")
            return

        if not messagebox.askyesno(
            "Авто-перевод",
            f"Перевести {len(to_translate)} строк через Google Translate?\n\n"
            "Перевод финансовых терминов может быть неточным — проверьте результат."
        ):
            return

        self._stop_flag[0] = False
        self._auto_btn.config(text="Остановить", command=self._stop_auto_translate)

        threading.Thread(
            target=self._auto_translate_worker,
            args=(to_translate, iids),
            daemon=True
        ).start()

    def _stop_auto_translate(self):
        self._stop_flag[0] = True
        self._q(action="status", text="Остановка авто-перевода...")

    def _auto_translate_worker(self, texts: list[str], iids: list[str]):
        total = len(texts)
        self._q(action="progress", value=0, maximum=total)

        def on_progress(done, total_texts):
            self._q(action="progress", value=done)
            self._q(action="status", text=f"Авто-перевод: {done}/{total_texts}...")

        try:
            results = auto_translate_texts(texts, progress_cb=on_progress, stop_flag=self._stop_flag)
        except Exception as e:
            self._q(action="error", title="Ошибка", text=f"Ошибка при переводе:\n{e}")
            self._q(action="reset_auto_btn")
            return

        updates = [(iid, results[t]) for iid, t in zip(iids, texts) if results.get(t, "").strip()]
        self._q(action="translations_done", updates=updates,
                done=len(updates), total=total, stopped=self._stop_flag[0])

    def _reset_auto_btn(self):
        self._stop_flag[0] = False
        self._auto_btn.config(text="Авто-перевод (Google)", command=self._auto_translate)
        self.progress["value"] = 0

    def _add_row(self):
        dlg = CellEditor(self, "Оригинал", "")
        if dlg.result is None:
            return
        orig = dlg.result.strip()
        if not orig:
            return
        dlg2 = CellEditor(self, "Перевод", "")
        ru = dlg2.result.strip() if dlg2.result is not None else ""
        typ = _detect_text_type(orig)
        enabled = _ENABLED if typ in _DEFAULT_ENABLED_TYPES else _DISABLED
        iid = self._insert_master(orig, typ, ru, enabled)
        self._apply_filter()
        if self.tree.exists(iid):
            self.tree.see(iid)
            self.tree.selection_set(iid)

    def _del_row(self):
        selected = self.tree.selection()
        if not selected:
            return
        for iid in selected:
            self.tree.delete(iid)
            self._master.pop(iid, None)
        self._refresh_counter()

    def _on_single_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#4":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self._master:
            return
        row = self._master[iid]
        row[3] = _DISABLED if row[3] == _ENABLED else _ENABLED
        self.tree.item(iid, values=row)
        self._refresh_counter()

    def _on_double_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not iid or iid not in self._master:
            return

        col_idx = int(col.lstrip("#")) - 1
        if col_idx not in (0, 2):
            return

        col_name = "Оригинал" if col_idx == 0 else "Перевод"
        current = self._master[iid][col_idx]

        dlg = CellEditor(self, col_name, str(current))
        if dlg.result is not None:
            self._master[iid][col_idx] = dlg.result
            if col_idx == 0:
                self._master[iid][1] = _detect_text_type(dlg.result)
            self.tree.item(iid, values=self._master[iid])
            self._refresh_counter()

    def _get_sorted_terms(self) -> list[tuple[str, str]]:
        terms = []
        for row in self._master.values():
            orig, typ, ru, enabled = row
            orig, ru = str(orig).strip(), str(ru).strip()
            if orig and ru and str(enabled) == _ENABLED:
                terms.append((orig, ru))
        return sorted(terms, key=lambda x: len(x[0]), reverse=True)

    # ---- PDF файлы ----

    def _add_pdfs(self):
        paths = filedialog.askopenfilenames(
            title="Добавить PDF файлы",
            filetypes=[("PDF файлы", "*.pdf"), ("Все файлы", "*.*")]
        )
        existing = list(self.pdf_list.get(0, tk.END))
        for p in paths:
            if p not in existing:
                self.pdf_list.insert(tk.END, p)

    def _remove_pdfs(self):
        for idx in reversed(self.pdf_list.curselection()):
            self.pdf_list.delete(idx)

    def _choose_out_dir(self):
        d = filedialog.askdirectory(title="Выберите папку для сохранения")
        if d:
            self.out_dir.set(d)

    # ---- Перевод ----

    def _run_translation(self):
        sorted_terms = self._get_sorted_terms()
        if not sorted_terms:
            messagebox.showwarning(
                "Словарь пуст",
                "Нет включённых строк с переводом.\n\n"
                "Убедитесь, что в таблице есть строки с заполненным переводом и включённым ✓."
            )
            return

        pdf_files = list(self.pdf_list.get(0, tk.END))
        if not pdf_files:
            messagebox.showwarning("Нет файлов", "Добавьте PDF файлы для перевода.")
            return

        out_dir = Path(self.out_dir.get())
        if not out_dir.exists():
            messagebox.showerror("Ошибка", f"Папка не существует:\n{out_dir}")
            return

        threading.Thread(
            target=self._translate_worker,
            args=(pdf_files, sorted_terms, out_dir),
            daemon=True
        ).start()

    def _translate_worker(self, pdf_files, sorted_terms, out_dir):
        total_files = len(pdf_files)
        self._q(action="progress", value=0, maximum=total_files)

        errors = []
        for i, pdf_path in enumerate(pdf_files, 1):
            src = Path(pdf_path)
            out_path = out_dir / (src.stem + "_ru.pdf")
            self._q(action="status", text=f"Перевод {i}/{total_files}: {src.name}...")

            try:
                translate_pdf(str(pdf_path), sorted_terms, str(out_path))
            except Exception as e:
                errors.append(f"{src.name}: {e}")

            self._q(action="progress", value=i)

        if errors:
            self._q(action="status", text=f"Готово с ошибками ({len(errors)} файлов)")
            self._q(action="error", title="Ошибки", text="Ошибки при переводе:\n" + "\n".join(errors))
        else:
            self._q(action="status", text=f"Готово! Переведено файлов: {total_files}. Папка: {out_dir}")
            self._q(action="info", title="Готово",
                    text=f"Переведено файлов: {total_files}\n\nСохранено в:\n{out_dir}")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import traceback

    log_path = Path(__file__).parent / "error.log"

    def handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        try:
            messagebox.showerror("Критическая ошибка", msg)
        except Exception:
            pass

    sys.excepthook = handle_exception

    try:
        app = App()
        app.mainloop()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        handle_exception(*sys.exc_info())
