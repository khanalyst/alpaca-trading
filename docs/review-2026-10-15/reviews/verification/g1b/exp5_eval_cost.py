"""G1b exp5: cost of re-evaluating one historical session for one arm
(the slow path taken for every historical gate session after a memo miss)."""
import sys
sys.path.insert(0, "/home/user/alpaca-trading")
import json
import time
from datetime import datetime, timedelta, timezone

from tests.research import test_shadow_poll_metrics as pm

case = pm.ShadowPollMetricsTests(methodName="tearDown")
case.setUp()
try:
    runner = case._runner()
    candidate = case._candidate()
    arm = dict(candidate)
    t0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)   # 09:30 NY (EST)
    bars = []
    for m in range(390):
        b = case._bar(t0 + timedelta(minutes=m), m % 7)
        b["observed_at"] = (t0 + timedelta(minutes=m + 1, seconds=3)).isoformat()
        b["event_json"] = json.dumps(b)
        bars.append(b)
    session = "2026-01-05"
    started = time.perf_counter()
    result = runner._evaluate_arm_snapshot(
        arm, {session: tuple(bars)}, {session: (tuple(bars), (), ())},
        {"SPY": tuple(bars)}, {}, {}, ([], {}, 0.0))
    elapsed = time.perf_counter() - started
    kinds = {}
    for d in result["decisions"]:
        kinds[d["reason"]] = kinds.get(d["reason"], 0) + 1
    print(f"error={result['error']} events={len(result['decisions'])} seconds={elapsed:.2f} "
          f"ms_per_event={elapsed / max(1, len(result['decisions'])) * 1e3:.2f}")
    print("reasons:", kinds)
finally:
    case.tearDown()
