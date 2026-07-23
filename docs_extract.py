"""Извлечение текста из DOCX, XLSX и старого XLS.

.docx и .xlsx — это zip-архивы. Текст достаём напрямую (быстро, без OCR).
.xls (старый бинарный формат Excel 97-2003) — через xlrd 1.2.0
  (более новые версии xlrd поддержку .xls убрали).

Если внутри docx/xlsx есть вложенные картинки (закупщики иногда вставляют
скан как картинку) — эти картинки отдельно прогоняем через Yandex Vision OCR.
Декоративные элементы (логотипы, печати, фирменные бланки) обычно очень
маленькие по размеру файла и не содержат читаемого текста — пропускаем их,
чтобы не тратить OCR-вызовы и не рисковать зависанием на "битом" изображении.

Одна упавшая картинка НЕ должна ронять весь разбор — каждый вызов OCR
обёрнут в защиту, при сбое просто пропускаем эту картинку.

ВАЖНО: старый .doc (Word 97-2003) НЕ поддерживается — устойчивого
чистого Python-решения для этого формата нет, только конвертация через
LibreOffice (системная зависимость, требует отдельной инфраструктуры).
"""
import zipfile
import io

from docx import Document
from openpyxl import load_workbook
import xlrd

from ocr import recognize_image

IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".bmp": "image/bmp", ".tiff": "image/tiff"}
MIN_IMAGE_BYTES = 3000  # декоративные логотипы/иконки обычно меньше — реального текста в них нет


def _extract_embedded_images(file_bytes: bytes, media_prefix: str) -> list[tuple[bytes, str]]:
    """Достаёт байты вложенных картинок из zip-структуры docx/xlsx вместе с их MIME-типом.
    Пропускает слишком маленькие файлы — это почти всегда декоративные элементы, не текст."""
    images = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        for name in z.namelist():
            lower = name.lower()
            if not name.startswith(media_prefix):
                continue
            ext = next((e for e in IMAGE_EXTS if lower.endswith(e)), None)
            if not ext:
                continue
            info = z.getinfo(name)
            if info.file_size < MIN_IMAGE_BYTES:
                continue  # вероятно декоративный элемент — пропускаем
            images.append((z.read(name), IMAGE_EXTS[ext]))
    return images


async def _ocr_images_text(images: list[tuple[bytes, str]]) -> str:
    """Прогоняет список картинок через OCR. Сбой одной картинки не прерывает остальные."""
    parts = []
    for img_bytes, mime_type in images:
        try:
            text = await recognize_image(img_bytes, mime_type)
        except Exception:  # noqa: BLE001 — одна плохая картинка не должна ронять весь разбор
            text = ""
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


async def extract_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))

    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append(" | ".join(cells))

    text = "\n".join(parts)

    images = _extract_embedded_images(file_bytes, "word/media/")
    ocr_text = await _ocr_images_text(images)
    if ocr_text:
        text += "\n\n[Текст с вложенного изображения]\n" + ocr_text

    return text


async def extract_xlsx(file_bytes: bytes) -> str:
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)

    parts = []
    for sheet in wb.worksheets:
        parts.append(f"--- Лист: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                parts.append(" | ".join(cells))

    text = "\n".join(parts)

    images = _extract_embedded_images(file_bytes, "xl/media/")
    ocr_text = await _ocr_images_text(images)
    if ocr_text:
        text += "\n\n[Текст с вложенного изображения]\n" + ocr_text

    return text


async def extract_xls(file_bytes: bytes) -> str:
    """Старый бинарный формат Excel 97-2003. Без OCR картинок — xlrd их не видит,
    но это редкость в .xls-файлах на площадках закупок (обычно чистые таблицы)."""
    wb = xlrd.open_workbook(file_contents=file_bytes)

    parts = []
    for sheet in wb.sheets():
        parts.append(f"--- Лист: {sheet.name} ---")
        for row_idx in range(sheet.nrows):
            cells = [str(c.value) if c.value != "" else "" for c in sheet.row(row_idx)]
            if any(cells):
                parts.append(" | ".join(cells))

    return "\n".join(parts)
