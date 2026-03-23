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
AVAILABLE_MODELS = {
    "main": os.getenv("LLM_MODEL", "mlx-community/Qwen3-8B-4bit"),
    "coder": os.getenv("LLM_CODER_MODEL", "mlx-community/Llama-3.1-8B-Instruct-4bit"),
}

DEFAULT_MODEL = "coder"

# --- Model state -------------------------------------------------------------
# Only one model is loaded at a time. The swap lock ensures no two requests
# trigger a concurrent swap — requests queue normally and wait for the swap.

model = None
tokenizer = None
current_alias = None
_swap_lock = asyncio.Lock()


async def ensure_model(alias: str):
    """Load the requested model, swapping out the current one if needed."""
    global model, tokenizer, current_alias

    if alias not in AVAILABLE_MODELS:
        raise ValueError(
            f"Unknown model alias '{alias}'. Available: {list(AVAILABLE_MODELS)}"
        )

    if alias == current_alias:
        return  # already loaded, nothing to do

    async with _swap_lock:
        if alias == current_alias:
            return  # another coroutine swapped while we waited

        model_name = AVAILABLE_MODELS[alias]
        print(f"[MLX] Swapping from '{current_alias}' → '{alias}' ({model_name})...")

        # Unload current model
        model = None
        tokenizer = None

        # Load new model — runs in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        new_model, new_tokenizer = await loop.run_in_executor(
            None, lambda: load(model_name)
        )

        model = new_model
        tokenizer = new_tokenizer
        current_alias = alias
        print(f"[MLX] '{alias}' ready.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, tokenizer, current_alias
    alias = DEFAULT_MODEL
    model_name = AVAILABLE_MODELS[alias]
    print(f"[MLX] Loading '{alias}' ({model_name})...")
    model, tokenizer = load(model_name)
    current_alias = alias
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
            await ensure_model(job.model_alias)
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
        "current": current_alias,
        "available": {
            alias: {"model": name, "loaded": alias == current_alias}
            for alias, name in AVAILABLE_MODELS.items()
        },
    }


@app.get("/queue")
async def queue_status():
    q = get_queue()
    return {"queued": q.qsize(), "max": MAX_QUEUE_SIZE, "model": current_alias}


@app.post("/generate")
async def generate(req: PromptRequest):
    alias = req.model or DEFAULT_MODEL
    if alias not in AVAILABLE_MODELS:
        raise HTTPException(
            400, f"Unknown model '{alias}'. Choose from: {list(AVAILABLE_MODELS)}"
        )

    # Build prompt using the currently loaded tokenizer.
    # If a swap is needed it happens in the worker before generation.
    prompt = build_prompt(req)
    q = get_queue()

    if q.full():
        raise HTTPException(
            503, f"Queue full ({MAX_QUEUE_SIZE} requests). Try again later."
        )

    if req.stream:
        job = StreamJob(prompt, alias)
        await q.put(job)

        async def stream():
            async for token in job.tokens():
                yield token

        return StreamingResponse(
            stream(),
            media_type="text/plain",
            headers={"X-Model": alias},
        )

    job = CompleteJob(prompt, alias)
    await q.put(job)
    result = await job.result()
    return {"response": result, "model": alias}


# --- Entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("LLM_SERVER_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1, reload=False)
