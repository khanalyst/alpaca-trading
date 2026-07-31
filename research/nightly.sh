#!/usr/bin/env bash
# Nightly research cycle.
#
# Two evidence paths run here and they are not equals. The AUTHORITATIVE
# path replays the recorded snapshot through the production contract and risk
# engine; the EXPLORATORY path recomputes indicators from downloaded OHLCV.
# See research/plan/RECONCILIATION.md - a tier may be lowered on the second
# and raised only on the first.
#
# The authoritative path runs FIRST and its gate is hard. If G2 fidelity
# fails, the replay does not reproduce the live agent's own decisions, every
# number downstream of it is worthless, and this script stops rather than
# producing a report that looks fine. That is the plan's instruction: treat a
# G2 failure as a full stop, not a debugging task to work around.
#
# Historical downloads, durable findings, tournament runs and backup histories
# are append-only; only documented top-level latest views are refreshed. A
# killed run retains its evidence instead of replacing prior history.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ -n "${DATA_DIR:-}" ]; then
  # Explicit override names the exact fresh snapshot directory.
  DATA="$DATA_DIR"
else
  DATA="$ROOT/runtime/research/snapshots/$(date -u +%Y%m%dT%H%M%SZ)"
fi
MODE="${AGENT_MODE:-demo}"
DAYS="${HISTORY_DAYS:-730}"

if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY (set PYTHON=/path/to/python)" >&2
  exit 2
fi

PRICES="${PRICE_CACHE:-$ROOT/research/cache/prices.db}"
JOURNAL="${JOURNAL_DB:-$ROOT/runtime/$MODE/journal.db}"
STORE="${FINDINGS_DB:-$ROOT/research/cache/findings.db}"

# ---------------------------------------------------------------------------
# Authoritative path: journal replay.
# ---------------------------------------------------------------------------

echo "=== $(date -u +%FT%TZ) readiness ==="
# First, because it answers "is any of the rest worth running yet?" and
# because it is the one command that fails loudly when a passive order may
# have been left resting. Non-zero here is a real problem, not a delay.
"$PY" research.py readiness --db "$JOURNAL" || readiness_failed=1

echo "=== $(date -u +%FT%TZ) research learning loop ==="
# One invocation reviews at most one completed outcome. Provider or parse
# failures are persisted for retry and must not abort the wider nightly run.
"$PY" research.py research-loop --store "$STORE" \
  || echo "WARNING: research review deferred; deterministic outcomes remain stored" >&2

if [ -f "$JOURNAL" ]; then
  echo "=== $(date -u +%FT%TZ) corpus ==="
  "$PY" research.py corpus stats --db "$JOURNAL"

  echo "=== $(date -u +%FT%TZ) gate G2: replay fidelity ==="
  # Hard gate. Nothing below this line means anything if the replay cannot
  # reproduce what the agent actually decided.
  set +e
  "$PY" research.py replay --db "$JOURNAL" --variant momentum.baseline \
    --replay-mode recorded_llm --check-fidelity
  g2=$?
  set -e
  case "$g2" in
    0) ;;
    4) echo "G2 collecting - fewer than 100 proposals are recorded." >&2
       echo "Gated commands below will refuse; collection continues." >&2 ;;
    *) echo "G2 FAILED - stopping. Every downstream number would be " >&2
       echo "precise, plausible, internally consistent and wrong." >&2
       exit 3 ;;
  esac

  echo "=== $(date -u +%FT%TZ) funnel (gate G4) ==="
  # Published before any sweep: if the binding veto sits downstream of the
  # strategy contract, no contract parameter can change the trade count.
  "$PY" research.py funnel --db "$JOURNAL" --prices "$PRICES" || true

  echo "=== $(date -u +%FT%TZ) decision cadence (B9.2 evidence) ==="
  "$PY" research.py cadence --db "$JOURNAL" || true

  echo "=== $(date -u +%FT%TZ) pre-registered conditioning axes ==="
  # Conditioning first, always: these reuse every trade instead of dividing
  # them, so they are affordable at samples where a parameter sweep is not.
  for spec in "$ROOT"/research/sweeps/*.yaml; do
    echo "--- $(basename "$spec")"
    # A sweep that refuses an underpowered grid exits 3. That is a correct
    # outcome, not a failure of the run.
    "$PY" research.py sweep "$spec" --db "$JOURNAL" --prices "$PRICES" \
      || echo "  (refused or incomplete; see above)"
  done

  echo "=== $(date -u +%FT%TZ) three-arm H-E ==="
  "$PY" research.py three-arm --db "$JOURNAL" --prices "$PRICES" || true

  echo "=== $(date -u +%FT%TZ) paired real-time variant qualification ==="
  "$PY" research.py forward-qualify --store "$STORE" \
    || echo "  (collecting, unscoped, or no promotable edge; see above)"

  echo "=== $(date -u +%FT%TZ) regenerating scorecards ==="
  "$PY" research.py report --store "$STORE"
else
  echo "=== no journal at $JOURNAL; skipping the authoritative path ==="
  echo "The corpus is written by the running agent. Until it exists, only" >&2
  echo "exploratory evidence is available and no tier may be raised." >&2
fi

# ---------------------------------------------------------------------------
# Exploratory path: recomputed OHLCV. Cannot raise a tier.
# ---------------------------------------------------------------------------

echo "=== $(date -u +%FT%TZ) refreshing market history ==="
# A fresh directory makes file membership immutable and prevents a stale symbol
# from an older universe contaminating this run. Open interest and funding have
# short retention, so they are fetched again with the new snapshot.
"$PY" research/download_okx_history.py \
  --out "$DATA" --days "$DAYS" --min-volume-usd 30000000 --max-symbols 26 \
  || echo "WARNING: history snapshot incomplete; tournament will refuse it" >&2

echo "=== $(date -u +%FT%TZ) resolving forward evidence from the journal ==="
"$PY" research/export_live.py --mode "$MODE" --data "$DATA" \
  || echo "WARNING: no forward evidence resolved this run" >&2

echo "=== $(date -u +%FT%TZ) scoring the tournament ==="
set +e
"$PY" research/tournament.py --data "$DATA" --store "$STORE"
status=$?
set -e

echo "=== $(date -u +%FT%TZ) verified append-only backup ==="
backup_args=(backup --store "$STORE" --journal "$JOURNAL" --mode "$MODE")
if [ -n "${BACKUP_TARGET:-}" ]; then
  backup_args+=(--target "$BACKUP_TARGET")
fi
if [ "${REQUIRE_EXTERNAL_BACKUP:-0}" = "1" ]; then
  backup_args+=(--require-external)
fi
set +e
"$PY" research.py "${backup_args[@]}"
backup_status=$?
set -e
if [ "$backup_status" -ne 0 ]; then
  echo "=== backup FAILED; research service will exit nonzero ===" >&2
  # This oneshot service is separate from okx-trader.service. Its failure is
  # visible to systemd without stopping or restarting the trading engine.
  status=5
fi

if [ "${readiness_failed:-0}" = "1" ]; then
  echo "=== readiness reported a FAILED gate; see the top of this log ===" >&2
  status=4
fi

echo "=== $(date -u +%FT%TZ) done (exit $status) ==="
exit $status
