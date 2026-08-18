#!/usr/bin/env bash
set -euo pipefail

# Research is an offline job. Prefer an explicit normalized dataset, then the
# append-only dataset produced by the recorder.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python}"
if ! command -v "$python_bin" >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
  python_bin=python3
fi
agent_config="${ALPACA_AGENT_CONFIG:-$repo_root/config.yaml}"
if [[ "$agent_config" != /* ]]; then
  agent_config="$repo_root/$agent_config"
fi

# ``research.py`` emits detailed proof/result JSON itself. Keep those lines on
# their original streams and add one terminal record for the scheduler.
cycle_finalized=0
cycle_success=0
cycle_no_edge=0
cycle_unevaluable=0
cycle_outcomes=()

emit_progress() {
  local phase="$1"
  local done="$2"
  local total="$3"
  local unit="$4"
  local vehicle="${5:-both}"
  "$python_bin" - "$phase" "$unit" "$vehicle" "$done" "$total" <<'PY' >&2
from datetime import datetime, timezone
import json
import sys

phase, unit, vehicle, done, total = sys.argv[1:]
print(json.dumps({
    "schema": "research-progress.v1", "phase": phase, "unit": unit,
    "vehicle": vehicle, "done": int(done), "total": int(total),
    "updated_ts": datetime.now(timezone.utc).isoformat(),
}, separators=(",", ":"), sort_keys=True), flush=True)
PY
}

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

# Feed entitlements are part of the autonomous-research contract. Do this
# preflight before dataset work so an IEX/indicative override fails with a
# useful reason rather than silently producing evidence from a partial feed.
set +e
feed_guard="$($python_bin - "$agent_config" <<'PY'
import sys

from agent.config import load_config

try:
    config = load_config(sys.argv[1])
except Exception as exc:
    print(str(exc))
    raise SystemExit(1)
broker = config.get("broker") or {}
research = config.get("research") or {}
classes = (config.get("universe") or {}).get("asset_classes") or []
if research.get("enabled", True) and broker.get("data_feed") != "sip":
    print("autonomous research requires SIP equity entitlement")
    raise SystemExit(1)
option_lane = any(str(item).lower() in {"us_option", "option", "options"}
                  for item in classes)
if research.get("enabled", True) and option_lane and broker.get("options_feed") != "opra":
    print("option research requires OPRA entitlement")
    raise SystemExit(1)
print(f"{broker.get('data_feed')} {broker.get('options_feed')}")
PY
)"
feed_guard_status=$?
set -e
[ "$feed_guard_status" -eq 0 ] || \
  finish "failed" "feed entitlement/configuration validation failed: ${feed_guard:-unknown error}" 3
read -r configured_feed configured_option_feed <<< "$feed_guard"

# Enabling model-assisted research is an explicit operational contract.  Do
# not let an empty/default /dev/null secret silently open an authentication
# circuit and make a deterministic cycle look model-assisted.  Deterministic
# research remains available by setting research.strategy_llm.enabled=false.
set +e
llm_provider="$($python_bin - "$agent_config" <<'PY'
import sys

from agent.config import load_config

try:
    config = load_config(sys.argv[1])
except Exception as exc:
    print(str(exc))
    raise SystemExit(1)
research = config.get("research") if isinstance(config, dict) else {}
llm = research.get("strategy_llm") if isinstance(research, dict) else {}
if not isinstance(llm, dict) or not llm.get("enabled"):
    print("disabled")
else:
    print(str(llm.get("provider") or "openai").strip().lower())
PY
)"
llm_config_status=$?
set -e
[ "$llm_config_status" -eq 0 ] || \
  finish "failed" "agent configuration validation failed: ${llm_provider:-unknown error}" 3
case "$llm_provider" in
  disabled)
    echo '{"schema":"research-llm.v1","status":"disabled","reason":"configuration"}' >&2
    ;;
  openai)
    [ -n "${OPENAI_API_KEY:-}" ] || \
      finish "failed" "strategy LLM is enabled but OPENAI_API_KEY is unavailable" 3
    echo '{"schema":"research-llm.v1","status":"ready","provider":"openai"}' >&2
    ;;
  anthropic)
    [ -n "${ANTHROPIC_API_KEY:-}" ] || \
      finish "failed" "strategy LLM is enabled but ANTHROPIC_API_KEY is unavailable" 3
    echo '{"schema":"research-llm.v1","status":"ready","provider":"anthropic"}' >&2
    ;;
  *)
    finish "failed" "strategy LLM provider is unsupported" 3
    ;;
esac
dataset="${ALPACA_RESEARCH_DATASET:-}"
dataset_from_recorder=0
recorded_root="${ALPACA_RECORDED_DATASET_ROOT:-$repo_root/runtime/research/recorded}"
if [[ "$recorded_root" != /* ]]; then
  recorded_root="$repo_root/$recorded_root"
fi

tmp_root="${TMPDIR:-/tmp}"
mkdir -p "$tmp_root"
tmp_dir="$(mktemp -d "$tmp_root/alpaca-research.XXXXXX")"
emit_progress "preparing" 0 1 "steps" "both"

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
  dataset_from_recorder=1
  for candidate in "$recorded_root/market.jsonl" "$recorded_root/market.csv"; do
    if [ -s "$candidate" ]; then
      dataset="$candidate"
      break
    fi
  done
fi
if [ -z "$dataset" ] && [ -d "$recorded_root/sessions" ]; then
  dataset_from_recorder=1
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
replay_input="$tmp_dir/replay.jsonl"

source_format="jsonl"
[[ "$validated_input" == *.csv ]] && source_format="csv"
# ``research_dataset.py`` intentionally normalizes recorder CSV columns. For
# an explicitly external CSV, preserve exact calendar fields by first making a
# temporary JSONL view; otherwise those fields would be discarded before the
# production calendar gate could verify them.
if [ "$source_format" = "csv" ] && [ "$dataset_from_recorder" -eq 0 ]; then
  external_jsonl="$tmp_dir/external.jsonl"
  "$python_bin" - "$validated_input" "$external_jsonl" <<'PY'
import csv
import json
import sys
source, target = sys.argv[1:]
kinds = {"bar": "bar", "bar_1m": "bar", "quote": "quote",
         "quote_snapshot": "quote", "option": "option_snapshot",
         "option_snapshot": "option_snapshot", "option_quote": "option_snapshot"}
with open(source, newline="", encoding="utf-8") as source_file, \
        open(target, "w", encoding="utf-8") as target_file:
    # Preserve CSV's one-line header offset in quarantine diagnostics.
    target_file.write("\n")
    for row in csv.DictReader(source_file):
        # DictReader stores overflow CSV columns under a ``None`` key. Drop
        # that malformed-column bucket here; validate-data still receives the
        # named fields and can report semantic errors without JSON key sorting
        # crashing first.
        item = {key: value for key, value in row.items()
                if key is not None and value not in (None, "")}
        item["kind"] = kinds.get(str(row.get("event_type") or "").lower(),
                                  str(row.get("event_type") or "").lower())
        target_file.write(json.dumps(item, sort_keys=True) + "\n")
PY
  validated_input="$external_jsonl"
  source_format="jsonl"
fi
normalized_input="$tmp_dir/market.jsonl"
# Preserve the append-only recorder corpus byte-for-byte. Only the temporary
# research view quarantines the known legacy point-in-time inversion; malformed
# timestamps, symbols, values, and event kinds remain for validate-data to
# reject as hard integrity failures.
dataset_report="$("$python_bin" "$repo_root/deploy/research_dataset.py" "$validated_input" \
  --format "$source_format" --normalized "$normalized_input" \
  --bars "$bars_input" --quotes "$quotes_input" --options "$options_input" \
  --replay "$replay_input")"
validated_input="$normalized_input"

# Add exact broker calendar boundaries to the temporary research view. The
# recorder corpus remains append-only; external inputs must already carry both
# fields. Production config fails closed on missing/malformed metadata or
# extended-hours rows.
set +e
calendar_view_status="$($python_bin - "$validated_input" "$dataset" "$recorded_root" "$agent_config" "$dataset_from_recorder" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

view, source, recorded_root, config_path = map(Path, sys.argv[1:5])
from_recorder = sys.argv[5] == "1"
ny = ZoneInfo("America/New_York")
def parse(value):
    if value in (None, ""):
        return None
    try:
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
try:
    from agent.config import load_config
    config = load_config(str(config_path))
except Exception as exc:
    print(f"configuration validation failed: {exc}")
    raise SystemExit(1)
session = config.get("session") if isinstance(config, dict) else {}
required = bool(session.get("require_exact_calendar", True)) if isinstance(session, dict) else True
sidecar = recorded_root / ".recorder-index.json" if from_recorder else Path("-")
if not sidecar.is_file() and source != Path("-") and from_recorder:
    candidate = source.parent / ".recorder-index.json"
    if candidate.is_file():
        sidecar = candidate
calendar = None
if sidecar.is_file():
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        calendar = payload.get("session_calendar") if isinstance(payload, dict) else None
        if not isinstance(calendar, dict):
            calendar = None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        calendar = None
def boundaries(item, day, number):
    opened = parse(item.get("session_open"))
    closed = parse(item.get("session_close"))
    if (opened is None) != (closed is None):
        raise ValueError(f"row {number} has only one exact session boundary")
    side = calendar.get(day) if isinstance(calendar, dict) else None
    side_open = parse(side.get("open")) if isinstance(side, dict) else None
    side_close = parse(side.get("close")) if isinstance(side, dict) else None
    if side is not None and (side_open is None or side_close is None):
        raise ValueError(f"calendar metadata for {day} is malformed")
    if opened is None and side_open is not None:
        opened, closed = side_open, side_close
        item["session_open"], item["session_close"] = opened.isoformat(), closed.isoformat()
    if opened is None:
        if required:
            raise ValueError(f"exact broker calendar metadata missing for {day}")
        return None, None
    if opened >= closed or opened.astimezone(ny).date().isoformat() != day or closed.astimezone(ny).date().isoformat() != day:
        raise ValueError(f"conflicting exact broker calendar metadata for {day}")
    if side_open is not None and (opened != side_open or closed != side_close):
        raise ValueError(f"row {number} conflicts with recorder calendar for {day}")
    return opened, closed
temporary = view.with_suffix(view.suffix + ".calendar")
with view.open(encoding="utf-8") as source_file, temporary.open("w", encoding="utf-8") as target:
    for number, line in enumerate(source_file, 1):
        if not line.strip():
            continue
        item = json.loads(line)
        stamp = parse(item.get("timestamp"))
        if stamp is not None:
            day = stamp.astimezone(ny).date().isoformat()
            opened, closed = boundaries(item, day, number)
            if opened is not None and not opened <= stamp < closed:
                raise ValueError(f"row {number} is outside exact broker session {day}")
        elif required:
            raise ValueError(f"row {number} has no timestamp for exact calendar validation")
        target.write(json.dumps(item, sort_keys=True) + "\n")
temporary.replace(view)
PY
)"
calendar_view_exit=$?
set -e
if [ "$calendar_view_exit" -ne 0 ]; then
  finish "failed" "exact session calendar validation failed: ${calendar_view_status:-unknown error}" 3
fi

# Option rows are executable evidence only when they carry OPRA provenance.
# Pin their availability to the fetch observation boundary too: a stale quote
# generated at 10:00 but first observed at 10:05 must not be visible to a
# 10:01 replay decision. The source corpus remains append-only; this correction
# applies only to the temporary research view.
point_in_time_options="$tmp_dir/market-point-in-time.jsonl"
set +e
point_in_time_status="$($python_bin - "$validated_input" "$point_in_time_options" <<'PY'
from datetime import datetime
import json
import sys

source, target = sys.argv[1:]
option_kinds = {"option", "option_snapshot", "option_quote"}

def parse(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

with open(source, encoding="utf-8") as source_file, open(target, "w", encoding="utf-8") as target_file:
    for number, line in enumerate(source_file, 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if str(payload.get("kind", "")).lower() in option_kinds:
            feed = str(payload.get("feed") or "").strip().lower()
            if feed != "opra":
                print(
                    f"option row {number} has non-executable feed {feed or '[missing]'}; OPRA is required")
                raise SystemExit(1)
            observed = parse(payload.get("observed_at"))
            as_of = parse(payload.get("as_of"))
            if observed is not None and (as_of is None or as_of < observed):
                payload["as_of"] = payload["observed_at"]
        target_file.write(json.dumps(payload, sort_keys=True) + "\n")
PY
)"
point_in_time_exit=$?
set -e
if [ "$point_in_time_exit" -ne 0 ]; then
  finish "failed" "option evidence/feed validation failed: ${point_in_time_status:-unknown error}" 3
fi
validated_input="$point_in_time_options"

# Keep the option and replay views in lockstep with the corrected normalized
# stream. Without this rewrite a factory worker could still consume the
# legacy timestamp while discovery saw the safe availability boundary.
"$python_bin" - "$validated_input" "$options_input" "$replay_input" <<'PY'
import json
import sys

source, options_path, replay_path = sys.argv[1:]
bar_kinds = {"bar", "bar_1m", "underlying", "underlying_bar"}
option_kinds = {"option", "option_snapshot", "option_quote"}
with open(options_path, "w", encoding="utf-8") as options, \
        open(replay_path, "w", encoding="utf-8") as replay, \
        open(source, encoding="utf-8") as source_file:
    for line in source_file:
        if not line.strip():
            continue
        payload = json.loads(line)
        serialized = json.dumps(payload, sort_keys=True) + "\n"
        kind = str(payload.get("kind", "")).lower()
        if kind in option_kinds:
            options.write(serialized)
            replay.write(serialized)
        elif kind in bar_kinds:
            replay.write(serialized)
PY

# Preserve the quarantine record and report view sizes without rescanning the
# generated files. ``research_dataset.py`` counted them during its source pass.
"$python_bin" - "$dataset_report" <<'PY' >&2
import json
import sys

report = json.loads(sys.argv[1])
counts = report.pop("view_counts", {})
print(json.dumps(report, sort_keys=True))
print(json.dumps({
    "schema": "research-cycle-views.v1",
    "bars": int(counts.get("bars", 0)),
    "quotes": int(counts.get("quotes", 0)),
    "options": int(counts.get("options", 0)),
    "replay": int(counts.get("replay", 0)),
}, sort_keys=True))
PY
emit_progress "preparing" 1 1 "steps" "both"

# A CSV containing only its header is not a usable research dataset even
# though the source file itself is non-empty.
if [ ! -s "$validated_input" ] || ! grep -q '[^[:space:]]' "$validated_input"; then
  finish "no_data" "recorded dataset contains no rows" 2
fi

# The validated agent configuration is the feed contract.  Environment feed
# hints have already been folded into (and checked by) ``load_config``; using
# them again here could stamp a different CLI identity onto external rows.
feed="${configured_feed:-sip}"
emit_progress "validation" 0 1 "steps" "both"
set +e
"$python_bin" "$repo_root/research.py" validate-data "$validated_input" \
  --provider alpaca --feed "$feed"
validation_status=$?
set -e
if [ "$validation_status" -ne 0 ]; then
  finish "failed" "research dataset validation failed" "$validation_status"
fi
emit_progress "validation" 1 1 "steps" "both"

if [ "${ALPACA_RESEARCH_BACKTEST:-1}" = "1" ] && [ -s "$bars_input" ]; then
  emit_progress "backtest" 0 1 "steps" "equity"
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
  emit_progress "backtest" 1 1 "steps" "equity"
fi

edge_db="${ALPACA_EDGE_DB:-$repo_root/runtime/research/edge_lab.sqlite3}"
if [[ "$edge_db" != /* ]]; then
  edge_db="$repo_root/$edge_db"
fi
shadow_db="${ALPACA_SHADOW_DB:-$repo_root/runtime/research/shadow.sqlite3}"
if [[ "$shadow_db" != /* ]]; then
  shadow_db="$repo_root/$shadow_db"
fi
# Calibration is read-only and applies only at the promotion boundary. Keep
# discovery/factory diagnostic even when paper evidence is absent or thin, but
# never let shadow ingestion proceed without a fresh, explicitly authorized
# calibration report.
calibration_checked=0
calibration_authorized=0
calibration_reason="not_checked"
run_calibration() {
  [ "$calibration_checked" -eq 0 ] || return 0
  calibration_checked=1
  calibration_authorized=0
  calibration_reason="disabled"
  if [ "${ALPACA_RESEARCH_CALIBRATION_ENABLED:-1}" != "1" ]; then
    echo '{"schema":"research-calibration.v1","status":"blocked","reason":"calibration_disabled","authorization_exit_code":2}' >&2
    return 0
  fi
  local journal="${ALPACA_RESEARCH_JOURNAL:-$repo_root/runtime/paper/journal.db}"
  if [[ "$journal" != /* ]]; then
    journal="$repo_root/$journal"
  fi
  local max_age="${ALPACA_RESEARCH_CALIBRATION_MAX_AGE_SECONDS:-86400}"
  if ! [[ "$max_age" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    calibration_reason="invalid_max_age"
    echo '{"schema":"research-calibration.v1","status":"blocked","reason":"invalid_max_age","authorization_exit_code":2}' >&2
    return 0
  fi
  [ -s "$journal" ] || {
    calibration_reason="journal_unavailable"
    echo '{"schema":"research-calibration.v1","status":"blocked","reason":"journal_unavailable","authorization_exit_code":2}' >&2
    return 0
  }
  set +e
  local calibration_config="${ALPACA_RESEARCH_CALIBRATION_CONFIG:-}"
  local calibration_output_file="$tmp_dir/calibration-output.json"
  if [ -n "$calibration_config" ]; then
    if [[ "$calibration_config" != /* ]]; then
      calibration_config="$repo_root/$calibration_config"
    fi
    "$python_bin" "$repo_root/research.py" calibrate "$journal" \
      --config "$calibration_config" >"$calibration_output_file"
  else
    "$python_bin" "$repo_root/research.py" calibrate "$journal" >"$calibration_output_file"
  fi
  local status=$?
  set -e
  local calibration_report="${ALPACA_RESEARCH_CALIBRATION_REPORT:-$repo_root/runtime/research/calibration-latest.json}"
  if [[ "$calibration_report" != /* ]]; then
    calibration_report="$repo_root/$calibration_report"
  fi
  # Normalize and annotate the report in one deterministic step. This keeps
  # the original calibration fields intact while recording journal mtime,
  # report generation time, and the freshness threshold used for promotion.
  local normalized_report
  set +e
  normalized_report="$($python_bin - "$calibration_output_file" "$journal" "$max_age" "$status" <<'PY'
import json
import math
import os
import sys
import time
from pathlib import Path

output_path, journal_path, max_age_raw, command_status = sys.argv[1:]
now = time.time()
report = {}
reason = None
try:
    payload = json.loads(Path(output_path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        report = payload
    else:
        reason = "invalid_report"
except (OSError, ValueError, TypeError):
    reason = "missing_report"
try:
    max_age = float(max_age_raw)
except (TypeError, ValueError):
    max_age = float("nan")
if not math.isfinite(max_age) or max_age < 0:
    reason = "invalid_max_age"
try:
    journal_stat = os.stat(journal_path)
    journal_mtime = float(journal_stat.st_mtime)
    journal_age = max(0.0, now - journal_mtime)
except OSError:
    journal_mtime = None
    journal_age = None
if reason is None and journal_mtime is None:
    reason = "journal_unavailable"
auth = str(report.get("authorization_verdict") or "").strip().lower()
auth_code = report.get("authorization_exit_code")
if reason is None and str(command_status) != "0" and auth not in {
        "veto_optimistic_cost", "veto_underfilled_execution", "insufficient_data"}:
    reason = "calibration_failed"
if reason is None and auth != "authorized":
    reason = "authorization_" + (auth or "missing")
valid_auth_code = (not isinstance(auth_code, bool) and
                   auth_code in (0, 0.0, "0"))
if reason is None and not valid_auth_code:
    reason = "authorization_exit_code"
if reason is None and journal_age is not None and journal_age > max_age:
    reason = "stale_journal"
report.setdefault("schema", 2)
report["calibration_status"] = "authorized" if reason is None else "blocked"
report["authorization_reason"] = "authorized" if reason is None else reason
report["authorization_exit_code"] = 0 if reason is None else 2
report["provenance"] = {
    "journal_path": str(Path(journal_path).resolve()),
    "journal_mtime_ts": journal_mtime,
    "journal_age_seconds": journal_age,
    "generated_ts": now,
    "max_age_seconds": max_age,
}
print(json.dumps(report, sort_keys=True, allow_nan=False))
raise SystemExit(0 if reason is None else 2)
PY
)"
  local report_status=$?
  set -e
  local report_persisted=0
  mkdir -p "$(dirname "$calibration_report")" 2>/dev/null || true
  if [ -n "$normalized_report" ]; then
    if printf '%s\n' "$normalized_report" > "$calibration_report.tmp" 2>/dev/null &&
       mv "$calibration_report.tmp" "$calibration_report" 2>/dev/null; then
      report_persisted=1
      echo "$normalized_report" >&2
    fi
  fi
  if [ "$report_status" -eq 0 ] && [ "$report_persisted" -eq 1 ]; then
    calibration_authorized=1
    calibration_reason="authorized"
    echo '{"schema":"research-calibration.v1","status":"authorized","authorization_exit_code":0}' >&2
  else
    if [ "$report_persisted" -ne 1 ]; then
      calibration_reason="report_unwritable"
    else
      calibration_reason="$($python_bin - "$calibration_report" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, ValueError):
    payload = {}
print(payload.get("authorization_reason") or "calibration_unverified")
PY
      )"
    fi
    echo "{\"schema\":\"research-calibration.v1\",\"status\":\"blocked\",\"reason\":\"$calibration_reason\",\"authorization_exit_code\":2}" >&2
  fi
  return 0
}

run_discovery() {
  local vehicle="$1"
  emit_progress "discovery" 0 1 "steps" "$vehicle"
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
  emit_progress "discovery" 1 1 "steps" "$vehicle"
}

run_factory() {
  local vehicle="$1"
  emit_progress "factory" 0 1 "tasks" "$vehicle"
  set +e
  "$python_bin" "$repo_root/research.py" factory run \
    --data "$validated_input" --worker-data "$replay_input" \
    --vehicle "$vehicle" --db "$edge_db" \
    --agent-config "$agent_config" \
    --strategies "${ALPACA_FACTORY_STRATEGIES:-11}" \
    --variants "${ALPACA_FACTORY_VARIANTS:-4}" \
    --workers "${ALPACA_FACTORY_WORKERS:-2}" \
    --starting-cash "${ALPACA_FACTORY_STARTING_CASH:-100000}" \
    --min-trades "${ALPACA_FACTORY_MIN_TRADES:-100}" \
    --min-sessions "${ALPACA_FACTORY_MIN_SESSIONS:-30}" \
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
  emit_progress "factory" 1 1 "tasks" "$vehicle"
}

run_shadow_ingest() {
  local vehicle="$1"
  [ "${ALPACA_SHADOW_INGEST_ENABLED:-1}" = "1" ] || return 0
  emit_progress "shadow_ingest" 0 1 "steps" "$vehicle"
  run_calibration
  if [ "$calibration_authorized" -ne 1 ]; then
    cycle_outcomes+=("$vehicle:shadow-ingest:blocked:$calibration_reason")
    echo "{\"schema\":\"research-shadow-authorization.v1\",\"status\":\"blocked\",\"vehicle\":\"$vehicle\",\"reason\":\"$calibration_reason\"}" >&2
    emit_progress "shadow_ingest" 1 1 "steps" "$vehicle"
    return 0
  fi
  set +e
  "$python_bin" "$repo_root/research.py" edge ingest-shadow \
    --vehicle "$vehicle" --db "$edge_db" --shadow-db "$shadow_db" \
    --min-trades "${ALPACA_SHADOW_MIN_TRADES:-150}" \
    --min-sessions "${ALPACA_SHADOW_MIN_SESSIONS:-30}" \
    --alpha "${ALPACA_FACTORY_ALPHA:-0.05}"
  local status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    cycle_outcomes+=("$vehicle:shadow-ingest:completed")
  else
    finish "failed" "$vehicle shadow ingestion failed" "$status"
  fi
  emit_progress "shadow_ingest" 1 1 "steps" "$vehicle"
}

# The scheduled research profile studies both executable vehicles. Deployment
# still gates runtime entries to the one validated vehicle selected by the
# trader profile; ALPACA_RESEARCH_VEHICLES can narrow this deliberately.
set +e
vehicles="$("$python_bin" "$repo_root/research.py" vehicles \
  --agent-config "$agent_config" \
  --vehicles "${ALPACA_RESEARCH_VEHICLES:-all}")"
vehicle_status=$?
set -e
if [ "$vehicle_status" -ne 0 ] || [ -z "$vehicles" ]; then
  finish "failed" "no research vehicle resolved from the agent config" 3
fi

# Judge the paper-account trials before proposing anything new, so a trial that
# just finished below its floor is already a recorded lesson by the time this
# cycle's tuning reads its history. A parked edge exits 3, which is an
# operator-visible outcome, not a failure.
review_trials() {
  local vehicle="$1"
  emit_progress "trial" 0 1 "steps" "$vehicle"
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
  emit_progress "trial" 1 1 "steps" "$vehicle"
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
  emit_progress "completed" 1 1 "cycles" "both"
  finish "completed" "research cycle completed with proof" 0
fi
# A vehicle whose corpus priced nothing tested nothing. Reporting that as
# "no edge passed the gates" is indistinguishable from a real negative, so the
# cycle says so plainly -- but only when no vehicle produced a verdict either.
if [ "$cycle_unevaluable" -eq 1 ] && [ "$cycle_no_edge" -eq 0 ]; then
  emit_progress "completed" 1 1 "cycles" "both"
  finish "no_data" "corpus could not be priced; see .unevaluable.reason" 0
fi
cycle_no_edge=1
emit_progress "completed" 1 1 "cycles" "both"
finish "completed_no_edge" "no candidate was proved; review adequate-negative, underpowered, and untested classifications" 0
