"""TenderMate backend — FastAPI на российском VPS (Timeweb App Platform).

Полностью РФ-независимый пайплайн разбора тендера:

1. Извлечение текста — PDF (OCR) / DOCX / XLSX / XLS, по одному документу.
   Таблицы спецификации парсятся НАПРЯМУЮ по структуре (spec_table.py),
   минуя LLM — быстрее, дешевле и без риска отказа модели на формулировках
   вроде "раствор кислот и щелочей 40%" (обычный язык описания товара).
2. Extraction (Yandex AI) — текст документа (без сырых спецификаций) → факты
3. Synthesis (Yandex AI) — факты → финальный анализ

customerContacts, procurementNumber, platform тоже собираются напрямую из
фактов extraction, минуя synthesis — экономит токены, гарантирует структуру
для кнопок "скопировать" и не даёт модели придумать несуществующий номер.

Поддерживаемые форматы: .pdf, .docx, .xlsx, .xls, .jpg, .png
НЕ поддерживается: .doc (Word 97-2003) — нет надёжного чистого Python-решения.

Эндпоинты:
GET  /health — проверка состояния
POST /analyze — multipart: файлы + профиль; возвращает {job_id}, разбор в фоне
GET  /analyze?id={job_id} — статус и результат
POST /lookup — автозаполнение по ИНН (DaData)

Секреты (переменные окружения в Timeweb):
YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_MODEL_URI (опц.), DATABASE_URL, DADATA_TOKEN (опц.)
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

async def _extract_by_extension(name: str, file_bytes: bytes) -> tuple[str, list[dict]]:
    """Выбирает способ извлечения по расширению. Возвращает (текст для LLM, спецификация)."""
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
    text = await extract_text(file_bytes)  # .pdf и изображения — по умолчанию, спецификацию не парсим
    return text, []

def _split_pipe(value: str, n: int) -> list[str]:
    parts = [p.strip() for p in (value or "").split("|")]
    parts += [""] * (n - len(parts))
    return parts[:n]

def _collect_customer_contact(facts: list) -> dict:
    """Собирает контакты заказчика из самого информативного факта (обычно один на тендер)."""
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

def _collect_single_fact(facts: list, category: str) -> str:
    """Берёт самое длинное (обычно самое полное) значение факта данной категории
    среди всех документов. Как правило, категория встречается один раз, но длина
    как тай-брейкер защищает от случайной урезанной дублирующей записи."""
    values = [f.get("value", "").strip() for f in facts if f.get("category") == category]
    values = [v for v in values if v]
    return max(values, key=len) if values else ""

def _dedup_specification(items: list[dict]) -> list[dict]:
    """Один и тот же товар может встретиться в нескольких документах — такие дубли
    объединяем. НО товары с одинаковым названием и РАЗНЫМИ характеристиками
    (например «Перчатки нитриловые» размер L и размер S) — это разные позиции,
    их нельзя схлопывать. Поэтому ключ — название + характеристики вместе."""
    by_key: dict[tuple[str, str], dict] = {}
    for item in items:
        key = (item["name"].strip().lower(), item.get("summary", "").strip().lower())
        existing = by_key.get(key)
        if not existing:
            by_key[key] = item
        else:
            if not existing.get("qty") and item.get("qty"):
                existing["qty"] = item["qty"]
            if not existing.get("price") and item.get("price"):
                existing["price"] = item["price"]
    return list(by_key.values())

def _maybe_framework_risk(specification: list[dict]) -> dict | None:
    """Если ни у одной позиции спецификации нет количества, закупка вероятно
    рамочная (заказчик резервирует максимальную сумму, а объёмы поставки
    определяются отдельными заявками по ходу исполнения) — определяется
    структурно по уже собранным данным, без обращения к LLM."""
    if not specification:
        return None
    if any(item.get("qty") for item in specification):
        return None
    return {
        "type": "info",
        "t": "Вероятно рамочный договор",
        "d": "Ни у одной позиции спецификации не указано количество — обычно это "
             "означает, что заказчик резервирует максимальную сумму договора, а "
             "конкретные объёмы поставки определяются отдельными заявками по ходу "
             "исполнения, а не одной разовой поставкой.",
    }

# ─── Фоновый разбор тендера (2 этапа) ────────────────────────────────────────

async def process_job(job_id: str, files: list, profile: dict, today: str):
    try:
        all_facts = []
        specification: list[dict] = []

        # Этап 1: по каждому документу — извлечение текста (+ структурной спецификации) + фактов
        for f in files:
            try:
                text, spec_items = await _extract_by_extension(f["name"], f["bytes"])
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Не удалось прочитать документ «{f['name']}»: {e}") from e

            specification.extend(spec_items)

            if not text.strip():
                continue

            try:
                extraction = await complete_json(build_extraction_prompt(f["name"], text))
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Документ «{f['name']}»: {e}") from e

            facts = extraction.get("facts", [])
            for fact in facts:
                fact["doc"] = f["name"]
            all_facts.extend(facts)

        if not all_facts and not specification:
            raise RuntimeError("Не удалось извлечь факты ни из одного документа")

        # Эти категории не идут в synthesis — собираем напрямую, структура надёжнее
        customer_contact = _collect_customer_contact(all_facts)
        procurement_number = _collect_single_fact(all_facts, "procurementNumber")
        platform = _collect_single_fact(all_facts, "platform")
        direct_categories = {"customerContacts", "procurementNumber", "platform"}
        synthesis_facts = [f for f in all_facts if f.get("category") not in direct_categories]

        # Этап 2: синтез финального анализа из остальных фактов
        result = await complete_json(
            build_synthesis_prompt(profile, today, synthesis_facts), max_tokens=2500
        )
        result["specification"] = _dedup_specification(specification)
        result["customerContact"] = customer_contact
        result["procurementNumber"] = procurement_number
        result["platform"] = platform

        framework_risk = _maybe_framework_risk(result["specification"])
        if framework_risk:
            result.setdefault("risks", []).append(framework_risk)

        await update_job(job_id, {"status": "complete", "result": result})
    except Exception as e:  # noqa: BLE001
        error_text = str(e).strip() or f"{type(e).__name__} (без текста сообщения)"
        await update_job(job_id, {"status": "error", "error": error_text})

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
