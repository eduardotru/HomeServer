import base64
import hmac as hmac_lib
import json
import mimetypes
import os
from pathlib import Path
from typing import Optional

import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response

load_dotenv()

FILES_PORT = int(os.getenv("FILES_PORT", 9000))
FILES_ROOT = Path(os.getenv("FILES_ROOT") or "/data/files")

_raw_key = os.getenv("FILES_ENCRYPTION_KEY")
if not _raw_key:
    raise RuntimeError("FILES_ENCRYPTION_KEY is required but not set")

try:
    FERNET = Fernet(_raw_key.encode())
    HMAC_KEY = base64.urlsafe_b64decode(_raw_key.encode())  # 32 raw bytes
except Exception as e:
    raise RuntimeError(f"Invalid FILES_ENCRYPTION_KEY: {e}")

INDEX_PATH = FILES_ROOT / ".index"

app = FastAPI()


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def encrypt_bytes(data: bytes) -> bytes:
    return FERNET.encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    try:
        return FERNET.decrypt(data)
    except InvalidToken:
        raise HTTPException(status_code=500, detail="Decryption failed: corrupt data or wrong key")


def logical_to_disk(logical_path: str) -> str:
    """Deterministic HMAC-SHA256(key, path) → 64-char hex filename."""
    return hmac_lib.new(HMAC_KEY, logical_path.encode(), "sha256").hexdigest()


def load_index() -> dict:
    """Returns {logical_path: {"disk": hex, "size": int, "modified": float}}"""
    if not INDEX_PATH.exists():
        return {}
    return json.loads(decrypt_bytes(INDEX_PATH.read_bytes()).decode())


def save_index(index: dict) -> None:
    """Atomic write via temp file + rename."""
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_bytes(encrypt_bytes(json.dumps(index).encode()))
    tmp.rename(INDEX_PATH)


def validate_path(path: str) -> str:
    """Normalize path, reject traversal attempts."""
    parts = [p for p in path.split("/") if p and p not in (".", "..")]
    if not parts:
        raise HTTPException(status_code=400, detail="Invalid path")
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/files")
@app.get("/files/{prefix:path}")
def list_directory(prefix: str = ""):
    prefix_clean = prefix.rstrip("/") if prefix else ""
    index = load_index()
    seen: dict[str, dict] = {}
    for logical_path, meta in index.items():
        if prefix_clean:
            if not logical_path.startswith(prefix_clean + "/"):
                continue
            relative = logical_path[len(prefix_clean) + 1:]
        else:
            relative = logical_path
        parts = relative.split("/")
        name = parts[0]
        if name not in seen:
            if len(parts) == 1:
                seen[name] = {
                    "name": name,
                    "type": "file",
                    "size": meta["size"],
                    "modified": meta["modified"],
                }
            else:
                seen[name] = {"name": name, "type": "directory", "size": None, "modified": None}
    entries = sorted(seen.values(), key=lambda e: e["name"])
    return {"path": prefix_clean, "entries": entries}


@app.head("/file/{path:path}")
def file_metadata(path: str, response: Response):
    path = validate_path(path)
    index = load_index()
    if path not in index:
        raise HTTPException(status_code=404, detail="File not found")
    meta = index[path]
    mime, _ = mimetypes.guess_type(path)
    response.headers["Content-Length"] = str(meta["size"])
    response.headers["Content-Type"] = mime or "application/octet-stream"
    response.headers["Last-Modified"] = str(meta["modified"])


@app.get("/file/{path:path}")
async def read_file(
    path: str,
    request: Request,
    offset: Optional[int] = None,
    length: Optional[int] = None,
):
    path = validate_path(path)
    index = load_index()
    if path not in index:
        raise HTTPException(status_code=404, detail="File not found")

    raw = (FILES_ROOT / index[path]["disk"]).read_bytes()
    data = decrypt_bytes(raw) if raw else b""
    file_size = len(data)
    mime, _ = mimetypes.guess_type(path)
    content_type = mime or "application/octet-stream"

    # HTTP Range header
    range_header = request.headers.get("Range")
    if range_header:
        try:
            range_val = range_header.replace("bytes=", "")
            start_str, end_str = range_val.split("-")
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid Range header")
        if start >= file_size or end >= file_size or start > end:
            raise HTTPException(status_code=416, detail="Range not satisfiable")
        return Response(
            content=data[start:end + 1],
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    # Query param byte range
    if offset is not None or length is not None:
        start = offset or 0
        end = (start + length - 1) if length is not None else (file_size - 1)
        end = min(end, file_size - 1)
        if start >= file_size:
            raise HTTPException(status_code=416, detail="Offset beyond file size")
        return Response(
            content=data[start:end + 1],
            media_type=content_type,
            headers={
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"},
    )


@app.put("/file/{path:path}")
async def write_file(path: str, request: Request):
    path = validate_path(path)
    body = await request.body()
    disk_name = logical_to_disk(path)
    disk_path = FILES_ROOT / disk_name
    disk_path.write_bytes(encrypt_bytes(body))
    index = load_index()
    index[path] = {"disk": disk_name, "size": len(body), "modified": disk_path.stat().st_mtime}
    save_index(index)
    return {"path": path, "written": len(body)}


@app.patch("/file/{path:path}")
async def append_file(path: str, request: Request):
    path = validate_path(path)
    body = await request.body()
    index = load_index()
    disk_name = logical_to_disk(path)
    disk_path = FILES_ROOT / disk_name
    if path in index:
        existing = decrypt_bytes(disk_path.read_bytes())
    else:
        existing = b""
    combined = existing + body
    disk_path.write_bytes(encrypt_bytes(combined))
    index[path] = {"disk": disk_name, "size": len(combined), "modified": disk_path.stat().st_mtime}
    save_index(index)
    return {"path": path, "appended": len(body)}


@app.delete("/file/{path:path}")
def delete_path(path: str, recursive: bool = False):
    path = validate_path(path)
    index = load_index()
    if path in index:
        (FILES_ROOT / index[path]["disk"]).unlink(missing_ok=True)
        del index[path]
        save_index(index)
        return {"deleted": path}
    # Virtual directory deletion
    dir_prefix = path.rstrip("/") + "/"
    children = [k for k in index if k.startswith(dir_prefix)]
    if not children:
        raise HTTPException(status_code=404, detail="Path not found")
    if not recursive:
        raise HTTPException(status_code=400, detail="Directory not empty. Use ?recursive=true")
    for child in children:
        (FILES_ROOT / index[child]["disk"]).unlink(missing_ok=True)
        del index[child]
    save_index(index)
    return {"deleted": path}


if __name__ == "__main__":
    FILES_ROOT.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=FILES_PORT)
