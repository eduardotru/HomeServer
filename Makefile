# =============================================================================
# Local AI Platform
# =============================================================================
# Usage:
#   make start          — start everything
#   make stop           — stop everything
#   make restart        — stop then start everything
#   make restart-chat   — rebuild + restart only the chat container
#   make restart-llm    — restart only the LLM server
#   make dev            — start with uvicorn --reload on LLM server
#   make logs           — tail logs from all services
#   make build          — rebuild the chat container
#   make llm            — start only the LLM server
#   make chat           — start only the chat container
#   make db             — start only the Postgres container
#   make db-reset       — wipe the database volume
#   make status         — show what's running

include .env
export

CHAT_APP_PORT   ?= 5000
LLM_SERVER_PORT ?= 8000
LLM_EMBED_PORT  ?= 8010
POSTGRES_PORT   ?= 5432
FILES_PORT      ?= 9000
LLM_MODE        ?= local

.PHONY: start stop restart restart-chat restart-llm restart-embed restart-code restart-search restart-files dev logs build build-code build-search build-files kill-buildkit llm embed chat db db-reset code searxng search files status stop-chat stop-llm stop-embed stop-code stop-search stop-files setup new-app

# --- Setup -------------------------------------------------------------------

setup:
	@echo "▶ Setting up LLM server venv..."
	@python3 -m venv $(CURDIR)/llm/.venv
	@$(CURDIR)/llm/.venv/bin/pip install --quiet --upgrade pip
	@$(CURDIR)/llm/.venv/bin/pip install --quiet -r $(CURDIR)/llm/requirements.txt
	@echo "  LLM server venv ready."
	@echo "▶ Starting container system..."
	@container system start

# --- Start everything --------------------------------------------------------

kill-buildkit:
	@container kill buildkit 2>/dev/null || true

# In remote mode we skip kill-buildkit (no local model to protect memory for)
ifeq ($(LLM_MODE),remote)
start: build build-code build-files db llm embed chat code searxng search files logs
else
start: build build-code build-files kill-buildkit db llm embed chat code searxng search files logs
endif

restart:        stop start
restart-chat:   stop-chat build chat
restart-llm:    stop-llm llm
restart-embed:  stop-embed embed
restart-code:   stop-code build-code code
restart-search: stop-search build-search search
restart-files:  stop-files build-files files

# --- Dev mode (LLM server with --reload) -------------------------------------
# Safe because lifespan loads the model after uvicorn forks.
# Only use during development — reload adds ~1s latency on file changes.

dev: build db chat
	@echo "▶ Starting LLM server in dev mode (--reload)..."
	@mkdir -p logs
	@$(CURDIR)/llm/.venv/bin/python -m uvicorn \
		--app-dir $(CURDIR)/llm server:app \
		--host 0.0.0.0 --port $(LLM_SERVER_PORT) \
		--reload --reload-dir $(CURDIR)/llm \
		> $(CURDIR)/logs/llm.log 2>&1 & echo $$! > $(CURDIR)/logs/llm.pid
	@echo "  LLM server PID: $$(cat logs/llm.pid) (reload on)"
	@make logs

# --- LLM server (native) -----------------------------------------------------
# LLM_MODE=local  → loads model via mlx-lm (default)
# LLM_MODE=remote → thin proxy to any OpenAI-compatible remote API

llm:
	@echo "▶ Starting LLM server (mode: $(LLM_MODE)) on port $(LLM_SERVER_PORT)..."
	@mkdir -p logs
ifeq ($(LLM_MODE),remote)
	@$(CURDIR)/llm/.venv/bin/python $(CURDIR)/llm/proxy.py > $(CURDIR)/logs/llm.log 2>&1 & echo $$! > $(CURDIR)/logs/llm.pid
else
	@$(CURDIR)/llm/.venv/bin/python $(CURDIR)/llm/server.py > $(CURDIR)/logs/llm.log 2>&1 & echo $$! > $(CURDIR)/logs/llm.pid
endif
	@echo "  LLM server PID: $$(cat logs/llm.pid)"

# --- Embedding service (native) ----------------------------------------------
# First run downloads the model (~274 MB) from HuggingFace automatically.

embed:
	@echo "▶ Starting embedding service on port $(LLM_EMBED_PORT)..."
	@mkdir -p logs
	@$(CURDIR)/llm/.venv/bin/python $(CURDIR)/llm/embed.py > $(CURDIR)/logs/embed.log 2>&1 & echo $$! > $(CURDIR)/logs/embed.pid
	@echo "  Embed service PID: $$(cat logs/embed.pid)"

