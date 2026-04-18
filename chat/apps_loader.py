"""App loader: scans /apps/*, mounts each as a FastAPI sub-router.

Design:
- Each app lives in /apps/<name>/ with a manifest.json and app.py.
- app.py must expose `router: APIRouter`; may expose async `setup(db_pool)`.
- Static files in /apps/<name>/static/ are served at /apps/<name>/app/.
- API routes live at /apps/<name>/api/... (app defines them on its router).
- Migrations in /apps/<name>/migrations/*.sql are applied idempotently, tracked
  in _meta.app_migrations(app, version).

Failure isolation: any app that fails to import or migrate is logged and
recorded in STATUS[name] = {"ok": False, "error": ...}. Chat still boots.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

APPS_ROOT = Path(os.getenv("APPS_ROOT", "/apps"))
SKIP_DIRS = {"_template"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# name → {"manifest": dict, "module": module|None, "ok": bool, "error": str|None}
STATUS: dict[str, dict[str, Any]] = {}


def _schema_for(name: str) -> str:
    return "app_" + name.replace("-", "_")


def scaffold_new_app(name: str) -> dict:
    """Copy apps/_template/ → apps/<name>/ and substitute the template name.

    Mirrors `make new-app NAME=<name>` but runnable from inside the chat
    container (where the apps/ volume is mounted). Safe to call as a tool.

    Returns {"ok": True, "path": ..., "files": [...]} on success,
    raises ValueError on validation errors.
    """
    if not NAME_RE.match(name):
        raise ValueError(f"app name must match {NAME_RE.pattern}")
    if name in SKIP_DIRS:
        raise ValueError(f"{name!r} is reserved")
    template = APPS_ROOT / "_template"
    if not template.is_dir():
        raise RuntimeError(f"template not found at {template}")
    target = APPS_ROOT / name
    if target.exists():
        raise ValueError(f"apps/{name} already exists")

    shutil.copytree(template, target)

    schema = _schema_for(name)
    # Order matters: replace the schema literal first so "_template" inside
    # "app__template" is not clobbered by the generic replacement.
    subs = [("app__template", schema), ("_template", name)]

    touched = []
    exts = {".py", ".json", ".sql", ".html", ".js", ".css", ".md"}
    for path in target.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        text = path.read_text(encoding="utf-8")
        new = text
        for old, new_val in subs:
            new = new.replace(old, new_val)
        if new != text:
            path.write_text(new, encoding="utf-8")
            touched.append(str(path.relative_to(APPS_ROOT.parent if APPS_ROOT.is_absolute() else Path("."))))

    return {
        "ok": True,
        "name": name,
        "schema": schema,
        "path": f"apps/{name}",
        "files_modified": touched,
        "note": "App created. Restart chat (`make restart-chat`) to mount it, or ask the user to.",
    }


async def _ensure_meta_schema(db_pool) -> None:
    await db_pool.execute(
        """
        CREATE SCHEMA IF NOT EXISTS _meta;
        CREATE TABLE IF NOT EXISTS _meta.app_migrations (
            app        TEXT        NOT NULL,
            version    TEXT        NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (app, version)
        );
        """
    )


async def _apply_migrations(db_pool, name: str, app_dir: Path) -> None:
    mig_dir = app_dir / "migrations"
    if not mig_dir.is_dir():
        return
    files = sorted(p for p in mig_dir.iterdir() if p.suffix == ".sql")
    if not files:
        return
    schema = _schema_for(name)
    async with db_pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        applied = {
            r["version"]
            for r in await conn.fetch(
                "SELECT version FROM _meta.app_migrations WHERE app = $1", name
            )
        }
        for path in files:
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text()
            # Strip line comments to see if the file has any executable SQL.
            # asyncpg's simple-query protocol chokes on comment-only input
            # ('NoneType' object has no attribute 'decode').
            stripped = "\n".join(
                line for line in sql.splitlines()
                if line.strip() and not line.strip().startswith("--")
            ).strip()
            if not stripped:
                await conn.execute(
                    "INSERT INTO _meta.app_migrations (app, version) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    name,
                    version,
                )
                continue
            async with conn.transaction():
                # search_path: app schema first, then public so pgcrypto/vector work
                await conn.execute(f'SET LOCAL search_path = "{schema}", public')
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _meta.app_migrations (app, version) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    name,
                    version,
                )
            print(f"[apps] {name}: applied migration {version}")


def _load_manifest(app_dir: Path) -> dict:
    with open(app_dir / "manifest.json") as f:
        return json.load(f)


def _import_app(name: str, app_dir: Path):
    # Hyphens aren't valid in Python module names; replace for sys.modules key.
    module_name = f"homeserver_apps.{name.replace('-', '_')}"
    init_py = app_dir / "app.py"
    spec = importlib.util.spec_from_file_location(
        module_name, init_py, submodule_search_locations=[str(app_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build import spec for {init_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "router"):
        raise AttributeError(f"{name}/app.py must expose a `router: APIRouter`")
    return module


def _discover() -> list[tuple[str, Path, dict]]:
    out = []
    if not APPS_ROOT.is_dir():
        return out
    for entry in sorted(APPS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in SKIP_DIRS or name.startswith("."):
            continue
        if not NAME_RE.match(name):
            print(f"[apps] skipping {name!r}: invalid app name")
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            print(f"[apps] skipping {name!r}: missing manifest.json")
            continue
        try:
            manifest = _load_manifest(entry)
        except Exception as e:
            STATUS[name] = {
                "manifest": {"name": name, "title": name},
                "module": None,
                "ok": False,
                "error": f"bad manifest: {e}",
            }
            print(f"[apps] {name}: bad manifest ({e})")
            continue
        out.append((name, entry, manifest))
    return out


async def install_all(fastapi_app: FastAPI, db_pool) -> None:
    """Discover, migrate, import, and mount every app under APPS_ROOT.

    Safe to call once during chat lifespan. Each app's failure is isolated.
    """
    await _ensure_meta_schema(db_pool)

    for name, app_dir, manifest in _discover():
        try:
            await _apply_migrations(db_pool, name, app_dir)
            module = _import_app(name, app_dir)
            if hasattr(module, "setup"):
                await module.setup(db_pool)

            fastapi_app.include_router(module.router, prefix=f"/apps/{name}")

            static_dir = app_dir / "static"
            if static_dir.is_dir():
                fastapi_app.mount(
                    f"/apps/{name}/app",
                    StaticFiles(directory=str(static_dir), html=True),
                    name=f"app_{name}_static",
                )

            STATUS[name] = {
                "manifest": manifest,
                "module": module,
                "ok": True,
                "error": None,
            }
            print(f"[apps] {name}: mounted at /apps/{name}")
        except Exception as e:
            traceback.print_exc()
            STATUS[name] = {
                "manifest": manifest,
                "module": None,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
            print(f"[apps] {name}: FAILED to load — {e}")


def install_routes(fastapi_app: FastAPI) -> None:
    """Register the launcher, wrapper, registry, and /ui static mount.

    Call once at app construction time (before lifespan runs is fine).
    """
    ui_dir = Path(os.getenv("UI_ROOT", "/ui"))
    if ui_dir.is_dir():
        fastapi_app.mount("/ui", StaticFiles(directory=str(ui_dir)), name="ui")
    else:
        print(f"[apps] UI directory not found at {ui_dir}; /ui will 404")

    @fastapi_app.get("/apps")
    async def _apps_launcher():
        return FileResponse("static/apps.html")

    @fastapi_app.get("/apps/{name}")
    async def _app_wrapper(name: str):
        if name not in STATUS:
            raise HTTPException(404, detail=f"app {name!r} not installed")
        return FileResponse("static/app-wrapper.html")

    @fastapi_app.get("/registry/apps")
    async def _registry():
        apps = []
        for name, s in STATUS.items():
            m = s.get("manifest") or {}
            apps.append(
                {
                    "name": name,
                    "title": m.get("title") or name,
                    "description": m.get("description") or "",
                    "icon": m.get("icon") or "📦",
                    "version": m.get("version") or "0.0.0",
                    "tables": m.get("tables") or {},
                    "capabilities": m.get("capabilities") or [],
                    "ok": s.get("ok", False),
                    "error": s.get("error"),
                }
            )
        return {"apps": apps}
