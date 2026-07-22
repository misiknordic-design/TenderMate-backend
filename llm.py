"""Вызов LLM через Yandex Cloud AI Studio (OpenAI-совместимый API).
РФ-юрисдикция, оплата рублём. Модель задаётся переменной окружения YANDEX_MODEL_URI,
поэтому можно переключаться между YandexGPT / Qwen / DeepSeek без правки кода.
"""
import os
import json

import httpx

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")
# Пример значений: "yandexgpt/latest", "qwen3-235b/latest", "deepseek-v3/latest"
# Полный каталог моделей смотри в консоли Yandex Cloud AI Studio.
YANDEX_MODEL = os.environ.get("YANDEX_MODEL_URI", "yandexgpt/latest")

CHAT_URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"


async def complete_json(prompt: str, max_tokens: int = 4000) -> dict:
    """Отправляет prompt модели, ожидает JSON-ответ, возвращает распарсенный dict."""
    model_uri = f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_MODEL}"

    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            CHAT_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
            },
            json={
                "model": model_uri,
                # temperature=0 — для извлечения фактов важна стабильность между
                # прогонами на одних и тех же документах, а не творческое разнообразие.
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"Yandex AI Studio: {r.status_code} {r.text}")

    data = r.json()
    text = data["choices"][0]["message"]["content"]
    clean = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)
