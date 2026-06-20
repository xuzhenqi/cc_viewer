"""Read-only FastAPI viewer for captured Claude API requests in data/.

Independent of the proxy: runs as its own process on a different port. Reads
already-dumped JSON files written by `dump.dump_request` and serves them via a
small HTML/JS UI. Does not forward anything upstream.

Also serves per-session aggregate JSON files from --systems-dir (produced by
`python -m claude_proxy extract-system`) so the browser can browse extracted
system prompts without recomputing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import dump


STATIC_DIR = Path(__file__).parent / "static"
SYSTEMS_DIR = Path("systems")

_FILENAME_RE = re.compile(r"^req-\d{5}-[\w\-]+\.json$")
_SESSION_FILENAME_RE = re.compile(r"^[\w\-]+\.json$")


def configure_systems_dir(path: str | Path) -> None:
    """Override the systems directory. Must be called before serving traffic."""
    global SYSTEMS_DIR
    SYSTEMS_DIR = Path(path).resolve()


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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
