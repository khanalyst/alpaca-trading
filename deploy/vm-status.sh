#!/usr/bin/env bash
# Read-only status of the paper deployment: why is (or isn't) it trading?
#
# Run on the VM from the deployment directory:
#   cd /opt/alpaca-agent-trading && bash deploy/vm-status.sh > vm-status.txt 2>&1
#
# It changes nothing: no orders, no flatten, no resume, no state writes. The
# broker calls are the authenticated read-only `check`, `status` and
# `inspect-deployment` commands. It prints no secrets (the account appears only
# as its non-secret fingerprint). Share vm-status.txt to diagnose.
set -u
section() { printf '\n===== %s =====\n' "$1"; }
run() { echo "\$ $*"; "$@" 2>&1 || echo "(exit $?)"; }

section "host and code"
run date -u
run git rev-parse HEAD
run git log -1 --format='%h %ad %s' --date=iso
run git status --short
run docker compose ps
run docker compose images

section "recent logs (trader, watchdog, shadow, recorder)"
for svc in trader watchdog shadow recorder; do
  run docker compose logs --no-color --tail=40 "$svc"
done

section "authenticated read-only checks"
run docker compose exec -T trader python main.py check
run docker compose exec -T trader python main.py status
run docker compose exec -T trader python main.py inspect-deployment

section "runtime state, paper trial, session acceptance, code identity"
docker compose exec -T trader python - <<'PY' 2>&1 || echo "(exit $?)"
import collections, json, os, sys
from pathlib import Path
sys.path.insert(0, "/app")
root = Path(os.getenv("ALPACA_AGENT_RUNTIME_ROOT") or "/app/runtime")

def load(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception as exc:  # report, never fail
        return {"_unreadable": f"{type(exc).__name__}: {exc}"}

for mode in ("paper", "live"):
    state = root / mode / "state.json"
    if not state.exists():
        continue
    s = load(state)
    print(f"[{mode}] state={s.get('state')} operator_pause={s.get('operator_pause')} "
          f"kill_reason={s.get('kill_reason')!r} runtime_mode={s.get('runtime_mode')}")
    print(f"[{mode}] risk_day={json.dumps(s.get('risk_day'), default=str)[:300]}")
    print(f"[{mode}] active_trades={len(s.get('active_trades') or {})} "
          f"protection={len(s.get('protection') or {})}")
    trial = s.get("paper_trial") or {}
    if trial:
        keep = ("trial_id", "variant_id", "state", "activation_confirmed", "required_sessions",
                "started_on", "blockers", "code_identity", "incumbent_identity")
        print(f"[{mode}] paper_trial=" + json.dumps({k: trial.get(k) for k in keep}, default=str))
        print(f"[{mode}] paper_trial accepted_sessions={len(trial.get('accepted_sessions') or [])} "
              f"outcomes={len(trial.get('outcomes') or [])}")
    beat = load(root / mode / "heartbeat.json")
    print(f"[{mode}] heartbeat=" + json.dumps(beat, default=str)[:600])

print("supervisor=" + json.dumps(load(root / "health" / "trader-supervisor.json"), default=str)[:600])

try:
    from agent.paper_trial import runtime_code_identity
    print("running code_identity=" + runtime_code_identity("/app"))
except Exception as exc:
    print(f"code identity unavailable: {exc}")

reports = sorted(Path("/app/shadow/session-acceptance").glob("session-*.report.json"))
print(f"session reports: {len(reports)}")
accepted, reasons = 0, collections.Counter()
for path in reports[-40:]:
    r = load(path)
    accepted += bool(r.get("accepted"))
    counts = r.get("reason_counts") or {}
    if isinstance(counts, dict):
        for reason, n in counts.items():
            kind = ":".join(str(reason).split(":")[::2]) if str(reason).startswith("symbol:") else str(reason)
            reasons[kind] += int(n) if isinstance(n, (int, float)) else 1
print(f"last {min(40, len(reports))} reports: {accepted} accepted")
for reason, n in reasons.most_common(15):
    print(f"  {n:8d}  {reason}")

events = root / "paper" / "events.jsonl"
if events.exists():
    lines = events.read_text(errors="replace").splitlines()[-25:]
    print("last events:")
    for line in lines:
        print("  " + line[:300])
PY

section "edgefinder trader (if deployed)"
run docker compose -f compose.yaml -f deploy/compose.edgefinder.yaml ps edgefinder-trader
echo "done"
