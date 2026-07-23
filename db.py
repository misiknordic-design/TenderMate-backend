"""Доступ к таблице tender_jobs в Timeweb Managed PostgreSQL через asyncpg."""
import os
import json
import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Регистрирует кодек для jsonb: Python dict/list <-> JSON-строка автоматически."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, init=_init_connection
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def create_job() -> str:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO tender_jobs (status) VALUES ('processing') RETURNING id"
    )
    return str(row["id"])


async def update_job(job_id: str, data: dict) -> None:
    if not data:
        return
    pool = await get_pool()
    set_parts = []
    values = []
    for i, (key, value) in enumerate(data.items(), start=1):
        set_parts.append(f"{key} = ${i}")
        values.append(value)
    values.append(job_id)
    query = (
        f"UPDATE tender_jobs SET {', '.join(set_parts)}, updated_at = now() "
        f"WHERE id = ${len(values)}"
    )
    await pool.execute(query, *values)


async def get_job(job_id: str) -> dict | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, status, result, error, stage FROM tender_jobs WHERE id = $1",
        job_id,
    )
    return dict(row) if row else None
