"""Command-line entry point for claude_proxy."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import typer
import uvicorn

from . import classify, dump
from .server import app, state
from .viewer import app as viewer_app, configure_systems_dir


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
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload (dev only)."),
):
    """Start the read-only viewer for captured requests."""
    dump.configure_data_dir(data_dir)
    configure_systems_dir(systems_dir)
    typer.echo(f"Viewer listening on http://{host}:{port}")
    typer.echo(f"Reading dumps   from {data_dir}/")
    typer.echo(f"Reading systems from {systems_dir}/")
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


cli.command(name="extract-system")(_extract_system)


if __name__ == "__main__":
    cli()
