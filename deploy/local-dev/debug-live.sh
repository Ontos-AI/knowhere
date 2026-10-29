#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/apps/worker"
exec uv run python "$ROOT/deploy/local-dev/debug_live.py" "$@"
