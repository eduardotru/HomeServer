# HomeServer

A local-first platform for running a small LLM on your Mac and building tiny web apps around it. Each app gets its own URL, its own SQL schema, and a chat sidebar scoped to its own API — so you can talk to the app, and the model can poke at the app, without ever leaving the box.

Built for an Apple Silicon Mac, runs natively where it matters (MLX + Metal) and containerised where it doesn't.

---

## Architecture

```
HomeServer/
├── llm/          — MLX inference server (native, Metal GPU)
├── chat/         — Chat UI, agent loop, search, routines (container)
├── ui/           — Shared web components used by chat + apps
├── apps/         — Your small apps (see apps/README.md)
├── code/         — Sandbox for the agent's shell / file tools (container)
├── files/        — Plain file-store service (container)
├── searxng/      — Self-hosted web search (container)
├── postgres/     — Shared DB (container)
├── Makefile      — Build / start / stop everything
└── .env          — All ports, URLs, model choices
```

| Service | Port | Notes |
|---|---|---|
| LLM (MLX) | 8000 | Native. Switch between local and remote with `LLM_MODE`. |
| Embed | 8010 | Native. Sentence embeddings for pgvector semantic recall. |
| Chat | 8001 | Main UI + agent loop. Mounts all apps under `/apps/<name>`. |
| Code | 8002 | Shell + file tools the agent uses. |
| Search | 8003 | SearXNG wrapper the chat & agent can hit. |
| SearXNG | 8080 | Metasearch engine. |
| Files | 9000 | Plain REST file store under `data/files/`. |
| Postgres | 5432 | `pgvector` + `pgcrypto`. One DB, one schema per app. |

The chat container owns `/`. Every folder in `apps/` gets auto-mounted at `/apps/<name>` on startup.

---

## Setup

```bash
cp .env.example .env      # fill in an API key if you want remote LLM mode
make setup                # one-time venv + model download bootstrap
make start                # builds containers, starts everything, tails logs
```

Open <http://localhost:8001>. First run pulls the MLX model (~3–5 GB depending on pick).

Daily:

```bash
make start / stop / restart / status / logs
make restart-chat         # rebuild chat only — fastest dev loop
make restart-llm          # reload the model
make dev                  # uvicorn --reload on the LLM server
```

Config lives in `.env` — ports, `LLM_MODEL`, `LLM_MODE=local|remote`, remote provider (anthropic / openai-compatible) with base URL + API key.

---

## Apps

An app is any folder in `apps/`. It ships its own:

- `manifest.json` — name, icon, capabilities
- `app.py` — a FastAPI `APIRouter` + a `setup(db_pool)` hook
- `migrations/NNNN_*.sql` — run once, tracked per-app
- `static/` — the UI, served at `/apps/<name>/`

Chat picks them up on startup, applies new migrations, and each app gets:

- Its own URL (`/apps/splitwise`)
- Its own Postgres schema (`app_splitwise.*`) with a shared connection pool
- A chat sidebar wired to the app's own API — the model has one tool, `app_call`, with GET/POST/DELETE scoped to `/apps/<name>/api/*`

Scaffold one in ten seconds:

```bash
make new-app NAME=todos
make restart-chat
```

Then edit `apps/todos/app.py` and go. A working reference lives in `apps/splitwise/` (friends, expenses, net-debt calculation, settle-up). More detail in [`apps/README.md`](apps/README.md).

---

## Agent mode

Toggle agent mode in the chat input. The model gets tools for web search, file/code reading, file writes, shell commands, memory, and sub-agent delegation. Destructive tool calls (writes, `run_command`) require explicit confirmation in the UI the first time — subsequent calls within the conversation are remembered as approved.

Tool access is gated by prompt keywords (e.g. mentioning "routine" unlocks the routine tools) so the model isn't drowning in irrelevant schemas on every turn. Full picture: [`chat/chat.py`](chat/chat.py) — search `select_tools_for`.

The agent can read most of the repo and write only inside specific allow-listed paths (apps/, chat/, etc.) in the code container. `.env`, `data/`, `.git/` are off-limits.

---

## Requirements

- Apple Silicon Mac (M1 or later), macOS 26 (Tahoe) or later
- Python 3.11+
- [Apple `container` CLI](https://developer.apple.com/documentation/virtualization)

---

## Troubleshooting

- **Port 5000 / AirPlay** — macOS owns 5000. That's why `CHAT_APP_PORT=8001` by default. Fine to change.
- **Model slow** — confirm the LLM server is running natively (not in a container) and Metal is active. Expect ~30 tok/s on M-series.
- **Chat won't start** — it needs Postgres up first. `make db` then `make chat`, or just `make start`.
- **Wipe everything** — `make db-reset` for Postgres, `rm -rf data/files` for the file store. No encryption, no backup dance.
