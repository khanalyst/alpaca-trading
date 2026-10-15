"""G1b exp4: memory and time of one authorizing snapshot decode per WAL row."""
import sys
sys.path.insert(0, "/home/user/alpaca-trading")
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

from research.live_shadow import ShadowConfig, ShadowRunner
from tests.research import test_shadow_poll_metrics as pm

case = pm.ShadowPollMetricsTests(methodName="tearDown")
case.setUp()
try:
    store_runner = ShadowRunner(ShadowConfig(case.corpus, case.edge, case.shadow,
                                             diagnostic=False, max_events=10**6,
                                             max_decisions=10**6))
    store = store_runner.store
    t0 = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    syms = [f"S{i:02d}" for i in range(24)]
    rows = []
    for m in range(120):
        base = t0 + timedelta(minutes=m)
        for s in syms:
            rows.append({"event_key": f"b|{s}|{m}", "event_type": "bar_1m", "symbol": s,
                         "timestamp": base.isoformat(),
                         "as_of": (base + timedelta(minutes=1)).isoformat(),
                         "observed_at": (base + timedelta(minutes=1, seconds=3)).isoformat(),
                         "provider": "alpaca", "feed": "iex", "open": "100", "high": "100.2",
                         "low": "99.9", "close": "100.1", "volume": "1000"})
            for sec in (5, 35):
                q = base + timedelta(seconds=sec)
                rows.append({"event_key": f"q|{s}|{m}|{sec}", "event_type": "quote", "symbol": s,
                             "timestamp": q.isoformat(), "as_of": q.isoformat(),
                             "observed_at": (q + timedelta(seconds=1)).isoformat(),
                             "provider": "alpaca", "feed": "iex", "bid": "100", "ask": "100.01"})
    with store._connection() as db:
        for row in rows:
            store._ingest_event(db, row, max_events=10**6, update_cursor=False)
    n = store.event_count()
    pass
    started = time.perf_counter()
    loaded = store_runner._load_events()
    elapsed = time.perf_counter() - started
    current, peak = 0, 0
    pass
    print(f"rows={n} load_seconds={elapsed:.2f} per_row_us={elapsed / n * 1e6:.1f} "
          f"retained_bytes_per_row={current / n:.0f} peak_bytes_per_row={peak / n:.0f}")
    # Cost of the replay-only membership scan at 7469-7472 for a late session.
    from research.live_shadow import _timestamp, NEW_YORK
    events = loaded[0]
    started = time.perf_counter()
    for _ in range(10):
        any((lambda r: (_timestamp(r.get("as_of") or r.get("timestamp"))
                        .astimezone(NEW_YORK).date().isoformat()))(row) == "2099-01-01"
            for row in events)
    scan = (time.perf_counter() - started) / 10
    print(f"full membership scan over {len(events)} rows: {scan:.3f} s "
          f"({scan / len(events) * 1e6:.2f} us/row)")
finally:
    case.tearDown()
