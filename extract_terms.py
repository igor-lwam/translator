"""
extract_terms.py
----------------
Извлекает все уникальные английские слова и фразы из PDF-файла
и сохраняет их в CSV с пустой колонкой для перевода.

Использование:
    python extract_terms.py document.pdf

Результат:
    document_mapping.csv — два столбца: english, russian
"""

import sys
import re
import csv
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("Установите библиотеку: pip install pdfplumber")
    sys.exit(1)


def extract_words_from_pdf(pdf_path: str) -> list[str]:
    """Извлекает все уникальные слова из PDF, сортирует по алфавиту."""
    all_words = set()

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"Страниц в PDF: {total_pages}")

        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            if not text:
                continue

            # Извлекаем слова (латиница, включая дефис внутри слова)
            words = re.findall(r"\b[A-Za-z][A-Za-z\-']{1,}\b", text)

            for word in words:
                cleaned = word.strip("-'").lower()
                if len(cleaned) > 2:
                    all_words.add(cleaned)

            print(f"  Страница {i}/{total_pages} — найдено слов: {len(all_words)}", end="\r")

    print(f"\nВсего уникальных слов: {len(all_words)}")
    return sorted(all_words)


def save_to_csv(words: list[str], output_path: str):
    """Сохраняет слова в CSV с пустой колонкой для перевода."""
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["english", "russian"])
        for word in words:
            writer.writerow([word, ""])

    print(f"CSV сохранён: {output_path}")
    print(f"Заполните колонку 'russian' и используйте его в приложении.")


def main():
    if len(sys.argv) < 2:
        print("Использование: python extract_terms.py <путь_к_pdf>")
        print("Пример:        python extract_terms.py contract.pdf")
        sys.exit(1)

    pdf_path = sys.argv[1]

    if not Path(pdf_path).exists():
        print(f"Файл не найден: {pdf_path}")
        sys.exit(1)

    output_csv = Path(pdf_path).stem + "_mapping.csv"

    print(f"Читаю PDF: {pdf_path}")
    words = extract_words_from_pdf(pdf_path)

    if not words:
        print("Слова не найдены. Возможно, PDF содержит только изображения.")
        sys.exit(1)

    save_to_csv(words, output_csv)


if __name__ == "__main__":
    main()
