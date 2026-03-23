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

CHAT_APP_PORT  ?= 5000
LLM_SERVER_PORT ?= 8000
POSTGRES_PORT  ?= 5432

.PHONY: start stop restart restart-chat restart-llm restart-code restart-search dev logs build build-code build-search llm chat db db-reset code searxng search status stop-chat stop-llm stop-code stop-search setup

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

start: build build-code db llm chat code searxng search logs

restart:      stop start
restart-chat: stop-chat build chat
restart-llm:  stop-llm llm
restart-code: stop-code build-code code
restart-search: stop-search build-search search

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

llm:
	@echo "▶ Starting LLM server on port $(LLM_SERVER_PORT)..."
	@mkdir -p logs
	@$(CURDIR)/llm/.venv/bin/python $(CURDIR)/llm/server.py > $(CURDIR)/logs/llm.log 2>&1 & echo $$! > $(CURDIR)/logs/llm.pid
	@echo "  LLM server PID: $$(cat logs/llm.pid)"

# --- Postgres (container) ----------------------------------------------------

db:
	@echo "▶ Starting Postgres on port $(POSTGRES_PORT)..."
	@container run --rm -d \
		--name postgres \
		-p $(POSTGRES_PORT):5432 \
		-e POSTGRES_DB=$(POSTGRES_DB) \
		-e POSTGRES_USER=$(POSTGRES_USER) \
		-e POSTGRES_PASSWORD=$(POSTGRES_PASSWORD) \
		-v $(CURDIR)/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql \
		-v $(CURDIR)/data/postgres:/var/lib/postgresql \
		postgres:16-alpine > logs/postgres.cid
	@echo "  Postgres container ID: $$(cat logs/postgres.cid)"
	@container logs -f postgres > logs/postgres.log 2>&1 &
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
		--name chat \
		-p $(CHAT_APP_PORT):$(CHAT_APP_PORT) \
		-e LLM_SERVER_URL=$(LLM_SERVER_URL) \
		-e CHAT_APP_PORT=$(CHAT_APP_PORT) \
		-e DATABASE_URL=$(DATABASE_URL) \
		-e CODE_CONTAINER_URL=$(CODE_CONTAINER_URL) \
		-e SEARCH_APP_URL=$(SEARCH_APP_URL) \
		local/chat > logs/chat.cid
	@echo "  Chat container ID: $$(cat logs/chat.cid)"
	@container logs -f chat > logs/chat.log 2>&1 &

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
		--name code \
		-p $(CODE_CONTAINER_PORT):$(CODE_CONTAINER_PORT) \
		-e CODE_CONTAINER_PORT=$(CODE_CONTAINER_PORT) \
		-v $(CURDIR):/workspace \
		local/code > logs/code.cid
	@echo "  Code container ID: $$(cat logs/code.cid)"
	@container logs -f code > logs/code.log 2>&1 &

stop-code:
	@container stop code 2>/dev/null && echo "  Code container stopped." || echo "  Code container already stopped."

# --- SearXNG (container) -----------------------------------------------------

searxng:
	@echo "▶ Starting SearXNG on port $(SEARXNG_PORT)..."
	@mkdir -p logs
	@container run --rm -d \
		--name searxng \
		-p $(SEARXNG_PORT):8080 \
		-v $(CURDIR)/searxng/settings.yml:/etc/searxng/settings.yml \
		searxng/searxng:latest > logs/searxng.cid
	@echo "  SearXNG container ID: $$(cat logs/searxng.cid)"
	@container logs -f searxng > logs/searxng.log 2>&1 &

# --- Search service (container) ----------------------------------------------

build-search:
	@mkdir -p logs
	@echo "▶ Building search container..."
	@container build \
		-t local/search -f search/Dockerfile . \
		> logs/build-search.log 2>&1
	@echo "  Build complete."

search: build-search
	@echo "▶ Starting search service on port $(SEARCH_APP_PORT)..."
	@mkdir -p logs
	@container run --rm -d \
		--name search \
		-p $(SEARCH_APP_PORT):$(SEARCH_APP_PORT) \
		-e SEARXNG_URL=$(SEARXNG_URL) \
		-e SEARCH_APP_PORT=$(SEARCH_APP_PORT) \
		local/search > logs/search.cid
	@echo "  Search container ID: $$(cat logs/search.cid)"
	@container logs -f search > logs/search.log 2>&1 &

stop-search:
	@container stop search 2>/dev/null && echo "  Search stopped." || echo "  Search already stopped."
	@container stop searxng 2>/dev/null && echo "  SearXNG stopped." || echo "  SearXNG already stopped."

# --- Stop everything ---------------------------------------------------------

stop: stop-llm stop-chat stop-code stop-search
	@container stop postgres 2>/dev/null && echo "  Postgres stopped." || echo "  Postgres already stopped."

stop-llm:
	@if [ -f logs/llm.pid ]; then \
		kill $$(cat logs/llm.pid) 2>/dev/null && echo "  LLM server stopped." || echo "  LLM server already stopped."; \
		rm -f logs/llm.pid; \
	fi

stop-chat:
	@container stop chat 2>/dev/null && echo "  Chat container stopped." || echo "  Chat container already stopped."

# --- Logs --------------------------------------------------------------------

logs:
	@echo "▶ Tailing logs (Ctrl+C to stop)..."
	@tail -f logs/llm.log logs/chat.log logs/postgres.log logs/code.log logs/search.log logs/searxng.log

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
