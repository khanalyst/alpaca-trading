"""Daily bars (with dividend/split events and adjusted close) for the universe; diagnostic only."""
import json, sys, time, urllib.request, datetime as dt
SYMS = "SPY QQQ IWM DIA XLF XLK XLE XLV XLI XLP XLY XLU XLB XLRE VTI VO VB EFA EEM TLT HYG GLD SLV SMH".split()
out = {}
for s in SYMS:
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval=1d&period1=1262304000&period2={int(time.time())}&events=div,splits"
    for attempt in range(4):
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=60)); break
        except Exception as e:
            time.sleep(3 * (attempt + 1)); d = None
    res = d["chart"]["result"][0]; q = res["indicators"]["quote"][0]; adj = res["indicators"]["adjclose"][0]["adjclose"]
    rows = []
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]): continue
        rows.append({"date": dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), "open": q["open"][i], "high": q["high"][i],
                     "low": q["low"][i], "close": q["close"][i], "adjclose": adj[i], "volume": q["volume"][i]})
    out[s] = {"rows": rows, "events": res.get("events", {})}
    print(s, len(rows), rows[0]["date"], rows[-1]["date"], flush=True); time.sleep(0.3)
json.dump(out, open(sys.argv[1], "w"))
