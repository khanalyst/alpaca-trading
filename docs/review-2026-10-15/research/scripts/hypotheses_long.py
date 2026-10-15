"""Independent long-history tests of classic intraday ETF hypotheses (diagnostic only).

Data: public consolidated-tape bars. Hourly bars 2024-10-16..2026-10-14 (about 500
sessions; bars start 09:30, 10:30, ..., 15:30, the last one is 30 minutes) and daily
bars 2010-01-04..2026-10-14 with adjusted closes for dividend-correct gaps.

H1 market intraday momentum (Gao, Han, Li, Zhou 2018): prior close->10:30 return
   predicts the 15:30->16:00 return. Note the runtime force-flats at 15:50.
H2 opening gap fade / continuation (daily data): open->close return vs overnight gap.
H3 cross-sectional intraday reversal: first-hour relative return vs 10:30->15:30
   relative return across the 24 ETFs (long losers / short winners).
H4 overnight vs intraday drift decomposition (daily data).
Inference: session (day) is the unit; pooled regressions use day-clustered SEs.
"""
import json, sys, math, collections
import numpy as np

D = sys.argv[1]; OUT = sys.argv[2]
H60 = json.load(open(f"{D}/bars_60m.json"))["bars"]
DAY = json.load(open(f"{D}/bars_1d.json"))
res = {}

def tstat(x):
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    if len(x) < 3: return None
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))

def summ(x, scale=1e4):
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    if len(x) < 3: return {"n": int(len(x))}
    return {"n": int(len(x)), "mean_bps": float(x.mean() * scale), "t": tstat(x),
            "hit": float((x > 0).mean()), "sd_bps": float(x.std(ddof=1) * scale)}

def cluster_ols(y, x, g):
    """y = a + b x, CR1 standard errors clustered by g. Returns b, t_b."""
    y = np.asarray(y, float); x = np.asarray(x, float); g = np.asarray(g)
    m = np.isfinite(y) & np.isfinite(x); y, x, g = y[m], x[m], g[m]
    X = np.column_stack([np.ones_like(x), x]); XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y; u = y - X @ beta
    meat = np.zeros((2, 2)); groups = collections.defaultdict(list)
    for i, gi in enumerate(g): groups[gi].append(i)
    G = len(groups)
    for idx in groups.values():
        s = X[idx].T @ u[idx]; meat += np.outer(s, s)
    n, k = X.shape
    V = XtX_inv @ meat @ XtX_inv * (G / (G - 1)) * ((n - 1) / (n - k))
    return {"n": int(n), "clusters": G, "beta": float(beta[1]), "t": float(beta[1] / math.sqrt(V[1, 1]))}

# ---------- daily adjustment factors (dividends) ----------
adj = {}
for s, d in DAY.items():
    adj[s] = {r["date"]: (r["adjclose"] / r["close"], r) for r in d["rows"] if r["close"]}

# ---------- hourly panel ----------
panel = {}   # sym -> session -> dict
for s, rows in H60.items():
    by = collections.defaultdict(dict)
    for r in rows: by[r["session"]][r["minute"]] = r
    sess = sorted(by)
    out = {}
    for i, d in enumerate(sess):
        b = by[d]
        if not all(m in b for m in (0, 60, 120, 180, 240, 300, 360)) or 210 in b:
            continue   # skip early closes / incomplete
        if i == 0 or 360 not in by[sess[i - 1]]: continue
        prev = by[sess[i - 1]][360]["close"]
        fa = adj[s].get(sess[i - 1]); fb = adj[s].get(d)
        if fa and fb: prev = prev * fa[0] / fb[0]
        out[d] = {"prev": prev, "o": b[0]["open"], "c1030": b[0]["close"], "o1530": b[360]["open"],
                  "c1600": b[360]["close"], "o1430": b[300]["open"], "c1530": b[300]["close"]}
    panel[s] = out
sessions = sorted(set.intersection(*[set(v) for v in panel.values()]))
res["hourly_sessions"] = {"n": len(sessions), "first": sessions[0], "last": sessions[-1]}

