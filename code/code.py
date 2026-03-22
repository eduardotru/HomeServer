import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

WORKSPACE = Path(os.getenv("WORKSPACE", "/workspace"))

app = FastAPI(title="Code Container")


# --- Safety ------------------------------------------------------------------

SAFE_READ_ONLY_COMMANDS = {
    "ls",
    "cat",
    "find",
    "grep",
    "head",
    "tail",
    "wc",
    "echo",
    "pwd",
    "diff",
    "tree",
}


def is_safe_path(path: str) -> bool:
    """Ensure the path stays inside /workspace."""
    try:
        resolved = (WORKSPACE / path.lstrip("/")).resolve()
        return resolved.is_relative_to(WORKSPACE)
    except Exception:
        return False


def is_readonly_command(cmd: str) -> bool:
    first = cmd.strip().split()[0] if cmd.strip() else ""
    return first in SAFE_READ_ONLY_COMMANDS


# --- Models ------------------------------------------------------------------


class ReadRequest(BaseModel):
    path: str


class WriteRequest(BaseModel):
    path: str
    content: str


class RunRequest(BaseModel):
    command: str
    working_dir: Optional[str] = None


class ListRequest(BaseModel):
    path: str = "."


# --- Routes ------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE)}


@app.post("/list")
def list_directory(req: ListRequest):
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not target.exists():
        raise HTTPException(404, f"Path not found: {req.path}")
    if not target.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")

    entries = []
    for entry in sorted(target.iterdir()):
        entries.append(
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            }
        )
    return {"path": req.path, "entries": entries}


@app.post("/read")
def read_file(req: ReadRequest):
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not target.exists():
        raise HTTPException(404, f"File not found: {req.path}")
    if not target.is_file():
        raise HTTPException(400, f"Not a file: {req.path}")
    try:
        content = target.read_text(encoding="utf-8")
        return {"path": req.path, "content": content}
    except Exception as e:
        raise HTTPException(500, f"Could not read file: {e}")


@app.post("/write")
def write_file(req: WriteRequest):
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(req.content, encoding="utf-8")
        return {"path": req.path, "written": len(req.content)}
    except Exception as e:
        raise HTTPException(500, f"Could not write file: {e}")


@app.post("/run")
def run_command(req: RunRequest):
    cwd = WORKSPACE
    if req.working_dir:
        if not is_safe_path(req.working_dir):
            raise HTTPException(400, "Working dir outside workspace")
        cwd = (WORKSPACE / req.working_dir.lstrip("/")).resolve()

    try:
        result = subprocess.run(
            req.command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
        )
        return {
            "command": req.command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Command timed out after 30s")
    except Exception as e:
        raise HTTPException(500, f"Command failed: {e}")


# --- Entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("CODE_CONTAINER_PORT", 6000))
    uvicorn.run("code:app", host="0.0.0.0", port=port, reload=False)
