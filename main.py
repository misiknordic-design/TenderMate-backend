"""TenderMate backend — FastAPI на российском VPS (Timeweb App Platform).

Эндпоинты:
  GET  /health              — проверка состояния (для платформы)
  POST /analyze             — создаёт job, возвращает {job_id}, разбор в фоне
  GET  /analyze?id={job_id} — статус и результат
  POST /analyze {action:"lookup"} — автозаполнение по ИНН (DaData)

Секреты (переменные окружения в Timeweb):
  ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DADATA_TOKEN (опц.)
"""
import os
import json

import httpx
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from prompt import build_prompt
from db import create_job, update_job, get_job

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DADATA_TOKEN = os.environ.get("DADATA_TOKEN")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── Фоновый разбор тендера ──────────────────────────────────────────────────

async def process_job(job_id: str, documents: list, profile: dict, today: str):
    try:
        content: list = []
        for doc in documents:
            mt = doc.get("media_type") or "application/pdf"
            if mt.startswith("image/"):
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": doc["data"]},
                })
            else:
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": doc["data"]},
                })
        content.append({"type": "text", "text": build_prompt(profile, today)})

        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 2500,
                    "messages": [{"role": "user", "content": content}],
                },
            )
        if r.status_code != 200:
            raise RuntimeError(f"Anthropic API: {r.status_code} {r.text}")

        data = r.json()
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)

        await update_job(job_id, {"status": "complete", "result": result})
    except Exception as e:  # noqa: BLE001
        await update_job(job_id, {"status": "error", "error": str(e)})


# ─── ИНН lookup (DaData) ─────────────────────────────────────────────────────

async def lookup_inn(inn: str) -> JSONResponse:
    if not DADATA_TOKEN:
        return JSONResponse({"error": "DADATA_TOKEN не задан"}, status_code=400)
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
async def analyze(request: Request, background: BackgroundTasks):
    body = await request.json()

    if body.get("action") == "lookup":
        return await lookup_inn(body.get("inn", ""))

    if not ANTHROPIC_API_KEY:
        return JSONResponse({"error": "ANTHROPIC_API_KEY не задан"}, status_code=500)

    job_id = await create_job()
    background.add_task(process_job, job_id, body["documents"], body["profile"], body.get("today", ""))
    return {"job_id": job_id}
