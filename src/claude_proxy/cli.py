"""Command-line entry point for claude_proxy."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import typer
import uvicorn

from . import classify, dump
from .prompts import (
    CatalogEntry,
    hash_text,
    migrate_seed_prompts,
    scan_prompts,
    write_prompt,
)
from .server import app, state
from .systems import aggregate_session_systems
from .viewer import app as viewer_app, configure_prompts_dir, configure_systems_dir


cli = typer.Typer(add_completion=False, invoke_without_command=True)


_DATA_DIR_OPTION = typer.Option(
    dump.DATA_DIR,
    "-d",
    "--data-dir",
    help="Directory to read/write captured request dumps.",
)

_SYSTEMS_DIR_OPTION = typer.Option(
    Path("systems"),
    "-S",
    "--systems-dir",
    help="Directory to read/write per-session system-prompt aggregates.",
)

_PROMPTS_DIR_OPTION = typer.Option(
    Path("prompts"),
    "-P",
    "--prompts-dir",
    help="Directory containing the prompt catalog (one .md file per entry).",
)

_SIMILARITY_THRESHOLD_OPTION = typer.Option(
    0.85,
    "--similarity-threshold",
    help="Minimum SequenceMatcher.ratio() (0..1) for a system block to count "
         "as a near-duplicate of a prompt in --prompts-dir.",
    min=0.0,
    max=1.0,
)

_NO_PROMOTE_OPTION = typer.Option(
    False,
    "--no-promote",
    help="Skip writing new prompts to --prompts-dir for unmatched system blocks.",
)


def _run(
    port: int = typer.Option(8002, "-p", "--port", help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", "-h", "--host", help="Host to bind to."),
    upstream: str = typer.Option(
        "https://api.minimaxi.com/anthropic",
        "-u",
        "--upstream",
        help="Upstream URL to forward requests to.",
    ),
    data_dir: Path = _DATA_DIR_OPTION,
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload (dev only)."),
):
    """Start the capture proxy."""
    dump.configure_data_dir(data_dir)
    state.configure(upstream=upstream)

    typer.echo(f"Listening on   http://{host}:{port}")
    typer.echo(f"Forwarding to  {upstream}")
    typer.echo(f"Dumping to     {data_dir}/")
    typer.echo("")
    typer.echo("To use:")
    typer.echo(f"  export ANTHROPIC_BASE_URL=http://{host}:{port}")
    typer.echo("  claude")
    typer.echo("")

    uvicorn.run(app, host=host, port=port, log_level="warning", reload=reload)


@cli.callback()
def _default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _run()


cli.command(name="run")(_run)


def _view(
    port: int = typer.Option(8003, "-p", "--port", help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", "-h", "--host", help="Host to bind to."),
    data_dir: Path = _DATA_DIR_OPTION,
    systems_dir: Path = _SYSTEMS_DIR_OPTION,
    prompts_dir: Path = _PROMPTS_DIR_OPTION,
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload (dev only)."),
):
    """Start the read-only viewer for captured requests."""
    dump.configure_data_dir(data_dir)
    configure_systems_dir(systems_dir)
    configure_prompts_dir(prompts_dir)
    typer.echo(f"Viewer listening on http://{host}:{port}")
    typer.echo(f"Reading dumps   from {data_dir}/")
    typer.echo(f"Reading systems from {systems_dir}/")
    typer.echo(f"Reading prompts from {prompts_dir}/")
    uvicorn.run(viewer_app, host=host, port=port, log_level="warning", reload=reload)


cli.command(name="view")(_view)


def _promote_unmatched_blocks(
    agg: dict,
    prompts_dir: Path,
    existing: set[str],
) -> int:
    """Write a new entry into `prompts_dir` for each block in `agg` that is
    not in the catalog (no `catalog_match`). The aggregate stores the full
    `text` for every block, so we don't need to re-read source dumps.

    Mutates `existing` in place. Returns the number of files written.
    """
    promoted = 0
    blocks_to_check: list[dict] = []
    blocks_to_check.extend(agg.get("top_level_system") or [])
    for kind_blocks in (agg.get("in_message_systems") or {}).values():
        blocks_to_check.extend(kind_blocks)
    for entry in blocks_to_check:
        if entry.get("catalog_match"):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text:
            continue
        if hash_text(text) in existing:
            continue
        result = write_prompt(prompts_dir, text, existing=existing)
        if result is None:
            continue
        _, written_hash = result
        existing.add(written_hash)
        promoted += 1
    return promoted


def _extract_system(
    src: Path = typer.Option(
        None,
        "-s",
        "--src",
        help="Input dump directory (defaults to the same as the viewer's --data-dir).",
    ),
    dst: Path = _SYSTEMS_DIR_OPTION,
    prompts_dir: Path = _PROMPTS_DIR_OPTION,
    similarity_threshold: float = _SIMILARITY_THRESHOLD_OPTION,
    no_promote: bool = _NO_PROMOTE_OPTION,
):
    """Extract system content from the last request of each session.

    For every distinct x-claude-code-session-id, writes one
    `<session-id>.json` into --systems-dir. Each output references the
    canonical prompt catalog (--prompts-dir) by name+hash; the original
    prompt text lives in the catalog, not in the systems JSON.

    Side effect: any `prompts/*.md` that lacks YAML frontmatter is
    rewritten in place to add it (idempotent). System blocks that do not
    match any catalog entry (exact or fuzzy) are written into
    --prompts-dir as new entries, unless --no-promote is set.
    """
    src = src or dump.DATA_DIR
    if not src.exists():
        typer.echo(f"Source directory not found: {src}", err=True)
        raise typer.Exit(1)
    dst.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    dump.configure_data_dir(src)

    # Migrate any bare .md seed files in prompts/ to the frontmatter shape
    # *before* loading the catalog, so the catalog picks up the migrated
    # entries. Idempotent on re-runs.
    migrated = migrate_seed_prompts(prompts_dir)
    if migrated:
        typer.echo(f"Migrated {migrated} bare prompt file(s) to frontmatter format.")
        typer.echo("")

    catalog: dict[str, CatalogEntry] = scan_prompts(prompts_dir)
    existing: set[str] = set(catalog.keys())

    # Group dumps by session id; un-sessioned dumps land in NO_SESSION.
    by_session: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(src.glob("req-*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        sid = dump.get_header(data.get("headers"), "x-claude-code-session-id")
        key = sid or classify.NO_SESSION
        by_session[key].append(path)

    written = 0
    promoted_total = 0
    for sid, paths in sorted(by_session.items(), key=lambda kv: kv[0]):
        agg = aggregate_session_systems(
            paths,
            catalog=catalog,
            similarity_threshold=similarity_threshold,
        )
        out_path = dst / f"{sid}.json"
        out_path.write_text(json.dumps(agg, indent=2, ensure_ascii=False))
        written += 1

        top = agg.get("top_level_system") or []
        in_msg = agg.get("in_message_systems") or {}
        in_msg_total = sum(len(v) for v in in_msg.values())
        matched = sum(
            1 for v in [top, *in_msg.values()] for e in v if e.get("catalog_match")
        )

        typer.echo(
            f"  {sid[:12]:<14}  "
            f"{agg.get('request_count', 0):>4} dumps  "
            f"{len(top):>2} top · {in_msg_total:>3} in-msg · "
            f"{matched:>3} matched  ->  {out_path.name}"
        )

        if not no_promote:
            promoted_total += _promote_unmatched_blocks(agg, prompts_dir, existing)

    typer.echo("")
    typer.echo(f"Wrote {written} session file(s) to {dst}/  (threshold={similarity_threshold})")
    if no_promote:
        typer.echo("--no-promote: skipped auto-promotion to prompts/")
    elif promoted_total:
        typer.echo(f"Promoted {promoted_total} new prompt(s) to {prompts_dir}/")
    else:
        typer.echo(f"No new prompts to promote; {prompts_dir}/ unchanged.")


cli.command(name="extract-system")(_extract_system)


if __name__ == "__main__":
    cli()
