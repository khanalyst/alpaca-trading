import json, sys, collections, statistics as st
D = json.load(open(sys.argv[1])); B = D["bars"]
sess = sorted({r["session"] for v in B.values() for r in v})
print("sessions", len(sess), sess)
cnt = collections.defaultdict(dict)
for s, rows in B.items():
    c = collections.Counter(r["session"] for r in rows)
    for d in sess: cnt[s][d] = c.get(d, 0)
print("min bars per symbol-session:", min(min(v.values()) for v in cnt.values()))
for s in B:
    low = {d: n for d, n in cnt[s].items() if n < 385}
    if low: print(s, low)
# 1-min median abs return (bps) and median range per symbol, plus daily range
for s, rows in B.items():
    r = [abs(rows[i]["close"]/rows[i-1]["close"]-1)*1e4 for i in range(1, len(rows)) if rows[i]["session"]==rows[i-1]["session"]]
    hl = [(x["high"]-x["low"])/x["close"]*1e4 for x in rows]
    zero_vol = sum(1 for x in rows if x["volume"] == 0)
    print(f"{s:5s} px {rows[-1]['close']:8.2f} med|r1m| {st.median(r):5.2f}bps  medHL {st.median(hl):5.2f}bps  zero-vol bars {zero_vol}")
