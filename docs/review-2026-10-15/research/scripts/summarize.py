import json, collections, statistics as st
A = json.load(open("results/analysis.json"))
f = lambda x, p=2: "-" if x is None else (f"{x:.{p}f}" if isinstance(x, float) else str(x))
rows = []
for a in sorted(A, key=lambda r: (r["cohort"], r["family"], r["role"])):
    al = a["all"]; u = a["by_window"]["unseen"]; sq = a["signal_quality"].get("60", {}) if a["signal_quality"] else {}
    p = a["portfolio_real"] or {}; ft = a["first_trade_open"]; fd = a["first_trade_delayed"]
    em = a["exit_mix"]
    rows.append(a)
pos_real = [a for a in A if a["all"]["real"].get("n", 0) and a["all"]["real"]["mean"] > 0]
pos_gross = [a for a in A if a["all"]["gross"].get("n", 0) and a["all"]["gross"]["mean"] > 0]
sig_neg = [a for a in A if a["all"]["real"].get("cluster_t") is not None and a["all"]["real"]["cluster_t"] < -2]
sig_pos = [a for a in A if a["all"]["real"].get("cluster_t") is not None and a["all"]["real"]["cluster_t"] > 2]
zero = [a for a in A if not a["all"]["gross"].get("n")]
print("arms", len(A), "| with trades", len(A) - len(zero), "| zero-trade arms", [(a["cohort"], a["family"]) for a in zero])
print("positive mean net(real)", len(pos_real), [(a["cohort"], a["family"], a["role"], round(a["all"]["real"]["mean"], 2), round(a["all"]["real"]["cluster_t"] or 0, 2), a["all"]["real"]["n"]) for a in pos_real])
print("positive mean gross", len(pos_gross))
print("net clustered t < -2:", len(sig_neg), "| net clustered t > +2:", len(sig_pos))
allt = [t for a in A for t in []]
# pooled across arms (each arm weighted equally)
for k in ("gross", "real", "model"):
    v = [a["all"][k]["mean"] for a in A if a["all"][k].get("n")]
    print(f"mean of arm means {k}: {st.mean(v):.2f} bps (median {st.median(v):.2f})")
# exit mix by stop floor
by_floor = collections.defaultdict(list)
for a in A:
    if not a["all"]["gross"].get("n"): continue
    fl = (a["spec"] or {}).get("min_stop_distance_bps") if a["family"] != "ibr" else "ibr"
    by_floor[str(fl)].append(a["exit_mix"])
for k, v in by_floor.items():
    keys = sorted({kk for d in v for kk in d})
    print("stop floor", k, "arms", len(v), {kk: round(st.mean(d.get(kk, 0) for d in v), 3) for kk in keys})
# delay sensitivity
d = [(a["first_trade_open"]["mean"] - a["first_trade_delayed"]["mean"]) for a in A if a["first_trade_open"].get("n") and a["first_trade_delayed"].get("n")]
print(f"first-trade net: entry at next open minus entry one bar later, mean over arms {st.mean(d):.2f} bps (median {st.median(d):.2f})")
# deadline refusals
for a in A:
    if a["errors"]: print("refusals", a["cohort"], a["family"], a["errors"])
# portfolio summary
port = [(a["portfolio_real"]["total_return_pct"], a["cohort"], a["family"], a["role"]) for a in A if a["portfolio_real"]]
port.sort()
print("portfolio total return over 22 sessions, real costs: best", port[-3:], "worst", port[:3])
print("mean risk used per trade (% equity):", round(st.mean(a["portfolio_real"]["mean_risk_used_pct"] for a in A if a["portfolio_real"]), 4))
# family-level (diag24 baselines + variants pooled by family): unseen window
fam = collections.defaultdict(list)
for a in A:
    if a["cohort"] == "diag24": fam[a["family"]].append(a)
print("\nFAMILY (diag24 baseline | variant): net mean bps (t) all-22 ; unseen-14 ; signal quality 60m delta (t)")
for k, v in sorted(fam.items()):
    s = " | ".join(f"{x['role'][:4]} n{x['all']['real']['n']} {f(x['all']['real']['mean'])} ({f(x['all']['real']['cluster_t'])}) ; {f(x['by_window']['unseen'].get('mean'))} ; sq60 {f(x['signal_quality'].get('60',{}).get('delta_bps'))} ({f(x['signal_quality'].get('60',{}).get('cluster_t'))})" for x in sorted(v, key=lambda z: z['role']))
    print(f"{k:25s} {s}")
