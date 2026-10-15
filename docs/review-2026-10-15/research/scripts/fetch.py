"""Fetch public consolidated-tape bars for the 24-ETF universe (diagnostic only, not Alpaca IEX)."""
import json, sys, time, urllib.request, datetime as dt
from zoneinfo import ZoneInfo
NY = ZoneInfo("America/New_York")
OUT = sys.argv[1]; INTERVAL = sys.argv[2]; DAYS = int(sys.argv[3]); CHUNK = int(sys.argv[4])
SYMS = "SPY QQQ IWM DIA XLF XLK XLE XLV XLI XLP XLY XLU XLB XLRE VTI VO VB EFA EEM TLT HYG GLD SLV SMH".split()
step = {"1m": 1, "2m": 2, "5m": 5, "60m": 60, "1d": None}[INTERVAL]
now = int(time.time()); start = now - DAYS * 86400 + 3600
out = {}; meta = {}
def get(u):
    for attempt in range(4):
        try:
            r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(r, timeout=30))
        except Exception as e:
            err = e; time.sleep(2 * (attempt + 1))
    print("FAIL", u, err, file=sys.stderr); return None
for s in SYMS:
    merged = {}
    p1 = start
    while p1 < now:
        p2 = min(now, p1 + CHUNK * 86400)
        d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval={INTERVAL}&period1={p1}&period2={p2}&includePrePost=false&events=div,splits")
        p1 = p2
        if not d or not d["chart"]["result"]: continue
        res = d["chart"]["result"][0]
        meta[s] = {k: res["meta"].get(k) for k in ("exchangeName", "instrumentType", "dataGranularity", "currentTradingPeriod")}
        ts = res.get("timestamp") or []; q = res["indicators"]["quote"][0]
        for i, t in enumerate(ts):
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None in (o, h, l, c): continue
            d2 = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(NY)
            m = d2.hour * 60 + d2.minute - 570
            if step and not (0 <= m < 390): continue
            merged[t] = {"symbol": s, "timestamp": d2.isoformat(), "open": float(o), "high": float(h),
                         "low": float(l), "close": float(c), "volume": float(v or 0),
                         "session": d2.date().isoformat(), "minute": m}
        time.sleep(0.3)
    out[s] = [merged[k] for k in sorted(merged)]
    print(s, len(out[s]), flush=True)
json.dump({"interval": INTERVAL, "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
           "source": "public consolidated-tape chart endpoint (diagnostic only)", "meta": meta, "bars": out}, open(OUT, "w"))
sess = sorted({r["session"] for v in out.values() for r in v})
print("SESSIONS", len(sess), sess[0] if sess else None, "..", sess[-1] if sess else None)