stop-embed:
	@if [ -f logs/embed.pid ]; then \
		kill $$(cat logs/embed.pid) 2>/dev/null && echo "  Embed service stopped." || echo "  Embed service already stopped."; \
		rm -f logs/embed.pid; \
	fi

# --- Postgres (container) ----------------------------------------------------

db:
	@echo "▶ Starting Postgres on port $(POSTGRES_PORT)..."
	@container run --rm -d \
	    --memory 200m \
		--name postgres \
		-p $(POSTGRES_PORT):5432 \
		-e POSTGRES_DB=$(POSTGRES_DB) \
		-e POSTGRES_USER=$(POSTGRES_USER) \
		-e POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
		-v $(CURDIR)/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql \
		-v $(CURDIR)/data/postgres:/var/lib/postgresql \
		pgvector/pgvector:pg16 > logs/postgres.cid
	@echo "  Postgres container ID: $$(cat logs/postgres.cid)"
	@echo "  Waiting for Postgres to be ready..."
	@for i in $$(seq 1 20); do \
		container exec postgres pg_isready -U $(POSTGRES_USER) > /dev/null 2>&1 && break; \
		sleep 1; \
	done
	@echo "  Postgres ready."

db-reset:
	@echo "⚠ Stopping Postgres and wiping all data and backups..."
	@container stop postgres 2>/dev/null || true
	@rm -rf $(CURDIR)/data/postgres
	@echo "  Done. Run 'make db' to start fresh."

build:
	@mkdir -p logs
	@echo "▶ Building chat container..."
	@container build \
		--build-arg CHAT_APP_PORT=$(CHAT_APP_PORT) \
		-t local/chat -f chat/Dockerfile . \
		> logs/build.log 2>&1
	@echo "  Build complete."

chat:
	@echo "▶ Starting chat container on port $(CHAT_APP_PORT)..."
	@mkdir -p logs
	@container run --rm -d \
	    --memory 300m \
		--name chat \
		-p $(CHAT_APP_PORT):$(CHAT_APP_PORT) \
		-e LLM_SERVER_URL=$(LLM_SERVER_URL) \
		-e CHAT_APP_PORT=$(CHAT_APP_PORT) \
		-e DATABASE_URL=$(DATABASE_URL) \
		-e CODE_CONTAINER_URL=$(CODE_CONTAINER_URL) \
		-v $(CURDIR)/apps:/apps \
		-v $(CURDIR)/ui:/ui \
		local/chat > logs/chat.cid
	@echo "  Chat container ID: $$(cat logs/chat.cid)"

# --- Apps --------------------------------------------------------------------
# `make new-app NAME=splitwise` scaffolds a new app from apps/_template.
# Chat picks it up automatically on next restart (make restart-chat).

