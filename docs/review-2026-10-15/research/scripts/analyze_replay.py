"""Summarise replay outputs: trade economics, independent signal quality, portfolio simulation.

Windows: AUDIT_EXAMINED 2026-09-14..09-21 (in the September audit's examined window),
AUDIT_FORWARD 09-22..09-23, UNSEEN 09-24..10-13 (no public analysis in the repository).
Inference unit: the session. cluster_t is the CR1 session-clustered t of the per-trade mean.
"""
import json, os, sys, math, glob, collections
import numpy as np

RDIR = sys.argv[1]; DATA = sys.argv[2]; OUT = sys.argv[3]
RAW = json.load(open(DATA))["bars"]
BARS = {s: collections.defaultdict(dict) for s in RAW}
for s, rows in RAW.items():
    for r in rows: BARS[s][r["session"]][r["minute"]] = r
SESS = sorted({r["session"] for rows in RAW.values() for r in rows})
def window(d):
    return "examined" if d <= "2026-09-21" else ("audit_fwd" if d <= "2026-09-23" else "unseen")

def cluster_t(values, groups):
    v = np.asarray(values, float); g = np.asarray(groups)
    n = len(v)
    if n < 3: return None, 0
    mu = v.mean(); u = v - mu
    G = sorted(set(g))
    if len(G) < 3: return None, len(G)
    s2 = sum((u[g == k].sum()) ** 2 for k in G)
    se = math.sqrt(s2 * (len(G) / (len(G) - 1)) * ((n - 1) / n)) / n
    return (mu / se if se > 0 else None), len(G)

def stats(trs, key):
    if not trs: return {"n": 0}
    v = [t[key] for t in trs]; g = [t["sess"] for t in trs]
    ct, G = cluster_t(v, g)
    return {"n": len(v), "sessions": G, "mean": float(np.mean(v)), "median": float(np.median(v)),
            "win": float(np.mean([x > 0 for x in v])), "cluster_t": ct}

def fwd(sym, sess, m0, h):
    b = BARS[sym][sess]
    if m0 not in b or (m0 + h - 1) not in b: return None
    return b[m0 + h - 1]["close"] / b[m0]["open"] - 1

CTRL_CACHE = {}
def ctrl(sym, sess, m0, h):
    key = (sym, sess, m0, h)
    if key in CTRL_CACHE: return CTRL_CACHE[key]
    vals = [fwd(sym, d, m0, h) for d in SESS if d != sess]
    vals = [x for x in vals if x is not None]
    CTRL_CACHE[key] = float(np.mean(vals)) if len(vals) >= 5 else None
    return CTRL_CACHE[key]

def signal_quality(firsts, horizons=(5, 15, 30, 60, 120)):
    out = {}
    for h in horizons:
        rows = []
        for f in firsts:
            t = f["open"]; d = 1 if t["dir"] == "long" else -1
            a = fwd(t["sym"], t["sess"], t["entry_min"], h); c = ctrl(t["sym"], t["sess"], t["entry_min"], h)
            if a is None or c is None: continue
            rows.append((d * (a - c) * 1e4, d * a * 1e4, t["sess"]))
        if len(rows) < 3: out[h] = {"n": len(rows)}; continue
        delta = [r[0] for r in rows]; raw = [r[1] for r in rows]; g = [r[2] for r in rows]
        ct, G = cluster_t(delta, g)
        # session sign-flip (one-sided, exact if <= 16 sessions else 20000 draws)
        per = collections.defaultdict(list)
        for x, s in zip(delta, g): per[s].append(x)
        sums = np.array([sum(v) for v in per.values()])
        obs = sums.sum()
        rng = np.random.default_rng(7)
        signs = rng.choice([-1, 1], size=(20000, len(sums)))
        p = float(((signs * sums).sum(1) >= obs).mean())
        out[h] = {"n": len(rows), "sessions": G, "delta_bps": float(np.mean(delta)), "raw_bps": float(np.mean(raw)),
                  "cluster_t": ct, "sign_flip_p_one_sided": p}
    return out

