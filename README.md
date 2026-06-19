# claude_proxy

Local HTTP proxy that captures every request Claude Code sends to the Anthropic
API, dumps them to disk as JSON, and forwards them transparently to whatever
endpoint Claude Code is configured to talk to.

The goal: study the real `messages`, `system`, and `tools` arrays that Claude Code
sends in plan-mode vs execute-mode — by capturing them as they fly past, without
re-implementing Claude Code via an SDK.

## Install

```bash
pip install -e .
```

## Use

Terminal A — start the proxy:
```bash
python -m claude_proxy run
# or
./scripts/start_proxy.sh
```

The proxy forwards to `https://api.minimaxi.com/anthropic` by default.
Override with `--upstream` if needed.

Terminal B — point Claude Code at the proxy and run as usual:
```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
claude
```

Each request Claude Code makes will appear as a JSON file under `data/`. Stop the
proxy with Ctrl+C when you're done capturing.

## Upstream URL

By default, the proxy forwards to `https://api.minimaxi.com/anthropic`. Override
with `--upstream <url>`:

```bash
python -m claude_proxy run --upstream https://api.anthropic.com
```

## Output format

Each request becomes one file, `data/req-NNNNN-<utc-timestamp>.json`:

```json
{
  "n": 1,
  "ts": "2026-06-14T10:30:45.123456+00:00",
  "method": "POST",
  "path": "v1/messages",
  "query": "",
  "upstream": "https://api.minimaxi.com/anthropic",
  "headers": {
    "x-api-key": "<redacted>",
    "anthropic-version": "2023-06-01",
    "content-type": "application/json"
  },
  "body_bytes_len": 12345,
  "body": {
    "model": "MiniMax-M3",
    "system": "...",
    "messages": [...],
    "tools": [...]
  }
}
```

Sensitive headers (`x-api-key`, `authorization`, `cookie`, `set-cookie`,
`proxy-authorization`) are replaced with `<redacted>` before writing. Bodies that
aren't valid JSON are preserved as base64 under `_raw_b64`.

## Verify

```bash
# List captured requests
ls data/

# Pretty-print a single request's body
cat data/req-00001-*.json | jq '.body | {model, system_len: (.system | length), tools: [.tools[].name], msg_count: (.messages | length)}'
```

## Project layout

```
src/claude_proxy/
├── __init__.py
├── __main__.py     # python -m claude_proxy entrypoint
├── cli.py          # typer CLI (run)
├── server.py       # FastAPI proxy + streaming passthrough
└── dump.py         # JSON-per-request writer
scripts/
└── start_proxy.sh  # convenience launcher
data/               # captured traffic (gitignored)
```