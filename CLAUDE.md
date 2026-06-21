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
├── cli.py        # typer app; defines `run`, `view`, `extract-system` subcommands
├── server.py     # capture proxy: catch-all routes + streaming passthrough
├── dump.py       # per-request JSON writer; redacts sensitive headers
├── prompts.py    # prompt-catalog data layer (Markdown + YAML frontmatter, SHA-256)
├── systems.py    # per-session system-prompt aggregator with catalog references
├── classify.py   # block-kind classification (mirrored in static/app.js)
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

- Independent process on a different port (default 8003); does not forward anything upstream. Reads only the JSON files `dump.py` produced, the per-session aggregates `cli.py extract-system` produced, and the prompt catalog at `--prompts-dir`.
- `/api/requests` returns a summary list (filename, n, ts, method, path, model, msg_count, session_id) sorted newest-first.
- `/api/requests/{filename}` returns the full captured request. Filenames are validated against `^req-\d{5}-[\w\-]+\.json$` to prevent path traversal.
- `/api/sessions` and `/api/sessions/{filename}` expose the per-session aggregates written by `extract-system`.
- `/api/catalog/entries` (and `POST` / `PUT /{hash}` / `DELETE /{hash}`) manages the prompt catalog: one Markdown file per entry, with YAML frontmatter carrying the SHA-256 identity hash and metadata.
- `/api/prompts/{name}` resolves a prompt by name and bundles a `referenced_in` list (most recent 200 captures that contain this prompt in `body.system`) by streaming `data/req-*.json` and matching on SHA-256. Used by the `/prompts/<name>` page.
- `/prompts/{name}` is an SPA deep-link that returns `index.html`; the JS router reads `location.pathname` and renders the prompt view.
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

### `extract-system` — per-session system aggregates + prompt catalog

A third subcommand of the CLI, `python -m claude_proxy extract-system`, walks the dumps grouped by `x-claude-code-session-id` and writes one `<session-id>.json` per session into `--systems-dir`. Unlike the proxy, this is an offline, idempotent analysis step.

Key design points:

- **Only the chronologically last request of each session is processed.** Claude Code re-sends the cumulative message list on every turn, so the last request already contains every system block currently in scope. The CLI avoids re-deduping across requests.
- **No prompt text is embedded in the systems JSON.** Each block in the output references the canonical prompt catalog (`--prompts-dir`, see `prompts.py` below) via a `catalog_match: {name, hash, ratio}` field when an exact-hash or near-duplicate hit exists. Blocks with no match carry the raw `text` inline so the user can see and re-promote them.
- **Similarity match** uses `difflib.SequenceMatcher` (stdlib). The default threshold is 0.85, tuned by `--similarity-threshold`. This handles whitespace/version drift between captures.
- **Auto-promote**: by default, blocks with no exact-hash and no fuzzy match are written to `--prompts-dir` as new catalog entries (`--no-promote` disables this). Slug comes from the existing `prompts.derive_slug`.
- **Seed migration**: on first run, `migrate_seed_prompts(prompts_dir)` rewrites any bare `.md` files (no frontmatter) in `--prompts-dir` to the catalog format in place, preserving mtime. Idempotent.

Output shape (one file per session):

```json
{
  "session_id": "...",
  "request_count": 42,
  "first_ts": "...", "last_ts": "...",
  "last_request_filename": "req-00042-...json",
  "summary": { "total_messages": ..., "top_level_blocks": N, "in_message_blocks": M, ... },
  "top_level_system": [
    { "kind": "billing|tagline|full", "hash": "sha256:...", "length": N,
      "source_filename": "req-...json", "text": "...",
      "catalog_match": { "name": "...", "hash": "sha256:...", "ratio": 0.97 } }
  ],
  "in_message_systems": {
    "system_reminders": [ { "hash": "...", "source_filename": "...", "source_msg_index": 5, ... } ],
    "commands":         [ ... ],
    "continuations":    [ ... ],
    "environments":     [ ... ]
  }
}
```

CLI flags: `--src` (default `--data-dir`), `--systems-dir` (default `./systems`), `--prompts-dir` (default `./prompts`), `--similarity-threshold` (0..1, default 0.85), `--no-promote`.

### `prompts.py` — prompt catalog

A small data layer for the `prompts/` directory: one Markdown file per prompt, with YAML frontmatter carrying `name`, `sha256:…` identity hash, and `created_at` / `updated_at` timestamps. The text body is the prompt itself; the SHA-256 of the body is the immutable identity. Used by `extract-system` for similarity matching and by the viewer for both the catalog CRUD endpoints and the `/prompts/<name>` page.

Public surface: `hash_text`, `CatalogEntry`, `scan_prompts`, `write_prompt`, `derive_slug`, `migrate_seed_prompts`, `existing_hashes`, plus the frontmatter helpers `render_markdown` / `parse_markdown` / `atomic_write`.

## Rules

* **Always run `git commit` after finishing a job — do not wait to be
  asked.** Every completed change (new section, refactor, bug fix, test)
  must be committed before the turn ends. Stage the specific files
  changed (prefer `git add <path>` over `git add -A` / `git add .`) and
  write a message that explains the *why*, not the *what*. If there is
  nothing to commit, say so explicitly rather than skipping silently.

## Code Maintainability

Keep the codebase small and avoid reinventing well-trodden code paths.

* **Prefer mature dependencies over hand-rolled implementations.** When
  stdlib or an already-declared third-party dependency provides the
  behaviour, use it instead of reimplementing inline.
