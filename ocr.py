"""Извлечение текста из PDF через Yandex Vision OCR.
РФ-хостинг, оплата рублём — не зависит от блокировок иностранных сервисов.

Синхронный API Yandex Vision OCR принимает только 1 страницу за запрос,
поэтому многостраничный PDF разбивается на отдельные страницы (pypdf),
каждая распознаётся отдельным вызовом, результат склеивается.
"""
import os
import base64
import io

import httpx
from pypdf import PdfReader, PdfWriter

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")

OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"


def _split_pages(pdf_bytes: bytes) -> list[bytes]:
    """Разбивает PDF на список однострочных PDF (по одной странице каждый)."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        pages.append(buf.getvalue())
    return pages


def _parse_ocr_response(data: dict) -> str:
    blocks = data.get("result", {}).get("textAnnotation", {}).get("blocks", [])
    lines = []
    for block in blocks:
        for line in block.get("lines", []):
            words = [w.get("text", "") for w in line.get("words", [])]
            lines.append(" ".join(words))
    return "\n".join(lines)


async def _call_ocr(client: httpx.AsyncClient, content_bytes: bytes, mime_type: str) -> str:
    content_b64 = base64.b64encode(content_bytes).decode("ascii")
    r = await client.post(
        OCR_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {YANDEX_API_KEY}",
            "x-folder-id": YANDEX_FOLDER_ID,
        },
        json={
            "mimeType": mime_type,
            "languageCodes": ["ru", "en"],
            "model": "page",
            "content": content_b64,
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"Yandex Vision OCR: {r.status_code} {r.text}")
    return _parse_ocr_response(r.json())


async def extract_text(pdf_bytes: bytes) -> str:
    """Возвращает распознанный текст всех страниц PDF, склеенный в одну строку.
    Постранично: если одна страница не распозналась — остальные всё равно обрабатываются,
    чтобы битая/нестандартная страница не роняла разбор всего документа."""
    pages = _split_pages(pdf_bytes)

    async with httpx.AsyncClient(timeout=60) as client:
        page_texts = []
        for page_bytes in pages:
            try:
                text = await _call_ocr(client, page_bytes, "application/pdf")
            except Exception:  # noqa: BLE001 — одна страница не должна ронять весь документ
                text = ""
            page_texts.append(text)

    return "\n\n".join(page_texts)


async def recognize_image(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Распознаёт текст на отдельной картинке (не PDF-страница), используется для
    картинок, вложенных в DOCX/XLSX. Любая ошибка (сеть, таймаут, битое изображение)
    гасится здесь и возвращает пустую строку — вызывающий код не должен падать
    из-за одной нераспознаваемой картинки (например декоративного логотипа)."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            return await _call_ocr(client, image_bytes, mime_type)
    except Exception:  # noqa: BLE001
        return ""
