"""Dump each captured request to data/ as a single JSON file.

Filename: data/req-NNNNN-<utc-timestamp>.json, sortable by name = chronological order.

Sensitive headers (x-api-key, authorization, cookie, set-cookie) are replaced with
"<redacted>" before writing. Body is parsed as JSON when possible; raw bytes are
preserved as base64 when not.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path("data")
SENSITIVE_HEADERS = {"x-api-key", "authorization", "cookie", "set-cookie", "proxy-authorization"}


def configure_data_dir(path: str | Path) -> None:
    """Override the data directory. Must be called before serving traffic."""
    global DATA_DIR
    DATA_DIR = Path(path).resolve()


def _redact_headers(headers: dict) -> dict:
    return {
        k: ("<redacted>" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def dump_request(
    *,
    counter: int,
    method: str,
    path: str,
    query: str,
    headers: dict,
    body_bytes: bytes,
    upstream_url: str,
) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts_compact = now.strftime("%Y%m%dT%H%M%S-%fZ")
    fname = f"req-{counter:05d}-{ts_compact}.json"
    out = DATA_DIR / fname

    body_parsed: object
    if not body_bytes:
        body_parsed = None
    else:
        try:
            body_parsed = json.loads(body_bytes)
        except json.JSONDecodeError:
            body_parsed = {"_raw_b64": base64.b64encode(body_bytes).decode("ascii")}

    payload = {
        "n": counter,
        "ts": now.isoformat(),
        "method": method,
        "path": path,
        "query": query,
        "upstream": upstream_url,
        "headers": _redact_headers(headers),
        "body_bytes_len": len(body_bytes),
        "body": body_parsed,
    }

    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out