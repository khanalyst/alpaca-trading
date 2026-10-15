"""G1b exp3: one session that never receives a closing bar pins the forward
event floor; every later complete session is then replayed on every poll and
the authorizing snapshot grows with history."""
import sys
sys.path.insert(0, "/home/user/alpaca-trading")
import csv
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.research import test_shadow_poll_metrics as pm

case = pm.ShadowPollMetricsTests(methodName="tearDown")
case.setUp()
try:
    utc = timezone.utc
    day_a = [case._bar(datetime(2026, 1, 2, 14, 30, tzinfo=utc) + timedelta(minutes=30 * i), i % 3)
             for i in range(11)]                       # last bar 19:30Z, ends 15:31 NY: incomplete
    day_b = [case._bar(datetime(2026, 1, 5, 14, 30, tzinfo=utc), 0),
             case._bar(datetime(2026, 1, 5, 20, 59, tzinfo=utc), 1)]  # ends 16:00 NY: complete
    case._write(day_a + day_b)
    runner = case._runner()
    case._candidate()

    def no_trade(cand, event, bars, quotes, options):
        return ("no_trade", "probe", {"session_date": str(event.get("timestamp"))[:10]}, None)

    calls = []

    def fake_replay(cand, sess, *a, **k):
        calls.append(sess)
        return True

    def poll(label):
        calls.clear()
        with patch.object(runner, "_evaluate", side_effect=no_trade), \
                patch.object(runner, "_replay", side_effect=fake_replay):
            result = runner.run_once()
        print(f"{label}: replays={sorted(calls)} events_loaded={result['events']} "
              f"floor={result['forward_event_floor']}")

    poll("poll 1")
    poll("poll 2 (no new rows)")
    # Append a further complete session (2026-01-06).
    day_c = [case._bar(datetime(2026, 1, 6, 14, 30, tzinfo=utc), 2),
             case._bar(datetime(2026, 1, 6, 20, 59, tzinfo=utc), 3)]
    with case.corpus.open("a", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=pm.FIELDS).writerows(day_c)
    poll("poll 3 (one more session)")
    poll("poll 4 (no new rows)")
finally:
    case.tearDown()
