"""Command-line entry point for claude_proxy."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import typer
import uvicorn

from . import classify, dump
from .prompts import existing_hashes, hash_text, write_prompt
from .server import app, state
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


def _extract_system(
    src: Path = typer.Option(
        None,
        "-s",
        "--src",
        help="Input dump directory (defaults to the same as the viewer's --data-dir).",
    ),
    dst: Path = _SYSTEMS_DIR_OPTION,
):
    """Extract system-filled content from captured dumps, grouped by session id.

    Writes one <session-id>.json per distinct session into --systems-dir. Each
    output file contains the deduplicated top-level system prompts plus every
    in-message system reminder / command metadata / continuation banner seen
    in that session, with counts and source-dump pointers.
    """
    src = src or dump.DATA_DIR
    if not src.exists():
        typer.echo(f"Source directory not found: {src}", err=True)
        raise typer.Exit(1)
    dst.mkdir(parents=True, exist_ok=True)
    dump.configure_data_dir(src)

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
    for sid, paths in sorted(by_session.items(), key=lambda kv: kv[0]):
        agg = classify.aggregate_session(paths)
        out_name = sid if sid != classify.NO_SESSION else classify.NO_SESSION
        out_path = dst / f"{out_name}.json"
        out_path.write_text(json.dumps(agg, indent=2, ensure_ascii=False))
        typer.echo(
            f"  {sid[:8] if sid != classify.NO_SESSION else '(no session)'}  "
            f"{agg['request_count']:>4} dumps  "
            f"{agg['summary']['distinct_full_prompts']} full prompts  "
            f"{agg['summary']['distinct_system_reminders']} reminder variants  ->  "
            f"{out_path.name}"
        )
        written += 1

    typer.echo("")
    typer.echo(f"Wrote {written} session file(s) to {dst}/")

    # Also build the prompts/ catalog: hash each text block in the LAST
    # request of every session; write any text that appears in >1 distinct
    # session. Within a session we dedupe identical text blocks (the last
    # request is append-only, so prior occurrences are already contained in
    # it) and skip any hash already present in prompts/ (e.g. seed entries).
    prompts_dir = Path("prompts")
    prompts_dir.mkdir(parents=True, exist_ok=True)
    configure_prompts_dir(prompts_dir)
    written_prompts = _extract_repeated_prompts(by_session, prompts_dir)

    if written_prompts:
        typer.echo("")
        typer.echo(f"Wrote {len(written_prompts)} prompt(s) to {prompts_dir}/")
        for path, h, count in written_prompts:
            typer.echo(f"  {h[:23]}  {count:>3} sessions  {path.name}")
    else:
        typer.echo("")
        typer.echo(f"No repeated prompts found across sessions; {prompts_dir}/ unchanged.")


cli.command(name="extract-system")(_extract_system)


def _extract_repeated_prompts(
    by_session: dict[str, list[Path]],
    prompts_dir: Path,
) -> list[tuple[Path, str, int]]:
    """Hash every text block in the last request of each session; collect
    hashes that appeared in >1 distinct session; write each as a prompt
    catalog entry. Returns [(path, hash, session_count), ...] sorted by
    (-session_count, hash)."""
    buckets: dict[str, dict] = {}  # hash -> {"text": str, "sessions": set[str]}

    for sid, paths in by_session.items():
        if not paths:
            continue
        # Filenames sort by counter + timestamp, so max-by-name == last request.
        last_path = max(paths, key=lambda p: p.name)
        try:
            data = json.loads(last_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        body = data.get("body") or {}
        messages = body.get("messages") or []
        seen: set[str] = set()  # dedupe text repeats inside the last request
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                blocks = [b for b in content if isinstance(b, dict)]
            else:
                continue
            for block in blocks:
                if block.get("type") != "text":
                    continue
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                h = hash_text(text)
                if h in seen:
                    continue
                seen.add(h)
                bucket = buckets.setdefault(h, {"text": text, "sessions": set()})
                bucket["sessions"].add(sid)

    existing = existing_hashes(prompts_dir)
    written: list[tuple[Path, str, int]] = []
    for h, bucket in sorted(
        buckets.items(),
        key=lambda kv: (-len(kv[1]["sessions"]), kv[0]),
    ):
        if len(bucket["sessions"]) <= 1:
            continue
        result = write_prompt(prompts_dir, bucket["text"], existing=existing)
        if result is None:
            continue
        path, written_hash = result
        existing.add(written_hash)
        written.append((path, written_hash, len(bucket["sessions"])))
    return written


if __name__ == "__main__":
    cli()
