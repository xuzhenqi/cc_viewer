"""Read-only FastAPI viewer for captured Claude API requests in data/.

Independent of the proxy: runs as its own process on a different port. Reads
already-dumped JSON files written by `dump.dump_request` and serves them via a
small HTML/JS UI. Does not forward anything upstream.
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

_FILENAME_RE = re.compile(r"^req-\d{5}-[\w\-]+\.json$")


def _summary(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    body = data.get("body")
    model = body.get("model") if isinstance(body, dict) else None
    messages = body.get("messages") if isinstance(body, dict) else None
    msg_count = len(messages) if isinstance(messages, list) else None

    headers = data.get("headers") or {}
    session_id = None
    for k, v in headers.items():
        if k.lower() == "x-claude-code-session-id" and isinstance(v, str) and v.strip():
            session_id = v.strip()
            break

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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
