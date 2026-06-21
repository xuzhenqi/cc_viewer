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

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import dump


STATIC_DIR = Path(__file__).parent / "static"
SYSTEMS_DIR = Path("systems")
PROMPTS_DIR = Path("prompts")

_FILENAME_RE = re.compile(r"^req-\d{5}-[\w\-]+\.json$")
_SESSION_FILENAME_RE = re.compile(r"^[\w\-]+\.json$")
_CATALOG_NAME_RE = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"
_CATALOG_HASH_RE = r"^sha256:[0-9a-f]{64}$"


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
_PROMPTS_CACHE: dict[str, "CatalogEntry"] | None = None  # {hash: entry}
_PROMPTS_MTIME: float | None = None                    # dir mtime for cache


class CatalogEntry(BaseModel):
    hash: str = Field(pattern=_CATALOG_HASH_RE)
    name: str = Field(pattern=_CATALOG_NAME_RE)
    text: str
    created_at: str
    updated_at: str


class UpsertEntry(BaseModel):
    name: str = Field(pattern=_CATALOG_NAME_RE)
    text: str


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_filename(entry: CatalogEntry) -> str:
    return f"{entry.name}.md"


def _entry_path(entry: CatalogEntry) -> Path:
    return PROMPTS_DIR / _entry_filename(entry)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _render_markdown(entry: CatalogEntry) -> str:
    # yaml.safe_dump preserves insertion order and gives us a stable, readable
    # frontmatter. The body follows a blank line so it doesn't accidentally
    # begin with `---`. We do not append a trailing newline: round-tripping
    # `text` exactly matters for the SHA-256 hash check.
    frontmatter = yaml.safe_dump(
        {
            "name": entry.name,
            "hash": entry.hash,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
        },
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{entry.text}"


def _parse_markdown(path: Path) -> CatalogEntry | None:
    """Parse a single catalog .md file. Returns None on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[catalog] skip {path.name}: read failed ({e})", file=sys.stderr)
        return None
    if not raw.startswith("---"):
        print(f"[catalog] skip {path.name}: missing frontmatter", file=sys.stderr)
        return None
    parts = raw.split("---", 2)
    if len(parts) < 3:
        print(f"[catalog] skip {path.name}: unterminated frontmatter", file=sys.stderr)
        return None
    fm_text, body = parts[1].strip(), parts[2].lstrip("\n").rstrip("\n")
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        print(f"[catalog] skip {path.name}: yaml parse failed ({e})", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"[catalog] skip {path.name}: frontmatter is not a mapping", file=sys.stderr)
        return None
    try:
        return CatalogEntry(
            hash=data["hash"],
            name=data["name"],
            text=body,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
    except Exception as e:
        print(f"[catalog] skip {path.name}: validation failed ({e})", file=sys.stderr)
        return None


def _scan_prompts() -> dict[str, CatalogEntry]:
    """Read all .md files in PROMPTS_DIR and return {hash: entry}."""
    out: dict[str, CatalogEntry] = {}
    if not PROMPTS_DIR.exists():
        return out
    for path in PROMPTS_DIR.glob("*.md"):
        if not path.is_file():
            continue
        entry = _parse_markdown(path)
        if entry is None:
            continue
        if entry.hash in out:
            print(f"[catalog] skip {path.name}: duplicate hash {entry.hash[:15]}…", file=sys.stderr)
            continue
        out[entry.hash] = entry
    return out


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
        _PROMPTS_CACHE = _scan_prompts()
        _PROMPTS_MTIME = current_mtime
        return _PROMPTS_CACHE


def _invalidate_cache() -> None:
    global _PROMPTS_CACHE, _PROMPTS_MTIME
    with _PROMPTS_LOCK:
        _PROMPTS_CACHE = None
        _PROMPTS_MTIME = None


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (tmp + fsync + replace)."""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".catalog.", suffix=".md.tmp", dir=str(PROMPTS_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_entry(entry: CatalogEntry, old_name: str | None = None) -> None:
    """Write a new .md file. If old_name differs from entry.name, unlink the old path first."""
    new_path = _entry_path(entry)
    if old_name is not None and old_name != entry.name:
        old_path = PROMPTS_DIR / f"{old_name}.md"
        try:
            old_path.unlink()
        except FileNotFoundError:
            pass
    _atomic_write(new_path, _render_markdown(entry))
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
    text_hash = _hash_text(body.text)
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
    if not re.match(_CATALOG_HASH_RE, hash):
        raise HTTPException(status_code=400, detail="invalid hash")
    text_hash = _hash_text(body.text)
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
    if not re.match(_CATALOG_HASH_RE, hash):
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


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
