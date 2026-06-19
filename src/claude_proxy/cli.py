"""Command-line entry point for claude_proxy."""
from __future__ import annotations

import typer
import uvicorn

from .server import app, state
from .viewer import app as viewer_app


cli = typer.Typer(add_completion=False, invoke_without_command=True)


def _run(
    port: int = typer.Option(8080, "-p", "--port", help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", "-h", "--host", help="Host to bind to."),
    upstream: str = typer.Option(
        "https://api.minimaxi.com/anthropic",
        "-u",
        "--upstream",
        help="Upstream URL to forward requests to.",
    ),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload (dev only)."),
):
    """Start the capture proxy."""
    state.configure(upstream=upstream)

    typer.echo(f"Listening on   http://{host}:{port}")
    typer.echo(f"Forwarding to  {upstream}")
    typer.echo(f"Dumping to     data/")
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
    port: int = typer.Option(8081, "-p", "--port", help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", "-h", "--host", help="Host to bind to."),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload (dev only)."),
):
    """Start the read-only viewer for captured requests in data/."""
    typer.echo(f"Viewer listening on http://{host}:{port}")
    typer.echo(f"Reading from      data/")
    uvicorn.run(viewer_app, host=host, port=port, log_level="warning", reload=reload)


cli.command(name="view")(_view)


if __name__ == "__main__":
    cli()