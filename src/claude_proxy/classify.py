"""Classify content inside captured Claude API request dumps.

Distinguishes between system-filled content (injected by Claude Code or its
proxy) and real user input. Used by the offline extraction CLI
(`extract-system`) to aggregate per-session system content, and re-implemented
in JS in `static/app.js` for the viewer's on-the-fly classification.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dump


# Block kind enum (string literals for easy JSON serialization).
SYSTEM_REMINDER = "system_reminder"
COMMAND_META = "command_meta"
LOCAL_COMMAND = "local_command"
CONTINUATION = "continuation"
ENVIRONMENT_CONTEXT = "environment_context"
USER_TEXT = "user_text"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
THINKING = "thinking"
IMAGE = "image"
UNKNOWN = "unknown"

SYSTEM_KINDS = frozenset({
    SYSTEM_REMINDER,
    COMMAND_META,
    LOCAL_COMMAND,
    CONTINUATION,
    ENVIRONMENT_CONTEXT,
})

# Detection regexes (compiled once; mirrored verbatim in static/app.js).
_SYSTEM_REMINDER_RE = re.compile(r"^\s*<system-reminder>")
_COMMAND_TAG_RE = re.compile(r"^\s*<command-(?:message|name|args)>")
_LOCAL_CMD_RE = re.compile(r"^\s*<local-command-(?:stdout|stderr)>")
_CONTINUATION_RE = re.compile(r"^This session is being continued from a previous conversation")
_ENV_HEADER_RE = re.compile(r"^# (?:claudeMd|currentDate|Environment|auto memory)\b")

# Session placeholder for dumps that lack x-claude-code-session-id.
NO_SESSION = "__no_session__"


def classify_text(text: str | None) -> str:
    """Classify a raw text payload into a block kind."""
    if not text:
        return UNKNOWN
    if _SYSTEM_REMINDER_RE.match(text):
        return SYSTEM_REMINDER
    stripped = text.lstrip()
    if _COMMAND_TAG_RE.match(stripped):
        return COMMAND_META
    if _LOCAL_CMD_RE.match(stripped):
        return LOCAL_COMMAND
    if _CONTINUATION_RE.match(stripped):
        return CONTINUATION
    if _ENV_HEADER_RE.match(stripped):
        return ENVIRONMENT_CONTEXT
    return USER_TEXT


def classify_block(block: dict) -> str:
    """Classify a single content block (which may be any Anthropic block type)."""
    t = block.get("type")
    if t == "text":
        return classify_text(block.get("text", ""))
    if t == "tool_use":
        return TOOL_USE
    if t == "tool_result":
        return TOOL_RESULT
    if t == "thinking":
        return THINKING
    if t == "image":
        return IMAGE
    return UNKNOWN


@dataclass
class ClassifiedBlock:
    raw: dict
    kind: str
    text: str  # best-effort flat string representation

    @property
    def is_system(self) -> bool:
        return self.kind in SYSTEM_KINDS


@dataclass
class ClassifiedMessage:
    index: int
    role: str
    blocks: list[ClassifiedBlock] = field(default_factory=list)

    @property
    def user_blocks(self) -> list[ClassifiedBlock]:
        return [b for b in self.blocks if b.kind == USER_TEXT]

    @property
    def system_blocks(self) -> list[ClassifiedBlock]:
        return [b for b in self.blocks if b.is_system]

    @property
    def has_tool_use(self) -> bool:
        return any(b.kind == TOOL_USE for b in self.blocks)

    @property
    def has_tool_result(self) -> bool:
        return any(b.kind == TOOL_RESULT for b in self.blocks)

    @property
    def has_thinking(self) -> bool:
        return any(b.kind == THINKING for b in self.blocks)

    @property
    def is_real_user_input(self) -> bool:
        return bool(self.user_blocks)

    @property
    def is_system_only(self) -> bool:
        # No real user text, and every block is system-injected or a tool_result.
        if not self.blocks or self.user_blocks:
            return False
        return all(b.is_system or b.kind == TOOL_RESULT for b in self.blocks)

    @property
    def primary_user_text(self) -> str:
        ut = self.user_blocks
        return ut[-1].text if ut else ""


def _block_text(b: dict) -> str:
    """Best-effort flat string representation of any block type."""
    t = b.get("type")
    if t == "text":
        return b.get("text", "")
    if t == "thinking":
        return b.get("thinking", "")
    if t == "tool_use":
        return f"[tool_use: {b.get('name', '?')}] " + json.dumps(
            b.get("input", {}), ensure_ascii=False
        )
    if t == "tool_result":
        c = b.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(_block_text(x) if isinstance(x, dict) else str(x) for x in c)
        if c is None:
            return ""
        return json.dumps(c, ensure_ascii=False)
    if t == "image":
        return "[image]"
    return json.dumps(b, ensure_ascii=False)


def classify_message(msg: dict, idx: int) -> ClassifiedMessage:
    content = msg.get("content")
    blocks: list[ClassifiedBlock] = []
    if isinstance(content, str):
        blocks.append(ClassifiedBlock(
            raw={"type": "text", "text": content},
            kind=classify_text(content),
            text=content,
        ))
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            blocks.append(ClassifiedBlock(
                raw=b,
                kind=classify_block(b),
                text=_block_text(b),
            ))
    return ClassifiedMessage(index=idx, role=msg.get("role", ""), blocks=blocks)


def _classify_system_prompt(system_field: Any) -> dict:
    """Split the top-level body.system into {billing, tagline, full, blocks}."""
    out: dict[str, Any] = {"blocks": []}
    if isinstance(system_field, str):
        out["blocks"].append(system_field)
        out["full"] = system_field
        return out
    if not isinstance(system_field, list):
        return out
    for b in system_field:
        if not isinstance(b, dict) or b.get("type") != "text":
            continue
        text = b.get("text", "")
        out["blocks"].append(text)
        stripped = text.lstrip()
        if stripped.startswith("x-anthropic-billing-header"):
            out["billing"] = text
        elif stripped.startswith("You are Claude Code"):
            out["tagline"] = text
        else:
            out["full"] = text
    return out


def _summary(msgs: list[ClassifiedMessage]) -> dict:
    return {
        "total_messages": len(msgs),
        "real_user_turns": sum(1 for m in msgs if m.is_real_user_input),
        "system_only_turns": sum(1 for m in msgs if m.is_system_only),
        "assistant_turns": sum(1 for m in msgs if m.role == "assistant"),
        "tool_use_messages": sum(1 for m in msgs if m.has_tool_use),
        "tool_result_messages": sum(1 for m in msgs if m.has_tool_result),
    }


def classify_dump(dump_data: dict) -> dict:
    """Classify a single dump. Returns plain dicts/lists (JSON-safe)."""
    body = dump_data.get("body") or {}
    messages = body.get("messages") or []
    classified = [
        classify_message(m, i)
        for i, m in enumerate(messages)
        if isinstance(m, dict)
    ]
    return {
        "system_prompt": _classify_system_prompt(body.get("system")),
        "messages": classified,
        "summary": _summary(classified),
    }


# ---------------------------------------------------------------------------
# Per-session aggregation (used by `extract-system` CLI)
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _preview(text: str, n: int = 200) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[:n] + "…"


def _dedup_by_text(
    entries: list[tuple[str, str, int]],
) -> list[dict]:
    """Group (text, first_filename, first_msg_index) by text hash."""
    by_hash: dict[str, dict] = {}
    for text, filename, msg_idx in entries:
        h = _hash(text)
        existing = by_hash.get(h)
        if existing is None:
            existing = {
                "count": 0,
                "preview": _preview(text),
                "length": len(text),
                "first_filename": filename,
                "first_msg_index": msg_idx,
                "samples": [],
            }
            by_hash[h] = existing
        existing["count"] += 1
        if len(existing["samples"]) < 3:
            existing["samples"].append(text)
    return sorted(by_hash.values(), key=lambda x: (-x["count"], x["first_filename"]))


def _top_level_kind(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("x-anthropic-billing-header"):
        return "billing"
    if stripped.startswith("You are Claude Code"):
        return "tagline"
    return "full"


# Billing headers differ only in a per-request hash; bucket them all under one
# key so a session with 200 dumps doesn't produce 200 billing entries.
_BILLING_KEY = "<billing-bucket>"


def _top_level_key(text: str, kind: str) -> str:
    if kind == "billing":
        return _BILLING_KEY
    return _hash(text)


def aggregate_session(paths: list[Path]) -> dict:
    """Build the per-session aggregate JSON from a list of dump file paths."""
    filenames: list[str] = []
    first_ts: str | None = None
    last_ts: str | None = None
    top_level_seen: list[tuple[str, str]] = []  # (text, filename)
    in_msg_entries: list[tuple[str, str, int, str]] = []  # (text, filename, msg_idx, kind)
    summary_totals = {
        "total_messages": 0,
        "real_user_turns": 0,
        "system_only_turns": 0,
        "assistant_turns": 0,
        "tool_use_messages": 0,
        "tool_result_messages": 0,
    }
    session_id: str | None = None

    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        filenames.append(path.name)
        ts = data.get("ts")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        if session_id is None:
            session_id = dump.get_header(data.get("headers"), "x-claude-code-session-id")
        classified = classify_dump(data)
        for block_text in classified["system_prompt"].get("blocks", []):
            top_level_seen.append((block_text, path.name))
        for m in classified["messages"]:
            for b in m.blocks:
                if b.kind in SYSTEM_KINDS:
                    in_msg_entries.append((b.text, path.name, m.index, b.kind))
        for k, v in classified["summary"].items():
            summary_totals[k] += v

    # Deduplicate top-level system blocks. Billing headers are all bucketed
    # under one entry; their per-request hash count is tracked separately.
    by_hash: dict[str, dict] = {}
    distinct_billing_hashes = 0
    for text, filename in top_level_seen:
        kind = _top_level_kind(text)
        key = _top_level_key(text, kind)
        if key not in by_hash:
            by_hash[key] = {
                "kind": kind,
                "length": len(text),
                "preview": _preview(text),
                "first_filename": filename,
            }
        if kind == "billing":
            distinct_billing_hashes += 1
    for v in by_hash.values():
        if v["kind"] == "billing":
            v["distinct_count"] = distinct_billing_hashes
    top_level = sorted(
        by_hash.values(),
        key=lambda x: ({"billing": 0, "tagline": 1, "full": 2}.get(x["kind"], 9),
                        x["first_filename"]),
    )

    in_message = {
        "system_reminders": _dedup_by_text(
            [(t, f, i) for (t, f, i, k) in in_msg_entries if k == SYSTEM_REMINDER]
        ),
        "commands": _dedup_by_text(
            [(t, f, i) for (t, f, i, k) in in_msg_entries if k == COMMAND_META]
        ),
        "continuations": _dedup_by_text(
            [(t, f, i) for (t, f, i, k) in in_msg_entries if k == CONTINUATION]
        ),
        "environments": _dedup_by_text(
            [(t, f, i) for (t, f, i, k) in in_msg_entries if k == ENVIRONMENT_CONTEXT]
        ),
        "local_commands": _dedup_by_text(
            [(t, f, i) for (t, f, i, k) in in_msg_entries if k == LOCAL_COMMAND]
        ),
    }

    return {
        "session_id": session_id,
        "request_count": len(filenames),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "filenames": filenames,
        "top_level_system": top_level,
        "in_message_systems": in_message,
        "summary": {
            **summary_totals,
            "distinct_full_prompts": sum(1 for v in top_level if v["kind"] == "full"),
            "distinct_billing_headers": distinct_billing_hashes,
            "distinct_system_reminders": len(in_message["system_reminders"]),
            "distinct_commands": len(in_message["commands"]),
        },
    }
