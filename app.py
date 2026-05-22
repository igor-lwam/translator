"""
app.py — PDF Translator
GUI-приложение для замены английских терминов на русские в PDF-файлах.
Использует словарь переводов из CSV-файла.

Зависимости: pip install pymupdf
"""

import csv
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

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

# fitz.Font для измерения ширины строки без рендеринга страницы
_FOBJ    = fitz.Font(fontfile=_FONT_PATH)
_FOBJ_B  = fitz.Font(fontfile=_FONT_BOLD_PATH)
_FOBJ_I  = fitz.Font(fontfile=_FONT_ITALIC_PATH)
_FOBJ_BI = fitz.Font(fontfile=_FONT_BI_PATH)

# (bold, italic) → (имя для page.insert_font, путь к файлу, fitz.Font)
_FONT_MAP = {
    (False, False): ("arial-cy",    _FONT_PATH,        _FOBJ),
    (True,  False): ("arial-cy-b",  _FONT_BOLD_PATH,   _FOBJ_B),
    (False, True):  ("arial-cy-i",  _FONT_ITALIC_PATH, _FOBJ_I),
    (True,  True):  ("arial-cy-bi", _FONT_BI_PATH,     _FOBJ_BI),
}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _block_dominant_span(block: dict) -> tuple:
    """(size, color_rgb, is_bold, is_italic) из самого длинного спана в блоке."""
    best_len = -1
    result = (10.0, (0.0, 0.0, 0.0), False, False)
    for line in block.get("lines", []):
        for sp in line.get("spans", []):
            ln = len(sp.get("text", ""))
            if ln > best_len:
                best_len = ln
                flags = sp.get("flags", 0)
                bold   = bool(flags & 16)
                italic = bool(flags & 2)
                raw = sp.get("color", 0)
                color = (0.0, 0.0, 0.0)
                if isinstance(raw, int):
                    color = (
                        ((raw >> 16) & 0xFF) / 255.0,
                        ((raw >> 8)  & 0xFF) / 255.0,
                        (raw & 0xFF) / 255.0,
                    )
                size = float(sp.get("size", 10.0))
                result = (size, color, bold, italic)
    return result


def _block_full_text(block: dict) -> str:
    """Полный текст блока, строки через \\n."""
    return "\n".join(
        "".join(sp.get("text", "") for sp in line.get("spans", []))
        for line in block.get("lines", [])
    )


def _block_align(block: dict) -> int:
    """Определяет выравнивание текста: 0=левое, 1=центр, 2=правое."""
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
        return 2  # правое
    if left_gap > bw * 0.25 and abs(left_gap - right_gap) < bw * 0.15:
        return 1  # центр
    return 0  # левое


def _apply_dict(text: str, sorted_terms: list) -> str:
    """Применяет все замены из словаря к строке (длинные фразы первыми)."""
    for en, ru in sorted_terms:
        if ru.strip():
            text = re.sub(r'(?i)\b' + re.escape(en) + r'\b', ru, text)
    return text


def _ensure_contrast(bg: tuple, fg: tuple) -> tuple:
    """Если фон тёмный, а текст тоже тёмный — возвращает белый."""
    bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    fg_lum = 0.299 * fg[0] + 0.587 * fg[1] + 0.114 * fg[2]
    if bg_lum < 0.4 and fg_lum < 0.4:
        return (1.0, 1.0, 1.0)
    return fg


def _bg_color(rect: fitz.Rect, drawings: list) -> tuple:
    """Возвращает цвет заливки наименьшего векторного прямоугольника, содержащего rect."""
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
        if len(c) == 4:                      # CMYK → RGB
            k = c[3]
            return ((1-c[0])*(1-k), (1-c[1])*(1-k), (1-c[2])*(1-k))
    return (1.0, 1.0, 1.0)


def _fit_size(text: str, fobj: fitz.Font, max_w: float, orig: float) -> float:
    """Двоичный поиск наибольшего размера, при котором text.width ≤ max_w."""
    lo, hi = max(orig * 0.45, 5.0), orig
    if fobj.text_length(text, fontsize=hi) <= max_w:
        return hi
    for _ in range(30):
        mid = (lo + hi) / 2
        if fobj.text_length(text, fontsize=mid) <= max_w:
            lo = mid
        else:
            hi = mid
    return lo


