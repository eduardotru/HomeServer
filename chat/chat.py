import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# --- Config ------------------------------------------------------------------

LLM_SERVER_URL = os.getenv("LLM_SERVER_URL", "http://localhost:8000")
CHAT_APP_PORT = int(os.getenv("CHAT_APP_PORT", 5000))
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://localai:localai@localhost:5432/localai"
)
CODE_CONTAINER_URL = os.getenv("CODE_CONTAINER_URL", "http://localhost:8002")
SEARCH_APP_URL = os.getenv("SEARCH_APP_URL", "http://localhost:8003")

# Compaction thresholds.
# Rough estimate: 1 token ≈ 4 chars. Qwen2.5-7B has a 32k context window.
# We target staying under 20k tokens to leave room for the response.
COMPACTION_CHAR_THRESHOLD = 20_000 * 4  # ~20k tokens → trigger summarisation
RECENT_MESSAGES_TO_KEEP = 6  # always keep the last N messages verbatim

# --- Database ----------------------------------------------------------------

db_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    print("[DB] Connected to Postgres.")
    yield
    await db_pool.close()
    print("[DB] Disconnected.")


app = FastAPI(title="Chat App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Models ------------------------------------------------------------------


class ChatRequest(BaseModel):
    prompt: str
    conversation_id: Optional[str] = None
    agent_mode: bool = False


# --- Context compaction ------------------------------------------------------


def estimate_chars(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages)


async def build_context(conv_id: uuid.UUID) -> list[dict]:
    """
    Build the message list to send to the LLM.

    Strategy:
    1. Fetch full history + any stored summary from DB.
    2. If total chars < threshold → send everything as-is.
    3. If total chars >= threshold → keep the stored summary (if any) +
       the most recent RECENT_MESSAGES_TO_KEEP messages, then trigger a
       background summarisation of what was dropped.
    """
    row = await db_pool.fetchrow(
        "SELECT summary FROM conversations WHERE id = $1", conv_id
    )
    summary = row["summary"] if row else None

    history = await db_pool.fetch(
        "SELECT role, content FROM messages WHERE conversation_id = $1 ORDER BY created_at",
        conv_id,
    )
    all_messages = [{"role": r["role"], "content": r["content"]} for r in history]

    # Build candidate context: optional summary block + full history
    context = []
    if summary:
        context.append(
            {"role": "user", "content": f"[Summary of earlier conversation: {summary}]"}
        )
        context.append(
            {
                "role": "assistant",
                "content": "Understood, I have the context from earlier.",
            }
        )
    context.extend(all_messages)

    if estimate_chars(context) < COMPACTION_CHAR_THRESHOLD:
        return context

    # Over threshold — keep summary preamble + recent messages only
    recent = all_messages[-RECENT_MESSAGES_TO_KEEP:]
    older = all_messages[:-RECENT_MESSAGES_TO_KEEP]

    compacted = []
    if summary:
        compacted.append(
            {"role": "user", "content": f"[Summary of earlier conversation: {summary}]"}
        )
        compacted.append(
            {
                "role": "assistant",
                "content": "Understood, I have the context from earlier.",
            }
        )
    compacted.extend(recent)

    # Trigger background summarisation of the dropped messages
    if older:
        asyncio.create_task(update_summary(conv_id, summary, older))

    print(
        f"[compaction] Dropped {len(older)} messages, kept {len(recent)} recent + summary."
    )
    return compacted


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
                json={"prompt": prompt, "stream": False},
            )
            new_summary = res.json().get("response", "").strip()
            if new_summary:
                await db_pool.execute(
                    "UPDATE conversations SET summary = $1 WHERE id = $2",
                    new_summary,
                    conv_id,
                )
                print(f"[compaction] Summary updated for {conv_id}.")
    except Exception as e:
        print(f"[compaction] Failed to update summary: {e}")


# --- Tool system prompt ------------------------------------------------------

TOOL_SYSTEM_PROMPT = """You are an AI coding assistant with access to tools that let you read, write, and run code in the user's project.

The workspace root contains the full HomeServer project:
- chat/       — chat app (FastAPI, serves the UI)
- llm/        — LLM inference server (MLX)
- code/       — this code container
- postgres/   — SQL schema
- .env        — service URLs and ports (read-only, do not modify)
- Makefile    — platform management

You can create new top-level directories for new services (e.g. new-service/).
Do not modify .env or Makefile unless explicitly asked.

When you want to use a tool, respond with a JSON block in this exact format:
<tool_call>
{
  "tool": "read_file" | "write_file" | "run_command" | "list_directory",
  "args": { ... },
  "destructive": true | false,
  "reason": "brief explanation of what you're doing"
}
</tool_call>

IMPORTANT: Always close the tag with </tool_call> (with a forward slash). Never repeat <tool_call> as a closing tag.

Tool schemas:
- list_directory: { "path": "relative/path" }
- read_file:      { "path": "relative/path/to/file" }
- write_file:     { "path": "relative/path/to/file", "content": "full file content" }
- run_command:    { "command": "shell command", "working_dir": "optional/path" }

Mark destructive=true for write_file and any run_command that modifies state.
Mark destructive=false for list_directory, read_file, and read-only commands (ls, cat, grep, find).

After receiving a tool result, continue your response naturally based on what you found.
You can chain multiple tool calls to accomplish a task — read first, then write.
Always show the user what you're doing and why."""


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
    "run_command": "run",
}


