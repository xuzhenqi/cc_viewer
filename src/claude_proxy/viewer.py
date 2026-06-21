"""Read-only FastAPI viewer for captured Claude API requests in data/.

Independent of the proxy: runs as its own process on a different port. Reads
already-dumped JSON files written by `dump.dump_request` and serves them via a
small HTML/JS UI. Does not forward anything upstream.

Also serves per-session aggregate JSON files from --systems-dir (produced by
`python -m claude_proxy extract-system`) so the browser can browse extracted
system prompts without recomputing.

The prompt catalog lives in PROMPTS_DIR as one Markdown file per entry
(name + sha256 hash in the filename, YAML frontmatter with metadata, body
with the prompt text). Catalog entries are matched to body.system text blocks
by SHA-256 hash, exact match. See plan file for details.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import dump
from .prompts import (
    CATALOG_HASH_RE,
    CATALOG_NAME_RE,
    CatalogEntry,
    atomic_write,
    hash_text,
    render_markdown,
    scan_prompts,
)
from .prompts import _now_iso


STATIC_DIR = Path(__file__).parent / "static"
SYSTEMS_DIR = Path("systems")
PROMPTS_DIR = Path("prompts")

_FILENAME_RE = re.compile(r"^req-\d{5}-[\w\-]+\.json$")
_SESSION_FILENAME_RE = re.compile(r"^[\w\-]+\.json$")


def configure_systems_dir(path: str | Path) -> None:
    """Override the systems directory. Must be called before serving traffic."""
    global SYSTEMS_DIR
    SYSTEMS_DIR = Path(path).resolve()


def configure_prompts_dir(path: str | Path) -> None:
    """Override the prompts-catalog directory. Must be called before serving traffic."""
    global PROMPTS_DIR, _PROMPTS_CACHE, _PROMPTS_MTIME
    PROMPTS_DIR = Path(path).resolve()
    _PROMPTS_CACHE = None
    _PROMPTS_MTIME = None


# --- Prompt catalog --------------------------------------------------------

_PROMPTS_LOCK = threading.RLock()
_PROMPTS_CACHE: dict[str, CatalogEntry] | None = None  # {hash: entry}
_PROMPTS_MTIME: float | None = None                    # dir mtime for cache


class UpsertEntry(BaseModel):
    name: str = Field(pattern=CATALOG_NAME_RE.pattern)
    text: str


def _entry_filename(entry: CatalogEntry) -> str:
    return f"{entry.name}.md"


def _entry_path(entry: CatalogEntry) -> Path:
    return PROMPTS_DIR / _entry_filename(entry)


def _load_catalog() -> dict[str, CatalogEntry]:
    """Return the catalog, refreshing the cache if the dir mtime changed."""
    global _PROMPTS_CACHE, _PROMPTS_MTIME
    with _PROMPTS_LOCK:
        try:
            current_mtime = PROMPTS_DIR.stat().st_mtime if PROMPTS_DIR.exists() else 0.0
        except OSError:
            current_mtime = 0.0
        if _PROMPTS_CACHE is not None and _PROMPTS_MTIME == current_mtime:
            return _PROMPTS_CACHE
        _PROMPTS_CACHE = scan_prompts(PROMPTS_DIR)
        _PROMPTS_MTIME = current_mtime
        return _PROMPTS_CACHE


def _invalidate_cache() -> None:
    global _PROMPTS_CACHE, _PROMPTS_MTIME
    with _PROMPTS_LOCK:
        _PROMPTS_CACHE = None
        _PROMPTS_MTIME = None


def _write_entry(entry: CatalogEntry, old_name: str | None = None) -> None:
    """Write a new .md file. If old_name differs from entry.name, unlink the old path first."""
    new_path = _entry_path(entry)
    if old_name is not None and old_name != entry.name:
        old_path = PROMPTS_DIR / f"{old_name}.md"
        try:
            old_path.unlink()
        except FileNotFoundError:
            pass
    atomic_write(new_path, render_markdown(entry), tmp_dir=PROMPTS_DIR)
    _invalidate_cache()


def _summary(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    body = data.get("body")
    model = body.get("model") if isinstance(body, dict) else None
    messages = body.get("messages") if isinstance(body, dict) else None
    msg_count = len(messages) if isinstance(messages, list) else None

    session_id = dump.get_header(data.get("headers"), "x-claude-code-session-id")

    return {
        "filename": path.name,
        "n": data.get("n"),
        "ts": data.get("ts"),
        "method": data.get("method"),
        "path": data.get("path"),
        "body_bytes_len": data.get("body_bytes_len"),
        "model": model,
        "msg_count": msg_count,
        "session_id": session_id,
    }


app = FastAPI(title="Claude Capture Viewer")


# Cap on referenced_in list size returned by /api/prompts/{name}. The list
# is a UI affordance ("which requests contained this prompt?"), not an
# index — full enumeration over data/ would be unbounded.
_REFERENCED_IN_CAP = 200


def _extract_system_texts(body: dict) -> list[str]:
    """Return the list of prompt-text blocks in `body.system`.

    `body.system` is either a single string, or a list of content blocks.
    We keep only `{type: "text", text: "…"}` entries (the other Anthropic
    block types don't carry prompt prose). Empty strings are dropped.
    """
    out: list[str] = []
    sys = body.get("system") if isinstance(body, dict) else None
    if isinstance(sys, str):
        if sys:
            out.append(sys)
        return out
    if not isinstance(sys, list):
        return out
    for b in sys:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t:
                out.append(t)
    return out


def _find_referenced_in(target_hash: str, *, cap: int = _REFERENCED_IN_CAP) -> list[dict]:
    """Walk data/req-*.json newest-first; for every request, hash each text
    block in `body.system` and emit a `{filename, ts, session_id}` record
    when its hash matches `target_hash`. Returns at most `cap` entries.

    Stop scanning once we have `cap` matches AND the remaining files are
    older than the oldest we already returned; this trades a small amount
    of completeness for predictable latency on large data dirs.
    """
    if not dump.DATA_DIR.exists():
        return []
    out: list[dict] = []
    files = sorted(dump.DATA_DIR.glob("req-*.json"), reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        body = data.get("body") or {}
        for text in _extract_system_texts(body):
            if hash_text(text) == target_hash:
                out.append({
                    "filename": path.name,
                    "ts": data.get("ts"),
                    "session_id": dump.get_header(
                        data.get("headers"), "x-claude-code-session-id"
                    ),
                })
                break  # one match per dump is enough
        if len(out) >= cap:
            break
    return out


@app.get("/api/requests")
async def list_requests() -> dict:
    if not dump.DATA_DIR.exists():
        return {"total": 0, "items": []}

    files = sorted(dump.DATA_DIR.glob("req-*.json"), reverse=True)
    items = [s for s in (_summary(p) for p in files) if s is not None]
    return {"total": len(items), "items": items}


@app.get("/api/requests/{filename}")
async def get_request(filename: str) -> dict:
    if not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = dump.DATA_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=503, detail="file is being written, retry")


@app.get("/api/sessions")
async def list_sessions() -> dict:
    if not SYSTEMS_DIR.exists():
        return {"total": 0, "items": []}

    items = []
    for path in sorted(SYSTEMS_DIR.glob("*.json"), reverse=True):
        if not _SESSION_FILENAME_RE.match(path.name):
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        summary = data.get("summary") or {}
        items.append({
            "session_id": data.get("session_id"),
            "filename": path.name,
            "request_count": data.get("request_count"),
            "first_ts": data.get("first_ts"),
            "last_ts": data.get("last_ts"),
            "total_messages": summary.get("total_messages"),
            "real_user_turns": summary.get("real_user_turns"),
            "assistant_turns": summary.get("assistant_turns"),
            "distinct_full_prompts": summary.get("distinct_full_prompts"),
            "distinct_system_reminders": summary.get("distinct_system_reminders"),
        })
    items.sort(key=lambda x: (x.get("last_ts") or ""), reverse=True)
    return {"total": len(items), "items": items}


@app.get("/api/sessions/{filename}")
async def get_session(filename: str) -> dict:
    if not _SESSION_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = SYSTEMS_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=503, detail="file is being written, retry")


# --- Prompt catalog endpoints ---------------------------------------------

@app.get("/api/catalog/entries")
def list_catalog_entries() -> dict:
    entries = _load_catalog()
    return {"version": 1, "entries": [e.model_dump() for e in entries.values()]}


@app.post("/api/catalog/entries", status_code=201)
def create_catalog_entry(body: UpsertEntry) -> dict:
    text_hash = hash_text(body.text)
    now = _now_iso()
    with _PROMPTS_LOCK:
        catalog = _load_catalog()
        if text_hash in catalog:
            raise HTTPException(
                status_code=409,
                detail={"error": "hash already exists", "entry": catalog[text_hash].model_dump()},
            )
        for e in catalog.values():
            if e.name == body.name:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "name already used", "hash": e.hash},
                )
        entry = CatalogEntry(
            hash=text_hash,
            name=body.name,
            text=body.text,
            created_at=now,
            updated_at=now,
        )
        _write_entry(entry)
    return entry.model_dump()


@app.put("/api/catalog/entries/{hash}")
def rename_catalog_entry(hash: str, body: UpsertEntry) -> dict:
    if not CATALOG_HASH_RE.match(hash):
        raise HTTPException(status_code=400, detail="invalid hash")
    text_hash = hash_text(body.text)
    if text_hash != hash:
        # Text changed → this is a new identity. Either a new entry (POST) or
        # refuse the rename; refuse to keep semantics simple.
        raise HTTPException(
            status_code=400,
            detail="text does not match hash; use POST to create a new entry",
        )
    with _PROMPTS_LOCK:
        catalog = _load_catalog()
        existing = catalog.get(hash)
        if existing is None:
            raise HTTPException(status_code=404, detail="not found")
        if body.name == existing.name:
            # No-op rename: just echo back.
            return existing.model_dump()
        for e in catalog.values():
            if e.name == body.name and e.hash != hash:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "name already used", "hash": e.hash},
                )
        updated = CatalogEntry(
            hash=existing.hash,
            name=body.name,
            text=existing.text,
            created_at=existing.created_at,
            updated_at=_now_iso(),
        )
        _write_entry(updated, old_name=existing.name)
    return updated.model_dump()


@app.delete("/api/catalog/entries/{hash}")
def delete_catalog_entry(hash: str) -> dict:
    if not CATALOG_HASH_RE.match(hash):
        raise HTTPException(status_code=400, detail="invalid hash")
    with _PROMPTS_LOCK:
        catalog = _load_catalog()
        existing = catalog.get(hash)
        if existing is None:
            return {"deleted": True}
        try:
            _entry_path(existing).unlink()
        except FileNotFoundError:
            pass
        _invalidate_cache()
    return {"deleted": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --- Prompt-by-name lookup ------------------------------------------------
#
# The catalog API is keyed by hash (immutable identity). But the user-facing
# /prompts/<name> URL is keyed by name. This endpoint resolves a name to a
# single entry and bundles a "referenced in" list so the prompt page can
# show where the prompt has been used.


@app.get("/api/prompts/{name}")
async def get_prompt_by_name(name: str) -> dict:
    if not CATALOG_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid name")
    catalog = _load_catalog()
    for entry in catalog.values():
        if entry.name == name:
            referenced = _find_referenced_in(entry.hash)
            payload = entry.model_dump()
            payload["referenced_in"] = referenced
            payload["referenced_in_truncated"] = len(referenced) >= _REFERENCED_IN_CAP
            return payload
    raise HTTPException(status_code=404, detail="not found")


@app.get("/prompts/{name}")
async def prompt_page(name: str) -> FileResponse:
    """Serve the SPA shell at /prompts/<name> for deep-link / reload.

    The SPA reads `location.pathname` and dispatches to the prompt view.
    We don't validate the name here — invalid names render a 404 inline.
    The `name` arg is required by FastAPI's path-param machinery; it is
    intentionally unused server-side.
    """
    del name  # consumed by the URL; the SPA handles the lookup
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
