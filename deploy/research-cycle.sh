#!/usr/bin/env bash
set -euo pipefail

# Research is an offline job. Prefer an explicit normalized dataset, then the
# append-only dataset produced by the recorder.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python}"
if ! command -v "$python_bin" >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  python_bin=python3
fi
dataset="${ALPACA_RESEARCH_DATASET:-}"
recorded_root="${ALPACA_RECORDED_DATASET_ROOT:-$repo_root/runtime/research/recorded}"
if [[ "$recorded_root" != /* ]]; then
  recorded_root="$repo_root/$recorded_root"
fi

if [ -z "$dataset" ]; then
  for candidate in "$recorded_root/market.jsonl" "$recorded_root/market.csv"; do
    if [ -s "$candidate" ]; then
      dataset="$candidate"
      break
    fi
  done
fi
if [ -n "$dataset" ] && [ "$dataset" != "-" ] && [[ "$dataset" != /* ]]; then
  dataset="$repo_root/$dataset"
fi
if [ -d "$dataset" ]; then
  for candidate in "$dataset/market.jsonl" "$dataset/market.csv"; do
    if [ -s "$candidate" ]; then
      dataset="$candidate"
      break
    fi
  done
fi
if [ -z "$dataset" ] || { [ "$dataset" != "-" ] && [ ! -s "$dataset" ]; }; then
  printf '%s\n' '{"status":"skipped","reason":"recorded dataset unavailable"}' >&2
  exit 0
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/alpaca-research.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT
validated_input="$dataset"
if [ "$dataset" = "-" ]; then
  validated_input="$tmp_dir/input.jsonl"
  cat > "$validated_input"
fi
bars_input="$tmp_dir/bars.jsonl"
options_input="$tmp_dir/options.jsonl"

if [[ "$validated_input" == *.csv ]]; then
  validated_input="$tmp_dir/market.jsonl"
  "$python_bin" - "$dataset" "$validated_input" "$bars_input" "$options_input" <<'PY'
import csv
import json
import sys

source, target, bars_target, options_target = sys.argv[1:]
def clean(value):
    return None if value in (None, "") else value

with open(source, newline="", encoding="utf-8") as handle, open(
        target, "w", encoding="utf-8") as output, open(
        bars_target, "w", encoding="utf-8") as bars_output, open(
        options_target, "w", encoding="utf-8") as options_output:
    for row in csv.DictReader(handle):
        event = str(row.get("event_type") or "").lower()
        common = {
            "provider": row.get("provider") or "alpaca",
            "feed": row.get("feed") or "iex", "symbol": clean(row.get("symbol")),
            "timestamp": clean(row.get("timestamp")),
            "observed_at": clean(row.get("observed_at") or row.get("timestamp")),
            "as_of": clean(row.get("as_of") or row.get("timestamp")),
        }
        if event in {"bar", "bar_1m"}:
            payload = {"kind": "bar", **common, "open": clean(row.get("open")),
                       "high": clean(row.get("high")), "low": clean(row.get("low")),
                       "close": clean(row.get("close")), "volume": clean(row.get("volume"))}
            serialized = json.dumps(payload, sort_keys=True) + "\n"
            output.write(serialized)
            bars_output.write(serialized)
        elif event == "quote":
            payload = {"kind": "quote", **common, "bid": clean(row.get("bid")),
                       "ask": clean(row.get("ask")), "bid_size": clean(row.get("bid_size")),
                       "ask_size": clean(row.get("ask_size"))}
            output.write(json.dumps(payload, sort_keys=True) + "\n")
        elif event in {"option", "option_snapshot"}:
            payload = {"kind": "option_snapshot", **common,
                       "contract": clean(row.get("contract")),
                       "underlying": clean(row.get("underlying")),
                       "expiration": clean(row.get("expiration")),
                       "strike": clean(row.get("strike")), "right": clean(row.get("right")),
                       "multiplier": clean(row.get("multiplier")),
                       "bid": clean(row.get("bid")), "ask": clean(row.get("ask")),
                       "last": clean(row.get("last")), "bid_size": clean(row.get("bid_size")),
                       "ask_size": clean(row.get("ask_size")), "volume": clean(row.get("volume")),
                       "open_interest": clean(row.get("open_interest")),
                       "underlying_price": clean(row.get("underlying_price"))}
            serialized = json.dumps(payload, sort_keys=True) + "\n"
            output.write(serialized)
            options_output.write(serialized)
PY
else
  # Validate the complete mixed JSONL input but derive bars/options-only views
  # for local replay and presence checks. Invalid JSON remains a hard failure.
  "$python_bin" - "$validated_input" "$bars_input" "$options_input" <<'PY'
import json
import sys

source, bars_target, options_target = sys.argv[1:]
with open(source, encoding="utf-8") as source_handle, open(
        bars_target, "w", encoding="utf-8") as bars_output, open(
        options_target, "w", encoding="utf-8") as options_output:
    for line in source_handle:
        if not line.strip():
            continue
        payload = json.loads(line)
        kind = str(payload.get("kind", "bar")).lower()
        serialized = json.dumps(payload, sort_keys=True) + "\n"
        if kind in {"bar", "underlying", "underlying_bar"}:
            bars_output.write(serialized)
        elif kind in {"option", "option_snapshot"}:
            options_output.write(serialized)
PY
fi

feed="${ALPACA_DATA_FEED:-${ALPACA_STOCK_FEED:-iex}}"
"$python_bin" "$repo_root/research.py" validate-data "$validated_input" \
  --provider alpaca --feed "$feed"

if [ "${ALPACA_RESEARCH_BACKTEST:-1}" = "1" ] && [ -s "$bars_input" ]; then
  "$python_bin" "$repo_root/research.py" backtest-ibr "$bars_input" \
    --provider alpaca --feed "$feed" --vehicle equity
fi

edge_db="${ALPACA_EDGE_DB:-$repo_root/runtime/research/edge_lab.sqlite3}"
if [[ "$edge_db" != /* ]]; then
  edge_db="$repo_root/$edge_db"
fi

run_discovery() {
  local vehicle="$1"
  set +e
  "$python_bin" "$repo_root/research.py" edge discover \
    --data "$validated_input" --vehicle "$vehicle" --lane auto --db "$edge_db"
  local status=$?
  set -e
  # Exit 2 is the documented insufficient/gate-failed outcome. Operational
  # errors use a distinct code and fail the scheduler cycle.
  if [ "$status" -ne 0 ] && [ "$status" -ne 2 ]; then
    exit "$status"
  fi
}

run_factory() {
  local vehicle="$1"
  "$python_bin" "$repo_root/research.py" factory run \
    --data "$validated_input" --vehicle "$vehicle" --db "$edge_db" \
    --strategies "${ALPACA_FACTORY_STRATEGIES:-7}" \
    --variants "${ALPACA_FACTORY_VARIANTS:-4}" \
    --workers "${ALPACA_FACTORY_WORKERS:-7}" \
    --starting-cash "${ALPACA_FACTORY_STARTING_CASH:-100000}" \
    --min-trades "${ALPACA_FACTORY_MIN_TRADES:-100}" \
    --min-sessions "${ALPACA_FACTORY_MIN_SESSIONS:-10}" \
    --alpha "${ALPACA_FACTORY_ALPHA:-0.05}" \
    --max-generations "${ALPACA_FACTORY_MAX_GENERATIONS:-5}"
}

if [ -s "$bars_input" ]; then
  run_discovery equity
  if [ "${ALPACA_FACTORY_ENABLED:-1}" = "1" ]; then
    run_factory equity
  fi
fi
if [ -s "$options_input" ]; then
  run_discovery option
  if [ "${ALPACA_FACTORY_ENABLED:-1}" = "1" ]; then
    run_factory option
  fi
fi
