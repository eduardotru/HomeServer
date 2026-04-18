"""
Local MLX embedding service.

Exposes POST /embed for generating text embeddings.
Used by chat.py for semantic context retrieval (pgvector).

Configure via environment:
  EMBED_MODEL      HuggingFace model ID (default: mlx-community/embeddinggemma-300m-4bit)
  LLM_EMBED_PORT   port to listen on (default: 8010)

nomic-embed-text-v1.5 notes:
  - 768 dimensions, ~274 MB on disk
  - Expects task prefixes: "search_query: " for queries, "search_document: " for storage
  - Downloaded automatically from HuggingFace on first run
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Union

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

EMBED_MODEL = os.getenv("EMBED_MODEL", "mlx-community/embeddinggemma-300m-4bit")
EMBED_PORT = int(os.getenv("LLM_EMBED_PORT", 8010))
EMBED_DIMENSIONS = 768  # nomic-embed-text-v1.5

model = None
tokenizer = None
_embed_lock = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, tokenizer, _embed_lock
    from mlx_embeddings import load
    _embed_lock = asyncio.Lock()
    print(f"[embed] Loading '{EMBED_MODEL}'...")
    model, tokenizer = load(EMBED_MODEL)
    print(f"[embed] Model ready ({EMBED_DIMENSIONS}d).")
    yield
    print("[embed] Shutting down.")


app = FastAPI(title="MLX Embedding Service", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: Union[str, list[str]]
    prefix: str = ""  # e.g. "search_query: " or "search_document: "


@app.get("/health")
async def health():
    return {"status": "ok", "model": EMBED_MODEL, "dimensions": EMBED_DIMENSIONS}


@app.post("/embed")
async def embed(req: EmbedRequest):
    texts = [req.texts] if isinstance(req.texts, str) else req.texts
    if req.prefix:
        texts = [req.prefix + t for t in texts]

    loop = asyncio.get_event_loop()

    def _run():
        # mlx_embeddings.generate() unpacks tokenizer output as **kwargs which
        # breaks embeddinggemma whose __call__ signature is (inputs, attention_mask=...).
        # Call the model directly to avoid the mismatch.
        inputs = tokenizer(
            texts,
            return_tensors="mlx",
            padding=True,
            truncation=True,
            max_length=512,
        )
        output = model(inputs["input_ids"], attention_mask=inputs.get("attention_mask"))
        # output.text_embeds is an mlx array — convert via numpy for JSON safety
        return np.array(output.text_embeds).tolist()

    try:
        async with _embed_lock:  # serialize — MLX can't handle concurrent model calls
            embeddings = await loop.run_in_executor(None, _run)
    except Exception as e:
        raise HTTPException(500, f"Embedding failed: {e}")

    return {
        "embeddings": embeddings,
        "model": EMBED_MODEL,
        "dimensions": len(embeddings[0]) if embeddings else EMBED_DIMENSIONS,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=EMBED_PORT, workers=1)