def _translate_page(page: fitz.Page, sorted_terms: list) -> None:
    blocks_data = page.get_text("dict")["blocks"]

    # Обрабатываем каждую СТРОКУ отдельно — не блок целиком.
    # PyMuPDF иногда группирует несколько колонок таблицы в один блок;
    # при попытке вставить их как multi-line в маленький rect шрифт сжимается.
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
            # Характеристики доминирующего спана строки
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

    # Определяем fill для каждой аннотации:
    # - Фон действительно тёмный (тёмный bg + светлый текст) → fill=тёмный цвет
    # - Всё остальное (светлый bg или ошибочная детекция) → fill=None,
    #   тогда оригинальный фоновый прямоугольник сохранится после apply_redactions
    fill_colors = []
    for (_, _, _, color, *_), bg in zip(replacements, raw_bgs):
        bg_lum = 0.299*bg[0] + 0.587*bg[1] + 0.114*bg[2]
        fg_lum = 0.299*color[0] + 0.587*color[1] + 0.114*color[2]
        # Настоящий тёмный фон: bg тёмный И текст светлый
        fill_colors.append(bg if (bg_lum < 0.4 and fg_lum >= 0.4) else None)

    for (rect, *_), fill in zip(replacements, fill_colors):
        page.add_redact_annot(rect, fill=fill)
    # graphics=0 (PDF_REDACT_LINE_ART_NONE) — не удалять векторные фоны
    page.apply_redactions(graphics=0)

    registered: set[str] = set()
    for _, _, _, _, bold, italic, _ in replacements:
        fname, ffile, _ = _FONT_MAP[(bold, italic)]
        if fname not in registered:
            page.insert_font(fontname=fname, fontfile=ffile)
            registered.add(fname)

    for (rect, text, orig_size, color, bold, italic, align), fill, bg in zip(replacements, fill_colors, raw_bgs):
        fname, _, fobj = _FONT_MAP[(bold, italic)]
        # Если fill=None — фон оригинальный светлый, используем span_color напрямую.
        # Если fill задан — фон тёмный, _ensure_contrast может переключить на белый.
        fg = _ensure_contrast(fill, color) if fill is not None else color
        print(f'[C] "{text[:30]}"  raw_bg={tuple(f"{v:.2f}" for v in bg)}  fill={tuple(f"{v:.2f}" for v in fill) if fill else "None"}  fg={tuple(f"{v:.2f}" for v in fg)}')
        size = _fit_size(text, fobj, rect.width, orig_size)
        tw   = fobj.text_length(text, fontsize=size)
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


def translate_pdf(pdf_path: str, mapping: dict[str, str], output_path: str,
                  progress_cb=None) -> None:
    """Заменяет английские термины на русские в PDF через PyMuPDF redact + insert."""
    sorted_terms = sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True)
    doc = fitz.open(pdf_path)
    n = len(doc)
    for idx in range(n):
        if progress_cb:
            progress_cb(idx + 1, n)
        _translate_page(doc[idx], sorted_terms)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


# ---------------------------------------------------------------------------
# Извлечение слов из PDF
# ---------------------------------------------------------------------------

