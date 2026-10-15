"""Independent, professionally-shaped versions of the repository's leading hypotheses.

Own code (does not import the repository evaluators), so it is also a replication check.
Data: public consolidated-tape bars. 1-minute: 22 sessions 2026-09-14..10-13.
5-minute: 42 sessions 2026-08-17..10-14. Diagnostic only.

V1 ORF-pro: opening range = first 15 minutes. From 09:45 to 11:30, the first bar whose
   high exceeds the range high by >= 5 bps and closes back inside the range (or mirror
   for the low) triggers a fade at the next bar open. Stop = failed-break extreme plus
   one tick (structural), target = range midpoint, time stop 60 minutes, flat 15:50.
V2 VWAP-reversion-pro: from 10:00 to 15:00, first bar per symbol-session whose close is
   >= k bps from session VWAP (typical-price, consolidated volume), k = 2.5 x the
   symbol's median 30-minute absolute return over prior sessions; fade at next open,
   target = VWAP at signal, stop = 1.5 x the distance beyond entry, time stop 60 min.
V3 ORB-5 (opening-range breakout on the first 5-minute bar): direction of the 09:30-09:35
   bar; enter at 09:35 open; stop = opposite extreme of the first bar; exit 15:50.
V4 VWAP-trend mirror of V2 (enter with the deviation) as a sign check.
Costs: real round trip = max(one tick, Corwin-Schultz median) + 1.0 slippage + 0.28 fees.
"""
import os as _os
REPO_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "..", ".."))
import json, sys, math, collections
import numpy as np

D = sys.argv[1]; OUT = sys.argv[2]
SPREAD = json.load(open(_os.path.join(REPO_ROOT, "docs", "audit-2026-09-21", "spread-reference.json")))["symbols"]
def real_rt(sym, px): return max(100.0 / px, SPREAD[sym]["corwin_schultz_median_bps"]) + 1.28

def sessions(path):
    raw = json.load(open(path))["bars"]
    out = {}
    for s, rows in raw.items():
        by = collections.defaultdict(list)
        for r in rows: by[r["session"]].append(r)
        out[s] = {d: sorted(v, key=lambda r: r["minute"]) for d, v in by.items()}
    return out

def walk(rows, j, direction, entry, stop, target, max_minutes, flat_min=380):
    """Enter at rows[j].open. Stop-first ties. Returns (exit_price, reason)."""
    start_min = rows[j]["minute"]
    for k in range(j, len(rows)):
        b = rows[k]
        if b["minute"] >= flat_min: return b["open"], "flat"
        if max_minutes is not None and b["minute"] - start_min >= max_minutes: return b["open"], "time"
        if direction > 0:
            if b["open"] <= stop: return b["open"], "stop_gap"
            if b["low"] <= stop: return stop, "stop"
            if target is not None and b["high"] >= target: return max(target, b["open"]), "target"
        else:
            if b["open"] >= stop: return b["open"], "stop_gap"
            if b["high"] >= stop: return stop, "stop"
            if target is not None and b["low"] <= target: return min(target, b["open"]), "target"
    return rows[-1]["close"], "end"

def record(out, name, sym, d, direction, entry, exit_px, reason, stop):
    g = direction * (exit_px / entry - 1) * 1e4
    out[name].append({"sym": sym, "sess": d, "gross": g, "real": g - real_rt(sym, entry), "reason": reason,
                      "risk_bps": abs(entry - stop) / entry * 1e4})

