"""Splitwise — shared expense tracking.

Tables: friends, expenses, expense_participants. An expense has one payer
and a set of participants who share the cost equally. The payer may or may
not be a participant (e.g. "A paid $10 for B only" = payer A, participants [B]).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

APP_NAME = "splitwise"
SCHEMA = "app_splitwise"

router = APIRouter()
db_pool = None


async def setup(pool):
    global db_pool
    db_pool = pool


# --- Models ------------------------------------------------------------------


class FriendIn(BaseModel):
    name: str


class ExpenseIn(BaseModel):
    amount_cents: int
    description: str
    payer_id: str
    participant_ids: list[str]


# --- Helpers -----------------------------------------------------------------


def _friend(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "created_at": r["created_at"].isoformat(),
    }


def _expense(r) -> dict:
    return {
        "id": str(r["id"]),
        "amount_cents": r["amount_cents"],
        "description": r["description"],
        "payer_id": str(r["payer_id"]) if r["payer_id"] else None,
        "payer": r["payer"],
        "participant_ids": [str(p) for p in (r["participant_ids"] or [])],
        "created_at": r["created_at"].isoformat(),
    }


_EXPENSE_SELECT = f"""
    SELECT e.id, e.amount_cents, e.description, e.payer_id, e.created_at,
           f.name AS payer,
           COALESCE(
               ARRAY(
                   SELECT ep.friend_id
                   FROM "{SCHEMA}".expense_participants ep
                   WHERE ep.expense_id = e.id
               ),
               ARRAY[]::uuid[]
           ) AS participant_ids
    FROM "{SCHEMA}".expenses e
    LEFT JOIN "{SCHEMA}".friends f ON f.id = e.payer_id
"""


# --- Friends -----------------------------------------------------------------


@router.get("/api/friends")
async def list_friends():
    rows = await db_pool.fetch(
        f'SELECT id, name, created_at FROM "{SCHEMA}".friends ORDER BY name'
    )
    return [_friend(r) for r in rows]


@router.post("/api/friends")
async def add_friend(body: FriendIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    row = await db_pool.fetchrow(
        f'INSERT INTO "{SCHEMA}".friends (name) VALUES ($1) '
        f"RETURNING id, name, created_at",
        name,
    )
    return _friend(row)


@router.delete("/api/friends/{friend_id}")
async def delete_friend(friend_id: str):
    await db_pool.execute(
        f'DELETE FROM "{SCHEMA}".friends WHERE id = $1', friend_id
    )
    return {"ok": True}


# --- Expenses ----------------------------------------------------------------


@router.get("/api/expenses")
async def list_expenses():
    rows = await db_pool.fetch(
        _EXPENSE_SELECT + " ORDER BY e.created_at DESC"
    )
    return [_expense(r) for r in rows]


@router.post("/api/expenses")
async def add_expense(body: ExpenseIn):
    description = body.description.strip()
    if not description:
        raise HTTPException(400, "description is required")
    if body.amount_cents <= 0:
        raise HTTPException(400, "amount_cents must be positive")
    if not body.participant_ids:
        raise HTTPException(400, "at least one participant is required")
    # Dedupe while preserving order.
    seen: set[str] = set()
    participants = [p for p in body.participant_ids if not (p in seen or seen.add(p))]

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f'INSERT INTO "{SCHEMA}".expenses (amount_cents, description, payer_id) '
                f"VALUES ($1, $2, $3) RETURNING id",
                body.amount_cents,
                description,
                body.payer_id,
            )
            expense_id = row["id"]
            await conn.executemany(
                f'INSERT INTO "{SCHEMA}".expense_participants (expense_id, friend_id) '
                f"VALUES ($1, $2)",
                [(expense_id, pid) for pid in participants],
            )
            full = await conn.fetchrow(
                _EXPENSE_SELECT + " WHERE e.id = $1", expense_id
            )
    return _expense(full)


@router.delete("/api/expenses/{expense_id}")
async def delete_expense(expense_id: str):
    await db_pool.execute(
        f'DELETE FROM "{SCHEMA}".expenses WHERE id = $1', expense_id
    )
    return {"ok": True}


# --- Summary & debts ---------------------------------------------------------


@router.get("/api/summary")
async def summary():
    """Total paid per friend."""
    rows = await db_pool.fetch(
        f"""
        SELECT f.id, f.name, COALESCE(SUM(e.amount_cents), 0) AS total_paid
        FROM "{SCHEMA}".friends f
        LEFT JOIN "{SCHEMA}".expenses e ON e.payer_id = f.id
        GROUP BY f.id, f.name
        ORDER BY f.name
        """
    )
    return [
        {"id": str(r["id"]), "name": r["name"], "total_paid": r["total_paid"]}
        for r in rows
    ]


@router.get("/api/debts")
async def debts():
    """Net amounts owed between pairs.

    For each expense, split `amount_cents` equally among its participants.
    Every non-payer participant owes that slice to the payer. Then net out
    A→B against B→A so we only report the residual direction.
    """
    rows = await db_pool.fetch(
        f"""
        SELECT e.id, e.amount_cents, e.payer_id,
               ARRAY(
                   SELECT ep.friend_id
                   FROM "{SCHEMA}".expense_participants ep
                   WHERE ep.expense_id = e.id
               ) AS participants
        FROM "{SCHEMA}".expenses e
        WHERE e.payer_id IS NOT NULL
        """
    )

    # (debtor_id, creditor_id) → cents owed (gross, pre-netting).
    gross: dict[tuple[str, str], int] = {}
    for r in rows:
        participants = [str(p) for p in r["participants"]]
        if not participants:
            continue
        payer = str(r["payer_id"])
        # Integer-cent split. Remainder stays with the payer (each non-payer
        # pays floor(amount/n); the payer's effective cost absorbs the rest).
        share = r["amount_cents"] // len(participants)
        if share == 0:
            continue
        for pid in participants:
            if pid == payer:
                continue
            key = (pid, payer)
            gross[key] = gross.get(key, 0) + share

    # Net pairs.
    net = []
    handled: set[tuple[str, str]] = set()
    for (a, b), amt in gross.items():
        if (a, b) in handled:
            continue
        reverse = gross.get((b, a), 0)
        handled.add((a, b))
        handled.add((b, a))
        diff = amt - reverse
        if diff > 0:
            net.append({"debtor_id": a, "creditor_id": b, "amount_cents": diff})
        elif diff < 0:
            net.append({"debtor_id": b, "creditor_id": a, "amount_cents": -diff})

    name_rows = await db_pool.fetch(
        f'SELECT id, name FROM "{SCHEMA}".friends'
    )
    names = {str(r["id"]): r["name"] for r in name_rows}
    for edge in net:
        edge["debtor"] = names.get(edge["debtor_id"], "?")
        edge["creditor"] = names.get(edge["creditor_id"], "?")

    net.sort(key=lambda e: -e["amount_cents"])
    return net
