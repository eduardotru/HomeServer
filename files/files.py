import mimetypes
import os
import shutil
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response

load_dotenv()

FILES_PORT = int(os.getenv("FILES_PORT", 9000))
FILES_ROOT = Path(os.getenv("FILES_ROOT") or "/data/files")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 100 * 1024 * 1024))  # 100 MB default

app = FastAPI()


def _resolve(path: str) -> Path:
    """Validate a logical path and map it to an absolute disk path under FILES_ROOT.

    Rejects `..` traversal, empty segments, and absolute paths. The returned
    Path may not exist yet — callers decide based on operation.
    """
    parts = [p for p in path.split("/") if p and p not in (".", "..")]
    if not parts:
        raise HTTPException(status_code=400, detail="Invalid path")
    resolved = (FILES_ROOT / "/".join(parts)).resolve()
    # Extra defense: resolved path must remain under FILES_ROOT.
    root = FILES_ROOT.resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(status_code=400, detail="Invalid path")
    return resolved


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/files")
@app.get("/files/{prefix:path}")
def list_directory(prefix: str = ""):
    target = FILES_ROOT if not prefix else _resolve(prefix)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        st = child.stat()
        if child.is_file():
            entries.append({
                "name": child.name,
                "type": "file",
                "size": st.st_size,
                "modified": st.st_mtime,
            })
        elif child.is_dir():
            entries.append({
                "name": child.name,
                "type": "directory",
                "size": None,
                "modified": None,
            })
    return {"path": prefix.rstrip("/"), "entries": entries}


@app.head("/file/{path:path}")
def file_metadata(path: str, response: Response):
    disk = _resolve(path)
    if not disk.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    st = disk.stat()
    mime, _ = mimetypes.guess_type(path)
    response.headers["Content-Length"] = str(st.st_size)
    response.headers["Content-Type"] = mime or "application/octet-stream"
    response.headers["Last-Modified"] = str(st.st_mtime)


@app.get("/file/{path:path}")
async def read_file(
    path: str,
    request: Request,
    offset: Optional[int] = None,
    length: Optional[int] = None,
):
    disk = _resolve(path)
    if not disk.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    data = disk.read_bytes()
    file_size = len(data)
    mime, _ = mimetypes.guess_type(path)
    content_type = mime or "application/octet-stream"

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
    disk = _resolve(path)
    body = await request.body()
    if len(body) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_FILE_SIZE} bytes)")
    if disk.exists() and disk.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(body)
    return {"path": path, "written": len(body)}


@app.patch("/file/{path:path}")
async def append_file(path: str, request: Request):
    disk = _resolve(path)
    body = await request.body()
    if disk.exists() and disk.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")
    existing_size = disk.stat().st_size if disk.is_file() else 0
    if existing_size + len(body) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_FILE_SIZE} bytes)")
    disk.parent.mkdir(parents=True, exist_ok=True)
    with disk.open("ab") as f:
        f.write(body)
    return {"path": path, "appended": len(body)}


@app.delete("/file/{path:path}")
async def delete_path(path: str, recursive: bool = False):
    disk = _resolve(path)
    if disk.is_file():
        disk.unlink()
        return {"deleted": path}
    if disk.is_dir():
        if not any(disk.iterdir()):
            disk.rmdir()
            return {"deleted": path}
        if not recursive:
            raise HTTPException(status_code=400, detail="Directory not empty. Use ?recursive=true")
        shutil.rmtree(disk)
        return {"deleted": path}
    raise HTTPException(status_code=404, detail="Path not found")


if __name__ == "__main__":
    FILES_ROOT.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=FILES_PORT)
