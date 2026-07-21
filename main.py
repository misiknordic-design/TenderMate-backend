"""TenderMate backend — FastAPI на российском VPS (Timeweb App Platform).

Полностью РФ-независимый пайплайн разбора тендера:
  1. OCR (Yandex Vision)      — PDF → текст, по одному документу
  2. Extraction (Yandex AI)   — текст документа → факты с источником (по одному документу)
  3. Synthesis (Yandex AI)    — все факты → финальный структурированный анализ

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


# ─── Фоновый разбор тендера (2 этапа) ────────────────────────────────────────

async def process_job(job_id: str, files: list, profile: dict, today: str):
    try:
        all_facts = []

        # Этап 1: по каждому документу — OCR + извлечение фактов
        for f in files:
            text = await extract_text(f["bytes"])
            if not text.strip():
                continue
            extraction = await complete_json(build_extraction_prompt(f["name"], text))
            facts = extraction.get("facts", [])
            for fact in facts:
                fact["doc"] = f["name"]
            all_facts.extend(facts)

        if not all_facts:
            raise RuntimeError("Не удалось извлечь факты ни из одного документа")

        # Этап 2: синтез финального анализа из всех фактов
        result = await complete_json(
            build_synthesis_prompt(profile, today, all_facts), max_tokens=2500
        )

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
    # Читаем байты файлов сразу — UploadFile недоступен из фоновой задачи
    docs = []
    for f in files:
        raw = await f.read()
        docs.append({"name": f.filename, "bytes": raw})

    profile_dict = json.loads(profile)
    job_id = await create_job()
    background.add_task(process_job, job_id, docs, profile_dict, today)
    return {"job_id": job_id}


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
