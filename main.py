"""TenderMate backend — FastAPI на российском VPS (Timeweb App Platform).

Полностью РФ-независимый пайплайн разбора тендера:
  1. Извлечение текста — PDF (OCR) / DOCX / XLSX / XLS, по одному документу
  2. Extraction (Yandex AI)   — текст документа → факты с источником
  3. Synthesis (Yandex AI)    — факты (кроме specification/customerContacts) → финальный анализ

specification и customerContacts собираются напрямую из фактов extraction, минуя synthesis:
экономит токены, исключает риск обрыва JSON, гарантирует структуру для кнопок "скопировать".
Специфицикация дедуплицируется по названию (один товар может встретиться в ТЗ и в
обосновании НМЦК — из документа без характеристик его не извлекаем вообще, но на
всякий случай ещё и подчищаем дубликаты здесь).

Поддерживаемые форматы: .pdf, .docx, .xlsx, .xls, .jpg, .png
НЕ поддерживается: .doc (Word 97-2003) — нет надёжного чистого Python-решения.

Эндпоинты:
  GET  /health              — проверка состояния
  POST /analyze              — multipart: файлы + профиль; возвращает {job_id}, разбор в фоне
  GET  /analyze?id={job_id} — статус и результат
  POST /lookup               — автозаполнение по ИНН (DaData)

Секреты (переменные окружения в Timeweb):
  YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_MODEL_URI (опц.),
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DADATA_TOKEN (опц.)
"""
import os
import json

import httpx
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ocr import extract_text
from docs_extract import extract_docx, extract_xlsx, extract_xls
from llm import complete_json
from prompt import build_extraction_prompt, build_synthesis_prompt
from db import create_job, update_job, get_job

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DADATA_TOKEN = os.environ.get("DADATA_TOKEN")


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _extract_by_extension(name: str, file_bytes: bytes) -> str:
    """Выбирает способ извлечения текста по расширению файла."""
    lower = name.lower()
    if lower.endswith(".docx"):
        return await extract_docx(file_bytes)
    if lower.endswith(".xlsx"):
        return await extract_xlsx(file_bytes)
    if lower.endswith(".xls"):
        return await extract_xls(file_bytes)
    if lower.endswith(".doc"):
        raise RuntimeError(
            f"Формат .doc (старый Word) не поддерживается: «{name}». "
            "Пересохраните файл в .docx (Word → Сохранить как → .docx) и загрузите заново."
        )
    return await extract_text(file_bytes)  # .pdf и изображения — по умолчанию


def _split_pipe(value: str, n: int) -> list[str]:
    parts = [p.strip() for p in (value or "").split("|")]
    parts += [""] * (n - len(parts))
    return parts[:n]


def _collect_specification(facts: list) -> list[dict]:
    """Собирает и дедуплицирует позиции спецификации по нормализованному названию.
    Если одно и то же наименование встретилось несколько раз (например упомянуто и в ТЗ,
    и в обосновании НМЦК) — оставляем запись с более длинной (информативной) сводкой."""
    by_name: dict[str, dict] = {}
    for f in facts:
        if f.get("category") != "specification":
            continue
        name, qty, unit, summary = _split_pipe(f.get("value", ""), 4)
        if not name:
            continue
        key = name.strip().lower()
        item = {"name": name, "qty": qty, "unit": unit, "summary": summary}
        existing = by_name.get(key)
        if not existing:
            by_name[key] = item
        else:
            # оставляем более полную версию: длиннее сводка — она информативнее
            if len(summary) > len(existing.get("summary", "")):
                existing["summary"] = summary
            if not existing.get("qty") and qty:
                existing["qty"] = qty
            if not existing.get("unit") and unit:
                existing["unit"] = unit
    return list(by_name.values())


def _collect_customer_contact(facts: list) -> dict:
    """Собирает контакты заказчика из первого информативного факта (обычно один на тендер)."""
    best = {"name": "", "phone": "", "email": ""}
    for f in facts:
        if f.get("category") != "customerContacts":
            continue
        name, phone, email = _split_pipe(f.get("value", ""), 3)
        filled = sum(bool(x) for x in (name, phone, email))
        best_filled = sum(bool(x) for x in best.values())
        if filled > best_filled:
            best = {"name": name, "phone": phone, "email": email}
    return best


# ─── Фоновый разбор тендера (2 этапа) ────────────────────────────────────────

async def process_job(job_id: str, files: list, profile: dict, today: str):
    try:
        all_facts = []

        # Этап 1: по каждому документу — извлечение текста + фактов
        for f in files:
            text = await _extract_by_extension(f["name"], f["bytes"])
            if not text.strip():
                continue
            extraction = await complete_json(build_extraction_prompt(f["name"], text))
            facts = extraction.get("facts", [])
            for fact in facts:
                fact["doc"] = f["name"]
            all_facts.extend(facts)

        if not all_facts:
            raise RuntimeError("Не удалось извлечь факты ни из одного документа")

        # specification и customerContacts не идут в synthesis — собираем напрямую
        specification   = _collect_specification(all_facts)
        customer_contact = _collect_customer_contact(all_facts)
        synthesis_facts = [
            f for f in all_facts
            if f.get("category") not in ("specification", "customerContacts")
        ]

        # Этап 2: синтез финального анализа из остальных фактов
        result = await complete_json(
            build_synthesis_prompt(profile, today, synthesis_facts), max_tokens=2500
        )
        result["specification"] = specification
        result["customerContact"] = customer_contact

        await update_job(job_id, {"status": "complete", "result": result})
    except Exception as e:  # noqa: BLE001
        await update_job(job_id, {"status": "error", "error": str(e)})


# ─── Роуты ───────────────────────────────────────────────────────────────────

@app.get("/analyze")
async def analyze_status(id: str | None = None):
    if not id:
        return JSONResponse({"error": "missing id"}, status_code=400)
    job = await get_job(id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return job


@app.post("/analyze")
async def analyze(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    profile: str = Form(...),
    today: str = Form(""),
):
    try:
        docs = []
        for f in files:
            raw = await f.read()
            docs.append({"name": f.filename, "bytes": raw})

        profile_dict = json.loads(profile)
        job_id = await create_job()
        background.add_task(process_job, job_id, docs, profile_dict, today)
        return {"job_id": job_id}
    except Exception as e:  # noqa: BLE001
        import traceback
        print("ANALYZE ENDPOINT ERROR:", traceback.format_exc(), flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/lookup")
async def lookup(payload: dict):
    if not DADATA_TOKEN:
        return JSONResponse({"error": "DADATA_TOKEN не задан"}, status_code=400)
    inn = payload.get("inn", "")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {DADATA_TOKEN}",
            },
            json={"query": inn},
        )
    suggestions = r.json().get("suggestions") or []
    if not suggestions:
        return JSONResponse({"error": "not found"}, status_code=404)
    d = suggestions[0]["data"]
    return JSONResponse({
        "name": (d.get("name") or {}).get("with_opf") or suggestions[0].get("value") or "",
        "kpp": d.get("kpp") or "",
        "ogrn": d.get("ogrn") or "",
        "addr": (d.get("address") or {}).get("value") or "",
        "dir": (d.get("management") or {}).get("name") or "",
    })
