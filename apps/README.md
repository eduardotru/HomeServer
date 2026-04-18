# Apps

This directory holds self-contained apps that run inside the chat container.
Each app gets its own postgres schema, its own UI, and its own API routes,
but shares the Python runtime to keep memory low.

## Layout

```
apps/<name>/
  manifest.json            describes the app to the UI + the LLM
  app.py                   FastAPI APIRouter + optional async setup()
  migrations/              *.sql, applied in sorted order, idempotent
    0001_initial.sql
  static/                  served at /apps/<name>/app/
    index.html
    app.js
    app.css
```

Only files matching `^[a-z][a-z0-9_-]*$` as directory names are discovered.
`_template` is skipped.

## Creating an app

```
make new-app NAME=splitwise
make restart-chat
```

`make new-app` copies `apps/_template/` and rewrites `_template` → `splitwise`
and the schema name `app__template` → `app_splitwise`. Nothing else to do —
next chat boot will scan, migrate, and mount it at `/apps/splitwise/`.

## Routing inside the chat container

| URL                               | What it serves                                  |
|-----------------------------------|-------------------------------------------------|
| `/apps`                           | Launcher (grid of installed apps)               |
| `/apps/<name>`                    | Wrapper page: iframe + chat sidebar             |
| `/apps/<name>/app/`               | Your app's `static/` directory                  |
| `/apps/<name>/api/...`            | Whatever your router defines under `/api/...`   |
| `/registry/apps`                  | JSON list of all installed apps + their status  |
| `/ui/kit.js`, `/ui/kit.css`, etc. | Shared UI kit — import from every app           |

## `app.py` contract

```python
from fastapi import APIRouter

APP_NAME = "splitwise"
SCHEMA = "app_splitwise"

router = APIRouter()
db_pool = None

async def setup(pool):
    """Called once at chat boot, after migrations. Stash the pool here."""
    global db_pool
    db_pool = pool

@router.get("/api/balances")
async def balances():
    rows = await db_pool.fetch(
        f'SELECT friend, SUM(amount) AS total FROM "{SCHEMA}".expenses GROUP BY friend'
    )
    return [dict(r) for r in rows]
```

Notes:
- Routes under `router` are mounted at `/apps/<name>/` — so define them as
  `@router.get("/api/thing")`, never `"/apps/<name>/api/thing"`.
- Always qualify tables with the schema: `"app_splitwise".expenses`. The
  shared `db_pool` is **not** scoped — you share it with chat and all other apps.
- A broken `app.py` is caught at import; chat still boots. The launcher
  shows the app as "failed to load" with the error.

## Migrations

Each `.sql` file in `migrations/` is applied once, in filename sort order.
Track them in the `_meta.app_migrations` table (handled for you).

- `search_path` is set to `"app_<name>", public` before each migration so
  `CREATE TABLE foo (...)` lands in your schema and `gen_random_uuid()` /
  `vector` from `public` still work.
- Each migration runs in a transaction. Keep them idempotent (`IF NOT EXISTS`)
  so partial failures don't corrupt state.
- Name them `0001_...`, `0002_...` etc. Never edit an applied migration —
  write a new one.

## Frontend

`static/index.html` is served with `html=True`, so `/apps/<name>/app/` loads
it. Keep asset references relative (`./app.js`) or absolute against the UI
kit (`/ui/kit.js`).

Every app should include:

```html
<link rel="stylesheet" href="/ui/tokens.css" />
<link rel="stylesheet" href="/ui/kit.css" />
<script type="module" src="/ui/kit.js"></script>
```

Use the `<hs-*>` components (`hs-card`, `hs-button`, `hs-form`, `hs-list`,
`hs-table`, `hs-upload`, `hs-input`, `hs-field`, `hs-empty`). They use light
DOM — you can style and query them like normal elements.

To let the AI sidebar follow what the user does, post events to the parent
frame:

```js
import { notifyHost } from "/ui/kit.js";
notifyHost("expense.added", { id, amount, friend });
```

## Manifest

```json
{
  "name": "splitwise",
  "title": "Splitwise",
  "description": "Track shared expenses with friends",
  "icon": "💰",
  "version": "0.1.0",
  "tables": {
    "expenses": "amount, payer, split_with, description, created_at",
    "friends":  "name, email"
  },
  "capabilities": [
    {"name": "add_expense",   "path": "POST /api/expenses",   "params": {"amount": "number", "payer": "string", "description": "string"}},
    {"name": "list_balances", "path": "GET /api/balances",    "params": {}}
  ]
}
```

`tables` and `capabilities` are how the LLM learns what this app can do.
Keep them short and factual — they feed into a future `call_app(name, ...)`
and `query_app(name, sql)` tool.

## What the chat container gives you

- `DATABASE_URL` — shared pool already connected when `setup()` runs
- `FILES_URL` — use for file storage, put your files under `apps/<name>/...`
- `LLM_SERVER_URL` — call the local LLM if you need AI inside the app itself
- `DB_ENCRYPTION_KEY` — use `armor(pgp_sym_encrypt(x, $KEY))` for sensitive columns

## When the LLM modifies your app

1. Edit any file under `apps/<name>/`.
2. Add a new migration under `migrations/` if the schema changes.
3. `make restart-chat` to reload everything (~5s).
4. Every change is a git commit under your control — easy rollback.