def portfolio(trades, equity=100000.0, risk_pct=0.5, max_notional_pct=25.0, gross_pct=50.0, max_pos=3, daily_loss_pct=2.0, key="real"):
    trades = sorted(trades, key=lambda t: (t["sess"], t["entry_min"], t["sym"]))
    taken = []; by_sess = collections.defaultdict(list)
    for t in trades: by_sess[t["sess"]].append(t)
    daily = {}
    capped = 0
    for sess in SESS:
        openp = []; pnl = 0.0; stopped = False
        for t in by_sess.get(sess, []):
            openp = [o for o in openp if o["exit_min"] > t["entry_min"]]
            # realised P&L of trades closed before this entry
            if stopped: continue
            realised = sum(o["usd"] for o in taken if o["sess"] == sess and o["exit_min"] <= t["entry_min"])
            if realised <= -equity * daily_loss_pct / 100: stopped = True; continue
            if len(openp) >= max_pos: continue
            if any(o["sym"] == t["sym"] for o in openp): continue
            risk_frac = t["risk_bps"] / 1e4
            notional = equity * risk_pct / 100 / risk_frac
            if notional > equity * max_notional_pct / 100:
                notional = equity * max_notional_pct / 100; capped += 1
            gross = sum(o["notional"] for o in openp)
            if gross + notional > equity * gross_pct / 100:
                notional = equity * gross_pct / 100 - gross
                if notional <= 0: continue
            row = {**t, "notional": notional, "usd": notional * t[key] / 1e4,
                   "risk_used_pct": notional * risk_frac / equity * 100}
            taken.append(row); openp.append(row)
        daily[sess] = sum(o["usd"] for o in taken if o["sess"] == sess)
    d = np.array([daily[s] for s in SESS]) / equity
    curve = np.cumsum(d); dd = float((curve - np.maximum.accumulate(curve)).min()) if len(curve) else 0.0
    return {"trades_taken": len(taken), "notional_capped": capped,
            "mean_risk_used_pct": float(np.mean([o["risk_used_pct"] for o in taken])) if taken else None,
            "total_return_pct": float(d.sum() * 100), "mean_daily_bps": float(d.mean() * 1e4),
            "daily_sd_bps": float(d.std(ddof=1) * 1e4) if len(d) > 1 else None,
            "ann_sharpe_naive": float(d.mean() / d.std(ddof=1) * math.sqrt(252)) if len(d) > 1 and d.std(ddof=1) > 0 else None,
            "max_drawdown_pct": dd * 100}

res = []
for path in sorted(glob.glob(os.path.join(RDIR, "*.json"))):
    a = json.load(open(path)); trs = a["trades"]
    for t in trs: t["win"] = window(t["sess"])
    reasons = collections.Counter(t["reason"] for t in trs)
    row = {"vid": a["vid"], "cohort": a["cohort"], "also_in": a.get("also_in"), "family": a["family"], "role": a["role"],
           "spec": a.get("spec"), "signals": a["signals_considered"], "vetoed": a["vetoed"], "errors": a["errors"],
           "exit_mix": {k: round(v / max(1, len(trs)), 3) for k, v in reasons.items()},
           "median_stop_bps": float(np.median([t["stop_bps_from_signal"] for t in trs])) if trs else None,
           "median_target_bps": float(np.median([t["target_bps"] for t in trs])) if trs else None,
           "mean_hold_bars": float(np.mean([t["held"] for t in trs])) if trs else None,
           "mean_R_gross": float(np.mean([t["R_gross"] for t in trs if t["R_gross"] is not None])) if trs else None,
           "all": {k: stats(trs, k) for k in ("gross", "real", "model")},
           "by_window": {w: stats([t for t in trs if t["win"] == w], "real") for w in ("examined", "audit_fwd", "unseen")},
           "by_window_gross": {w: stats([t for t in trs if t["win"] == w], "gross") for w in ("examined", "audit_fwd", "unseen")},
           "long": stats([t for t in trs if t["dir"] == "long"], "real"), "short": stats([t for t in trs if t["dir"] == "short"], "real"),
           "first_trade_open": stats([f["open"] for f in a["firsts"]], "real"),
           "first_trade_delayed": stats([f["delayed"] for f in a["firsts"] if f["delayed"]], "real"),
           "signal_quality": signal_quality(a["firsts"]) if a["firsts"] else {},
           "portfolio_real": portfolio(trs) if trs else None,
           "portfolio_gross": portfolio(trs, key="gross") if trs else None}
    res.append(row)
json.dump(res, open(OUT, "w"), indent=1)
print("arms", len(res))
fmt = lambda x, p=2: "-" if x is None else (f"{x:.{p}f}" if isinstance(x, float) else str(x))
print(f"{'cohort':11s} {'family':25s} {'role':26s} {'n':>5s} {'gross':>6s} {'real':>6s} {'t':>5s} {'win':>4s} {'unseen':>7s} {'t_un':>5s} {'sq60':>6s} {'t60':>5s} {'port%':>6s}")
for r in sorted(res, key=lambda r: (r["cohort"], r["family"], r["role"])):
    a = r["all"]; u = r["by_window"]["unseen"]; sq = r["signal_quality"].get(60, {}) if r["signal_quality"] else {}
    sq = r["signal_quality"].get("60", sq) if isinstance(r["signal_quality"], dict) else sq
    p = r["portfolio_real"] or {}
    print(f"{r['cohort']:11s} {r['family']:25s} {r['role'][:26]:26s} {a['gross'].get('n',0):5d} {fmt(a['gross'].get('mean')):>6s} {fmt(a['real'].get('mean')):>6s} {fmt(a['real'].get('cluster_t')):>5s} {fmt(a['real'].get('win')):>4s} {fmt(u.get('mean')):>7s} {fmt(u.get('cluster_t')):>5s} {fmt(sq.get('delta_bps')):>6s} {fmt(sq.get('cluster_t')):>5s} {fmt(p.get('total_return_pct')):>6s}")
