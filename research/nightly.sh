#!/usr/bin/env bash
# Nightly research cycle.
#
# Two evidence paths run here and they are not equals. The AUTHORITATIVE
# path replays the recorded snapshot through the production contract and risk
# engine; the EXPLORATORY path recomputes indicators from downloaded OHLCV.
# See research/AUTONOMOUS_RESEARCH.md - a tier may be lowered on the second and
# raised only on the first.
#
# The authoritative path runs FIRST and proposal fidelity is a hard boundary.
# If replay does not reproduce the live agent's own decisions, every number
# downstream of it is worthless, and this script stops rather than producing a
# report that looks fine. Treat a proposal-fidelity mismatch as a full stop,
# not a debugging task to work around.
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
# Bound the LLM review queue so one nightly run cannot monopolise the
# provider or starve the rest of the evidence pipeline. Override explicitly
# for an operator-run batch; the shipped nightly cap is eight outcomes.
RESEARCH_REVIEW_CAP="${RESEARCH_REVIEW_CAP:-8}"

if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY (set PYTHON=/path/to/python)" >&2
  exit 2
fi

# Resolve research paths and the active strategy through the same validated
# config used by ``research.py``. Environment overrides remain explicit
# operator choices, but the defaults must not drift from config.yaml.
CONFIG_PATH="$ROOT/config.yaml"
CONFIG_VALUES="$($PY - "$CONFIG_PATH" "$ROOT" <<'PY'
import sys
from pathlib import Path
import yaml

from agent.config import validate_config
from agent.variants import baseline_variant_id

config_path, repo = sys.argv[1:]
with open(config_path, encoding="utf-8") as handle:
    cfg = validate_config(yaml.safe_load(handle) or {})
research = cfg.get("research") or {}
root = Path(repo)

def resolve(value, fallback):
    path = Path(value or fallback)
    return path if path.is_absolute() else root / path

print("\t".join((
    str(resolve(research.get("findings_store"),
                "research/cache/findings.db")),
    str(resolve(research.get("staging_store"),
                "research/cache/staging.db")),
    str(cfg["strategy"]["id"]),
    baseline_variant_id(str(cfg["strategy"]["id"])),
)))
PY
)"
IFS=$'\t' read -r CONFIG_FINDINGS CONFIG_STAGING CONFIG_STRATEGY \
  CONFIG_BASELINE <<<"$CONFIG_VALUES"

PRICES="${PRICE_CACHE:-$ROOT/research/cache/prices.db}"
JOURNAL="${JOURNAL_DB:-$ROOT/runtime/$MODE/journal.db}"
# These stores are part of runtime identity. Do not allow a nightly-only
# environment override to point research at a different database than the
# running trader uses.
STORE="$CONFIG_FINDINGS"
STAGING="$CONFIG_STAGING"
STRATEGY_ID="$CONFIG_STRATEGY"
BASELINE_VARIANT="$CONFIG_BASELINE"

# Qualification must name the deterministic feed lane. Wildcard selection in
# deterministic mode creates only deterministic scopes; it does not create an
# ``:llm`` sibling. Analyst mode may create that separate, non-comparable scope,
# so an operator may provide FORWARD_SCOPE explicitly. Otherwise derive it only
# when the journal proves one account identity and the configured feed version
# supplies one unambiguous deterministic scope. If identity is unavailable,
# leave it empty and the CLI remains fail-closed.
FORWARD_SCOPE="${FORWARD_SCOPE:-}"
if [ -z "$FORWARD_SCOPE" ] && [ -f "$JOURNAL" ]; then
  FORWARD_SCOPE="$($PY - "$JOURNAL" "$MODE" "$ROOT/config.yaml" <<'PY'
import sqlite3
import sys

journal, mode, config_path = sys.argv[1:]
try:
    with sqlite3.connect(f"file:{journal}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT account_fingerprint FROM runs "
            "WHERE mode=? AND account_fingerprint IS NOT NULL "
            "AND account_fingerprint <> ''",
            (mode,),
        ).fetchall()
except sqlite3.Error:
    rows = []

identities = {str(row[0]) for row in rows if row[0]}
if len(identities) == 1:
    try:
        import yaml
        with open(config_path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        feed = int((config.get("research") or {}).get(
            "forward_feed_version") or 1)
    except (OSError, TypeError, ValueError):
        feed = None
    if feed is not None:
        print(f"{mode}:{next(iter(identities))}:feed-v{feed}")
PY
)"
fi

# Authoritative path: journal replay.

echo "=== $(date -u +%FT%TZ) readiness ==="
# Readiness runs first because it detects a possibly resting passive order;
# nonzero is an operational failure, not a collection delay.
"$PY" research.py readiness --db "$JOURNAL" --store "$STORE" \
  || readiness_failed=1