# H1 intraday momentum
h1 = {}
for s in ["SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "ALL"]:
    syms = list(panel) if s == "ALL" else [s]
    y, x, g, strat = [], [], [], []
    for sym in syms:
        for d in sessions:
            p = panel[sym][d]
            r_first = p["c1030"] / p["prev"] - 1; r_last = p["c1600"] / p["o1530"] - 1
            y.append(r_last); x.append(r_first); g.append(d); strat.append(math.copysign(1, r_first) * r_last if r_first else 0)
    h1[s] = {"ols": cluster_ols(y, x, g), "sign_strategy_last30": summ(strat)}
    # split halves for stability
    half = len(sessions) // 2
    for name, ss in (("first_half", set(sessions[:half])), ("second_half", set(sessions[half:]))):
        idx = [i for i, d in enumerate(g) if d in ss]
        h1[s][name] = summ([strat[i] for i in idx])
res["H1_intraday_momentum"] = h1

# H3 cross-sectional intraday reversal (equal-weight long bottom 4 / short top 4 by first-hour return)
xs = []; xs_cont = []
syms = [s for s in panel if s not in ("SPY",)]
for d in sessions:
    fh = {s: panel[s][d]["c1030"] / panel[s][d]["o"] - 1 for s in syms}
    rest = {s: panel[s][d]["o1530"] / panel[s][d]["c1030"] - 1 for s in syms}
    ranked = sorted(syms, key=lambda s: fh[s])
    losers, winners = ranked[:4], ranked[-4:]
    spread = np.mean([rest[s] for s in losers]) - np.mean([rest[s] for s in winners])
    xs.append(spread)
res["H3_cross_sectional_reversal_10_30_to_15_30"] = {"long_losers_short_winners": summ(xs),
    "first_half": summ(xs[:len(xs)//2]), "second_half": summ(xs[len(xs)//2:]),
    "note": "8 legs per day, gross of costs; real round trip on 8 legs ~2-3 bps per leg"}

# ---------- daily: H2 gap fade and H4 overnight/intraday ----------
h2 = {}; h4 = {}
for s, d in DAY.items():
    rows = d["rows"]; y, x, g, years = [], [], [], []
    on, intra = [], []
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        if not (a["close"] and b["open"] and b["close"]): continue
        prev = a["close"] * (a["adjclose"] / a["close"]) / (b["adjclose"] / b["close"])
        gap = b["open"] / prev - 1; oc = b["close"] / b["open"] - 1
        y.append(oc); x.append(gap); g.append(b["date"]); years.append(b["date"][:4])
        on.append(gap); intra.append(oc)
    y = np.array(y); x = np.array(x); years = np.array(years)
    sd = np.std(x)
    big = np.abs(x) > 2 * sd
    fade = -np.sign(x[big]) * y[big]
    by_year = {}
    for yr in sorted(set(years)):
        m = (years == yr) & big
        if m.sum() >= 5: by_year[yr] = summ(-np.sign(x[m]) * y[m])
    recent = np.array([gg >= "2024-10-16" for gg in g])
    h2[s] = {"ols_all": cluster_ols(y, x, g), "gap_sd_bps": float(sd * 1e4),
             "fade_gaps_over_2sd": summ(fade),
             "fade_gaps_over_2sd_last2y": summ(-np.sign(x[big & recent]) * y[big & recent]),
             "years_fade_positive": int(sum(1 for v in by_year.values() if v.get("mean_bps", 0) > 0)), "years": len(by_year)}
    h4[s] = {"overnight": summ(on), "intraday_open_to_close": summ(intra)}
res["H2_gap_fade_daily"] = h2
res["H4_overnight_vs_intraday"] = h4
json.dump(res, open(OUT, "w"), indent=1)

# compact print
print("hourly sessions", res["hourly_sessions"])
for k, v in h1.items():
    print("H1", k, "beta %.3f t %.2f" % (v["ols"]["beta"], v["ols"]["t"]), "sign-strat", {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in v["sign_strategy_last30"].items()},
          "halves %.2f / %.2f" % (v["first_half"]["mean_bps"], v["second_half"]["mean_bps"]))
print("H3", {k: {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in v.items()} if isinstance(v, dict) else v for k, v in res["H3_cross_sectional_reversal_10_30_to_15_30"].items()})
for s, v in h2.items():
    print("H2 %-5s beta %.3f t %.2f | fade>2sd n %d mean %.1f t %.2f | last2y n %s mean %s | years+ %d/%d" % (
        s, v["ols_all"]["beta"], v["ols_all"]["t"], v["fade_gaps_over_2sd"]["n"], v["fade_gaps_over_2sd"]["mean_bps"], v["fade_gaps_over_2sd"]["t"],
        v["fade_gaps_over_2sd_last2y"].get("n"), round(v["fade_gaps_over_2sd_last2y"].get("mean_bps", float("nan")), 1), v["years_fade_positive"], v["years"]))
for s, v in h4.items():
    print("H4 %-5s overnight %.2f bps (t %.2f) intraday %.2f bps (t %.2f)" % (s, v["overnight"]["mean_bps"], v["overnight"]["t"], v["intraday_open_to_close"]["mean_bps"], v["intraday_open_to_close"]["t"]))
