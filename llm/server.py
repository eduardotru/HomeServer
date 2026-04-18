import asyncio
import os
import time
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
JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", 300))  # seconds before a stuck job is killed

LLM_MODEL = os.getenv("LLM_MODEL", "mlx-community/gemma-4-e4b-it-4bit")

# --- Model state -------------------------------------------------------------

model = None
tokenizer = None

# --- Worker stats (single worker → no locks needed) -------------------------

_active_job: Optional["StreamJob | CompleteJob"] = None
_active_job_start: Optional[float] = None
_stats = {"completed": 0, "failed": 0, "cancelled": 0}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, tokenizer
    print(f"[MLX] Loading '{LLM_MODEL}'...")
    model, tokenizer = load(LLM_MODEL)
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
    global _active_job, _active_job_start
    print("[queue] Generation worker started.")
    while True:
        job = await get_queue().get()
        _active_job = job
        _active_job_start = time.monotonic()
        try:
            if job.cancelled:
                # Job was cancelled while waiting in queue — skip it
                _stats["cancelled"] += 1
                print(f"[queue] Job {job.job_id} skipped (cancelled before start).")
            else:
                print(f"[queue] Job {job.job_id} starting...")
                await asyncio.wait_for(job.run(), timeout=JOB_TIMEOUT)
                _stats["completed"] += 1
                elapsed = time.monotonic() - _active_job_start
                print(f"[queue] Job {job.job_id} done in {elapsed:.1f}s.")
        except asyncio.TimeoutError:
            _stats["failed"] += 1
            print(f"[queue] Job {job.job_id} timed out after {JOB_TIMEOUT}s.")
            job.fail(RuntimeError(f"Generation timed out after {JOB_TIMEOUT}s"))
        except Exception as e:
            _stats["failed"] += 1
            print(f"[queue] Job {job.job_id} failed: {e}")
            job.fail(e)
        finally:
            _active_job = None
            _active_job_start = None
            get_queue().task_done()


# --- Job types ---------------------------------------------------------------


class StreamJob:
    def __init__(self, prompt: str, job_id: str):
        self.prompt = prompt
        self.job_id = job_id
        self.cancelled = False
        self._token_queue: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

    def cancel(self):
        """Signal the generation thread to stop at the next token boundary."""
        self.cancelled = True
        # Unblock any awaiter in .tokens() that's waiting for the next token
        self._loop.call_soon_threadsafe(self._token_queue.put_nowait, None)

    async def run(self):
        def generate():
            try:
                for token in stream_generate(
                    model, tokenizer, prompt=self.prompt, max_tokens=MAX_TOKENS
                ):
                    if self.cancelled:
                        break
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
    def __init__(self, prompt: str, job_id: str):
        self.prompt = prompt
        self.job_id = job_id
        self.cancelled = False
        self._future = asyncio.get_event_loop().create_future()

    def cancel(self):
        self.cancelled = True
        if not self._future.done():
            self._future.cancel()

    async def run(self):
        if self.cancelled:
            return
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
        if not self._future.done():
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
    model: Optional[str] = None
    tools: Optional[list] = None
    enable_thinking: bool = False


def build_prompt(req: PromptRequest) -> str:
    if req.messages:
        messages = req.messages
    elif req.prompt:
        messages = [{"role": "user", "content": req.prompt}]
    else:
        raise HTTPException(400, "Provide either 'prompt' or 'messages'")

    kwargs = {"add_generation_prompt": True, "tokenize": False}
    if req.tools:
        kwargs["tools"] = req.tools
    if req.enable_thinking:
        kwargs["enable_thinking"] = True

    return tokenizer.apply_chat_template(messages, **kwargs)


@app.get("/models")
async def get_models():
    return {"current": LLM_MODEL, "available": {"default": LLM_MODEL}}


@app.get("/queue")
async def queue_status():
    q = get_queue()
    active = None
    if _active_job is not None and _active_job_start is not None:
        active = {
            "job_id": _active_job.job_id,
            "elapsed_s": round(time.monotonic() - _active_job_start, 1),
            "cancelled": _active_job.cancelled,
        }
    return {
        "queued": q.qsize(),
        "max": MAX_QUEUE_SIZE,
        "model": LLM_MODEL,
        "active": active,
        "stats": _stats,
    }


@app.post("/clear")
async def clear_queue():
    """Cancel the active job and drain all pending jobs from the queue."""
    q = get_queue()
    cancelled = 0

    # Cancel whatever is currently running
    if _active_job is not None:
        _active_job.cancel()
        cancelled += 1

    # Drain pending jobs
    while not q.empty():
        try:
            job = q.get_nowait()
            job.cancel()
            q.task_done()
            cancelled += 1
        except asyncio.QueueEmpty:
            break

    _stats["cancelled"] += cancelled
    print(f"[queue] Cleared {cancelled} job(s).")
    return {"cancelled": cancelled, "model": LLM_MODEL}


@app.post("/generate")
async def generate(req: PromptRequest):
    prompt = build_prompt(req)
    q = get_queue()

    if q.full():
        raise HTTPException(503, f"Queue full ({MAX_QUEUE_SIZE} requests). Try again later.")

    import uuid
    job_id = str(uuid.uuid4())[:8]

    if req.stream:
        job = StreamJob(prompt, job_id)
        await q.put(job)

        async def stream():
            try:
                async for token in job.tokens():
                    yield token
            except GeneratorExit:
                # Client disconnected — cancel generation so the worker thread
                # stops at the next token boundary instead of running to MAX_TOKENS
                print(f"[queue] Job {job_id} client disconnected, cancelling.")
                job.cancel()
                _stats["cancelled"] += 1

        return StreamingResponse(
            stream(),
            media_type="text/plain",
            headers={"X-Model": LLM_MODEL, "X-Job-Id": job_id},
        )

    job = CompleteJob(prompt, job_id)
    await q.put(job)
    result = await job.result()
    return {"response": result, "model": LLM_MODEL, "job_id": job_id}


# --- Entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("LLM_SERVER_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1, reload=False)
