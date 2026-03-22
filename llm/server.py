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

# --- Model loading -----------------------------------------------------------

model = None
tokenizer = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, tokenizer
    model_name = os.getenv("LLM_MODEL", "mlx-community/Qwen2.5-7B-Instruct-4bit")
    print(f"[MLX] Loading {model_name}...")
    model, tokenizer = load(model_name)
    print("[MLX] Model ready.")
    # Start the generation worker
    asyncio.create_task(generation_worker())
    yield
    print("[MLX] Shutting down.")


app = FastAPI(title="MLX LLM Server", lifespan=lifespan)


# --- Generation queue --------------------------------------------------------
#
# All generate requests are placed on a single asyncio.Queue.
# A single worker coroutine pops them one at a time and runs MLX.
# This guarantees only one Metal operation runs at a time, while
# allowing any number of requests to queue up without being rejected.
#
# Each request carries a Future that gets resolved with the result,
# or an async generator that yields tokens as they arrive.

MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", 20))
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
            await job.run()
        except Exception as e:
            print(f"[queue] Job failed: {e}")
            job.fail(e)
        finally:
            get_queue().task_done()


# --- Job types ---------------------------------------------------------------


class StreamJob:
    """A streaming generation job. Yields tokens via an async queue."""

    def __init__(self, prompt: str):
        self.prompt = prompt
        self._token_queue: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

    async def run(self):
        def generate():
            try:
                for token in stream_generate(
                    model, tokenizer, prompt=self.prompt, max_tokens=1024
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
    """A non-streaming generation job. Resolves a Future with the full response."""

    def __init__(self, prompt: str):
        self.prompt = prompt
        self._future: asyncio.Future = asyncio.get_event_loop().create_future()

    async def run(self):
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: "".join(
                token.text
                for token in stream_generate(
                    model, tokenizer, prompt=self.prompt, max_tokens=1024
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


@app.get("/queue")
async def queue_status():
    """How many jobs are currently waiting."""
    q = get_queue()
    return {"queued": q.qsize(), "max": MAX_QUEUE_SIZE}


@app.post("/generate")
async def generate(req: PromptRequest):
    prompt = build_prompt(req)
    q = get_queue()

    if q.full():
        raise HTTPException(
            503, f"Queue full ({MAX_QUEUE_SIZE} requests). Try again later."
        )

    if req.stream:
        job = StreamJob(prompt)
        await q.put(job)

        async def stream():
            async for token in job.tokens():
                yield token

        return StreamingResponse(stream(), media_type="text/plain")

    job = CompleteJob(prompt)
    await q.put(job)
    result = await job.result()
    return {"response": result}


# --- Entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("LLM_SERVER_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1, reload=False)
