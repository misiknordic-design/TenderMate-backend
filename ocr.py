"""Извлечение текста из PDF через Yandex Vision OCR.
РФ-хостинг, оплата рублём — не зависит от блокировок иностранных сервисов.
"""
import os
import base64

import httpx

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")

OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"


async def extract_text(pdf_bytes: bytes) -> str:
    """Возвращает распознанный текст всех страниц PDF, склеенный в одну строку."""
    content_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            OCR_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "x-folder-id": YANDEX_FOLDER_ID,
            },
            json={
                "mimeType": "application/pdf",
                "languageCodes": ["ru", "en"],
                "model": "page",
                "content": content_b64,
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"Yandex Vision OCR: {r.status_code} {r.text}")

    data = r.json()
    # Ответ содержит результат по страницам — склеиваем весь текст
    pages = data.get("result", {}).get("textAnnotation", {}).get("blocks", [])
    if not pages:
        # Альтернативная структура ответа (batch/paged) — пробуем достать текст целиком
        full_text = data.get("result", {}).get("textAnnotation", {}).get("fullText", "")
        return full_text

    lines = []
    for block in pages:
        for line in block.get("lines", []):
            words = [w.get("text", "") for w in line.get("words", [])]
            lines.append(" ".join(words))
    return "\n".join(lines)
