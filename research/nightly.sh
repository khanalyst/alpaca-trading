#!/usr/bin/env bash
# Nightly research cycle: refresh data, resolve forward evidence, re-score
# every registered strategy, regenerate the report.
#
# Idempotent and safe to kill: downloads resume, and every step rewrites its
# output rather than appending. Exits non-zero if the tournament's benchmark
# check fails, so a broken harness surfaces as a failed unit rather than as a
# quietly wrong report.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
DATA="${DATA_DIR:-$ROOT/runtime/research/data}"
MODE="${AGENT_MODE:-demo}"
DAYS="${HISTORY_DAYS:-730}"

if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY (set PYTHON=/path/to/python)" >&2
  exit 2
fi

echo "=== $(date -u +%FT%TZ) refreshing market history ==="
# Open interest and funding have short retention, so this re-fetches the
# recent window every night. Candles resume and are skipped when covered.
"$PY" research/download_okx_history.py \
  --out "$DATA" --days "$DAYS" --min-volume-usd 30000000 --max-symbols 26 \
  || echo "WARNING: history refresh incomplete; scoring the data on disk" >&2

echo "=== $(date -u +%FT%TZ) resolving forward evidence from the journal ==="
"$PY" research/export_live.py --mode "$MODE" --data "$DATA" \
  || echo "WARNING: no forward evidence resolved this run" >&2

echo "=== $(date -u +%FT%TZ) scoring the tournament ==="
"$PY" research/tournament.py --data "$DATA"
status=$?

echo "=== $(date -u +%FT%TZ) done (tournament exit $status) ==="
exit $status
