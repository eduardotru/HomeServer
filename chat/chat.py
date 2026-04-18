import asyncio
import datetime
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import apps_loader

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)

# --- Config ------------------------------------------------------------------

LLM_SERVER_URL = os.getenv("LLM_SERVER_URL", "http://localhost:8000")
EMBED_URL = os.getenv("EMBED_URL", "http://localhost:8010")
CHAT_APP_PORT = int(os.getenv("CHAT_APP_PORT", 8001))
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://localai:localai@localhost:5432/localai"
)
CODE_CONTAINER_URL = os.getenv("CODE_CONTAINER_URL", "http://localhost:6000")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")
FILES_URL = os.getenv("FILES_URL", "http://localhost:9000")

DB_ENCRYPTION_KEY = os.getenv("DB_ENCRYPTION_KEY")
if not DB_ENCRYPTION_KEY:
    raise RuntimeError(
        "DB_ENCRYPTION_KEY is required but not set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

# Compaction thresholds — tuned for Gemma 4 E4B (4B params, ~8k effective context).
# 1 token ≈ 4 chars. Prompt budget: ~3k tokens for history so response has room.
COMPACTION_CHAR_THRESHOLD = int(os.getenv("COMPACTION_CHAR_THRESHOLD", 12_000))
RECENT_MESSAGES_TO_KEEP = int(os.getenv("RECENT_MESSAGES_TO_KEEP", 4))

# Truncate tool results fed back to the model to avoid context overflow.
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", 4000))
ROUTINE_JOB_TIMEOUT = int(os.getenv("ROUTINE_JOB_TIMEOUT", 300))

# Pending destructive-tool confirmations: token → {event, approved}
PENDING_CONFIRMATIONS: dict[str, dict] = {}

# --- Scheduler ---------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone="UTC")

# --- Database ----------------------------------------------------------------

db_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    async def _init_conn(conn):
        # Register JSONB codec so Python dicts/lists round-trip automatically.
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, init=_init_conn)
    print("[DB] Connected to Postgres.")
    # Apply schema migrations idempotently
    await db_pool.execute("""
        CREATE TABLE IF NOT EXISTS search_sessions (
            id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            title      TEXT        NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS search_messages (
            id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id UUID        NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
            role       TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
            content    TEXT        NOT NULL,
            sources    JSONB       NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_search_messages_session_id
            ON search_messages(session_id, created_at);
    """)
    # pgvector migration — gracefully skipped if extension not available
    try:
        await db_pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await db_pool.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding vector(768)"
        )
        await db_pool.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_embedding
                ON messages USING hnsw (embedding vector_cosine_ops)
                WHERE embedding IS NOT NULL
        """)
        print("[DB] pgvector ready.")
    except Exception as e:
        print(f"[DB] pgvector not available — semantic search disabled. ({e})")

    # Structured message kinds — lets the UI render thinking/tool_call/tool_result
    # distinctly when reloading a conversation. Idempotent on existing DBs.
    await db_pool.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS kind TEXT"
    )
    await db_pool.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS metadata JSONB"
    )
    await db_pool.execute(
        "UPDATE messages SET kind = role WHERE kind IS NULL"
    )
    await _migrate_legacy_tool_results()
    await db_pool.execute("""
        CREATE TABLE IF NOT EXISTS routines (
            id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT        NOT NULL,
            schedule    TEXT        NOT NULL,
            prompt      TEXT        NOT NULL,
            enabled     BOOLEAN     NOT NULL DEFAULT true,
            last_run_at TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS routine_runs (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            routine_id      UUID        NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
            conversation_id UUID,
            started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at     TIMESTAMPTZ,
            status          TEXT        NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            output          TEXT,
            error           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_routine_runs_routine_id
            ON routine_runs(routine_id, started_at DESC);
    """)
    print("[DB] Schema up to date.")
    # Load and schedule enabled routines
    routine_rows = await db_pool.fetch(
        "SELECT id, schedule, last_run_at FROM routines WHERE enabled = true"
    )
    for row in routine_rows:
        _schedule_routine(row["id"], row["schedule"])
    scheduler.start()
    print(f"[scheduler] Started with {len(routine_rows)} routine(s).")

    # Discover and mount /apps/* sub-apps. Each app's failure is isolated.
    await apps_loader.install_all(app, db_pool)

    # Catch up any routines that were missed while the server was down.
    # For each enabled routine, find the most recent scheduled fire time before
    # now. If it's newer than last_run_at (or the routine has never run), fire
    # it immediately so no run is silently skipped.
    try:
        from croniter import croniter
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        catchup_count = 0
        for row in routine_rows:
            try:
                cron = croniter(row["schedule"], now_utc)
                last_scheduled: datetime.datetime = cron.get_prev(datetime.datetime)
                last_run = row["last_run_at"]
                if last_run is not None and last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=datetime.timezone.utc)
                if last_run is None or last_scheduled > last_run:
                    print(
                        f"[scheduler] Missed run for routine {row['id']} "
                        f"(was due {last_scheduled.isoformat()}), firing now."
                    )
                    asyncio.create_task(run_routine(row["id"]))
                    catchup_count += 1
            except Exception as e:
                print(f"[scheduler] Catch-up check failed for {row['id']}: {e}")
        if catchup_count:
            print(f"[scheduler] Catching up {catchup_count} missed routine(s).")
    except ImportError:
        print("[scheduler] croniter not installed — skipping missed-run catch-up.")
    yield
    scheduler.shutdown(wait=False)
    await db_pool.close()
    print("[DB] Disconnected.")


app = FastAPI(title="Chat App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# /ui static mount, /apps launcher, /apps/{name} wrapper, /registry/apps.
# Sub-apps themselves mount inside lifespan once db_pool is ready.
apps_loader.install_routes(app)

# --- Models ------------------------------------------------------------------


# --- Embedding helpers -------------------------------------------------------

# Circuit breaker: after a failure, pause for EMBED_BACKOFF_SECS before retrying.
# This prevents log spam when the embed service is down or still loading.
EMBED_BACKOFF_SECS = 60
_embed_last_failure: float = 0.0


async def get_embedding(text: str) -> list[float] | None:
    """Call the local MLX embed service. Returns None if unavailable."""
    import time
    global _embed_last_failure
    if time.monotonic() - _embed_last_failure < EMBED_BACKOFF_SECS:
        return None  # still in backoff window — skip silently
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=30.0, write=5.0, pool=5.0)
        ) as client:
            res = await client.post(
                f"{EMBED_URL}/embed",
                json={"texts": [text], "prefix": "search_query: "},
            )
            res.raise_for_status()
            _embed_last_failure = 0.0  # reset on success
            return res.json()["embeddings"][0]
    except Exception as e:
        _embed_last_failure = time.monotonic()
        print(f"[embed] unavailable ({e.__class__.__name__}), pausing {EMBED_BACKOFF_SECS}s")


def _emb_str(v: list[float]) -> str:
    """Format a float list as a pgvector literal: '[0.1,0.2,...]'"""
    return "[" + ",".join(f"{x:.8f}" for x in v) + "]"


async def _store_embedding(msg_id: uuid.UUID, embedding: list[float]) -> None:
    """Background task: write an embedding into the messages table."""
    try:
        await db_pool.execute(
            "UPDATE messages SET embedding = $1::vector WHERE id = $2",
            _emb_str(embedding),
            msg_id,
        )
    except Exception as e:
        print(f"[embed] store failed for {msg_id}: {e}")


async def save_message(
    conv_id: uuid.UUID,
    role: str,
    content: str,
    *,
    kind: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> uuid.UUID:
    """Insert a message and schedule a background embedding update.

    `kind` is one of 'user' | 'assistant' | 'tool_call' | 'tool_result'.
    Defaults to `role` so existing call sites don't need to change.
    """
    kind = kind or role
    msg_id = await db_pool.fetchval(
        "INSERT INTO messages (conversation_id, role, content, kind, metadata) "
        "VALUES ($1, $2, armor(pgp_sym_encrypt($3, $4)), $5, $6) RETURNING id",
        conv_id,
        role,
        content,
        DB_ENCRYPTION_KEY,
        kind,
        metadata,
    )
    if content and content.strip():
        asyncio.create_task(_embed_message_bg(msg_id, content))
    return msg_id


async def _migrate_legacy_tool_results() -> None:
    """Convert old `role='user'` rows that began with "Tool result for X:" into
    structured `kind='tool_result'` rows. Runs once on startup; subsequent
    boots are no-ops because upgraded rows no longer have kind='user'.
    """
    try:
        rows = await db_pool.fetch(
            "SELECT id, pgp_sym_decrypt(dearmor(content), $1) AS content "
            "FROM messages WHERE kind = 'user' AND metadata IS NULL",
            DB_ENCRYPTION_KEY,
        )
    except Exception as e:
        print(f"[DB] legacy tool-result migration query failed: {e}")
        return

    upgraded = 0
    for r in rows:
        c = r["content"] or ""
        if not c.startswith("Tool result for "):
            continue
        rest = c[len("Tool result for ") :]
        colon = rest.find(":\n")
        if colon == -1:
            continue
        tool = rest[:colon].strip()
        body = rest[colon + 2 :]
        try:
            parsed_result = json.loads(body)
        except Exception:
            parsed_result = body
        metadata = {"tool": tool, "ok": True, "result": parsed_result}
        try:
            await db_pool.execute(
                "UPDATE messages SET kind = 'tool_result', metadata = $2 WHERE id = $1",
                r["id"], metadata,
            )
            upgraded += 1
        except Exception as e:
            print(f"[DB] failed to upgrade legacy tool result {r['id']}: {e}")
    if upgraded:
        print(f"[DB] Upgraded {upgraded} legacy tool-result messages.")


async def _embed_message_bg(msg_id: uuid.UUID, content: str) -> None:
    """Embed content and store — runs in background, never raises."""
    try:
        emb = await get_embedding(content)
        if emb:
            await _store_embedding(msg_id, emb)
    except Exception as e:
        print(f"[embed] background embedding failed for {msg_id}: {e}")


# --- Context compaction ------------------------------------------------------


def estimate_chars(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages)


async def build_context(conv_id: uuid.UUID) -> tuple[list[dict], Optional[str]]:
    """
    Build (messages, memory_snippet) to send to the LLM.

    The caller appends `memory_snippet` to the system prompt. We avoid
    synthetic user/assistant turns because small models (Gemma 4 E4B)
    tend to copy their phrasing into real replies.

    Strategy when within char limit: return full history, no snippet.
    Strategy when over limit:
      1. Keep last RECENT_MESSAGES_TO_KEEP messages verbatim.
      2. Build a compact memory_snippet from prior summary + top-k
         semantically relevant dropped messages.
      3. Trigger background summarisation of dropped messages.
    """
    row = await db_pool.fetchrow(
        "SELECT CASE WHEN summary IS NULL THEN NULL "
        "ELSE pgp_sym_decrypt(dearmor(summary), $2) END AS summary "
        "FROM conversations WHERE id = $1",
        conv_id, DB_ENCRYPTION_KEY,
    )
    summary = row["summary"] if row else None

    history = await db_pool.fetch(
        "SELECT id, role, pgp_sym_decrypt(dearmor(content), $2) AS content "
        "FROM messages WHERE conversation_id = $1 "
        "AND COALESCE(kind, role) IN ('user', 'assistant') "
        "ORDER BY created_at",
        conv_id, DB_ENCRYPTION_KEY,
    )
    all_messages = [
        {"id": str(r["id"]), "role": r["role"], "content": r["content"]}
        for r in history
        if r["content"] and r["content"].strip()
    ]
    plain = [{"role": m["role"], "content": m["content"]} for m in all_messages]

    snippet_parts: list[str] = []
    if summary:
        snippet_parts.append(f"Summary so far: {summary}")

    if estimate_chars(plain) < COMPACTION_CHAR_THRESHOLD:
        print(
            f"[context] conv={conv_id} no compaction — "
            f"{len(plain)} msgs, {estimate_chars(plain)} chars "
            f"(threshold {COMPACTION_CHAR_THRESHOLD})"
        )
        return plain, ("\n".join(snippet_parts) if snippet_parts else None)

    # --- Over threshold: semantic memory + recent window --------------------
    recent = all_messages[-RECENT_MESSAGES_TO_KEEP:]
    older = all_messages[:-RECENT_MESSAGES_TO_KEEP]

    if older:
        current_text = recent[-1]["content"] if recent else None
        if current_text:
            emb = await get_embedding(current_text)
            if emb:
                try:
                    recent_contents = {m["content"] for m in recent}
                    sim_rows = await db_pool.fetch(
                        """
                        SELECT role, pgp_sym_decrypt(dearmor(content), $3) AS content
                        FROM messages
                        WHERE conversation_id = $1
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> $2::vector
                        LIMIT 8
                        """,
                        conv_id,
                        _emb_str(emb),
                        DB_ENCRYPTION_KEY,
                    )
                    relevant = [
                        {"role": r["role"], "content": r["content"]}
                        for r in sim_rows
                        if r["content"] not in recent_contents
                    ][:3]
                    if relevant:
                        mem_lines = "\n".join(
                            f"- {m['role']}: {m['content'][:300]}" for m in relevant
                        )
                        snippet_parts.append(f"Earlier relevant turns:\n{mem_lines}")
                        print(f"[embed] Folded {len(relevant)} relevant msgs into system prompt.")
                except Exception as e:
                    print(f"[embed] Semantic retrieval failed: {e}")

        asyncio.create_task(update_summary(conv_id, summary, older))

    recent_msgs = [{"role": m["role"], "content": m["content"]} for m in recent]
    print(f"[compaction] Dropped {len(older)} msgs, kept {len(recent)} recent + snippet.")
    return recent_msgs, ("\n\n".join(snippet_parts) if snippet_parts else None)


async def update_summary(
    conv_id: uuid.UUID, existing_summary: Optional[str], new_messages: list[dict]
):
    """
    Ask the LLM to produce an updated summary combining the existing summary
    (if any) with the newly-dropped messages.
    """
    try:
        history_text = "\n".join(
            f"{m['role'].capitalize()}: {m['content'][:400]}" for m in new_messages
        )

        if existing_summary:
            prompt = (
                f"You have a running summary of a conversation:\n{existing_summary}\n\n"
                f"Update it to include these additional exchanges (be concise, max 300 words):\n{history_text}"
            )
        else:
            prompt = f"Summarise this conversation history concisely (max 300 words):\n{history_text}"

        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                f"{LLM_SERVER_URL}/generate",
                json={"prompt": prompt, "stream": False, "model": "main"},
            )
            new_summary = res.json().get("response", "").strip()
            if new_summary:
                await db_pool.execute(
                    "UPDATE conversations SET summary = armor(pgp_sym_encrypt($1, $3)) WHERE id = $2",
                    new_summary,
                    conv_id,
                    DB_ENCRYPTION_KEY,
                )
                print(f"[compaction] Summary updated for {conv_id}.")
    except Exception as e:
        print(f"[compaction] Failed to update summary: {e}")


# --- Scheduler helpers -------------------------------------------------------


def _schedule_routine(routine_id: uuid.UUID, cron_expr: str) -> None:
    """Add or replace a routine job in the scheduler."""
    try:
        scheduler.add_job(
            run_routine,
            CronTrigger.from_crontab(cron_expr, timezone="UTC"),
            id=str(routine_id),
            args=[routine_id],
            replace_existing=True,
            misfire_grace_time=300,
        )
    except Exception as e:
        print(f"[scheduler] Failed to schedule {routine_id}: {e}")


def _unschedule_routine(routine_id: uuid.UUID) -> None:
    """Remove a routine job from the scheduler (silent if not found)."""
    try:
        scheduler.remove_job(str(routine_id))
    except Exception:
        pass


def _next_run(routine_id: uuid.UUID) -> str | None:
    """Return ISO next-run time for a scheduled job, or None."""
    job = scheduler.get_job(str(routine_id))
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


async def run_headless_agent(prompt: str) -> tuple[str, uuid.UUID]:
    """
    Run the full agent loop without streaming.
    Creates a new conversation, returns (final_output, conv_id).
    """
    conv_id = uuid.uuid4()
    await db_pool.execute(
        "INSERT INTO conversations (id, title) VALUES ($1, armor(pgp_sym_encrypt($2, $3)))",
        conv_id, f"[Routine] {prompt[:60]}", DB_ENCRYPTION_KEY,
    )
    await save_message(conv_id, "user", prompt)

    history, memory_snippet = await build_context(conv_id)
    system_content = TOOL_SYSTEM_PROMPT
    if memory_snippet:
        system_content += f"\n\nRelevant earlier context:\n{memory_snippet}"
    active_tools = select_tools_for(prompt, allow_delegate=True)
    print(
        f"[tools] headless conv={conv_id} "
        f"selected {len(active_tools)}/{len(GEMMA_TOOLS)}: "
        f"{[t['function']['name'] for t in active_tools]}"
    )
    messages = [
        {"role": "system", "content": system_content},
    ] + TOOL_EXAMPLE_MESSAGES + history

    final_output = ""
    consecutive_errors = 0

    for _ in range(15):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
            ) as client:
                res = await client.post(
                    f"{LLM_SERVER_URL}/generate",
                    json={"messages": messages, "stream": False, "model": "coder", "tools": active_tools},
                )
                res.raise_for_status()
                full_text = res.json().get("response", "")
        except Exception as e:
            raise RuntimeError(f"LLM call failed: {type(e).__name__}: {e}")

        clean_text = _strip_thinking(full_text)
        preamble, tool_call = _parse_tool_call(clean_text)
        display_text = preamble if tool_call else clean_text

        if display_text.strip():
            final_output = display_text

        if not tool_call:
            messages.append({"role": "assistant", "content": clean_text})
            break

        # Append assistant message with native tool_calls field
        messages.append({
            "role": "assistant",
            "content": preamble or "",
            "tool_calls": [{"function": {"name": tool_call["tool"], "arguments": tool_call["args"]}}],
        })

        try:
            result = await execute_tool(tool_call["tool"], tool_call.get("args", {}))
            result_str = json.dumps(result)
            if len(result_str) > MAX_TOOL_RESULT_CHARS:
                result_str = (
                    result_str[:MAX_TOOL_RESULT_CHARS]
                    + f"\n[TRUNCATED at {MAX_TOOL_RESULT_CHARS} chars — re-call with a narrower range if needed]"
                )
            messages.append({"role": "tool", "name": tool_call["tool"], "content": result_str})
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            messages.append({"role": "tool", "name": tool_call["tool"], "content": f"Error: {e}"})
            if consecutive_errors >= 3:
                break

    await save_message(conv_id, "assistant", final_output)
    return final_output, conv_id


SUB_AGENT_SYSTEM_PROMPT = """You are a sub-agent executing a specific task. You have your own context — you cannot see the parent conversation.

RULES:
- Focus only on the task given. Be thorough but concise.
- Use tools to investigate, build, or modify as needed.
- Write important findings to shared memory with memory_write so the parent agent can read them.
- Use namespaced keys: task_name/finding (e.g. auth_research/oauth_flow, migration/step1).
- When done, summarize what you accomplished and what you wrote to memory."""


async def run_sub_agent(task: str, context: str = "", parent_agent: str = "main") -> dict:
    """
    Run a sub-agent with isolated context.
    It shares memory with the parent but has no access to the parent's conversation.
    Returns a summary dict.
    """
    agent_name = f"sub:{uuid.uuid4().hex[:8]}"

    # Build the sub-agent's message list — fresh context, just the task
    user_content = task
    if context:
        user_content = f"Context: {context}\n\nTask: {task}"

    messages = [
        {"role": "system", "content": SUB_AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    sub_tools = select_tools_for(user_content, allow_delegate=False)
    print(
        f"[tools] sub-agent={agent_name} "
        f"selected {len(sub_tools)}/{len(SUB_AGENT_TOOLS)}: "
        f"{[t['function']['name'] for t in sub_tools]}"
    )

    final_output = ""
    tools_used = []
    consecutive_errors = 0

    for turn in range(15):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
            ) as client:
                res = await client.post(
                    f"{LLM_SERVER_URL}/generate",
                    json={"messages": messages, "stream": False, "tools": sub_tools},
                )
                res.raise_for_status()
                full_text = res.json().get("response", "")
        except Exception as e:
            return {"ok": False, "error": f"LLM call failed: {e}", "agent": agent_name}

        clean_text = _strip_thinking(full_text)
        preamble, tool_call = _parse_tool_call(clean_text)
        display_text = preamble if tool_call else clean_text

        if display_text.strip():
            final_output = display_text

        if not tool_call:
            messages.append({"role": "assistant", "content": clean_text})
            break

        tool_name = tool_call["tool"]
        tools_used.append(tool_name)

        messages.append({
            "role": "assistant",
            "content": preamble or "",
            "tool_calls": [{"function": {"name": tool_name, "arguments": tool_call["args"]}}],
        })

        try:
            result = await execute_tool(tool_name, tool_call.get("args", {}), agent_name=agent_name)
            result_str = json.dumps(result)
            if len(result_str) > MAX_TOOL_RESULT_CHARS:
                result_str = (
                    result_str[:MAX_TOOL_RESULT_CHARS]
                    + f"\n[TRUNCATED at {MAX_TOOL_RESULT_CHARS} chars — re-call with a narrower range if needed]"
                )
            messages.append({"role": "tool", "name": tool_name, "content": result_str})
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            messages.append({"role": "tool", "name": tool_name, "content": f"Error: {e}"})
            if consecutive_errors >= 3:
                break

    print(f"[sub-agent] {agent_name} finished: {len(tools_used)} tool calls, output={len(final_output)} chars")
    return {
        "ok": True,
        "agent": agent_name,
        "summary": final_output,
        "tools_used": tools_used,
        "turns": turn + 1,
    }


async def run_routine(routine_id: uuid.UUID) -> None:
    """Execute a routine's prompt through the headless agent."""
    print(f"[routine] Running {routine_id}…")
    run_id = None
    try:
        row = await db_pool.fetchrow(
            "SELECT pgp_sym_decrypt(dearmor(prompt), $2) AS prompt "
            "FROM routines WHERE id = $1 AND enabled = true",
            routine_id, DB_ENCRYPTION_KEY,
        )
        if not row:
            print(f"[routine] {routine_id} not found or disabled — skipping.")
            return

        run_id = await db_pool.fetchval(
            "INSERT INTO routine_runs (routine_id, status) VALUES ($1, 'running') RETURNING id",
            routine_id,
        )

        try:
            output, conv_id = await asyncio.wait_for(
                run_headless_agent(row["prompt"]), timeout=ROUTINE_JOB_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Timed out after {ROUTINE_JOB_TIMEOUT}s")

        await db_pool.execute(
            "UPDATE routine_runs SET status='completed', finished_at=NOW(), "
            "conversation_id=$2, output=armor(pgp_sym_encrypt($3, $4)) WHERE id=$1",
            run_id, conv_id, output or "", DB_ENCRYPTION_KEY,
        )
        await db_pool.execute("UPDATE routines SET last_run_at=NOW() WHERE id=$1", routine_id)
        print(f"[routine] {routine_id} completed.")

    except Exception as e:
        print(f"[routine] {routine_id} failed: {e}")
        if run_id:
            try:
                await db_pool.execute(
                    "UPDATE routine_runs SET status='failed', finished_at=NOW(), "
                    "error=armor(pgp_sym_encrypt($2, $3)) WHERE id=$1",
                    run_id, str(e), DB_ENCRYPTION_KEY,
                )
            except Exception:
                pass


# --- Tool system prompt ------------------------------------------------------

# --- Gemma 4 native tool definitions (OpenAI function-calling format) --------

GEMMA_TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description": "Search the web for current information, documentation, or anything beyond training knowledge.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "The search query"}, "num_results": {"type": "integer", "description": "Number of results (default 5)"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_directory", "description": "List files and directories. Works for code paths and notes/ paths.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path (e.g. chat/ or notes/)"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_files", "description": "Find files by name/glob pattern.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Glob pattern like *.py"}, "path": {"type": "string", "description": "Directory to search in (default .)"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "search_code", "description": "Search code for a regex or text pattern. Returns matching lines with context.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Regex or text pattern"}, "path": {"type": "string", "description": "Directory (default .)"}, "glob": {"type": "string", "description": "File filter like *.py"}, "context_lines": {"type": "integer", "description": "Context lines (default 2)"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file from code or notes. Use start_line/end_line for sections.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Path (e.g. chat/chat.py or notes/ideas.md)"}, "start_line": {"type": "integer", "description": "First line (1-indexed)"}, "end_line": {"type": "integer", "description": "Last line (1-indexed)"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace one exact string in a file. Works for code and notes/ paths.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Path to file"}, "old_str": {"type": "string", "description": "Exact text to find"}, "new_str": {"type": "string", "description": "Replacement text"}}, "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or overwrite a file. Works for code and notes/ paths.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Path (e.g. new_script.py or notes/ideas.md)"}, "content": {"type": "string", "description": "Full file content"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run a shell command. Use for builds, tests, or custom scripts you create.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command"}, "working_dir": {"type": "string", "description": "Working directory (optional)"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "files_append", "description": "Append content to a notes file without overwriting.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Notes path (e.g. notes/log.md)"}, "content": {"type": "string", "description": "Content to append"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "files_delete", "description": "Delete a file from the notes store.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Notes path (e.g. notes/old.md)"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_routines", "description": "List all scheduled routines with their IDs.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "create_routine", "description": "Create a scheduled routine.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Routine name"}, "schedule": {"type": "string", "description": "Cron expression in UTC"}, "prompt": {"type": "string", "description": "What to do when it fires"}, "enabled": {"type": "boolean", "description": "Active (default true)"}}, "required": ["name", "schedule", "prompt"]}}},
    {"type": "function", "function": {"name": "update_routine", "description": "Update a routine. Call list_routines first for the ID.", "parameters": {"type": "object", "properties": {"id": {"type": "string", "description": "Routine UUID"}, "name": {"type": "string"}, "schedule": {"type": "string"}, "prompt": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "delete_routine", "description": "Delete a routine by ID.", "parameters": {"type": "object", "properties": {"id": {"type": "string", "description": "Routine UUID"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "memory_read", "description": "Read a value from shared agent memory by key.", "parameters": {"type": "object", "properties": {"key": {"type": "string", "description": "Memory key (e.g. research/topic)"}}, "required": ["key"]}}},
    {"type": "function", "function": {"name": "memory_write", "description": "Write a value to shared agent memory. Overwrites if key exists.", "parameters": {"type": "object", "properties": {"key": {"type": "string", "description": "Memory key (use / for namespaces, e.g. research/findings)"}, "value": {"type": "string", "description": "Content to store"}}, "required": ["key", "value"]}}},
    {"type": "function", "function": {"name": "memory_list", "description": "List keys in shared agent memory, optionally filtered by prefix.", "parameters": {"type": "object", "properties": {"prefix": {"type": "string", "description": "Key prefix filter (e.g. research/)"}}, "required": []}}},
    {"type": "function", "function": {"name": "delegate", "description": "Spawn a sub-agent with its own context to handle a task. The sub-agent has access to all tools and shared memory. Returns a summary of what it accomplished.", "parameters": {"type": "object", "properties": {"task": {"type": "string", "description": "Clear description of what the sub-agent should do"}, "context": {"type": "string", "description": "Background info the sub-agent needs (it cannot see your conversation)"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "new_app", "description": "Scaffold a new app from apps/_template under apps/<name>/. Creates manifest.json, app.py, migrations/, static/ with the template name substituted. After this, edit the files to build the app, then ask the user to run `make restart-chat`.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "App name. Must match [a-z][a-z0-9_-]* — e.g. 'splitwise', 'fitness', 'trips'."}}, "required": ["name"]}}},
]

# Sub-agent tool set — same as main but without delegate (no recursive spawning)
SUB_AGENT_TOOLS = [t for t in GEMMA_TOOLS if t["function"]["name"] != "delegate"]


# --- Tool selection by intent ------------------------------------------------
# Shipping all 18 tools to a 4B model on every turn drowns the prompt. Select
# a relevant subset based on keywords in the latest user message. Always keep
# the read-only navigation core; add more only when the task needs them.

_TOOL_GROUPS = {
    "core": {"web_search", "list_directory", "search_files", "search_code", "read_file"},
    "write": {"edit_file", "write_file", "run_command", "files_append", "files_delete"},
    "routine": {"list_routines", "create_routine", "update_routine", "delete_routine"},
    "memory": {"memory_read", "memory_write", "memory_list"},
    "delegate": {"delegate"},
    "apps": {"new_app"},
}

_WRITE_HINTS = re.compile(
    r"\b(edit|write|create|add|remove|delete|fix|patch|update|modify|rename|refactor|run|execute|install|build|test|append|save|make a|new file)\b",
    re.IGNORECASE,
)
_ROUTINE_HINTS = re.compile(
    r"\b(routine|schedule|cron|every (day|hour|minute|morning|night|week)|daily|hourly|weekly|reminder|remind me)\b",
    re.IGNORECASE,
)
_MEMORY_HINTS = re.compile(
    r"\b(remember|recall|memory|note for later|save (?:this|that|it) for|store (?:this|that|it))\b",
    re.IGNORECASE,
)
_DELEGATE_HINTS = re.compile(
    r"\b(delegate|sub-?agent|research deeply|in parallel|separately|break (?:this|it) down)\b",
    re.IGNORECASE,
)
_APPS_HINTS = re.compile(
    r"\b(new app|scaffold (?:an? )?app|create (?:an? )?app|build (?:an? )?app|app called|splitwise|fitness|trips)\b",
    re.IGNORECASE,
)


def select_tools_for(message: str, *, allow_delegate: bool = True) -> list[dict]:
    """Pick a GEMMA_TOOLS subset based on keyword hints in the user message.

    Read-only navigation is always included. Write/routine/memory/delegate
    groups are added only when the message suggests the user needs them.
    Falls back to the full set on short/ambiguous input.
    """
    text = message or ""
    chosen = set(_TOOL_GROUPS["core"])

    if _WRITE_HINTS.search(text):
        chosen |= _TOOL_GROUPS["write"]
    if _ROUTINE_HINTS.search(text):
        chosen |= _TOOL_GROUPS["routine"]
    if _MEMORY_HINTS.search(text):
        chosen |= _TOOL_GROUPS["memory"]
    if allow_delegate and _DELEGATE_HINTS.search(text):
        chosen |= _TOOL_GROUPS["delegate"]
    if _APPS_HINTS.search(text):
        chosen |= _TOOL_GROUPS["apps"]
        chosen |= _TOOL_GROUPS["write"]  # scaffolding an app implies file edits

    pool = GEMMA_TOOLS if allow_delegate else SUB_AGENT_TOOLS
    return [t for t in pool if t["function"]["name"] in chosen]

# Tools that modify state — require user confirmation unless always_allow is set
DESTRUCTIVE_TOOLS = {
    "edit_file", "write_file", "run_command",
    "create_routine", "update_routine", "delete_routine",
    "files_append", "files_delete",
    "delegate",  # spawns autonomous sub-agent
    "new_app",   # creates files under apps/
}

TOOL_SYSTEM_PROMPT = """You are an agent with tools. Investigate before answering.

Rules:
- Call a tool when you need a fact. Never guess file contents, paths, or structure.
- One tool per turn. Chain naturally: search → read → edit → verify.
- If a tool errors, read the error and try a different approach (different path, different pattern).
- If the question needs no tool (greetings, general knowledge, opinions), answer directly with no tool call.
- Stop as soon as you have the answer. Do not make extra calls.

Workspace: chat/, llm/, code/, postgres/, Makefile at project root.
Paths starting with notes/ are the encrypted notes store."""


# Few-shot examples — cover the patterns Gemma 4 E4B most often fails on:
# (1) answering without a tool, (2) chaining two tools, (3) recovering from error.
TOOL_EXAMPLE_MESSAGES = [
    # (1) No-tool answer — model often over-calls tools for general questions.
    {"role": "user", "content": "hi, how are you?"},
    {"role": "assistant", "content": "Doing well — what would you like to work on?"},

    # (2) Tool chain: search → read.
    {"role": "user", "content": "What port does the chat app listen on?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "search_code", "arguments": {"pattern": "CHAT_APP_PORT", "glob": "*.py"}}}],
    },
    {"role": "tool", "name": "search_code", "content": '{"matches":[{"file":"chat/chat.py","line":28,"text":"CHAT_APP_PORT = int(os.getenv(\\"CHAT_APP_PORT\\", 8001))"}]}'},
    {"role": "assistant", "content": "Port **8001**, set via `CHAT_APP_PORT` in `chat/chat.py:28`."},

    # (3) Error recovery: bad path → retry with correct one.
    {"role": "user", "content": "Show me the Makefile targets."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "makefile"}}}],
    },
    {"role": "tool", "name": "read_file", "content": '{"error":"File not found: makefile"}'},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "Makefile"}}}],
    },
    {"role": "tool", "name": "read_file", "content": '{"content":"run:\\n\\tpython chat/chat.py\\ntest:\\n\\tpytest"}'},
    {"role": "assistant", "content": "The Makefile has two targets: `run` (starts the chat app) and `test` (runs pytest)."},
]


# --- Tool call parsing (Gemma 4 native format) ------------------------------


def _strip_thinking(text: str) -> str:
    """Remove Gemma 4 thinking blocks and stray channel/thinking tokens."""
    # Remove full thinking blocks (newline after 'thought' is optional)
    text = re.sub(r"<\|channel>thought\s*[\s\S]*?<channel\|>", "", text)
    # Clean up any stray opening/closing channel tokens the model may emit
    text = re.sub(r"<\|channel>\w*\s*", "", text)
    text = re.sub(r"<channel\|>", "", text)
    return text.strip()


def _parse_gemma_args(args_str: str) -> dict:
    """Parse Gemma 4 tool call arguments: key:<|"|>val<|"|>,key2:123

    Handles string values (quoted with <|"|>), booleans, and numbers.
    """
    if not args_str or not args_str.strip():
        return {}

    # Extract all <|"|>...<|"|> strings and replace with placeholders
    strings: list[str] = []

    def _replace_str(m):
        strings.append(m.group(1))
        return f"__S{len(strings) - 1}__"

    s = re.sub(r'<\|"\|>([\s\S]*?)<\|"\|>', _replace_str, args_str)

    result = {}
    for pair in s.split(","):
        pair = pair.strip()
        if not pair:
            continue
        colon = pair.find(":")
        if colon == -1:
            continue
        key = pair[:colon].strip()
        val = pair[colon + 1 :].strip()

        # Restore string placeholder
        str_match = re.match(r"__S(\d+)__", val)
        if str_match:
            result[key] = strings[int(str_match.group(1))]
        elif val.lower() == "true":
            result[key] = True
        elif val.lower() == "false":
            result[key] = False
        else:
            try:
                result[key] = int(val)
            except ValueError:
                try:
                    result[key] = float(val)
                except ValueError:
                    result[key] = val
    return result


def _parse_tool_call(text: str) -> tuple[str, dict | None]:
    """Extract Gemma 4 native tool call from LLM text.

    Gemma 4 format: <|tool_call>call:tool_name{key:<|"|>val<|"|>,...}<tool_call|>

    Returns (preamble, tool_dict) where tool_dict has 'tool' and 'args' keys.
    Returns (text, None) if no valid tool call is found.
    """
    # Properly closed tag
    match = re.search(r"<\|tool_call>call:(\w+)\{([\s\S]*?)\}<tool_call\|>", text)
    if not match:
        # Unclosed — model may have been cut off
        match = re.search(r"<\|tool_call>call:(\w+)\{([\s\S]*)\}", text)
    if not match:
        # Minimal — just the tool name with no args
        match = re.search(r"<\|tool_call>call:(\w+)\{?\}?<tool_call\|>", text)
        if match:
            preamble = text[: match.start()].rstrip()
            return preamble, {"tool": match.group(1), "args": {}}
        return text, None

    tool_name = match.group(1)
    args_str = match.group(2) if match.lastindex >= 2 else ""

    try:
        args = _parse_gemma_args(args_str)
    except Exception:
        return text, None

    preamble = text[: match.start()].rstrip()
    return preamble, {"tool": tool_name, "args": args}


def _looks_like_malformed_tool_call(text: str) -> bool:
    """Return True when the model seems to have attempted a tool call we couldn't parse.

    Lets the agent loop surface a correction back to the model instead of
    treating garbled output as a final answer.
    """
    lowered = text.lower()
    return "<|tool_call" in lowered or "tool_call|>" in lowered or "call:" in lowered[:200]


_TOOL_FORMAT_REMINDER = (
    "Your previous output looked like a tool call but I could not parse it. "
    "Use this exact format: <|tool_call>call:TOOL_NAME{key:<|\"|>value<|\"|>,num:123}<tool_call|> "
    "— strings wrapped in <|\"|>…<|\"|>, numbers bare, comma-separated. "
    "Or, if no tool is needed, answer directly with no tool tags."
)


# --- Search system prompt & helpers ------------------------------------------

SEARCH_SYSTEM_PROMPT = """You are a research agent. Given a question, you plan targeted web searches, evaluate the results, and iterate until you can give a comprehensive answer.

Workflow:
1. Analyze the question and output 2-4 targeted search queries that cover different angles. Wrap them in a tag, one per line:
<search_queries>
first search query
second search query
</search_queries>
2. You will receive numbered search results. Evaluate whether they are sufficient.
3. If you need more information, output another <search_queries> block with refined or follow-up queries.
4. When you have enough, write a clear, well-structured final answer. Do NOT include any <search_queries> tags in a final answer.

Rules:
- Cite sources as [1], [2], etc. matching the numbered results provided.
- You may search up to 5 rounds. Be efficient — usually 1-2 rounds is enough.
- Focus each query on a specific aspect of the question for broad coverage.
- Do not repeat queries you have already searched for."""


def _parse_search_queries(text: str) -> tuple[str, list[str] | None]:
    """Extract <search_queries> block with one query per line.

    Returns (preamble, list_of_queries) or (text, None) if no queries found.
    Falls back to single <search_query> tag for robustness.
    """
    # Try plural format (new)
    match = re.search(r"<search_queries>([\s\S]*?)</search_queries>", text)
    if match:
        queries = [q.strip() for q in match.group(1).strip().split("\n") if q.strip()]
        if queries:
            return text[: match.start()].rstrip(), queries
    # Fall back to singular (in case the model uses the old format)
    match = re.search(r"<search_query>([\s\S]*?)</search_query>", text)
    if match:
        q = match.group(1).strip()
        if q:
            return text[: match.start()].rstrip(), [q]
    return text, None


def _format_results_for_llm(results: list[dict], offset: int = 0) -> str:
    return "\n\n".join(
        f"[{i + 1 + offset}] {r['title']}\nURL: {r['url']}\n{r['snippet']}"
        for i, r in enumerate(results)
        if r.get("snippet")
    )


# --- Tool execution ----------------------------------------------------------

READONLY_COMMANDS = {
    "ls",
    "cat",
    "find",
    "grep",
    "head",
    "tail",
    "wc",
    "diff",
    "tree",
    "pwd",
    "echo",
}

TOOL_ROUTES = {
    "list_directory": "list",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "search_code": "search_code",
    "search_files": "search_files",
    "run_command": "run",
}


async def execute_tool(tool: str, args: dict, agent_name: str = "main") -> dict:
    """Route a tool call to the appropriate service."""

    # --- Shared agent memory --------------------------------------------------
    if tool == "memory_read":
        key = args["key"]
        row = await db_pool.fetchrow(
            "SELECT pgp_sym_decrypt(dearmor(value), $2) AS value, agent, updated_at "
            "FROM agent_memory WHERE key = $1",
            key, DB_ENCRYPTION_KEY,
        )
        if not row:
            return {"key": key, "found": False}
        return {"key": key, "found": True, "value": row["value"], "agent": row["agent"],
                "updated_at": row["updated_at"].isoformat()}

    if tool == "memory_write":
        key = args["key"]
        value = args["value"]
        await db_pool.execute(
            "INSERT INTO agent_memory (key, value, agent) "
            "VALUES ($1, armor(pgp_sym_encrypt($2, $4)), $3) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = armor(pgp_sym_encrypt($2, $4)), agent = $3, updated_at = NOW()",
            key, value, agent_name, DB_ENCRYPTION_KEY,
        )
        return {"ok": True, "key": key}

    if tool == "memory_list":
        prefix = args.get("prefix", "")
        if prefix:
            rows = await db_pool.fetch(
                "SELECT key, agent, updated_at FROM agent_memory WHERE key LIKE $1 ORDER BY key",
                prefix + "%",
            )
        else:
            rows = await db_pool.fetch(
                "SELECT key, agent, updated_at FROM agent_memory ORDER BY key",
            )
        return {"keys": [{"key": r["key"], "agent": r["agent"],
                          "updated_at": r["updated_at"].isoformat()} for r in rows]}

    if tool == "delegate":
        task = args["task"]
        context = args.get("context", "")
        result = await run_sub_agent(task, context, parent_agent=agent_name)
        return result

    if tool == "new_app":
        name = args.get("name", "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        try:
            return apps_loader.scaffold_new_app(name)
        except (ValueError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}

    # Search tool is handled locally
    if tool == "web_search":
        query = args.get("query", "")
        num_results = args.get("num_results", 5)
        return await search(SearchRequest(query=query, num_results=num_results))

    # Routine management tools — handled in-process
    if tool == "list_routines":
        rows = await db_pool.fetch(
            "SELECT id, pgp_sym_decrypt(dearmor(name), $1) AS name, schedule, "
            "pgp_sym_decrypt(dearmor(prompt), $1) AS prompt, enabled, last_run_at "
            "FROM routines ORDER BY created_at DESC",
            DB_ENCRYPTION_KEY,
        )
        return {
            "routines": [
                {
                    "id": str(r["id"]),
                    "name": r["name"],
                    "schedule": r["schedule"],
                    "prompt": r["prompt"],
                    "enabled": r["enabled"],
                    "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
                    "next_run_at": _next_run(r["id"]),
                }
                for r in rows
            ]
        }

    if tool == "create_routine":
        name = args.get("name", "").strip()
        schedule = args.get("schedule", "").strip()
        prompt = args.get("prompt", "").strip()
        enabled = bool(args.get("enabled", True))
        if not name or not schedule or not prompt:
            raise ValueError("name, schedule, and prompt are required")
        try:
            CronTrigger.from_crontab(schedule)
        except Exception:
            raise ValueError(f"Invalid cron expression: '{schedule}'")
        routine_id = await db_pool.fetchval(
            "INSERT INTO routines (name, schedule, prompt, enabled) "
            "VALUES (armor(pgp_sym_encrypt($1, $5)), $2, armor(pgp_sym_encrypt($3, $5)), $4) RETURNING id",
            name, schedule, prompt, enabled, DB_ENCRYPTION_KEY,
        )
        if enabled:
            _schedule_routine(routine_id, schedule)
        return {"id": str(routine_id), "name": name, "schedule": schedule, "enabled": enabled}

    if tool == "update_routine":
        rid = uuid.UUID(args["id"])
        sets, params = [], [rid, DB_ENCRYPTION_KEY]
        i = 3
        for field in ("name", "prompt"):
            if field in args:
                sets.append(f"{field} = armor(pgp_sym_encrypt(${i}, $2))")
                params.append(str(args[field])); i += 1
        if "schedule" in args:
            try:
                CronTrigger.from_crontab(args["schedule"])
            except Exception:
                raise ValueError(f"Invalid cron expression: '{args['schedule']}'")
            sets.append(f"schedule = ${i}")
            params.append(str(args["schedule"])); i += 1
        if "enabled" in args:
            sets.append(f"enabled = ${i}")
            params.append(bool(args["enabled"])); i += 1
        if not sets:
            raise ValueError("No fields to update")
        await db_pool.execute(
            f"UPDATE routines SET {', '.join(sets)} WHERE id = $1", *params
        )
        row = await db_pool.fetchrow("SELECT schedule, enabled FROM routines WHERE id = $1", rid)
        if row["enabled"]:
            _schedule_routine(rid, row["schedule"])
        else:
            _unschedule_routine(rid)
        return {"ok": True, "id": str(rid)}

    if tool == "delete_routine":
        rid = uuid.UUID(args["id"])
        _unschedule_routine(rid)
        await db_pool.execute("DELETE FROM routines WHERE id = $1", rid)
        return {"ok": True, "id": str(rid)}

    # Files service tools
    if tool == "files_list":
        path = args.get("path", "").strip("/")
        url = f"{FILES_URL}/files/{path}" if path else f"{FILES_URL}/files"
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(url)
            res.raise_for_status()
            return res.json()

    if tool == "files_read":
        path = args["path"].strip("/")
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(f"{FILES_URL}/file/{path}")
            res.raise_for_status()
            return {"path": path, "content": res.text}

    if tool == "files_write":
        path = args["path"].strip("/")
        content = args.get("content", "")
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.put(
                f"{FILES_URL}/file/{path}",
                content=content.encode(),
            )
            res.raise_for_status()
            return res.json()

    if tool == "files_append":
        path = args["path"].strip("/")
        content = args.get("content", "")
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.patch(
                f"{FILES_URL}/file/{path}",
                content=content.encode(),
            )
            res.raise_for_status()
            return res.json()

    if tool == "files_delete":
        path = args["path"].strip("/")
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.delete(f"{FILES_URL}/file/{path}")
            res.raise_for_status()
            return res.json()

    # --- Route notes/ paths to the encrypted files service --------------------
    path = args.get("path", "")
    if path.startswith("notes/") or path == "notes":
        path_clean = path.strip("/")
        if tool == "list_directory":
            url = f"{FILES_URL}/files/{path_clean}" if path_clean else f"{FILES_URL}/files"
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(url)
                res.raise_for_status()
                return res.json()
        if tool == "read_file":
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(f"{FILES_URL}/file/{path_clean}")
                res.raise_for_status()
                return {"path": path_clean, "content": res.text}
        if tool == "write_file":
            content = args.get("content", "")
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.put(
                    f"{FILES_URL}/file/{path_clean}",
                    content=content.encode(),
                )
                res.raise_for_status()
                return res.json()
        if tool == "edit_file":
            # Read → replace → write back
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(f"{FILES_URL}/file/{path_clean}")
                res.raise_for_status()
                original = res.text
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            if original.count(old_str) == 0:
                raise ValueError(f"old_str not found in {path_clean}")
            if original.count(old_str) > 1:
                raise ValueError(f"old_str appears {original.count(old_str)} times — be more specific")
            updated = original.replace(old_str, new_str, 1)
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.put(
                    f"{FILES_URL}/file/{path_clean}",
                    content=updated.encode(),
                )
                res.raise_for_status()
                return {"ok": True, "path": path_clean}

    # Code tools go to the code container
    endpoint = TOOL_ROUTES.get(tool)
    if not endpoint:
        raise ValueError(f"Unknown tool: {tool}")
    async with httpx.AsyncClient(timeout=35) as client:
        res = await client.post(f"{CODE_CONTAINER_URL}/{endpoint}", json=args)
        res.raise_for_status()
        return res.json()


@app.post("/tool")
async def run_tool(req: dict):
    """
    Execute a tool call. Called by the frontend after user confirms.
    Returns the tool result to be fed back into the LLM.
    """
    tool = req.get("tool")
    args = req.get("args", {})
    if not tool:
        raise HTTPException(400, "tool is required")
    try:
        result = await execute_tool(tool, args)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class AgentRequest(BaseModel):
    prompt: str
    conversation_id: Optional[str] = None
    always_allow: bool = False


@app.post("/agent")
async def agent_endpoint(req: AgentRequest):
    """
    Server-side agent loop: LLM → tool → LLM … until no more tool calls.
    Streams newline-delimited JSON events to the client:
      {"e":"text",      "d":"clean LLM text"}
      {"e":"tool_start","d":{"tool":…,"args":…,"destructive":…,"reason":…,"token":…|null}}
      {"e":"tool_done", "d":{"ok":true,"result":…} | {"ok":false,"error":…}}
      {"e":"error",     "d":"message"}
      {"e":"done",      "d":{"conv_id":"…"}}
    """
    is_new = req.conversation_id is None

    if is_new:
        conv_id = uuid.uuid4()
        await db_pool.execute(
            "INSERT INTO conversations (id, title) VALUES ($1, armor(pgp_sym_encrypt($2, $3)))",
            conv_id,
            "New conversation",
            DB_ENCRYPTION_KEY,
        )
    else:
        conv_id = uuid.UUID(req.conversation_id)
        exists = await db_pool.fetchval(
            "SELECT id FROM conversations WHERE id = $1", conv_id
        )
        if not exists:
            raise HTTPException(404, "Conversation not found")

    await save_message(conv_id, "user", req.prompt)

    history, memory_snippet = await build_context(conv_id)
    system_content = TOOL_SYSTEM_PROMPT
    if memory_snippet:
        system_content += f"\n\nRelevant earlier context:\n{memory_snippet}"
    active_tools = select_tools_for(req.prompt, allow_delegate=True)
    print(
        f"[tools] conv={conv_id} "
        f"selected {len(active_tools)}/{len(GEMMA_TOOLS)}: "
        f"{[t['function']['name'] for t in active_tools]}"
    )
    messages = [
        {"role": "system", "content": system_content},
    ] + TOOL_EXAMPLE_MESSAGES + history

    async def event_stream():
        loop_messages = list(messages)  # local copy — avoids nonlocal scoping issues
        first_response: str | None = None
        consecutive_errors = 0

        for turn in range(10):
            print(f"[agent] conv={conv_id} turn={turn + 1}")

            # --- accumulate full LLM response (buffered to avoid streaming
            #     raw tool_call markup to the client) ---
            full_text = ""
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream(
                        "POST",
                        f"{LLM_SERVER_URL}/generate",
                        json={
                            "messages": loop_messages,
                            "stream": True,
                            "model": "coder",
                            "tools": active_tools,
                        },
                    ) as res:
                        # Stream Gemma 4 thinking blocks in real-time while buffering the rest.
                        # Gemma 4 uses <|channel>thought\n...\n<channel|> for thinking.
                        _THINK_OPEN = "<|channel>thought"
                        _THINK_CLOSE = "<channel|>"
                        in_think = False
                        think_done = False
                        pending = ""  # lookahead buffer for tag boundary detection
                        async for chunk in res.aiter_text():
                            full_text += chunk
                            if think_done:
                                continue
                            pending += chunk
                            if not in_think:
                                idx = pending.find(_THINK_OPEN)
                                if idx != -1:
                                    in_think = True
                                    yield json.dumps({"e": "thinking_start"}) + "\n"
                                    pending = pending[idx + len(_THINK_OPEN):]
                                elif len(pending) > len(_THINK_OPEN) + 5:
                                    # No opening tag found yet — response has no thinking
                                    think_done = True
                                    continue
                            if in_think:
                                close_idx = pending.find(_THINK_CLOSE)
                                if close_idx != -1:
                                    before = pending[:close_idx]
                                    if before:
                                        yield json.dumps({"e": "thinking", "d": before}) + "\n"
                                    yield json.dumps({"e": "thinking_end"}) + "\n"
                                    in_think = False
                                    think_done = True
                                    pending = ""
                                else:
                                    # Emit safe portion, keep enough back to avoid splitting the tag
                                    safe = len(pending) - len(_THINK_CLOSE)
                                    if safe > 0:
                                        yield json.dumps({"e": "thinking", "d": pending[:safe]}) + "\n"
                                        pending = pending[safe:]
                        # Stream ended — flush any remaining thinking content
                        if in_think and pending:
                            yield json.dumps({"e": "thinking", "d": pending}) + "\n"
                            yield json.dumps({"e": "thinking_end"}) + "\n"
            except Exception as e:
                yield json.dumps({"e": "error", "d": str(e)}) + "\n"
                return

            # Extract Gemma 4 thinking block so we can persist it for reload.
            _think_match = re.search(
                r"<\|channel>thought\s*([\s\S]*?)<channel\|>", full_text
            )
            thinking_text = _think_match.group(1).strip() if _think_match else ""

            # Strip thinking blocks before parsing or displaying
            clean_text = _strip_thinking(full_text)
            preamble, tool_call = _parse_tool_call(clean_text)
            display_text = preamble if tool_call else clean_text

            print(f"[agent]   tool={tool_call.get('tool') if tool_call else None} display_len={len(display_text)}")

            # Emit clean text for this turn
            if display_text.strip():
                yield json.dumps({"e": "text", "d": display_text}) + "\n"

            # Build the persisted assistant content: <think>…</think> + display.
            # Skip saving pure tool-call turns that have no preamble and no
            # thinking (nothing to render on reload).
            saved_parts = []
            if thinking_text:
                saved_parts.append(f"<think>{thinking_text}</think>")
            if display_text.strip():
                saved_parts.append(display_text)
            saved_text = "\n".join(saved_parts)
            if saved_text:
                await save_message(conv_id, "assistant", saved_text)

            if first_response is None:
                first_response = display_text or full_text
                if is_new:
                    asyncio.create_task(generate_title(conv_id, req.prompt, first_response))

            if not tool_call:
                if _looks_like_malformed_tool_call(clean_text):
                    # Feed the format rules back in so the model can self-correct.
                    print(f"[agent]   malformed tool call detected, injecting reminder (turn {turn + 1})")
                    loop_messages.append({"role": "assistant", "content": clean_text})
                    loop_messages.append({"role": "user", "content": _TOOL_FORMAT_REMINDER})
                    yield json.dumps({"e": "text", "d": "(reminded the model of the tool-call format)"}) + "\n"
                    continue
                break  # final answer — done

            # --- confirm destructive tools unless always_allow ---
            tool_name = tool_call.get("tool", "")
            is_destructive = tool_name in DESTRUCTIVE_TOOLS
            needs_confirm = is_destructive and not req.always_allow
            token: str | None = None
            if needs_confirm:
                token = str(uuid.uuid4())
                PENDING_CONFIRMATIONS[token] = {
                    "event": asyncio.Event(),
                    "approved": None,
                }

            tool_args = tool_call.get("args", {})
            tool_reason = preamble.strip() if preamble else ""
            yield (
                json.dumps(
                    {
                        "e": "tool_start",
                        "d": {
                            "tool": tool_name,
                            "args": tool_args,
                            "destructive": is_destructive,
                            "reason": tool_reason,
                            "token": token,
                        },
                    }
                )
                + "\n"
            )
            await save_message(
                conv_id, "assistant", "",
                kind="tool_call",
                metadata={
                    "tool": tool_name,
                    "args": tool_args,
                    "reason": tool_reason,
                    "destructive": is_destructive,
                },
            )

            if needs_confirm:
                try:
                    await asyncio.wait_for(
                        PENDING_CONFIRMATIONS[token]["event"].wait(), timeout=300.0
                    )
                    approved = PENDING_CONFIRMATIONS.pop(token, {}).get(
                        "approved", False
                    )
                except asyncio.TimeoutError:
                    PENDING_CONFIRMATIONS.pop(token, None)
                    approved = False

                if not approved:
                    cancel_msg = f"Tool call {tool_name} was cancelled by the user."
                    yield (
                        json.dumps(
                            {"e": "tool_done", "d": {"ok": False, "error": "Cancelled"}}
                        )
                        + "\n"
                    )
                    # Native format: assistant with tool_calls + tool response
                    loop_messages.append({
                        "role": "assistant",
                        "content": preamble or "",
                        "tool_calls": [{"function": {"name": tool_name, "arguments": tool_call.get("args", {})}}],
                    })
                    loop_messages.append({"role": "tool", "name": tool_name, "content": cancel_msg})
                    await save_message(
                        conv_id, "user", "",
                        kind="tool_result",
                        metadata={"tool": tool_name, "ok": False, "error": "Cancelled by user"},
                    )
                    continue

            # --- execute tool ---
            tool_ok = True
            tool_result_meta: dict
            try:
                result = await execute_tool(
                    tool_name, tool_call.get("args", {})
                )
                consecutive_errors = 0
                yield (
                    json.dumps({"e": "tool_done", "d": {"ok": True, "result": result}})
                    + "\n"
                )
                result_text = json.dumps(result, indent=2)
                if len(result_text) > MAX_TOOL_RESULT_CHARS:
                    result_text = (
                        result_text[:MAX_TOOL_RESULT_CHARS]
                        + f"\n[TRUNCATED at {MAX_TOOL_RESULT_CHARS} chars — for more, call read_file again with start_line/end_line, or narrow the pattern]"
                    )
                tool_result_meta = {"tool": tool_name, "ok": True, "result": result}
            except Exception as e:
                consecutive_errors += 1
                err_str = str(e)
                print(f"[agent]   tool error ({consecutive_errors} consecutive): {err_str}")
                yield (
                    json.dumps({"e": "tool_done", "d": {"ok": False, "error": err_str}})
                    + "\n"
                )
                result_text = (
                    f"Error running {tool_name}: {err_str}\n"
                    "Fix the arguments or try a different tool. Common causes: wrong path case, "
                    "file does not exist, pattern has unescaped regex metacharacters."
                )
                tool_ok = False
                tool_result_meta = {"tool": tool_name, "ok": False, "error": err_str}
                if consecutive_errors >= 3:
                    # Still record this result before giving up, so the UI can show it.
                    await save_message(
                        conv_id, "user", "",
                        kind="tool_result",
                        metadata=tool_result_meta,
                    )
                    yield json.dumps({"e": "error", "d": f"Stopping after 3 consecutive tool errors. Last: {err_str}"}) + "\n"
                    return

            # Feed result back as native Gemma 4 tool call/response
            loop_messages.append({
                "role": "assistant",
                "content": preamble or "",
                "tool_calls": [{"function": {"name": tool_name, "arguments": tool_call.get("args", {})}}],
            })
            loop_messages.append({"role": "tool", "name": tool_name, "content": result_text})
            await save_message(
                conv_id, "user", "",
                kind="tool_result",
                metadata=tool_result_meta,
            )

        yield json.dumps({"e": "done", "d": {"conv_id": str(conv_id)}}) + "\n"

    return StreamingResponse(event_stream(), media_type="text/plain")


@app.post("/agent/confirm/{token}")
async def agent_confirm(token: str, body: dict):
    """Resolve a pending destructive-tool confirmation."""
    entry = PENDING_CONFIRMATIONS.get(token)
    if not entry:
        raise HTTPException(404, "Confirmation token not found or expired")
    entry["approved"] = body.get("approved", False)
    entry["event"].set()
    return {"ok": True}


@app.get("/api/models")
async def proxy_models():
    """Proxy the LLM server's /models endpoint to the frontend."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            res = await client.get(f"{LLM_SERVER_URL}/models")
            return res.json()
        except Exception:
            return {"current": None, "available": {}}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/chat/{conv_id}")
async def chat_conversation_page(conv_id: str):
    """Serve the SPA for direct conversation URLs like /chat/{uuid}."""
    return FileResponse("static/index.html")


@app.get("/search")
async def search_page():
    return FileResponse("static/search.html")


class SearchRequest(BaseModel):
    query: str
    num_results: Optional[int] = 5
    language: Optional[str] = "en"


async def search(req: SearchRequest):
    """Search the web via SearXNG and return clean results."""
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            res = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q": req.query,
                    "format": "json",
                    "lang": req.language,
                },
            )
            res.raise_for_status()
        except httpx.TimeoutException:
            raise HTTPException(504, "Search timed out")
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"SearXNG error: {e.response.status_code}")
        except httpx.RequestError as e:
            raise HTTPException(503, f"Could not reach SearXNG: {e}")

    data = res.json()
    results = data.get("results", [])[: req.num_results]

    return {
        "query": req.query,
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "engine": r.get("engine", ""),
            }
            for r in results
        ],
    }


class SearchChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    stream: bool = True


@app.post("/search-chat")
async def search_chat(req: SearchChatRequest):
    """
    Research agent: LLM plans queries, evaluates results, iterates until satisfied.
    Streams NDJSON events:
      {"e":"searching","d":"status text"}   — status update (query being run, planning, etc.)
      {"e":"sources","d":[...]}             — results from one search
      {"e":"thinking_start"}                — LLM thinking block started
      {"e":"thinking","d":"..."}            — thinking content
      {"e":"thinking_end"}                  — thinking block ended
      {"e":"text","d":"answer markdown"}    — LLM answer for this turn
      {"e":"done","d":{"session_id":"..."}} — finished
      {"e":"error","d":"message"}           — error
    """
    # --- Session setup -------------------------------------------------------
    is_new = req.session_id is None
    if is_new:
        session_id = await db_pool.fetchval(
            "INSERT INTO search_sessions (title) VALUES (armor(pgp_sym_encrypt($1, $2))) RETURNING id",
            req.query[:80], DB_ENCRYPTION_KEY,
        )
    else:
        session_id = uuid.UUID(req.session_id)
        exists = await db_pool.fetchval(
            "SELECT id FROM search_sessions WHERE id = $1", session_id
        )
        if not exists:
            raise HTTPException(404, "Search session not found")

    await db_pool.execute(
        "INSERT INTO search_messages (session_id, role, content) "
        "VALUES ($1, $2, armor(pgp_sym_encrypt($3, $4)))",
        session_id, "user", req.query, DB_ENCRYPTION_KEY,
    )

    # --- Load session history for context ------------------------------------
    history_rows = await db_pool.fetch(
        "SELECT role, pgp_sym_decrypt(dearmor(content), $2) AS content, sources "
        "FROM search_messages WHERE session_id = $1 ORDER BY created_at",
        session_id, DB_ENCRYPTION_KEY,
    )
    # Build prior turns as context messages (exclude the message we just inserted)
    prior_turns = list(history_rows)[:-1]

    async def event_stream():
        all_sources: list[dict] = []

        # Build initial LLM message list: system + prior session turns
        llm_messages = [
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
        ]
        # Replay prior turns as context.
        # Sources are stored on the assistant row — re-attach as a compact
        # reference list so the LLM knows what was cited in earlier answers.
        for row in prior_turns:
            role = row["role"]
            content = row["content"]
            if role == "assistant":
                prior_sources = row["sources"] or []
                if prior_sources:
                    refs = "\n".join(
                        f"[{i+1}] {s.get('title','')} — {s.get('url','')}"
                        for i, s in enumerate(prior_sources)
                    )
                    content = f"{content}\n\nSources used:\n{refs}"
            llm_messages.append({"role": role, "content": content})

        # --- Research loop: LLM → search → LLM → ... → final answer ----------
        llm_messages.append({"role": "user", "content": req.query})
        max_rounds = 5
        _THINK_OPEN = "<|channel>thought\n"
        _THINK_CLOSE = "\n<channel|>"

        for round_num in range(max_rounds):
            # Status: planning or refining
            if round_num == 0:
                yield json.dumps({"e": "searching", "d": "analyzing your question\u2026"}) + "\n"
            else:
                yield json.dumps({"e": "searching", "d": "evaluating results\u2026"}) + "\n"

            # --- Call LLM (stream thinking blocks in real-time) ---------------
            full_text = ""
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream(
                        "POST",
                        f"{LLM_SERVER_URL}/generate",
                        json={"messages": llm_messages, "stream": True},
                    ) as res:
                        in_think = False
                        think_done = False
                        pending = ""
                        async for chunk in res.aiter_text():
                            full_text += chunk
                            if think_done:
                                continue
                            pending += chunk
                            if not in_think:
                                idx = pending.find(_THINK_OPEN)
                                if idx != -1:
                                    in_think = True
                                    yield json.dumps({"e": "thinking_start"}) + "\n"
                                    pending = pending[idx + len(_THINK_OPEN):]
                                elif len(pending) > len(_THINK_OPEN) + 5:
                                    think_done = True
                                    continue
                            if in_think:
                                close_idx = pending.find(_THINK_CLOSE)
                                if close_idx != -1:
                                    before = pending[:close_idx]
                                    if before:
                                        yield json.dumps({"e": "thinking", "d": before}) + "\n"
                                    yield json.dumps({"e": "thinking_end"}) + "\n"
                                    in_think = False
                                    think_done = True
                                    pending = ""
                                else:
                                    safe = len(pending) - len(_THINK_CLOSE)
                                    if safe > 0:
                                        yield json.dumps({"e": "thinking", "d": pending[:safe]}) + "\n"
                                        pending = pending[safe:]
                        if in_think and pending:
                            yield json.dumps({"e": "thinking", "d": pending}) + "\n"
                            yield json.dumps({"e": "thinking_end"}) + "\n"
            except Exception as e:
                yield json.dumps({"e": "error", "d": str(e)}) + "\n"
                return

            clean = _strip_thinking(full_text)
            preamble, queries = _parse_search_queries(clean)

            # --- No queries → this is the final answer -----------------------
            if not queries:
                answer = clean
                yield json.dumps({"e": "text", "d": answer}) + "\n"
                llm_messages.append({"role": "assistant", "content": answer})

                await db_pool.execute(
                    "INSERT INTO search_messages (session_id, role, content, sources) "
                    "VALUES ($1, $2, armor(pgp_sym_encrypt($3, $5)), $4)",
                    session_id, "assistant", answer, all_sources, DB_ENCRYPTION_KEY,
                )
                break

            # --- Execute all planned searches --------------------------------
            llm_messages.append({"role": "assistant", "content": clean})
            round_results: list[dict] = []

            for qi, q in enumerate(queries):
                if qi > 0:
                    await asyncio.sleep(1.5)  # stagger queries to avoid rate limits
                yield json.dumps({"e": "searching", "d": q}) + "\n"
                try:
                    search_data = await search(SearchRequest(query=q, num_results=8))
                except Exception as e:
                    yield json.dumps({"e": "searching", "d": f"search failed: {q}"}) + "\n"
                    continue
                results = search_data.get("results", [])
                round_results.extend(results)
                all_sources.extend(results)
                if results:
                    yield json.dumps({"e": "sources", "d": results}) + "\n"

            # --- Feed results back to LLM ------------------------------------
            if round_results:
                offset = len(all_sources) - len(round_results)
                ctx = _format_results_for_llm(round_results, offset=offset)
                feedback = (
                    f"Search results:\n{ctx}\n\n"
                    f"Evaluate these results. If you have enough information to answer the "
                    f"original question comprehensively, write your final answer now (no "
                    f"<search_queries> tags). Otherwise, output another <search_queries> "
                    f"block with refined queries for the gaps."
                )
            else:
                feedback = (
                    "No results were found for those queries. Try different search terms "
                    "or write your best answer with what you know."
                )
            llm_messages.append({"role": "user", "content": feedback})
        else:
            # Exhausted all rounds — ask for final answer
            llm_messages.append({
                "role": "user",
                "content": "You have used all search rounds. Write your final answer now with the information gathered.",
            })
            # One last LLM call for the answer
            try:
                async with httpx.AsyncClient(timeout=180) as client:
                    resp = await client.post(
                        f"{LLM_SERVER_URL}/generate",
                        json={"messages": llm_messages, "stream": False},
                    )
                    resp.raise_for_status()
                    final = _strip_thinking(resp.json().get("response", ""))
            except Exception as e:
                yield json.dumps({"e": "error", "d": str(e)}) + "\n"
                return
            yield json.dumps({"e": "text", "d": final}) + "\n"
            await db_pool.execute(
                "INSERT INTO search_messages (session_id, role, content, sources) "
                "VALUES ($1, $2, armor(pgp_sym_encrypt($3, $5)), $4)",
                session_id, "assistant", final, all_sources, DB_ENCRYPTION_KEY,
            )

        yield json.dumps({"e": "done", "d": {"session_id": str(session_id)}}) + "\n"

    return StreamingResponse(event_stream(), media_type="text/plain")


# --- Search session CRUD -----------------------------------------------------

@app.get("/search-sessions")
async def list_search_sessions():
    rows = await db_pool.fetch(
        "SELECT id, pgp_sym_decrypt(dearmor(title), $1) AS title, created_at "
        "FROM search_sessions ORDER BY created_at DESC LIMIT 50",
        DB_ENCRYPTION_KEY,
    )
    return [{"id": str(r["id"]), "title": r["title"], "created_at": r["created_at"].isoformat()} for r in rows]


@app.get("/search-sessions/{session_id}/messages")
async def get_search_messages(session_id: str):
    rows = await db_pool.fetch(
        "SELECT role, pgp_sym_decrypt(dearmor(content), $2) AS content, sources "
        "FROM search_messages WHERE session_id = $1 ORDER BY created_at",
        uuid.UUID(session_id), DB_ENCRYPTION_KEY,
    )
    return [{"role": r["role"], "content": r["content"], "sources": r["sources"] or []} for r in rows]


@app.delete("/search-sessions/{session_id}")
async def delete_search_session(session_id: str):
    await db_pool.execute("DELETE FROM search_sessions WHERE id = $1", uuid.UUID(session_id))
    return {"ok": True}


@app.get("/conversations")
async def list_conversations():
    rows = await db_pool.fetch(
        "SELECT id, pgp_sym_decrypt(dearmor(title), $1) AS title, created_at "
        "FROM conversations ORDER BY created_at DESC LIMIT 50",
        DB_ENCRYPTION_KEY,
    )
    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str):
    rows = await db_pool.fetch(
        "SELECT role, COALESCE(kind, role) AS kind, "
        "pgp_sym_decrypt(dearmor(content), $2) AS content, metadata "
        "FROM messages WHERE conversation_id = $1 ORDER BY created_at",
        uuid.UUID(conversation_id), DB_ENCRYPTION_KEY,
    )
    return [
        {
            "role": r["role"],
            "kind": r["kind"],
            "content": r["content"],
            "metadata": r["metadata"],
        }
        for r in rows
    ]


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    await db_pool.execute(
        "DELETE FROM conversations WHERE id = $1", uuid.UUID(conversation_id)
    )
    return {"ok": True}


# --- Notes page & files proxy ------------------------------------------------

@app.get("/notes")
async def notes_page():
    return FileResponse("static/notes.html")


@app.api_route("/api/files/{path:path}", methods=["GET"])
async def proxy_files_list(path: str, request: Request):
    """Proxy GET /api/files/* → files service /files/*"""
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(f"{FILES_URL}/files/{path}", params=dict(request.query_params))
        return Response(content=res.content, status_code=res.status_code,
                        media_type=res.headers.get("content-type", "application/json"))


@app.get("/api/files")
async def proxy_files_root():
    """Proxy GET /api/files → files service /files"""
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(f"{FILES_URL}/files")
        return Response(content=res.content, status_code=res.status_code,
                        media_type=res.headers.get("content-type", "application/json"))


@app.api_route("/api/file/{path:path}", methods=["GET", "PUT", "PATCH", "DELETE"])
async def proxy_file(path: str, request: Request):
    """Proxy /api/file/* → files service /file/*"""
    body = await request.body()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.request(
            method=request.method,
            url=f"{FILES_URL}/file/{path}",
            content=body if body else None,
            params=dict(request.query_params),
        )
        return Response(content=res.content, status_code=res.status_code,
                        media_type=res.headers.get("content-type", "application/octet-stream"))


# --- Routines page -----------------------------------------------------------

@app.get("/routines")
async def routines_page():
    return FileResponse("static/routines.html")


# --- Routines API ------------------------------------------------------------

class RoutineRequest(BaseModel):
    name: str
    schedule: str
    prompt: str
    enabled: bool = True


class RoutinePatch(BaseModel):
    name: Optional[str] = None
    schedule: Optional[str] = None
    prompt: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/api/routines")
async def list_routines():
    rows = await db_pool.fetch(
        "SELECT id, pgp_sym_decrypt(dearmor(name), $1) AS name, schedule, "
        "pgp_sym_decrypt(dearmor(prompt), $1) AS prompt, "
        "enabled, last_run_at, created_at FROM routines ORDER BY created_at DESC",
        DB_ENCRYPTION_KEY,
    )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "schedule": r["schedule"],
            "prompt": r["prompt"],
            "enabled": r["enabled"],
            "last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
            "next_run_at": _next_run(r["id"]),
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@app.post("/api/routines")
async def create_routine(req: RoutineRequest):
    try:
        CronTrigger.from_crontab(req.schedule)
    except Exception:
        raise HTTPException(400, f"Invalid cron expression: '{req.schedule}'")

    routine_id = await db_pool.fetchval(
        "INSERT INTO routines (name, schedule, prompt, enabled) "
        "VALUES (armor(pgp_sym_encrypt($1, $5)), $2, armor(pgp_sym_encrypt($3, $5)), $4) RETURNING id",
        req.name, req.schedule, req.prompt, req.enabled, DB_ENCRYPTION_KEY,
    )
    if req.enabled:
        _schedule_routine(routine_id, req.schedule)
    return {"id": str(routine_id)}


@app.patch("/api/routines/{routine_id}")
async def update_routine(routine_id: str, req: RoutinePatch):
    rid = uuid.UUID(routine_id)
    sets, params = [], [rid, DB_ENCRYPTION_KEY]
    i = 3
    if req.name is not None:
        sets.append(f"name = armor(pgp_sym_encrypt(${i}, $2))")
        params.append(req.name); i += 1
    if req.schedule is not None:
        try:
            CronTrigger.from_crontab(req.schedule)
        except Exception:
            raise HTTPException(400, f"Invalid cron expression: '{req.schedule}'")
        sets.append(f"schedule = ${i}")
        params.append(req.schedule); i += 1
    if req.prompt is not None:
        sets.append(f"prompt = armor(pgp_sym_encrypt(${i}, $2))")
        params.append(req.prompt); i += 1
    if req.enabled is not None:
        sets.append(f"enabled = ${i}")
        params.append(req.enabled); i += 1

    if not sets:
        raise HTTPException(400, "No fields to update")

    await db_pool.execute(
        f"UPDATE routines SET {', '.join(sets)} WHERE id = $1",
        *params,
    )

    row = await db_pool.fetchrow("SELECT schedule, enabled FROM routines WHERE id = $1", rid)
    if row["enabled"]:
        _schedule_routine(rid, row["schedule"])
    else:
        _unschedule_routine(rid)

    return {"ok": True}


@app.delete("/api/routines/{routine_id}")
async def delete_routine(routine_id: str):
    rid = uuid.UUID(routine_id)
    _unschedule_routine(rid)
    await db_pool.execute("DELETE FROM routines WHERE id = $1", rid)
    return {"ok": True}


@app.get("/api/routines/{routine_id}/runs")
async def get_routine_runs(routine_id: str):
    rows = await db_pool.fetch(
        "SELECT id, conversation_id, started_at, finished_at, status, "
        "CASE WHEN output IS NULL THEN NULL ELSE pgp_sym_decrypt(dearmor(output), $2) END AS output, "
        "CASE WHEN error IS NULL THEN NULL ELSE pgp_sym_decrypt(dearmor(error), $2) END AS error "
        "FROM routine_runs WHERE routine_id = $1 ORDER BY started_at DESC LIMIT 20",
        uuid.UUID(routine_id), DB_ENCRYPTION_KEY,
    )
    return [
        {
            "id": str(r["id"]),
            "conversation_id": str(r["conversation_id"]) if r["conversation_id"] else None,
            "started_at": r["started_at"].isoformat(),
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            "status": r["status"],
            "output": r["output"],
            "error": r["error"],
        }
        for r in rows
    ]


@app.post("/api/routines/{routine_id}/run")
async def trigger_routine(routine_id: str):
    rid = uuid.UUID(routine_id)
    exists = await db_pool.fetchval("SELECT id FROM routines WHERE id = $1", rid)
    if not exists:
        raise HTTPException(404, "Routine not found")
    asyncio.create_task(run_routine(rid))
    return {"ok": True}



async def generate_title(conv_id: uuid.UUID, user_msg: str, assistant_msg: str):
    try:
        # Strip thinking blocks before using as title context
        clean_assistant = _strip_thinking(assistant_msg)
        prompt = (
            f"Based on this exchange, generate a short conversation title (max 6 words, no quotes):\n\n"
            f"User: {user_msg[:200]}\n{'Assistant:' if assistant_msg else ''} {clean_assistant[:200]}"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{LLM_SERVER_URL}/generate",
                json={"prompt": prompt, "stream": False, "model": "main"},
            )
            title = res.json().get("response", "").strip().strip('"').strip("'")[:80]
            if title:
                await db_pool.execute(
                    "UPDATE conversations SET title = armor(pgp_sym_encrypt($1, $3)) WHERE id = $2",
                    title, conv_id, DB_ENCRYPTION_KEY,
                )
    except Exception as e:
        print(f"[title] Failed to generate title: {e}")


# --- Entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    module = os.path.splitext(os.path.basename(__file__))[0]
    uvicorn.run(
        f"{module}:app", host="0.0.0.0", port=CHAT_APP_PORT, workers=1, reload=False
    )