async def execute_tool(tool: str, args: dict) -> dict:
    """Route a tool call to the code container."""
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


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/search")
async def search_page():
    return FileResponse("static/search.html")


class SearchChatRequest(BaseModel):
    query: str
    stream: bool = True


@app.post("/search-chat")
async def search_chat(req: SearchChatRequest):
    """
    Search the web for the query, inject results as context,
    then stream an LLM response grounded in the search results.
    """
    # 1. Fetch search results
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                f"{SEARCH_APP_URL}/search",
                json={"query": req.query, "num_results": 5},
            )
            res.raise_for_status()
            search_data = res.json()
    except Exception as e:
        raise HTTPException(502, f"Search failed: {e}")

    results = search_data.get("results", [])

    # 2. Format results as context for the LLM
    if results:
        context = "\n\n".join(
            f"[{i + 1}] {r['title']}\nURL: {r['url']}\n{r['snippet']}"
            for i, r in enumerate(results)
            if r.get("snippet")
        )
        prompt = (
            f"Using the following search results, answer this query: {req.query}\n\n"
            f"Search results:\n{context}\n\n"
            f"Provide a clear, well-structured answer based on the search results. "
            f"Cite sources by number where relevant."
        )
    else:
        prompt = (
            f"No search results were found for: {req.query}\n"
            f"Answer based on your training knowledge and note that no web results were available."
        )

    # 3. Stream LLM response
    messages = [{"role": "user", "content": prompt}]

    async def stream():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{LLM_SERVER_URL}/generate",
                json={"messages": messages, "stream": True},
            ) as llm_res:
                async for chunk in llm_res.aiter_text():
                    yield chunk

    # Also return the search results as a header for the UI to display
    return StreamingResponse(
        stream(),
        media_type="text/plain",
        headers={
            "X-Search-Results": __import__("json").dumps(results),
        },
    )


@app.get("/conversations")
async def list_conversations():
    rows = await db_pool.fetch(
        "SELECT id, title, created_at FROM conversations ORDER BY created_at DESC LIMIT 50"
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
        "SELECT role, content FROM messages WHERE conversation_id = $1 ORDER BY created_at",
        uuid.UUID(conversation_id),
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    await db_pool.execute(
        "DELETE FROM conversations WHERE id = $1", uuid.UUID(conversation_id)
    )
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest):
    is_new = req.conversation_id is None

    if is_new:
        conv_id = uuid.uuid4()
        await db_pool.execute(
            "INSERT INTO conversations (id, title) VALUES ($1, $2)",
            conv_id,
            "New conversation",
        )
    else:
        conv_id = uuid.UUID(req.conversation_id)
        exists = await db_pool.fetchval(
            "SELECT id FROM conversations WHERE id = $1", conv_id
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Conversation not found")

    # Save user message
    await db_pool.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)",
        conv_id,
        "user",
        req.prompt,
    )

    # Build compacted context for the LLM
    messages = await build_context(conv_id)

    # Inject tool system prompt as first message in agent mode
    if req.agent_mode:
        messages = [
            {"role": "user", "content": TOOL_SYSTEM_PROMPT},
            {
                "role": "assistant",
                "content": "Understood. I'll use the tools to help you.",
            },
        ] + messages

    async def stream():
        full_response = []

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{LLM_SERVER_URL}/generate",
                json={"messages": messages, "stream": True},
            ) as res:
                async for chunk in res.aiter_text():
                    full_response.append(chunk)
                    yield chunk

        # Strip the conversation ID trailer before saving
        raw = "".join(full_response)
        trailer_idx = raw.find("\n__CONV_ID__")
        assistant_content = raw[:trailer_idx] if trailer_idx != -1 else raw

        await db_pool.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES ($1, $2, $3)",
            conv_id,
            "assistant",
            assistant_content,
        )

        if is_new:
            asyncio.create_task(generate_title(conv_id, req.prompt, assistant_content))

        yield f"\n__CONV_ID__{conv_id}__END__"

    return StreamingResponse(
        stream(),
        media_type="text/plain",
        headers={"X-Conversation-Id": str(conv_id)},
    )


async def generate_title(conv_id: uuid.UUID, user_msg: str, assistant_msg: str):
    try:
        import re

        # Strip thinking blocks before using as title context
        clean_assistant = re.sub(r"<think>[\s\S]*?</think>", "", assistant_msg).strip()
        prompt = (
            f"Based on this exchange, generate a short conversation title (max 6 words, no quotes):\n\n"
            f"User: {user_msg[:200]}\nAssistant: {clean_assistant[:200]}"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                f"{LLM_SERVER_URL}/generate",
                json={"prompt": prompt, "stream": False},
            )
            title = res.json().get("response", "").strip().strip('"').strip("'")[:80]
            if title:
                await db_pool.execute(
                    "UPDATE conversations SET title = $1 WHERE id = $2", title, conv_id
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
