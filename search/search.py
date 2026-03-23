import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")
SEARCH_APP_PORT = int(os.getenv("SEARCH_APP_PORT", 7000))

app = FastAPI(title="Search Service")


class SearchRequest(BaseModel):
    query: str
    num_results: Optional[int] = 5
    language: Optional[str] = "en"


@app.get("/health")
async def health():
    return {"status": "ok", "searxng": SEARXNG_URL}


@app.post("/search")
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
                    "engines": "google,duckduckgo,bing",
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


if __name__ == "__main__":
    import uvicorn

    module = os.path.splitext(os.path.basename(__file__))[0]
    uvicorn.run(f"{module}:app", host="0.0.0.0", port=SEARCH_APP_PORT, reload=False)
