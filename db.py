"""Доступ к таблице tender_jobs в Supabase PostgreSQL через REST API."""
import os
import httpx

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


async def create_job() -> str:
    """Создаёт job со статусом processing, возвращает его id."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/tender_jobs",
            headers={**_HEADERS, "Prefer": "return=representation"},
            json={"status": "processing"},
        )
        r.raise_for_status()
        return r.json()[0]["id"]


async def update_job(job_id: str, data: dict) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/tender_jobs?id=eq.{job_id}",
            headers=_HEADERS,
            json=data,
        )


async def get_job(job_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/tender_jobs?id=eq.{job_id}&select=id,status,result,error",
            headers=_HEADERS,
        )
        rows = r.json()
        return rows[0] if rows else None
