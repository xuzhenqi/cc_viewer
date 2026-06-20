# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`claude_proxy` — a local HTTP proxy that sits between Claude Code and the Anthropic-compatible API. It captures every request Claude Code sends, dumps it to disk as JSON, and forwards it transparently upstream. The point is to study the real `messages`, `system`, and `tools` payloads Claude Code emits in plan vs. execute mode, by recording them as they fly past — no SDK re-implementation required.

## Install / run

```bash
pip install -e .                       # editable install (uses src/ layout)
python -m claude_proxy run             # start the capture proxy (default :8002)
./scripts/start_proxy.sh               # convenience wrapper around the above
python -m claude_proxy view            # start the read-only viewer (default :8003)
```

CLI flags (all on `run` and `view`):
- `-p/--port` — listen port (default `8002` for `run`, `8003` for `view`)
- `-h/--host` — bind host (default `127.0.0.1`)
- `-u/--upstream` — upstream base URL (default `https://api.minimaxi.com/anthropic`)
- `-d/--data-dir` — where to write/read captured JSON (default `./data`)
- `--reload` — uvicorn autoreload (dev only)

Pointing Claude Code at the proxy:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8002
claude
```

## Tests / lint

There is no test suite yet (`pyproject.toml` configures `pytest` with `testpaths = ["tests"]`, but the directory does not exist). No linter / formatter is configured. Verify changes manually:

```bash
# Smoke-test the proxy
python -m claude_proxy run &
curl -s -X POST http://127.0.0.1:8002/v1/messages \
  -H 'x-api-key: test' -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"MiniMax-M3","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}'
ls data/                               # expect a req-00001-*.json file
```

## Code architecture

Two independent processes, both `python -m claude_proxy <subcommand>`, both thin wrappers around FastAPI/uvicorn.

```
src/claude_proxy/
├── __main__.py   # re-exports cli.cli  → `python -m claude_proxy` works
├── cli.py        # typer app; defines `run` and `view` subcommands + defaults
├── server.py     # capture proxy: catch-all routes + streaming passthrough
├── dump.py       # per-request JSON writer; redacts sensitive headers
├── viewer.py     # read-only FastAPI app serving a JSON-over-HTTP API
└── static/       # index.html + app.js + style.css for the viewer UI
```

### `server.py` — capture proxy

- Single `ProxyState` singleton holds the upstream URL, a long-lived `httpx.AsyncClient`, and a thread-safe request counter. `cli.py` calls `state.configure(...)` before uvicorn starts.
- `_handle(request, full_path)` is the only handler. It (1) reads the body, (2) increments the counter and calls `dump.dump_request(...)` to write a JSON file *before* forwarding, (3) builds a new httpx request, (4) streams the response back via `StreamingResponse` so SSE/streaming works end-to-end.
- Catch-all `@app.post/@app.get/...("/{full_path:path}")` covers every method/path. A separate `@app.api_route("/")` handles the bare-root case where the captured `full_path` is empty.
- Hop-by-hop headers (`host`, `content-length`, `connection`, `transfer-encoding`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailers`, `upgrade`) are stripped on both ingress and egress.
- Lifespan handler `aclose()`s the httpx client on shutdown.

### `dump.py` — JSON writer

- Module-level `DATA_DIR` (default `Path("data")`); `configure_data_dir()` rebinds it. Both `cli.py` and `viewer.py` call this.
- Filename: `req-NNNNN-<utc-timestamp>.json` (counter is zero-padded, timestamp is `YYYYMMDDTHHMMSS-microsecZ`) — sortable by name = chronological order.
- Body is parsed as JSON when possible; non-JSON bodies are preserved as `{"_raw_b64": base64(...)}`.
- Sensitive headers (`x-api-key`, `authorization`, `cookie`, `set-cookie`, `proxy-authorization`) are replaced with `"<redacted>"` *before* writing — never let real keys touch disk.
- `data/` is gitignored. The dump is atomic per request: written before forwarding, so a proxy or upstream crash doesn't lose the capture.

### `viewer.py` + `static/` — read-only browser UI

- Independent process on a different port (default 8003); does not forward anything upstream. Reads only the JSON files `dump.py` produced.
- `/api/requests` returns a summary list (filename, n, ts, method, path, model, msg_count, session_id) sorted newest-first. The session id is pulled from the `x-claude-code-session-id` header, which is what the latest commit's "Group requests with session id" work added.
- `/api/requests/{filename}` returns the full captured request. Filenames are validated against `^req-\d{5}-[\w\-]+\.json$` to prevent path traversal.
- `/` serves the static SPA; `/static/*` is mounted via `StaticFiles`.

### Data flow

```
Claude Code  ──HTTP──▶  server.py  ──write JSON──▶  data/req-NNNNN-*.json
                              │
                              └──HTTP (streaming)──▶  upstream (api.minimaxi.com/anthropic)
                              ▲
viewer.py  ──read JSON──▶  data/   (separate process, separate port)
     │
     └──HTTP──▶  browser SPA
```

The two processes are decoupled: the proxy never knows the viewer exists, and the viewer can be started/stopped/restarted at any time without affecting capture.
