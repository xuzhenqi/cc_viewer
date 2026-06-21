"""Per-session systems aggregation with prompt-catalog references.

Produces a compact per-session JSON that points at the canonical prompt
catalog (`prompts/`) rather than embedding prompt text inline. The output
is the shape consumed by `python -m claude_proxy view` in systems mode.

Key design choices:

- Only the chronologically last request in a session is processed. Claude
  Code re-sends the cumulative message list on every turn, so the last
  request already contains every system block currently in scope.
- Every block in the output references the source request filename and
  (for in-message blocks) the source message index, so the UI can deep-link
  back to the original dump.
- Every block gets a `catalog_match` when an exact-hash or near-duplicate
  hit exists in the prompt catalog. The frontend hides the raw text and
  renders the prompt name as a clickable link when `catalog_match` is
  present. Raw text is NOT included in the output; the catalog is the
  single source of truth.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path

from . import classify, dump
from .prompts import CatalogEntry, hash_text


# Top-level block kinds. Mirrors classify._top_level_kind but kept local so
# we don't reach into classify's privates.
_KIND_BILLING = "billing"
_KIND_TAGLINE = "tagline"
_KIND_FULL = "full"


def _top_level_kind(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("x-anthropic-billing-header"):
        return _KIND_BILLING
    if stripped.startswith("You are Claude Code"):
        return _KIND_TAGLINE
    return _KIND_FULL


def find_similar(
    text: str,
    catalog: dict[str, CatalogEntry],
    *,
    threshold: float = 0.85,
) -> tuple[CatalogEntry, float] | None:
    """Best catalog entry whose `SequenceMatcher.ratio()` to `text` is `>= threshold`.

    Exact-hash hits win unconditionally (ratio=1.0). Otherwise, the best
    fuzzy match above `threshold` is returned, or `None` if the catalog is
    empty or no candidate clears the bar.
    """
    if not text or not catalog:
        return None
    target_hash = hash_text(text)
    if target_hash in catalog:
        return catalog[target_hash], 1.0
    best_entry: CatalogEntry | None = None
    best_ratio = 0.0
    for entry in catalog.values():
        sm = difflib.SequenceMatcher(a=text, b=entry.text, autojunk=False)
        # quick_ratio is a cheap upper bound; only call ratio() if it could
        # improve on the current best.
        if sm.quick_ratio() <= best_ratio:
            continue
        r = sm.ratio()
        if r > best_ratio:
            best_ratio = r
            best_entry = entry
    if best_entry is not None and best_ratio >= threshold:
        return best_entry, best_ratio
    return None


def _build_match(
    text: str,
    catalog: dict[str, CatalogEntry],
    threshold: float,
) -> dict | None:
    """Resolve a system block to a `catalog_match` dict, or None on miss."""
    hit = find_similar(text, catalog, threshold=threshold)
    if hit is None:
        return None
    entry, ratio = hit
    return {"name": entry.name, "hash": entry.hash, "ratio": round(ratio, 4)}


def _read_dump(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _first_ts(paths: list[Path]) -> str | None:
    earliest: str | None = None
    for p in paths:
        d = _read_dump(p)
        if d is None:
            continue
        ts = d.get("ts")
        if ts and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def aggregate_session_systems(
    paths: list[Path],
    *,
    catalog: dict[str, CatalogEntry] | None = None,
    similarity_threshold: float = 0.85,
) -> dict:
    """Build the per-session systems JSON from `paths`.

    Only the chronologically last request is processed (filenames sort by
    counter + timestamp, so `max(paths, key=lambda p: p.name)` is "last in
    time"). Returns a plain dict ready to be `json.dumps`'d.

    `catalog` is the `{hash: CatalogEntry}` map from
    `prompts.scan_prompts(...)`. It may be empty; in that case no
    `catalog_match` will be attached.
    """
    if not paths:
        return {}
    last_path = max(paths, key=lambda p: p.name)
    data = _read_dump(last_path)
    if data is None:
        return {}
    catalog = catalog or {}

    classified = classify.classify_dump(data)
    body = data.get("body") or {}
    messages = body.get("messages") or []
    sp = classified.get("system_prompt") or {}

    # --- top-level system -----------------------------------------------
    top_level: list[dict] = []
    for text in sp.get("blocks") or []:
        if not isinstance(text, str) or not text:
            continue
        entry: dict = {
            "kind": _top_level_kind(text),
            "hash": hash_text(text),
            "length": len(text),
            "source_filename": last_path.name,
            "text": text,
        }
        match = _build_match(text, catalog, similarity_threshold)
        if match is not None:
            entry["catalog_match"] = match
        top_level.append(entry)

    # --- in-message system blocks ---------------------------------------
    by_kind: dict[str, list[dict]] = {
        classify.SYSTEM_REMINDER: [],
        classify.COMMAND_META: [],
        classify.CONTINUATION: [],
        classify.ENVIRONMENT_CONTEXT: [],
    }
    in_message_count = 0
    for i in range(len(messages)):
        if i >= len(classified.get("messages") or []):
            break
        for b in classified["messages"][i].blocks:
            if b.kind not in by_kind:
                continue
            block: dict = {
                "hash": hash_text(b.text),
                "length": len(b.text),
                "source_filename": last_path.name,
                "source_msg_index": i,
                "text": b.text,
            }
            match = _build_match(b.text, catalog, similarity_threshold)
            if match is not None:
                block["catalog_match"] = match
            by_kind[b.kind].append(block)
            in_message_count += 1

    summary = classified.get("summary") or {}
    return {
        "session_id": dump.get_header(data.get("headers"), "x-claude-code-session-id"),
        "request_count": len(paths),
        "first_ts": _first_ts(paths),
        "last_ts": data.get("ts"),
        "last_request_filename": last_path.name,
        "summary": {
            **summary,
            "top_level_blocks": len(top_level),
            "in_message_blocks": in_message_count,
        },
        "top_level_system": top_level,
        "in_message_systems": {
            "system_reminders": by_kind[classify.SYSTEM_REMINDER],
            "commands":         by_kind[classify.COMMAND_META],
            "continuations":    by_kind[classify.CONTINUATION],
            "environments":     by_kind[classify.ENVIRONMENT_CONTEXT],
        },
    }
