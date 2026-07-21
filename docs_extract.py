"""Извлечение текста из DOCX, XLSX и старого XLS.

.docx и .xlsx — это zip-архивы. Текст достаём напрямую (быстро, без OCR).
.xls (старый бинарный формат Excel 97-2003) — через xlrd 1.2.0
  (более новые версии xlrd поддержку .xls убрали).

Если внутри docx/xlsx есть вложенные картинки (закупщики иногда вставляют
скан как картинку) — эти картинки отдельно прогоняем через Yandex Vision OCR.

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

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tiff")


def _extract_embedded_images(file_bytes: bytes, media_prefix: str) -> list[bytes]:
    """Достаёт байты вложенных картинок из zip-структуры docx/xlsx."""
    images = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        for name in z.namelist():
            if name.startswith(media_prefix) and name.lower().endswith(IMAGE_EXTS):
                images.append(z.read(name))
    return images


async def extract_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))

    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append(" | ".join(cells))

    text = "\n".join(parts)

    images = _extract_embedded_images(file_bytes, "word/media/")
    for img_bytes in images:
        ocr_text = await recognize_image(img_bytes)
        if ocr_text.strip():
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
    for img_bytes in images:
        ocr_text = await recognize_image(img_bytes)
        if ocr_text.strip():
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
