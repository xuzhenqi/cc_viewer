#!/usr/bin/env bash
# Start the capture proxy on port 8080 with default upstream discovery.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m claude_proxy run "$@"