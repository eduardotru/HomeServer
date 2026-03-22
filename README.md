# HomeServer — Local AI Platform

A self-hosted AI platform that runs entirely on your Mac. No cloud, no API keys, no data leaving your machine. Built on Apple Silicon with MLX for fast local inference.

---

## Architecture

```
HomeServer/
├── llm/          — Inference server (native, Metal GPU)
├── chat/         — Chat app + UI (container)
├── code/         — Code execution sandbox (container)
├── postgres/     — Database schema
├── .env          — All ports and config
├── Makefile      — Platform management
└── data/
    └── backups/  — Automatic DB snapshots
```

### Services

| Service | Type | Port | Description |
|---|---|---|---|
| LLM server | Native (macOS) | 8000 | MLX inference, Metal GPU, request queue |
| Chat app | Container | 5000 | FastAPI + UI, conversation history |
| Code container | Container | 6000 | Sandboxed code execution for agent mode |
| Postgres | Container | 5432 | Conversation storage |

The LLM server runs natively to get direct Metal GPU access. Everything else runs in Apple containers.

---

## Requirements

- Apple Silicon Mac (M1/M2/M3/M4)
- macOS 26 (Tahoe) or later
- Python 3.11+
- [Apple container CLI](https://developer.apple.com/documentation/virtualization) (`brew install --cask container`)
- Homebrew

---

## Setup

### 1. Clone and configure

```bash
git clone <your-repo> HomeServer
cd HomeServer
cp .env.example .env   # edit ports/model if needed
```

### 2. Install dependencies

```bash
make setup
```

This creates the LLM server venv and installs all Python dependencies. The chat app and code container dependencies are handled automatically during `make start` via Docker.

### 3. Start everything

```bash
make start
```

On first run, the LLM model (~4GB) is downloaded from HuggingFace automatically.

Open **http://localhost:5000** in your browser.

---

## Daily Usage

```bash
make setup          # install dependencies (run once after cloning)
make start          # start all services + tail logs
make stop           # backup DB and stop everything
make restart        # stop then start
make status         # show what's running
make logs           # re-attach to logs
```

### Targeted restarts (faster iteration)

```bash
make restart-chat   # rebuild + restart chat app only (~10s)
make restart-llm    # restart LLM server only (reloads model, ~30s)
make restart-code   # rebuild + restart code container only
make dev            # LLM server with --reload on file changes
```

### Database

```bash
make db-backup      # manual snapshot to data/backups/
make db-restore     # restore latest snapshot
make db-reset       # wipe all data and backups
```

Backups happen automatically on `make stop` and restore automatically on `make db`.

---

## Configuration

All config lives in `.env`. Edit and restart the relevant service.

```bash
# Switch models
LLM_MODEL=mlx-community/Qwen3-8B-4bit
# → make restart-llm

# Change ports
CHAT_APP_PORT=8080
# → make restart-chat

# Tune the request queue
MAX_QUEUE_SIZE=20
# → make restart-llm
```

### Recommended models for 16GB RAM

| Model | Size | Notes |
|---|---|---|
| `mlx-community/Qwen3-8B-4bit` | ~5GB | Best all-round, has thinking mode |
| `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` | ~4.5GB | Best for code |
| `mlx-community/Llama-3.1-8B-Instruct-4bit` | ~5GB | 128k context |
| `mlx-community/Phi-4-4bit` | ~8GB | Strong reasoning |

---

## Agent Mode

Toggle **agent mode** in the chat UI (bottom right of input). The LLM gets access to tools that let it read and write files and run commands inside the code container.

### What it can access

| Path | Read | Write |
|---|---|---|
| `chat/`, `llm/`, `code/`, `postgres/` | ✅ | ✅ |
| `Makefile`, other root files | ✅ | ❌ |
| `.env`, `data/`, `logs/`, `.git/` | ❌ | ❌ |

Destructive operations (writes, shell commands) require your explicit confirmation in the UI before they execute.

### To allow writes to a new service directory

Add it to `WRITE_ALLOWLIST` in `code/code.py` and run `make restart-code`.

---

## Adding a New Service

1. Create a directory: `mkdir my-service`
2. Add it to `WRITE_ALLOWLIST` in `code/code.py`
3. Add port entries to `.env`
4. Add `build-X`, `X`, `stop-X` targets to `Makefile` following the existing pattern
5. Run `make restart`

---

## API

### LLM server (port 8000)

```bash
# Generate (streaming)
curl http://localhost:8000/generate \
  -X POST -H "Content-Type: application/json" \
  -d '{"prompt": "Hello", "stream": true}'

# Generate with conversation history
curl http://localhost:8000/generate \
  -X POST -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}], "stream": false}'

# Queue status
curl http://localhost:8000/queue
```

### Chat app (port 5000)

```bash
# Send a message
curl http://localhost:5000/chat \
  -X POST -H "Content-Type: application/json" \
  -d '{"prompt": "Hello", "conversation_id": null}'

# List conversations
curl http://localhost:5000/conversations

# Get messages for a conversation
curl http://localhost:5000/conversations/<uuid>/messages

# Execute a tool (agent mode)
curl http://localhost:5000/tool \
  -X POST -H "Content-Type: application/json" \
  -d '{"tool": "list_directory", "args": {"path": "."}}'
```

Full interactive API docs at **http://localhost:5000/docs** and **http://localhost:8000/docs**.

---

## Troubleshooting

**Segfault on LLM startup**
The LLM server must be started with `python server.py` directly, not via `flask run` or `uvicorn` with a string module path. The Makefile handles this correctly. Avoid `--reload` in production.

**Port already in use**
```bash
lsof -i :<port>
kill $(lsof -t -i :<port>)
```
Note: macOS reserves port 5000 for AirPlay. Disable in System Settings → General → AirDrop & Handoff, or change `CHAT_APP_PORT` in `.env`.

**Model too slow**
Check that the LLM server is running natively (not in a container) and that Metal is active. The model should achieve ~30 tokens/sec on M-series chips.

**Database not persisting**
Run `make db-backup` before `make stop`. Backups are saved to `data/backups/` and restored automatically on next `make db`.
