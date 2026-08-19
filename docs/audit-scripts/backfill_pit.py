import sys; sys.path.insert(0,"/home/user/alpaca-trading")
from datetime import datetime, timedelta, timezone
from research.market_data import normalize_underlying_bar, record_available_at, record_is_available

bar_ts = datetime(2026,3,10,14,31,tzinfo=timezone.utc)   # a bar inside the session
backfill_now = datetime(2026,8,19,2,0,tzinfo=timezone.utc)  # when backfill was run

def mk(observed):
    return normalize_underlying_bar({
        "event_type":"bar_1m","symbol":"SPY","provider":"alpaca","feed":"sip",
        "timestamp":bar_ts.isoformat(),
        "as_of":(bar_ts+timedelta(minutes=1)).isoformat(),
        "observed_at":observed,
        "open":"600","high":"600.5","low":"599.5","close":"600.2","volume":"1000",
    })

live = mk((bar_ts+timedelta(minutes=1)).isoformat())   # recorder: observed at bar close
back = mk(backfill_now.isoformat())                    # backfill: observed = run wall clock

for name, rec in (("recorder", live), ("backfill", back)):
    avail = record_available_at(rec)
    # the cutoff edge_discovery_core.py:705 uses for entry-bar visibility
    visible = record_is_available(rec, rec.timestamp)
    visible_end = record_is_available(rec, rec.end) if hasattr(rec,"end") else None
    print(f"{name:>9}: available_at={avail}  visible@bar_ts={visible}  visible@bar_end={visible_end}")
