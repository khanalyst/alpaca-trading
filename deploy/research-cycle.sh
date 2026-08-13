#!/usr/bin/env bash
set -euo pipefail

# Research is an offline job. Prefer an explicit normalized dataset, then the
# append-only dataset produced by the recorder.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python}"
if ! command -v "$python_bin" >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  python_bin=python3
fi

# ``research.py`` emits detailed proof/result JSON itself. Keep those lines on
# their original streams and add one terminal record for the scheduler.
cycle_finalized=0
cycle_success=0
cycle_no_edge=0
cycle_unevaluable=0
cycle_outcomes=()

emit_cycle() {
  local status="$1"
  local reason="$2"
  local exit_code="$3"
  local outcomes="${cycle_outcomes[*]-}"
  "$python_bin" - "$status" "$reason" "$exit_code" "$outcomes" "$cycle_success" "$cycle_no_edge" <<'PY'
import json
import sys

status, reason, exit_code, raw_outcomes, success, no_edge = sys.argv[1:]
print(json.dumps({
    "schema": "research-cycle.v1", "status": status, "reason": reason,
    "exit_code": int(exit_code),
    "outcomes": raw_outcomes.split() if raw_outcomes else [],
    "proofs": bool(int(success)), "no_edge": bool(int(no_edge)),
}, sort_keys=True))
PY
}

finish() {
  local status="$1"
  local reason="$2"
  local exit_code="$3"
  cycle_finalized=1
  emit_cycle "$status" "$reason" "$exit_code"
  exit "$exit_code"
}

on_exit() {
  local code=$?
  if [ "$cycle_finalized" -eq 0 ]; then
    emit_cycle "failed" "research cycle aborted before completion" "$code"
  fi
  rm -rf "${tmp_dir:-}" 2>/dev/null || true
  exit "$code"
}
trap on_exit EXIT

