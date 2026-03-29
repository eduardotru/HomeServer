"""
Remote LLM proxy — presents the same /generate API as server.py but
forwards requests to a remote provider.

Configure via environment:
  LLM_REMOTE_PROVIDER  "openai" (default) or "anthropic"
  LLM_REMOTE_BASE_URL  base URL (not needed for anthropic, auto-set)
  LLM_REMOTE_API_KEY   your API key
  LLM_REMOTE_MODEL     e.g. claude-sonnet-4-6 or meta-llama/llama-3.1-8b-instruct
  LLM_SERVER_PORT      port to listen on (default 8000)

OpenAI-compatible providers: OpenRouter, Together, Groq, etc.
  LLM_REMOTE_PROVIDER=openai
  LLM_REMOTE_BASE_URL=https://openrouter.ai/api/v1
  LLM_REMOTE_API_KEY=sk-or-...
  LLM_REMOTE_MODEL=meta-llama/llama-3.1-8b-instruct

Anthropic (Claude):
  LLM_REMOTE_PROVIDER=anthropic
  LLM_REMOTE_API_KEY=sk-ant-...
  LLM_REMOTE_MODEL=claude-sonnet-4-6
"""

import json
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PROVIDER = os.getenv("LLM_REMOTE_PROVIDER", "openai").lower()
REMOTE_BASE_URL = os.getenv("LLM_REMOTE_BASE_URL", "").rstrip("/")
REMOTE_API_KEY = os.getenv("LLM_REMOTE_API_KEY", "")
REMOTE_MODEL = os.getenv("LLM_REMOTE_MODEL", "")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", 8192))

if PROVIDER == "anthropic":
    REMOTE_BASE_URL = "https://api.anthropic.com/v1"
elif not REMOTE_BASE_URL:
    raise RuntimeError("LLM_REMOTE_BASE_URL is required for openai-compatible providers")

if not REMOTE_MODEL:
    raise RuntimeError("LLM_REMOTE_MODEL is required for remote mode")

app = FastAPI(title="LLM Proxy")


class PromptRequest(BaseModel):
    prompt: Optional[str] = None
    messages: Optional[list] = None
    stream: bool = False
    model: Optional[str] = None


def _build_messages(req: PromptRequest) -> list:
    if req.messages:
        return req.messages
    if req.prompt:
        return [{"role": "user", "content": req.prompt}]
    raise HTTPException(400, "Provide either 'prompt' or 'messages'")


@app.get("/models")
async def models():
    return {"current": REMOTE_MODEL, "available": {"default": REMOTE_MODEL}}


@app.get("/queue")
async def queue_status():
    return {"queued": 0, "max": 0, "model": REMOTE_MODEL}


# --- OpenAI-compatible (OpenRouter, Groq, Together, etc.) --------------------

def _openai_headers() -> dict:
    return {
        "Authorization": f"Bearer {REMOTE_API_KEY}",
        "Content-Type": "application/json",
    }


async def _openai_stream(messages: list):
    payload = {"model": REMOTE_MODEL, "messages": messages, "stream": True}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{REMOTE_BASE_URL}/chat/completions",
            headers=_openai_headers(),
            json=payload,
        ) as res:
            if res.status_code != 200:
                body = await res.aread()
                raise HTTPException(res.status_code, body.decode())
            async for line in res.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    content = chunk["choices"][0]["delta"].get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


async def _openai_complete(messages: list) -> str:
    payload = {"model": REMOTE_MODEL, "messages": messages, "stream": False}
    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(
            f"{REMOTE_BASE_URL}/chat/completions",
            headers=_openai_headers(),
            json=payload,
        )
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    return res.json()["choices"][0]["message"]["content"]


# --- Anthropic (Claude) -------------------------------------------------------

def _anthropic_headers() -> dict:
    return {
        "x-api-key": REMOTE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }


def _anthropic_payload(messages: list, stream: bool) -> dict:
    # Anthropic requires alternating user/assistant turns and no system role in messages.
    # Pull out a leading system message if present.
    system = None
    filtered = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            filtered.append(m)
    payload = {
        "model": REMOTE_MODEL,
        "messages": filtered,
        "max_tokens": MAX_TOKENS,
        "stream": stream,
    }
    if system:
        payload["system"] = system
    return payload


async def _anthropic_stream(messages: list):
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{REMOTE_BASE_URL}/messages",
            headers=_anthropic_headers(),
            json=_anthropic_payload(messages, stream=True),
        ) as res:
            if res.status_code != 200:
                body = await res.aread()
                raise HTTPException(res.status_code, body.decode())
            async for line in res.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                    if event.get("type") == "content_block_delta":
                        text = event.get("delta", {}).get("text", "")
                        if text:
                            yield text
                except (json.JSONDecodeError, KeyError):
                    continue


async def _anthropic_complete(messages: list) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(
            f"{REMOTE_BASE_URL}/messages",
            headers=_anthropic_headers(),
            json=_anthropic_payload(messages, stream=False),
        )
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    data = res.json()
    return data["content"][0]["text"]


# --- Route -------------------------------------------------------------------

@app.post("/generate")
async def generate(req: PromptRequest):
    messages = _build_messages(req)

    if req.stream:
        if PROVIDER == "anthropic":
            gen = _anthropic_stream(messages)
        else:
            gen = _openai_stream(messages)
        return StreamingResponse(gen, media_type="text/plain", headers={"X-Model": REMOTE_MODEL})

    if PROVIDER == "anthropic":
        content = await _anthropic_complete(messages)
    else:
        content = await _openai_complete(messages)

    return {"response": content, "model": REMOTE_MODEL}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("LLM_SERVER_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)
