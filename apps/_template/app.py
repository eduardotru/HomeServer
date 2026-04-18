"""Template app — a working CRUD example. Copy the patterns here.

Contract (see apps/README.md for full docs):
  - `router`: APIRouter mounted by chat at /apps/<name>/ — routes live under /api/...
  - `SCHEMA`: your postgres schema; always qualify tables with it.
  - `db_pool`: shared asyncpg pool; assigned by `setup()` at chat boot.

Use asyncpg directly (not SQLAlchemy — the chat container doesn't ship it).
Reference the column names from your migrations/*.sql files.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

APP_NAME = "_template"
SCHEMA = "app__template"

router = APIRouter()
db_pool = None  # assigned in setup()


async def setup(pool):
    global db_pool
    db_pool = pool


# --- Models ------------------------------------------------------------------


class ItemIn(BaseModel):
    name: str


# --- Helpers -----------------------------------------------------------------


def _row_to_item(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "created_at": r["created_at"].isoformat(),
    }


# --- Routes ------------------------------------------------------------------


@router.get("/api/items")
async def list_items():
    rows = await db_pool.fetch(
        f'SELECT id, name, created_at FROM "{SCHEMA}".items ORDER BY created_at DESC'
    )
    return [_row_to_item(r) for r in rows]


@router.post("/api/items")
async def add_item(body: ItemIn):
    if not body.name.strip():
        raise HTTPException(400, "name is required")
    row = await db_pool.fetchrow(
        f'INSERT INTO "{SCHEMA}".items (name) VALUES ($1) '
        f"RETURNING id, name, created_at",
        body.name.strip(),
    )
    return _row_to_item(row)


@router.delete("/api/items/{item_id}")
async def delete_item(item_id: str):
    await db_pool.execute(
        f'DELETE FROM "{SCHEMA}".items WHERE id = $1', item_id
    )
    return {"ok": True}