if [ -f "$JOURNAL" ]; then
  echo "=== $(date -u +%FT%TZ) corpus ==="
  "$PY" research.py corpus stats --db "$JOURNAL"

  echo "=== $(date -u +%FT%TZ) proposal-fidelity replay ==="
  # Hard gate. Nothing below this line means anything if the replay cannot
  # reproduce what the agent actually decided.
  set +e
  "$PY" research.py replay --db "$JOURNAL" --variant "$BASELINE_VARIANT" \
    --replay-mode recorded_llm --check-fidelity
  proposal_fidelity_status=$?
  set -e
  case "$proposal_fidelity_status" in
    0) proposal_fidelity_ready=1 ;;
    4) echo "Proposal fidelity collecting - fewer than 100 proposals are recorded." >&2
       echo "Dependent lifecycle commands are skipped; collection continues." >&2
       proposal_fidelity_ready=0 ;;
    *) echo "Proposal fidelity FAILED - stopping. Every downstream number would be " >&2
       echo "precise, plausible, internally consistent and wrong." >&2
       exit 3 ;;
  esac

  echo "=== $(date -u +%FT%TZ) veto-distribution funnel ==="
  # Published before any sweep: if the binding veto sits downstream of the
  # strategy contract, no contract parameter can change the trade count.
  "$PY" research.py funnel --db "$JOURNAL" --prices "$PRICES" || true

  echo "=== $(date -u +%FT%TZ) decision-cadence evidence ==="
  "$PY" research.py cadence --db "$JOURNAL" || true

  echo "=== $(date -u +%FT%TZ) pre-registered conditioning axes ==="
  # Conditioning first, always: these reuse every trade instead of dividing
  # them, so they are affordable at samples where a parameter sweep is not.
  for spec in "$ROOT"/research/sweeps/*.yaml; do
    echo "--- $(basename "$spec")"
    # A sweep that refuses an underpowered grid exits 3. That is a correct
    # outcome, not a failure of the run.
    "$PY" research.py sweep "$spec" --db "$JOURNAL" --prices "$PRICES" \
      --store "$STORE" \
      || echo "  (refused or incomplete; see above)"
  done

  echo "=== $(date -u +%FT%TZ) three-arm analyst-effect analysis ==="
  "$PY" research.py three-arm --db "$JOURNAL" --prices "$PRICES" \
    --store "$STORE" || true

  echo "=== $(date -u +%FT%TZ) paired real-time variant qualification ==="
  forward_args=(forward-qualify --store "$STORE" --strategy "$STRATEGY_ID")
  if [ -n "$FORWARD_SCOPE" ]; then
    forward_args+=(--scope "$FORWARD_SCOPE")
  fi
  "$PY" research.py "${forward_args[@]}" \
    || echo "  (collecting, unscoped, or no promotable edge; see above)"

  if [ "$proposal_fidelity_ready" = "1" ]; then
    # Only now may the lifecycle consume evidence. Corpus loading and proposal
    # fidelity are prerequisites: a provider or validation failure below is a
    # real failed job, and set -e prevents dependent generation/handoff work.
    echo "=== $(date -u +%FT%TZ) adjudicating and refining staged mechanisms ==="
    "$PY" research.py review-staged --store "$STORE" --staging "$STAGING"

    echo "=== $(date -u +%FT%TZ) starting supported staged PAPER lanes ==="
    "$PY" research.py qualify-staged --store "$STORE" --staging "$STAGING"

    echo "=== $(date -u +%FT%TZ) research learning loop ==="
    # Every attempted outcome remains isolated and persisted, but any provider
    # or parse failure makes this invocation nonzero after the bounded batch.
    "$PY" research.py research-loop --store "$STORE" \
      --max-reviews "$RESEARCH_REVIEW_CAP"

    echo "=== $(date -u +%FT%TZ) authoring bounded candidate neighborhoods ==="
    "$PY" research.py author --store "$STORE" --staging "$STAGING"

    echo "=== $(date -u +%FT%TZ) preparing staged human-review handoffs ==="
    "$PY" research.py prepare-handoff --store "$STORE" --staging "$STAGING"
  else
    echo "=== staged lifecycle skipped until proposal fidelity passes ==="
  fi

  echo "=== $(date -u +%FT%TZ) preparing operator review artifacts ==="
  # Only a draft, content-addressed T3 packet is created automatically, and
  # only after every non-manual evidence check passes. Registry/config review
  # and any capital-enabling change remain explicit operator actions.
  "$PY" research.py prepare-review-artifacts --store "$STORE" --db "$JOURNAL" \
    || echo "WARNING: review artifact preparation deferred" >&2

  echo "=== $(date -u +%FT%TZ) candidate shortlist ==="
  "$PY" research.py shortlist --store "$STORE" --staging "$STAGING" \
    --out "$ROOT/research/results/shortlist.md" || true

  echo "=== $(date -u +%FT%TZ) regenerating scorecards ==="
  "$PY" research.py report --store "$STORE"
else
  echo "=== no journal at $JOURNAL; skipping the authoritative path ==="
  echo "The corpus is written by the running agent. Until it exists, only" >&2
  echo "exploratory evidence is available and no tier may be raised." >&2
fi

# Exploratory path: recomputed OHLCV. Cannot raise a tier.

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
  echo "=== readiness reported a failure; see the top of this log ===" >&2
  status=4
fi

echo "=== $(date -u +%FT%TZ) done (exit $status) ==="
exit $status
