import os
import re
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

# Directories the LLM is allowed to read and write.
WRITE_ALLOWLIST = {
    "chat",
    "llm",
    "code",
    "postgres",
    "search",
    "searxng",
    "files",
    "apps",
    "ui",
}

# Paths the LLM can never read or write.
READ_DENYLIST = {
    ".env",
    "data",
    "logs",
    ".git",
}


def is_safe_path(path: str) -> bool:
    try:
        resolved = (WORKSPACE / path.lstrip("/")).resolve()
        return resolved.is_relative_to(WORKSPACE)
    except Exception:
        return False


def is_denied(path: str) -> bool:
    clean = path.lstrip("/").split("/")[0]
    return clean in READ_DENYLIST


def is_write_allowed(path: str) -> bool:
    clean = path.lstrip("/").split("/")[0]
    return clean in WRITE_ALLOWLIST


def is_readonly_command(cmd: str) -> bool:
    first = cmd.strip().split()[0] if cmd.strip() else ""
    return first in SAFE_READ_ONLY_COMMANDS


# --- Models ------------------------------------------------------------------


class ReadRequest(BaseModel):
    path: str
    start_line: Optional[int] = None  # 1-indexed, inclusive
    end_line: Optional[int] = None    # 1-indexed, inclusive


class WriteRequest(BaseModel):
    path: str
    content: str


class EditRequest(BaseModel):
    path: str
    old_str: str   # exact text to find — must appear exactly once
    new_str: str   # replacement text


class RunRequest(BaseModel):
    command: str
    working_dir: Optional[str] = None


class ListRequest(BaseModel):
    path: str = "."


class SearchCodeRequest(BaseModel):
    pattern: str               # regex or literal string
    path: str = "."            # directory to search (relative)
    glob: str = "*"            # file name pattern, e.g. "*.py"
    context_lines: int = 2     # lines of context around each match
    max_results: int = 30


class SearchFilesRequest(BaseModel):
    pattern: str        # glob pattern, e.g. "*.py" or "chat*"
    path: str = "."     # directory to search from


# --- Routes ------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE)}


@app.post("/list")
def list_directory(req: ListRequest):
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    if is_denied(req.path):
        raise HTTPException(403, f"Access denied: {req.path}")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not target.exists():
        raise HTTPException(404, f"Path not found: {req.path}")
    if not target.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")

    entries = []
    for entry in sorted(target.iterdir()):
        if not is_denied(str(entry.relative_to(WORKSPACE))):
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            })
    return {"path": req.path, "entries": entries}


@app.post("/read")
def read_file(req: ReadRequest):
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    if is_denied(req.path):
        raise HTTPException(403, f"Access denied: {req.path}")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not target.exists():
        raise HTTPException(404, f"File not found: {req.path}")
    if not target.is_file():
        raise HTTPException(400, f"Not a file: {req.path}")
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"Could not read file: {e}")

    if req.start_line is not None or req.end_line is not None:
        lines = content.splitlines(keepends=True)
        total = len(lines)
        start = max(0, (req.start_line or 1) - 1)
        end = min(total, req.end_line or total)
        content = "".join(lines[start:end])
        return {"path": req.path, "content": content, "start_line": start + 1, "end_line": end, "total_lines": total}

    return {"path": req.path, "content": content, "total_lines": len(content.splitlines())}


@app.post("/write")
def write_file(req: WriteRequest):
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    if is_denied(req.path):
        raise HTTPException(403, f"Access denied: {req.path}")
    if not is_write_allowed(req.path):
        raise HTTPException(403, f"Write not allowed outside of: {', '.join(sorted(WRITE_ALLOWLIST))}")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(req.content, encoding="utf-8")
        return {"path": req.path, "written": len(req.content)}
    except Exception as e:
        raise HTTPException(500, f"Could not write file: {e}")


@app.post("/edit")
def edit_file(req: EditRequest):
    """Replace an exact string in a file. Fails if the string is not found
    or appears more than once — forces the model to be specific."""
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    if is_denied(req.path):
        raise HTTPException(403, f"Access denied: {req.path}")
    if not is_write_allowed(req.path):
        raise HTTPException(403, f"Write not allowed outside of: {', '.join(sorted(WRITE_ALLOWLIST))}")
    target = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not target.exists():
        raise HTTPException(404, f"File not found: {req.path}")
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"Could not read file: {e}")

    count = content.count(req.old_str)
    if count == 0:
        raise HTTPException(422, f"String not found in {req.path}. Use search_code to verify the exact text.")
    if count > 1:
        raise HTTPException(422, f"Found {count} occurrences in {req.path} — make old_str more specific (add more surrounding lines).")

    new_content = content.replace(req.old_str, req.new_str, 1)
    try:
        target.write_text(new_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"Could not write file: {e}")
    return {"path": req.path, "replaced": 1}


@app.post("/search_code")
def search_code(req: SearchCodeRequest):
    """Search files for a regex pattern, returning matching lines with context."""
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    if is_denied(req.path):
        raise HTTPException(403, f"Access denied: {req.path}")

    base = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not base.exists():
        raise HTTPException(404, f"Path not found: {req.path}")

    try:
        regex = re.compile(req.pattern)
    except re.error as e:
        raise HTTPException(400, f"Invalid regex: {e}")

    results = []
    context = max(0, min(req.context_lines, 10))

    for filepath in sorted(base.rglob(req.glob)):
        if not filepath.is_file():
            continue
        rel = str(filepath.relative_to(WORKSPACE))
        if is_denied(rel):
            continue
        # Skip binary-looking files
        try:
            text = filepath.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, PermissionError):
            continue

        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            ctx = "\n".join(
                f"{j + 1}{'>' if j == i else ' '} {lines[j]}"
                for j in range(start, end)
            )
            results.append({"file": rel, "line": i + 1, "match": line.strip(), "context": ctx})
            if len(results) >= req.max_results:
                return {"pattern": req.pattern, "results": results, "truncated": True}

    return {"pattern": req.pattern, "results": results, "truncated": False}


@app.post("/search_files")
def search_files(req: SearchFilesRequest):
    """Find files matching a glob pattern."""
    if not is_safe_path(req.path):
        raise HTTPException(400, "Path outside workspace")
    if is_denied(req.path):
        raise HTTPException(403, f"Access denied: {req.path}")

    base = (WORKSPACE / req.path.lstrip("/")).resolve()
    if not base.exists():
        raise HTTPException(404, f"Path not found: {req.path}")

    results = []
    for entry in sorted(base.rglob(req.pattern)):
        rel = str(entry.relative_to(WORKSPACE))
        if not is_denied(rel):
            results.append({
                "path": rel,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            })
        if len(results) >= 100:
            break

    return {"pattern": req.pattern, "results": results}


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