new-app:
	@if [ -z "$(NAME)" ]; then \
		echo "usage: make new-app NAME=<app-name>"; exit 1; \
	fi
	@echo "$(NAME)" | grep -Eq '^[a-z][a-z0-9_-]*$$' || { \
		echo "app name must match ^[a-z][a-z0-9_-]*$$"; exit 1; \
	}
	@if [ -e apps/$(NAME) ]; then \
		echo "apps/$(NAME) already exists"; exit 1; \
	fi
	@cp -r apps/_template apps/$(NAME)
	@SCHEMA=$$(echo "$(NAME)" | tr '-' '_'); \
	find apps/$(NAME) -type f \( -name '*.py' -o -name '*.json' -o -name '*.sql' -o -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.md' \) \
	  -exec sed -i '' "s/app__template/app_$$SCHEMA/g; s/_template/$(NAME)/g" {} \;
	@echo "▶ Created apps/$(NAME). Restart chat to mount it: make restart-chat"

# --- Code container ----------------------------------------------------------

build-code:
	@mkdir -p logs
	@echo "▶ Building code container..."
	@container build \
		-t local/code -f code/Dockerfile code \
		> logs/build-code.log 2>&1
	@echo "  Build complete."

code:
	@echo "▶ Starting code container on port $(CODE_CONTAINER_PORT)..."
	@mkdir -p logs
	@container run --rm -d \
	    --memory 200m \
		--name code \
		-p $(CODE_CONTAINER_PORT):$(CODE_CONTAINER_PORT) \
		-e CODE_CONTAINER_PORT=$(CODE_CONTAINER_PORT) \
		-v $(CURDIR):/workspace \
		local/code > logs/code.cid
	@echo "  Code container ID: $$(cat logs/code.cid)"

stop-code:
	@container stop code 2>/dev/null && echo "  Code container stopped." || echo "  Code container already stopped."

# --- SearXNG (container) -----------------------------------------------------

searxng:
	@echo "▶ Starting SearXNG on port $(SEARXNG_PORT)..."
	@mkdir -p logs
	@mkdir -p data/searxng
	@container run --rm -d \
	    --memory 300m \
		--name searxng \
		-p $(SEARXNG_PORT):8080 \
		-v $(CURDIR)/searxng/:/etc/searxng/ \
		-v $(CURDIR)/data/searxng:/var/cache/searxng/ \
		searxng/searxng:latest > logs/searxng.cid
	@echo "  SearXNG container ID: $$(cat logs/searxng.cid)"

searxng-reset:
	@echo "⚠ Stopping SearXNG and wiping all cached data..."
	@container stop searxng 2>/dev/null || true
	@rm -rf $(CURDIR)/data/searxng
	@echo "  Done. Run 'make searxng' to start fresh."

# --- Files service (container) -----------------------------------------------

build-files:
	@mkdir -p logs
	@echo "▶ Building files container..."
	@container build \
		-t local/files -f files/Dockerfile files \
		> logs/build-files.log 2>&1
	@echo "  Build complete."

files:
	@echo "▶ Starting files service on port $(FILES_PORT)..."
	@mkdir -p logs data/files
	@container run --rm -d \
	    --memory 200m \
		--name files \
		-p $(FILES_PORT):$(FILES_PORT) \
		-e FILES_PORT=$(FILES_PORT) \
		-e FILES_ROOT=$(FILES_ROOT) \
		-e FILES_ENCRYPTION_KEY=$(FILES_ENCRYPTION_KEY) \
		-v $(CURDIR)/data/files:/data/files \
		local/files > logs/files.cid
	@echo "  Files container ID: $$(cat logs/files.cid)"

stop-files:
	@container stop files 2>/dev/null && echo "  Files service stopped." || echo "  Files service already stopped."

# --- Stop everything ---------------------------------------------------------

stop: stop-llm stop-embed stop-chat stop-code stop-files
	@container stop postgres 2>/dev/null && echo "  Postgres stopped." || echo "  Postgres already stopped."
	@container stop searxng 2>/dev/null && echo "  Searxng stopped." || echo "  Searxng already stopped."


stop-llm:
	@if [ -f logs/llm.pid ]; then \
		kill $$(cat logs/llm.pid) 2>/dev/null && echo "  LLM server stopped." || echo "  LLM server already stopped."; \
		rm -f logs/llm.pid; \
	fi

stop-chat:
	@container stop chat 2>/dev/null && echo "  Chat container stopped." || echo "  Chat container already stopped."

# --- Logs --------------------------------------------------------------------

logs:
	@container logs -f files > logs/files.log 2>&1 &
	@container logs -f postgres > logs/postgres.log 2>&1 &
	@container logs -f chat > logs/chat.log 2>&1 &
	@container logs -f code > logs/code.log 2>&1 &
	@container logs -f searxng > logs/searxng.log 2>&1 &
	@echo "▶ Tailing logs (Ctrl+C to stop)..."
	@tail -f logs/llm.log logs/embed.log logs/chat.log logs/postgres.log logs/code.log logs/search.log logs/searxng.log logs/files.log

# --- Status ------------------------------------------------------------------

status:
	@echo "=== LLM Server ==="
	@if [ -f logs/llm.pid ] && kill -0 $$(cat logs/llm.pid) 2>/dev/null; then \
		echo "  ✓ Running (PID $$(cat logs/llm.pid)) → http://localhost:$(LLM_SERVER_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
	@echo "=== Postgres ==="
	@if container ps 2>/dev/null | grep -q postgres; then \
		echo "  ✓ Running → localhost:$(POSTGRES_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
	@echo "=== Chat App ==="
	@if container ps 2>/dev/null | grep -q chat; then \
		echo "  ✓ Running → http://localhost:$(CHAT_APP_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
	@echo "=== Code Container ==="
	@if container ps 2>/dev/null | grep -q code; then \
		echo "  ✓ Running → http://localhost:$(CODE_CONTAINER_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
	@echo "=== SearXNG ==="
	@if container ps 2>/dev/null | grep -q searxng; then \
		echo "  ✓ Running → http://localhost:$(SEARXNG_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
	@echo "=== Search Service ==="
	@if container ps 2>/dev/null | grep -q search; then \
		echo "  ✓ Running → http://localhost:$(SEARCH_APP_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
	@echo "=== Files Service ==="
	@if container ps 2>/dev/null | grep -q files; then \
		echo "  ✓ Running → http://localhost:$(FILES_PORT)"; \
	else \
		echo "  ✗ Stopped"; \
	fi
