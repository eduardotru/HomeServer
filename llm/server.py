import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from mlx_lm import load, stream_generate
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MAX_TOKENS = int(os.getenv("MAX_TOKENS", 8192))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", 20))

# Available models — key is the alias used in requests
LLM_MODEL = os.getenv("LLM_MODEL", "mlx-community/Qwen3-14B-4bit")

# --- Model state -------------------------------------------------------------
# Only one model is loaded at a time. The swap lock ensures no two requests
# trigger a concurrent swap — requests queue normally and wait for the swap.

model = None
tokenizer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, tokenizer
    model_name = LLM_MODEL
    print(f"[MLX] Loading '{LLM_MODEL}' ({model_name})...")
    model, tokenizer = load(model_name)
    print("[MLX] Model ready.")
    asyncio.create_task(generation_worker())
    yield
    print("[MLX] Shutting down.")


app = FastAPI(title="MLX LLM Server", lifespan=lifespan)


# --- Generation queue --------------------------------------------------------

_queue: asyncio.Queue = None


def get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    return _queue


async def generation_worker():
    """Single worker — pops jobs and runs MLX one at a time."""
    print("[queue] Generation worker started.")
    while True:
        job = await get_queue().get()
        try:
            # Swap model if needed before running the job
            print("[queue] Job starting...")
            await job.run()
        except Exception as e:
            print(f"[queue] Job failed: {e}")
            job.fail(e)
        finally:
            get_queue().task_done()


# --- Job types ---------------------------------------------------------------


class StreamJob:
    def __init__(self, prompt: str, model_alias: str):
        self.prompt = prompt
        self.model_alias = model_alias
        self._token_queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

    async def run(self):
        def generate():
            try:
                for token in stream_generate(
                    model, tokenizer, prompt=self.prompt, max_tokens=MAX_TOKENS
                ):
                    self._loop.call_soon_threadsafe(
                        self._token_queue.put_nowait, token.text
                    )
            finally:
                self._loop.call_soon_threadsafe(self._token_queue.put_nowait, None)

        await self._loop.run_in_executor(None, generate)

    def fail(self, exc: Exception):
        self._loop.call_soon_threadsafe(self._token_queue.put_nowait, None)

    async def tokens(self):
        while True:
            token = await self._token_queue.get()
            if token is None:
                break
            yield token


class CompleteJob:
    def __init__(self, prompt: str, model_alias: str):
        self.prompt = prompt
        self.model_alias = model_alias
        self._future = asyncio.get_event_loop().create_future()

    async def run(self):
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: "".join(
                t.text
                for t in stream_generate(
                    model, tokenizer, prompt=self.prompt, max_tokens=MAX_TOKENS
                )
            ),
        )
        self._future.set_result(result)

    def fail(self, exc: Exception):
        if not self._future.done():
            self._future.set_exception(exc)

    async def result(self) -> str:
        return await self._future


# --- Routes ------------------------------------------------------------------


class PromptRequest(BaseModel):
    prompt: Optional[str] = None
    messages: Optional[list] = None
    stream: bool = False
    model: Optional[str] = None  # alias: "main" | "coder"


def build_prompt(req: PromptRequest) -> str:
    if req.messages:
        messages = req.messages
    elif req.prompt:
        messages = [{"role": "user", "content": req.prompt}]
    else:
        raise HTTPException(400, "Provide either 'prompt' or 'messages'")

    return tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=False,
    )


@app.get("/models")
async def models():
    """List available models and which is currently loaded."""
    return {
        "current": LLM_MODEL,
        "available": {
            "default": LLM_MODEL,
        },
    }


@app.get("/queue")
async def queue_status():
    q = get_queue()
    return {"queued": q.qsize(), "max": MAX_QUEUE_SIZE, "model": LLM_MODEL}

@app.get("/clear")
async def queue_status():
    q = get_queue()
    old_size = q.qsize()
    while not q.empty():
        q.task_done()
    return {"queued": old_size, "max": MAX_QUEUE_SIZE, "model": LLM_MODEL}

@app.post("/generate")
async def generate(req: PromptRequest):
    # Build prompt using the currently loaded tokenizer.
    # If a swap is needed it happens in the worker before generation.
    prompt = build_prompt(req)
    q = get_queue()

    if q.full():
        raise HTTPException(
            503, f"Queue full ({MAX_QUEUE_SIZE} requests). Try again later."
        )

    if req.stream:
        job = StreamJob(prompt, LLM_MODEL)
        await q.put(job)

        async def stream():
            async for token in job.tokens():
                yield token

        return StreamingResponse(
            stream(),
            media_type="text/plain",
            headers={"X-Model": LLM_MODEL},
        )

    job = CompleteJob(prompt, LLM_MODEL)
    await q.put(job)
    result = await job.result()
    return {"response": result, "model": LLM_MODEL}


# --- Entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("LLM_SERVER_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1, reload=False)
