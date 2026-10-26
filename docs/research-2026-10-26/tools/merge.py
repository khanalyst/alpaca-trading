"""Merge two public bar fetches by (symbol, timestamp); report overlap agreement.

Usage: merge.py OLD.json NEW.json OUT.json
The older fetch wins on overlapping bars so earlier published results stay reproducible.
"""
import json, sys
old, new, out = sys.argv[1:4]
A = json.load(open(old)); B = json.load(open(new))
assert A["interval"] == B["interval"], (A["interval"], B["interval"])
merged = {}
stats = {"overlap": 0, "price_mismatch": 0, "volume_mismatch": 0, "only_old": 0, "only_new": 0}
worst = 0.0
for sym in sorted(set(A["bars"]) | set(B["bars"])):
    a = {r["timestamp"]: r for r in A["bars"].get(sym, [])}
    b = {r["timestamp"]: r for r in B["bars"].get(sym, [])}
    rows = {}
    for ts in set(a) | set(b):
        if ts in a and ts in b:
            stats["overlap"] += 1
            ra, rb = a[ts], b[ts]
            diff = max(abs(ra[k] - rb[k]) / ra[k] for k in ("open", "high", "low", "close") if ra[k])
            worst = max(worst, diff)
            if diff > 1e-6: stats["price_mismatch"] += 1
            if ra["volume"] != rb["volume"]: stats["volume_mismatch"] += 1
            rows[ts] = ra
        elif ts in a:
            stats["only_old"] += 1; rows[ts] = a[ts]
        else:
            stats["only_new"] += 1; rows[ts] = b[ts]
    merged[sym] = [rows[ts] for ts in sorted(rows)]
sessions = sorted({r["session"] for rows in merged.values() for r in rows})
json.dump({"interval": A["interval"], "fetched_utc": [A.get("fetched_utc"), B.get("fetched_utc")],
           "source": A.get("source"), "meta": B.get("meta", A.get("meta")), "bars": merged}, open(out, "w"))
print(json.dumps({**stats, "worst_relative_price_diff": worst, "sessions": len(sessions),
                  "first": sessions[0], "last": sessions[-1]}))
