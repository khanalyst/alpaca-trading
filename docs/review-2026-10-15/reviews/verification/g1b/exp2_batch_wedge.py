"""G1b exp2: a catch-up backlog whose compacted rows exceed max_events raises
on every poll and never advances source offsets (scaled down: max_events=100)."""
import sys
sys.path.insert(0, "/home/user/alpaca-trading")
from datetime import datetime, timedelta, timezone

from research.live_shadow import ShadowError
from tests.research import test_shadow_poll_metrics as pm

case = pm.ShadowPollMetricsTests(methodName="tearDown")
case.setUp()
try:
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    rows = [case._bar(start + timedelta(minutes=i), i % 5) for i in range(150)]
    case._write(rows)
    runner = case._runner()          # max_events=100, as in the fixture
    case._candidate()
    for poll in (1, 2, 3):
        try:
            runner.run_once()
            print(f"poll {poll}: ok")
        except ShadowError as exc:
            print(f"poll {poll}: ShadowError: {exc}")
        print(f"  offsets={runner.store.source_offsets()} events={runner.store.event_count()}")
    # Also measure the compacted size of one synthetic RTH minute with the
    # recorder's 30 s cadence: 24 bars + 2 quotes per symbol.
    from research.live_shadow import _compact_shadow_rows
    syms = [f"S{i:02d}" for i in range(24)]
    minute_rows = []
    t0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    for m in range(60):
        base = t0 + timedelta(minutes=m)
        for s in syms:
            minute_rows.append({"event_key": f"b|{s}|{m}", "event_type": "bar_1m", "symbol": s,
                                "timestamp": base.isoformat(),
                                "as_of": (base + timedelta(minutes=1)).isoformat(),
                                "observed_at": (base + timedelta(minutes=1, seconds=3)).isoformat()})
            for sec in (5, 35):
                q = base + timedelta(seconds=sec)
                minute_rows.append({"event_key": f"q|{s}|{m}|{sec}", "event_type": "quote", "symbol": s,
                                    "timestamp": q.isoformat(), "as_of": q.isoformat(),
                                    "observed_at": (q + timedelta(seconds=1)).isoformat(),
                                    "bid": "100", "ask": "100.01"})
    kept = _compact_shadow_rows(minute_rows)
    print(f"60 synthetic minutes: raw={len(minute_rows)} compacted={len(kept)} "
          f"per_minute={len(kept)/60:.1f} minutes_to_20000={20000/(len(kept)/60):.0f}")
finally:
    case.tearDown()