def extract_words_from_pdf(pdf_path: str, progress_cb=None) -> list[str]:
    """Возвращает отсортированный список уникальных английских слов из PDF."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber не установлен: pip install pdfplumber")

    all_words: set[str] = set()
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            if progress_cb:
                progress_cb(i, total)
            text = page.extract_text()
            if not text:
                continue
            for word in re.findall(r"\b[A-Za-z][A-Za-z\-']{1,}\b", text):
                cleaned = word.strip("-'").lower()
                if len(cleaned) > 2:
                    all_words.add(cleaned)
    return sorted(all_words)


def auto_translate_words(words: list[str], progress_cb=None,
                         stop_flag: list[bool] | None = None) -> dict[str, str]:
    """
    Переводит список слов через Google Translate пакетами по 50 штук.
    Возвращает {english: russian}.
    stop_flag — список с одним bool, установите [True] для остановки.
    """
    if not _translator_available:
        raise RuntimeError("Установите: pip install deep-translator")

    translator = GoogleTranslator(source="en", target="ru")
    results: dict[str, str] = {}
    batch_size = 50

    for i in range(0, len(words), batch_size):
        if stop_flag and stop_flag[0]:
            break
        batch = words[i:i + batch_size]
        # Объединяем через разделитель, чтобы один запрос вместо 50
        joined = "\n".join(batch)
        try:
            translated = translator.translate(joined)
            parts = translated.split("\n")
            # Если разбивка совпадает — берём попарно, иначе переводим поодиночке
            if len(parts) == len(batch):
                for en, ru in zip(batch, parts):
                    results[en] = ru.strip()
            else:
                for word in batch:
                    try:
                        results[word] = translator.translate(word)
                    except Exception:
                        results[word] = ""
        except Exception:
            # При ошибке переводим поодиночке
            for word in batch:
                if stop_flag and stop_flag[0]:
                    break
                try:
                    results[word] = translator.translate(word)
                except Exception:
                    results[word] = ""

        if progress_cb:
            progress_cb(min(i + batch_size, len(words)), len(words))

    return results


# ---------------------------------------------------------------------------
# Диалог редактирования ячейки таблицы
# ---------------------------------------------------------------------------

class CellEditor(tk.Toplevel):
    def __init__(self, parent, title, value):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result = None

        tk.Label(self, text=title + ":", anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        self.entry = tk.Entry(self, width=40)
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
# Главное окно приложения
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Translator")
        self.minsize(800, 500)
        self._stop_flag: list[bool] = [False]
        self._ui_queue: queue.Queue = queue.Queue()
        self._build_ui()
        self._poll_queue()

    # ---- Очередь GUI-обновлений из фоновых потоков ----

    def _poll_queue(self):
        """Читает сообщения от фоновых потоков и обновляет GUI в главном потоке."""
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
                elif action == "words_loaded":
                    words = msg["words"]
                    existing = {
                        str(self.tree.item(iid)["values"][0]).lower()
                        for iid in self.tree.get_children()
                    }
                    added = sum(
                        1 for w in words
                        if w not in existing
                        and not self.tree.insert("", "end", values=(w, "")) or True
                    )
                    self.status_var.set(
                        f"Извлечено слов: {len(words)}, добавлено новых: {added}. "
                        f"Заполните колонку 'Русский' и сохраните CSV."
                    )
                elif action == "translations_done":
                    for iid, ru in msg["updates"]:
                        vals = list(self.tree.item(iid)["values"])
                        vals[1] = ru
                        self.tree.item(iid, values=vals)
                    self._q(action="reset_auto_btn")
                    stopped = msg.get("stopped", False)
                    done = msg["done"]
                    total = msg["total"]
                    self.status_var.set(
                        f"{'Остановлено' if stopped else 'Авто-перевод завершён'}: "
                        f"переведено {done}/{total} слов."
                    )
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _q(self, **kwargs):
        """Удобный метод для отправки сообщений в очередь из любого потока."""
        self._ui_queue.put(kwargs)

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
        paned.add(left, weight=1)

        # Toolbar
        tb = tk.Frame(left)
        tb.pack(fill="x", pady=(0, 4))
        ttk.Button(tb, text="Извлечь из PDF...", command=self._extract_from_pdf).pack(side="left", padx=2)
        ttk.Button(tb, text="Загрузить CSV", command=self._load_csv).pack(side="left", padx=2)
        ttk.Button(tb, text="Сохранить CSV", command=self._save_csv).pack(side="left", padx=2)
        self._auto_btn = ttk.Button(tb, text="Авто-перевод (Google)", command=self._auto_translate)
        self._auto_btn.pack(side="left", padx=2)
        ttk.Button(tb, text="+ Строка", command=self._add_row).pack(side="right", padx=2)
        ttk.Button(tb, text="− Удалить", command=self._del_row).pack(side="right", padx=2)

        # Таблица
        cols = ("english", "russian")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("english", text="English")
        self.tree.heading("russian", text="Русский")
        self.tree.column("english", width=180, minwidth=100)
        self.tree.column("russian", width=180, minwidth=100)

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.bind("<Double-1>", self._on_double_click)

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

        # Нижняя часть правой панели: папка вывода + кнопка
        bottom = tk.Frame(right)
        bottom.pack(fill="x", side="bottom", pady=4)

        tk.Label(bottom, text="Выходная папка:").grid(row=0, column=0, sticky="w", pady=2)
        self.out_dir = tk.StringVar(value=str(Path.home() / "Downloads"))
        tk.Entry(bottom, textvariable=self.out_dir, width=28).grid(row=0, column=1, padx=4)
        ttk.Button(bottom, text="Выбрать...", command=self._choose_out_dir).grid(row=0, column=2)

        ttk.Button(bottom, text="  ПЕРЕВЕСТИ  ", command=self._run_translation,
                   style="Accent.TButton").grid(row=1, column=0, columnspan=3, pady=6, sticky="ew")

        # Прогресс и статус
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=2)

        self.status_var = tk.StringVar(value="Готов")
        tk.Label(bottom, textvariable=self.status_var, anchor="w").grid(
            row=3, column=0, columnspan=3, sticky="w")

        bottom.columnconfigure(1, weight=1)

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

        self.tree.delete(*self.tree.get_children())
        header_skipped = False
        for row in rows:
            if not header_skipped and row and row[0].lower() in ("english", "en"):
                header_skipped = True
                continue
            if len(row) >= 2:
                self.tree.insert("", "end", values=(row[0], row[1]))
            elif len(row) == 1 and row[0]:
                self.tree.insert("", "end", values=(row[0], ""))

        self.status_var.set(f"Загружен словарь: {Path(path).name} ({len(self.tree.get_children())} строк)")

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
                writer.writerow(["english", "russian"])
                for iid in self.tree.get_children():
                    writer.writerow(self.tree.item(iid)["values"])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить CSV:\n{e}")
            return
        self.status_var.set(f"Словарь сохранён: {Path(path).name}")

    def _extract_from_pdf(self):
        path = filedialog.askopenfilename(
            title="Выберите PDF для извлечения слов",
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

            words = extract_words_from_pdf(pdf_path, progress_cb=on_progress)
        except Exception as e:
            self._q(action="error", title="Ошибка", text=f"Не удалось прочитать PDF:\n{e}")
            self._q(action="status", text="Ошибка извлечения слов")
            return

        # Все GUI-операции — через очередь в главный поток
        self._q(action="words_loaded", words=words)

    def _auto_translate(self):
        if not _translator_available:
            messagebox.showerror("Ошибка", "Установите библиотеку:\n\nuv add deep-translator")
            return

        # Собираем только строки без перевода
        to_translate = []
        iids = []
        for iid in self.tree.get_children():
            en, ru = self.tree.item(iid)["values"]
            if not str(ru).strip():
                to_translate.append(str(en).strip())
                iids.append(iid)

        if not to_translate:
            messagebox.showinfo("Авто-перевод", "Все строки уже переведены.")
            return

        if not messagebox.askyesno(
            "Авто-перевод",
            f"Перевести {len(to_translate)} слов через Google Translate?\n\n"
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

    def _auto_translate_worker(self, words: list[str], iids: list[str]):
        total = len(words)
        self._q(action="progress", value=0, maximum=total)

        def on_progress(done, total_words):
            self._q(action="progress", value=done)
            self._q(action="status", text=f"Авто-перевод: {done}/{total_words} слов...")

        try:
            results = auto_translate_words(words, progress_cb=on_progress, stop_flag=self._stop_flag)
        except Exception as e:
            self._q(action="error", title="Ошибка", text=f"Ошибка при переводе:\n{e}")
            self._q(action="reset_auto_btn")
            return

        updates = [(iid, results.get(w, "")) for iid, w in zip(iids, words) if results.get(w, "")]
        done_count = len(updates)
        self._q(action="translations_done", updates=updates,
                done=done_count, total=total, stopped=self._stop_flag[0])

    def _reset_auto_btn(self):
        self._stop_flag[0] = False
        self._auto_btn.config(text="Авто-перевод (Google)", command=self._auto_translate)
        self.progress["value"] = 0

    def _add_row(self):
        dlg = CellEditor(self, "English", "")
        if dlg.result is None:
            return
        en = dlg.result.strip()
        if not en:
            return
        dlg2 = CellEditor(self, "Русский перевод", "")
        ru = dlg2.result.strip() if dlg2.result is not None else ""
        iid = self.tree.insert("", "end", values=(en, ru))
        self.tree.see(iid)
        self.tree.selection_set(iid)

    def _del_row(self):
        selected = self.tree.selection()
        if not selected:
            return
        for iid in selected:
            self.tree.delete(iid)

    def _on_double_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not iid:
            return

        col_idx = int(col.lstrip("#")) - 1
        col_name = ["English", "Русский перевод"][col_idx]
        current = self.tree.item(iid)["values"][col_idx]

        dlg = CellEditor(self, col_name, str(current))
        if dlg.result is not None:
            values = list(self.tree.item(iid)["values"])
            values[col_idx] = dlg.result
            self.tree.item(iid, values=values)

    def _get_mapping(self) -> dict[str, str]:
        mapping = {}
        for iid in self.tree.get_children():
            en, ru = self.tree.item(iid)["values"]
            en, ru = str(en).strip(), str(ru).strip()
            if en and ru:
                mapping[en] = ru
        return mapping

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
        mapping = self._get_mapping()
        if not mapping:
            messagebox.showwarning("Словарь пуст", "Загрузите словарь переводов и заполните колонку 'Русский'.")
            return

        pdf_files = list(self.pdf_list.get(0, tk.END))
        if not pdf_files:
            messagebox.showwarning("Нет файлов", "Добавьте PDF файлы для перевода.")
            return

        out_dir = Path(self.out_dir.get())
        if not out_dir.exists():
            messagebox.showerror("Ошибка", f"Папка не существует:\n{out_dir}")
            return

        # Запускаем в отдельном потоке чтобы GUI не зависал
        threading.Thread(
            target=self._translate_worker,
            args=(pdf_files, mapping, out_dir),
            daemon=True
        ).start()

    def _translate_worker(self, pdf_files, mapping, out_dir):
        total_files = len(pdf_files)
        self._q(action="progress", value=0, maximum=total_files)

        errors = []
        for i, pdf_path in enumerate(pdf_files, 1):
            src = Path(pdf_path)
            out_path = out_dir / (src.stem + "_ru.pdf")
            self._q(action="status", text=f"Перевод {i}/{total_files}: {src.name}...")

            try:
                translate_pdf(str(pdf_path), mapping, str(out_path))
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
            return  # нормальное завершение
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