# Load only provider keys from an optional, separate dotenv-style file. Never
# source arbitrary shell and never consult the broker credential file.
load_llm_secrets() {
  local source="${ALPACA_RESEARCH_LLM_SECRETS_FILE:-}"
  local line key value
  [ -z "$source" ] && return 0
  if [[ "$source" != /* ]]; then
    source="$repo_root/$source"
  fi
  [ -r "$source" ] || finish "failed" "LLM secrets file is unreadable" 3
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#${line%%[![:space:]]*}}"
    [ -z "$line" ] || [[ "$line" == \#* ]] || {
      line="${line#export }"
      if [[ "$line" =~ ^(OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENAI_BASE_URL|ANTHROPIC_BASE_URL)[[:space:]]*=(.*)$ ]]; then
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        value="${value#${value%%[![:space:]]*}}"
        value="${value%${value##*[![:space:]]}}"
        if [[ "$value" == \"*\" && "$value" == *\" ]]; then
          value="${value:1:${#value}-2}"
        elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
          value="${value:1:${#value}-2}"
        fi
        export "$key=$value"
      fi
    }
  done < "$source"
}

load_llm_secrets
dataset="${ALPACA_RESEARCH_DATASET:-}"
recorded_root="${ALPACA_RECORDED_DATASET_ROOT:-$repo_root/runtime/research/recorded}"
if [[ "$recorded_root" != /* ]]; then
  recorded_root="$repo_root/$recorded_root"
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/alpaca-research.XXXXXX")"

# The recorder partitions its corpus by session date. Concatenate the requested
# window of partitions once, in order, instead of keeping an unbounded file.
merge_partitions() {
  local root="$1"
  local window="${ALPACA_RESEARCH_SESSION_WINDOW:-0}"
  local merged="$tmp_dir/market.csv"
  local -a files
  files=()
  # macOS ships Bash 3.2, which has no mapfile/readarray.  Read the sorted
  # partition list with the POSIX-compatible read loop instead.
  local listed_file
  while IFS= read -r listed_file; do
    [ -n "$listed_file" ] && files[${#files[@]}]="$listed_file"
  done < <(ls -1 "$root"/market-*.csv 2>/dev/null | sort)
  [ "${#files[@]}" -gt 0 ] || return 1
  if [ "$window" -gt 0 ] && [ "${#files[@]}" -gt "$window" ]; then
    files=("${files[@]: -$window}")
  fi
  head -n 1 "${files[0]}" > "$merged"
  local file
  for file in "${files[@]}"; do
    tail -n +2 "$file" >> "$merged"
  done
  printf '%s' "$merged"
}

if [ -z "$dataset" ]; then
  for candidate in "$recorded_root/market.jsonl" "$recorded_root/market.csv"; do
    if [ -s "$candidate" ]; then
      dataset="$candidate"
      break
    fi
  done
fi
if [ -z "$dataset" ] && [ -d "$recorded_root/sessions" ]; then
  dataset="$(merge_partitions "$recorded_root/sessions" || true)"
fi
if [ -n "$dataset" ] && [ -d "$dataset/sessions" ]; then
  dataset="$(merge_partitions "$dataset/sessions" || true)"
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
if [ -z "$dataset" ] || { [ "$dataset" != "-" ] && { [ -d "$dataset" ] || [ ! -s "$dataset" ]; }; }; then
  finish "no_data" "recorded dataset unavailable" 2
fi

validated_input="$dataset"
if [ "$dataset" = "-" ]; then
  validated_input="$tmp_dir/input.jsonl"
  cat > "$validated_input"
fi
if [ ! -s "$validated_input" ] || ! grep -q '[^[:space:]]' "$validated_input"; then
  finish "no_data" "recorded dataset is empty" 2
fi
bars_input="$tmp_dir/bars.jsonl"
options_input="$tmp_dir/options.jsonl"
# Quotes are the executable price at a boundary fill instant. Routing them
# into their own view keeps the bars-only replay input valid while letting the
# shared cost/fill model use recorded quotes instead of bar prices.
quotes_input="$tmp_dir/quotes.jsonl"

if [[ "$validated_input" == *.csv ]]; then
  validated_input="$tmp_dir/market.jsonl"
  "$python_bin" - "$dataset" "$validated_input" "$bars_input" "$options_input" "$quotes_input" <<'PY'
import csv
import json
import sys

source, target, bars_target, options_target, quotes_target = sys.argv[1:]
def clean(value):
    return None if value in (None, "") else value

with open(source, newline="", encoding="utf-8") as handle, open(
        target, "w", encoding="utf-8") as output, open(
        bars_target, "w", encoding="utf-8") as bars_output, open(
        options_target, "w", encoding="utf-8") as options_output, open(
        quotes_target, "w", encoding="utf-8") as quotes_output:
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
            serialized = json.dumps(payload, sort_keys=True) + "\n"
            output.write(serialized)
            quotes_output.write(serialized)
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
  "$python_bin" - "$validated_input" "$bars_input" "$options_input" "$quotes_input" <<'PY'
import json
import sys

source, bars_target, options_target, quotes_target = sys.argv[1:]
with open(source, encoding="utf-8") as source_handle, open(
        bars_target, "w", encoding="utf-8") as bars_output, open(
        options_target, "w", encoding="utf-8") as options_output, open(
        quotes_target, "w", encoding="utf-8") as quotes_output:
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
        elif kind in {"quote", "quote_snapshot", "equity_quote", "underlying_quote"}:
            quotes_output.write(serialized)
PY
fi

# Report what each view actually received. Routing quotes into their own view
# is only useful if rows arrive there, and that is otherwise invisible.
"$python_bin" - "$bars_input" "$quotes_input" "$options_input" <<'PY' >&2
import json
import sys

print(json.dumps({"schema": "research-cycle-views.v1", **{
    name: sum(1 for line in open(path, encoding="utf-8") if line.strip())
    for name, path in zip(("bars", "quotes", "options"), sys.argv[1:])}},
    sort_keys=True))
PY

# A CSV containing only its header is not a usable research dataset even
# though the source file itself is non-empty.
if [ ! -s "$validated_input" ] || ! grep -q '[^[:space:]]' "$validated_input"; then
  finish "no_data" "recorded dataset contains no rows" 2
fi

feed="${ALPACA_DATA_FEED:-${ALPACA_STOCK_FEED:-iex}}"
set +e
"$python_bin" "$repo_root/research.py" validate-data "$validated_input" \
  --provider alpaca --feed "$feed"
validation_status=$?
set -e
if [ "$validation_status" -ne 0 ]; then
  finish "failed" "research dataset validation failed" "$validation_status"
fi

if [ "${ALPACA_RESEARCH_BACKTEST:-1}" = "1" ] && [ -s "$bars_input" ]; then
  set +e
  if [ -s "$quotes_input" ]; then
    "$python_bin" "$repo_root/research.py" backtest-ibr "$bars_input" \
      --provider alpaca --feed "$feed" --vehicle equity \
      --quotes "$quotes_input"
  else
    # Avoid expanding an empty array under ``set -u``; Bash 3.2 treats that
    # expansion as an unbound variable.
    "$python_bin" "$repo_root/research.py" backtest-ibr "$bars_input" \
      --provider alpaca --feed "$feed" --vehicle equity
  fi
  backtest_status=$?
  set -e
  if [ "$backtest_status" -ne 0 ]; then
    finish "failed" "research backtest failed" "$backtest_status"
  fi
fi

edge_db="${ALPACA_EDGE_DB:-$repo_root/runtime/research/edge_lab.sqlite3}"
if [[ "$edge_db" != /* ]]; then
  edge_db="$repo_root/$edge_db"
fi
shadow_db="${ALPACA_SHADOW_DB:-$repo_root/runtime/research/shadow.sqlite3}"
if [[ "$shadow_db" != /* ]]; then
  shadow_db="$repo_root/$shadow_db"
fi
agent_config="${ALPACA_AGENT_CONFIG:-$repo_root/config.yaml}"
if [[ "$agent_config" != /* ]]; then
  agent_config="$repo_root/$agent_config"
fi

# Calibration is read-only and advisory.  A missing/thin journal or an
# optimistic finding must never promote, demote, or mutate a broker account;
# emit the result for operators and continue the offline cycle.
run_calibration() {
  [ "${ALPACA_RESEARCH_CALIBRATION_ENABLED:-1}" = "1" ] || return 0
  local journal="${ALPACA_RESEARCH_JOURNAL:-$repo_root/runtime/paper/journal.db}"
  if [[ "$journal" != /* ]]; then
    journal="$repo_root/$journal"
  fi
  [ -s "$journal" ] || {
    echo '{"schema":"research-calibration.v1","status":"skipped","reason":"journal_unavailable"}' >&2
    return 0
  }
  set +e
  local calibration_config="${ALPACA_RESEARCH_CALIBRATION_CONFIG:-}"
  if [ -n "$calibration_config" ]; then
    if [[ "$calibration_config" != /* ]]; then
      calibration_config="$repo_root/$calibration_config"
    fi
    "$python_bin" "$repo_root/research.py" calibrate "$journal" \
      --config "$calibration_config" >&2
  else
    "$python_bin" "$repo_root/research.py" calibrate "$journal" >&2
  fi
  local status=$?
  set -e
  echo "{\"schema\":\"research-calibration.v1\",\"status\":\"completed\",\"exit_code\":$status}" >&2
  return 0
}
run_calibration

run_discovery() {
  local vehicle="$1"
  set +e
  "$python_bin" "$repo_root/research.py" edge discover \
    --data "$validated_input" --vehicle "$vehicle" --lane auto --db "$edge_db" \
    --agent-config "$agent_config"
  local status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    cycle_success=1
    cycle_outcomes+=("$vehicle:discover:completed")
  elif [ "$status" -eq 2 ]; then
    cycle_no_edge=1
    cycle_outcomes+=("$vehicle:discover:no_edge")
  elif [ "$status" -eq 4 ]; then
    # Opportunities existed and none could be priced. That is a corpus the
    # replay cannot evaluate -- usually bars recorded without quotes under a
    # strict market-data policy -- not an absence of edges, and reporting it
    # as "no edge" would hide the misconfiguration indefinitely. It is scoped
    # to this vehicle: another one may still have a corpus that prices.
    cycle_unevaluable=1
    cycle_outcomes+=("$vehicle:discover:unevaluable")
  else
    finish "failed" "$vehicle edge discovery failed" "$status"
  fi
}

run_factory() {
  local vehicle="$1"
  set +e
  "$python_bin" "$repo_root/research.py" factory run \
    --data "$validated_input" --vehicle "$vehicle" --db "$edge_db" \
    --agent-config "$agent_config" \
    --strategies "${ALPACA_FACTORY_STRATEGIES:-7}" \
    --variants "${ALPACA_FACTORY_VARIANTS:-4}" \
    --workers "${ALPACA_FACTORY_WORKERS:-7}" \
    --starting-cash "${ALPACA_FACTORY_STARTING_CASH:-100000}" \
    --min-trades "${ALPACA_FACTORY_MIN_TRADES:-100}" \
    --min-sessions "${ALPACA_FACTORY_MIN_SESSIONS:-10}" \
    --alpha "${ALPACA_FACTORY_ALPHA:-0.05}" \
    --max-generations "${ALPACA_FACTORY_MAX_GENERATIONS:-5}"
  local status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    cycle_success=1
    cycle_outcomes+=("$vehicle:factory:completed")
  elif [ "$status" -eq 2 ]; then
    cycle_no_edge=1
    cycle_outcomes+=("$vehicle:factory:no_proof")
  elif [ "$status" -eq 4 ]; then
    cycle_unevaluable=1
    cycle_outcomes+=("$vehicle:factory:unevaluable")
  else
    finish "failed" "$vehicle factory failed" "$status"
  fi
}

run_shadow_ingest() {
  local vehicle="$1"
  [ "${ALPACA_SHADOW_INGEST_ENABLED:-1}" = "1" ] || return 0
  set +e
  "$python_bin" "$repo_root/research.py" edge ingest-shadow \
    --vehicle "$vehicle" --db "$edge_db" --shadow-db "$shadow_db" \
    --min-trades "${ALPACA_FACTORY_MIN_TRADES:-100}" \
    --min-sessions "${ALPACA_FACTORY_MIN_SESSIONS:-10}" \
    --alpha "${ALPACA_FACTORY_ALPHA:-0.05}"
  local status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    cycle_outcomes+=("$vehicle:shadow-ingest:completed")
  else
    finish "failed" "$vehicle shadow ingestion failed" "$status"
  fi
}

# A trader process runs one execution profile, so proving an edge in the other
# vehicle produces evidence it can never deploy. Study what this deployment can
# actually trade; ALPACA_RESEARCH_VEHICLES=all restores both lanes deliberately.
set +e
vehicles="$("$python_bin" "$repo_root/research.py" vehicles \
  --agent-config "$agent_config")"
vehicle_status=$?
set -e
if [ "$vehicle_status" -ne 0 ] || [ -z "$vehicles" ]; then
  finish "failed" "no research vehicle resolved from the agent config" 3
fi

# Judge the demo-account trials before proposing anything new, so a trial that
# just finished below its floor is already a recorded lesson by the time this
# cycle's tuning reads its history. A parked edge exits 3, which is an
# operator-visible outcome, not a failure.
review_trials() {
  local vehicle="$1"
  set +e
  "$python_bin" "$repo_root/research.py" edge trials \
    --vehicle "$vehicle" --db "$edge_db" --agent-config "$agent_config"
  local status=$?
  set -e
  case "$status" in
    0) cycle_outcomes+=("$vehicle:trial:running") ;;
    3) cycle_outcomes+=("$vehicle:trial:parked") ;;
    *) finish "failed" "$vehicle trial review failed" "$status" ;;
  esac
}

for vehicle in $vehicles; do
  case "$vehicle" in
    equity)
      if [ ! -s "$bars_input" ]; then
        cycle_outcomes+=("equity:skipped:no_bars")
        continue
      fi
      ;;
    option)
      if [ ! -s "$options_input" ]; then
        cycle_outcomes+=("option:skipped:no_option_snapshots")
        continue
      fi
      ;;
    *)
      finish "failed" "unsupported research vehicle: $vehicle" 3
      ;;
  esac
  if [ "${ALPACA_TRIAL_REVIEW_ENABLED:-1}" = "1" ]; then
    review_trials "$vehicle"
  fi
  run_discovery "$vehicle"
  if [ "${ALPACA_FACTORY_ENABLED:-1}" = "1" ]; then
    run_factory "$vehicle"
  fi
  run_shadow_ingest "$vehicle"
done

if [ "$cycle_success" -eq 1 ]; then
  finish "completed" "research cycle completed with proof" 0
fi
# A vehicle whose corpus priced nothing tested nothing. Reporting that as
# "no edge passed the gates" is indistinguishable from a real negative, so the
# cycle says so plainly -- but only when no vehicle produced a verdict either.
if [ "$cycle_unevaluable" -eq 1 ] && [ "$cycle_no_edge" -eq 0 ]; then
  finish "no_data" "corpus could not be priced; see .unevaluable.reason" 0
fi
cycle_no_edge=1
finish "completed_no_edge" "no edge or proof passed the research gates" 0
