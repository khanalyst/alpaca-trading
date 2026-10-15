import sys, csv, io
sys.path.insert(0, "/home/user/alpaca-trading")
from datetime import datetime, timezone, timedelta
from deploy.recorder_market import _event_key
fields = ["event_key","observed_at","provider","feed","event_type","symbol","contract","timestamp","as_of","open","high","low","close","volume","bid","ask","last"]
t = datetime(2026,10,14,14,30,tzinfo=timezone.utc)
bar = {"event_key": _event_key("bar_1m","SPY",t.isoformat()), "observed_at": (t+timedelta(minutes=1,seconds=2.123456)).isoformat(),
       "provider":"alpaca","feed":"iex","event_type":"bar_1m","symbol":"SPY","contract":"","timestamp":t.isoformat(),
       "as_of":(t+timedelta(minutes=1)).isoformat(),"open":"571.23","high":"571.40","low":"571.10","close":"571.31","volume":"12345","bid":"","ask":"","last":""}
quote = dict(bar, event_key=_event_key("quote","SPY",t.isoformat()), event_type="quote", timestamp=(t+timedelta(seconds=5.123456)).isoformat(),
             as_of=(t+timedelta(seconds=5.123456)).isoformat(), open="", high="", low="", close="", volume="", bid="571.30", ask="571.32")
buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=fields); w.writerow(bar); b = len(buf.getvalue())
buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=fields); w.writerow(quote); q = len(buf.getvalue())
per_min = 24*b + 48*q
print(f"bar_bytes={b} quote_bytes={q} bytes_per_rth_minute={per_min} minutes_to_64MB={64*1024*1024/per_min:.0f} trading_days={64*1024*1024/per_min/390:.1f}")
