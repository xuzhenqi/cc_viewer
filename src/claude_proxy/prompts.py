"""Prompt catalog: Markdown+YAML frontmatter files in a directory, keyed by SHA-256.

Pure data layer — no module globals, no caches, no locks. The viewer wraps
this with its own dir-mtime cache; the CLI uses scan_prompts() and
write_prompt() directly.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


# Catalog entry filename / hash format constraints. The name regex caps the
# slug at 64 chars of [a-zA-Z0-9._-] starting with an alphanumeric; the
# hash regex pins to sha256:<64hex>.
CATALOG_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
CATALOG_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class CatalogEntry(BaseModel):
    hash: str = Field(pattern=CATALOG_HASH_RE.pattern)
    name: str = Field(pattern=CATALOG_NAME_RE.pattern)
    text: str
    created_at: str
    updated_at: str


def hash_text(text: str) -> str:
    """Stable identity hash for a prompt's text body. Returns 'sha256:<hex>'."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def render_markdown(entry: CatalogEntry) -> str:
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


def parse_markdown(path: Path) -> CatalogEntry | None:
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


def atomic_write(path: Path, content: str, *, tmp_dir: Path | None = None) -> None:
    """Write content to `path` atomically (tmp + fsync + os.replace).

    `tmp_dir` defaults to `path.parent`. Caller is responsible for ensuring
    `tmp_dir` exists if it differs from `path.parent`.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tdir = Path(tmp_dir) if tmp_dir is not None else parent
    tdir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".catalog.", suffix=".md.tmp", dir=str(tdir))
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


def scan_prompts(directory: Path) -> dict[str, CatalogEntry]:
    """Read every *.md in `directory` and return {hash: entry}.

    Broken files are skipped (a warning is printed to stderr). Duplicate
    hashes inside the directory: first file wins, subsequent are skipped.
    """
    out: dict[str, CatalogEntry] = {}
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.md")):
        if not path.is_file():
            continue
        entry = parse_markdown(path)
        if entry is None:
            continue
        if entry.hash in out:
            print(
                f"[catalog] skip {path.name}: duplicate hash {entry.hash[:15]}…",
                file=sys.stderr,
            )
            continue
        out[entry.hash] = entry
    return out


def existing_hashes(directory: Path) -> set[str]:
    """All hashes present in `directory/*.md`. Returns an empty set if the
    directory does not exist."""
    return set(scan_prompts(directory).keys())


_NON_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_SLUG_MAX_LEN = 64


def derive_slug(text: str, directory: Path, hash_hex: str) -> str:
    """Build a filename-safe slug from `text`.

    1. Take up to the first 3 non-empty stripped lines.
    2. Join with '-', lower-case.
    3. Replace every run of non-[a-zA-Z0-9._-] with '-'.
    4. Strip leading/trailing '-'.
    5. Truncate to 64 chars (CATALOG_NAME_RE upper bound), strip a trailing
       '-' after the cut.
    6. Empty → fall back to `prompt-<first8hex>`.
    7. If `directory / f"{base}.md"` already exists, append `-<first8hex>`
       so we never overwrite a curated entry with a different hash.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
    raw = "-".join(lines).lower()
    base = _NON_SLUG_RE.sub("-", raw).strip("-")[:_SLUG_MAX_LEN].rstrip("-")
    if not base:
        return f"prompt-{hash_hex[:8]}"
    if (directory / f"{base}.md").exists():
        suffix = f"-{hash_hex[:8]}"
        max_base = _SLUG_MAX_LEN - len(suffix)
        return base[:max_base].rstrip("-") + suffix
    return base


def write_prompt(
    directory: Path,
    text: str,
    *,
    name: str | None = None,
    existing: set[str] | None = None,
) -> tuple[Path, str] | None:
    """Write a prompt catalog entry to `directory`.

    Returns `(path, hash)` on success. Returns `None` if the hash already
    exists (in `existing` if provided, else in `directory`). The caller is
    responsible for adding the returned hash back into `existing` if it
    wants to write a batch.
    """
    directory.mkdir(parents=True, exist_ok=True)
    text_hash = hash_text(text)
    if existing is None:
        existing = existing_hashes(directory)
    if text_hash in existing:
        return None
    slug = name or derive_slug(text, directory, text_hash.split(":", 1)[1])
    now = _now_iso()
    entry = CatalogEntry(
        hash=text_hash,
        name=slug,
        text=text,
        created_at=now,
        updated_at=now,
    )
    path = directory / f"{slug}.md"
    atomic_write(path, render_markdown(entry), tmp_dir=directory)
    return path, text_hash