def run(B, step):
    out = collections.defaultdict(list)
    or_bars = 15 // step
    # prior-session 30-minute absolute move per symbol for V2 thresholds
    for sym, S in B.items():
        days = sorted(S)
        hist30 = []
        for di, d in enumerate(days):
            rows = S[d]
            if len(rows) < 300 // step: continue
            tick = 0.01
            # ---- V1 ORF-pro ----
            orr = rows[:or_bars]
            if len(orr) == or_bars and orr[-1]["minute"] == 15 - step:
                hi = max(r["high"] for r in orr); lo = min(r["low"] for r in orr); mid = (hi + lo) / 2
                for i in range(or_bars, len(rows) - 1):
                    b = rows[i]
                    if b["minute"] > 120: break
                    if b["high"] > hi * 1.0005 and lo < b["close"] < hi:
                        e = rows[i + 1]["open"]; st = b["high"] + tick
                        if e < st and e > mid:
                            px, why = walk(rows, i + 1, -1, e, st, mid, 60); record(out, "V1_ORF_pro", sym, d, -1, e, px, why, st)
                        break
                    if b["low"] < lo * 0.9995 and lo < b["close"] < hi:
                        e = rows[i + 1]["open"]; st = b["low"] - tick
                        if e > st and e < mid:
                            px, why = walk(rows, i + 1, 1, e, st, mid, 60); record(out, "V1_ORF_pro", sym, d, 1, e, px, why, st)
                        break
            # ---- V3 ORB-5 ----
            f = rows[0]
            if f["minute"] == 0 and step == 5 and f["close"] != f["open"]:
                dirn = 1 if f["close"] > f["open"] else -1
                e = rows[1]["open"]; st = f["low"] if dirn > 0 else f["high"]
                if (dirn > 0 and e > st) or (dirn < 0 and e < st):
                    px, why = walk(rows, 1, dirn, e, st, None, None); record(out, "V3_ORB5", sym, d, dirn, e, px, why, st)
            # ---- V2 / V4 VWAP reversion and trend ----
            if len(hist30) >= 5:
                k = 2.5 * float(np.median(hist30[-10:]))
                pv = 0.0; vv = 0.0
                for i in range(len(rows) - 1):
                    b = rows[i]; tp = (b["high"] + b["low"] + b["close"]) / 3
                    pv += tp * b["volume"]; vv += b["volume"]
                    if b["minute"] < 30 or vv <= 0: continue
                    if b["minute"] >= 330: break
                    vwap = pv / vv; dev = (b["close"] / vwap - 1) * 1e4
                    if abs(dev) >= k:
                        e = rows[i + 1]["open"]; dirn = -1 if dev > 0 else 1
                        dist = abs(e - vwap)
                        st = e - dirn * 1.5 * dist
                        if (dirn > 0 and vwap > e) or (dirn < 0 and vwap < e):
                            px, why = walk(rows, i + 1, dirn, e, st, vwap, 60); record(out, "V2_VWAP_rev_pro", sym, d, dirn, e, px, why, st)
                        # V4 mirror: with the deviation, symmetric bracket, same horizon
                        st4 = e + dirn * 1.5 * max(dist, e * 5e-4); tg4 = e - dirn * max(dist, e * 5e-4)
                        px, why = walk(rows, i + 1, -dirn, e, st4, tg4, 60); record(out, "V4_VWAP_trend_mirror", sym, d, -dirn, e, px, why, st4)
                        break
            # update 30-minute absolute move history (whole session, completed day only)
            closes = [r["close"] for r in rows]
            n30 = 30 // step
            moves = [abs(closes[i + n30] / closes[i] - 1) * 1e4 for i in range(0, len(closes) - n30, n30)]
            if moves: hist30.append(float(np.median(moves)))
    return out

def summarise(trs):
    if not trs: return {"n": 0}
    v = np.array([t["real"] for t in trs]); g = np.array([t["sess"] for t in trs])
    per = collections.defaultdict(float); cnt = collections.Counter()
    for t in trs: per[t["sess"]] += t["real"]; cnt[t["sess"]] += 1
    u = v - v.mean(); G = sorted(set(g)); n = len(v)
    s2 = sum((u[g == k].sum()) ** 2 for k in G)
    se = math.sqrt(s2 * (len(G) / max(1, len(G) - 1)) * ((n - 1) / n)) / n if n > 1 else float("nan")
    first_half = [t["real"] for t in trs if t["sess"] < G[len(G) // 2]]
    second_half = [t["real"] for t in trs if t["sess"] >= G[len(G) // 2]]
    return {"n": n, "sessions": len(G), "gross": float(np.mean([t["gross"] for t in trs])), "real": float(v.mean()),
            "cluster_t": float(v.mean() / se) if se and se > 0 else None, "win": float((v > 0).mean()),
            "first_half_real": float(np.mean(first_half)) if first_half else None,
            "second_half_real": float(np.mean(second_half)) if second_half else None,
            "exits": dict(collections.Counter(t["reason"] for t in trs)),
            "median_risk_bps": float(np.median([t["risk_bps"] for t in trs]))}

res = {}
for label, path, step in (("1m_22s", f"{D}/bars_1m.json", 1), ("5m_42s", f"{D}/bars_5m.json", 5)):
    B = sessions(path); out = run(B, step)
    res[label] = {k: summarise(v) for k, v in sorted(out.items())}
    for k in ("SPY", "QQQ"):
        if "V3_ORB5" in out:
            res[label][f"V3_ORB5_{k}"] = summarise([t for t in out["V3_ORB5"] if t["sym"] == k])
json.dump(res, open(OUT, "w"), indent=1)
for label, v in res.items():
    for k, s in v.items():
        print(label, f"{k:22s}", {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in s.items()})
