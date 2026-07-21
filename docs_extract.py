"""Извлечение текста из DOCX и XLSX.

.docx и .xlsx — это zip-архивы. Текст достаём напрямую (быстро, без OCR).
Если внутри есть вложенные картинки (закупщики иногда вставляют скан
как картинку в Word/Excel) — эти картинки отдельно прогоняем через
Yandex Vision OCR и добавляем распознанный текст к обычному.
"""
import zipfile
import io

from docx import Document
from openpyxl import load_workbook

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
