"""Runtime-faithful view: FIRST trade per symbol-session only (engine_cycle.py:743 burns the symbol-session
on the first emitted signal), entry at next-bar open, portfolio limits from config.yaml."""
import json, sys, glob, os, collections
src = open("scripts/analyze_replay.py").read().split("res = []")[0]
sys.argv = ["x", "results/replay", "data/bars_1m.json", "/dev/null"]; ns = {}; exec(compile(src, "helpers", "exec"), ns)
stats, portfolio, window = ns["stats"], ns["portfolio"], ns["window"]
out = []
for p in sorted(glob.glob("results/replay/*.json")):
    a = json.load(open(p))
    ft = [f["open"] for f in a["firsts"]]
    for t in ft: t["win"] = window(t["sess"])
    fd = [f["delayed"] for f in a["firsts"] if f["delayed"]]
    row = {"vid": a["vid"], "cohort": a["cohort"], "family": a["family"], "role": a["role"],
           "first": {k: stats(ft, k) for k in ("gross", "real", "model")},
           "first_unseen": stats([t for t in ft if t["win"] == "unseen"], "real"),
           "first_examined": stats([t for t in ft if t["win"] == "examined"], "real"),
           "first_delayed_real": stats(fd, "real"),
           "exit_mix_first": {k: round(v / max(1, len(ft)), 3) for k, v in collections.Counter(t["reason"] for t in ft).items()},
           "portfolio_first_real": portfolio(ft) if ft else None}
    out.append(row)
json.dump(out, open("results/runtime_faithful.json", "w"), indent=1)
f = lambda x, p=2: "-" if x is None else (f"{x:.{p}f}" if isinstance(x, float) else str(x))
print(f"{'cohort':11s} {'family':25s} {'role':24s} {'n':>4s} {'gross':>6s} {'net':>6s} {'t':>6s} {'win':>5s} {'unseen':>7s} {'t_un':>6s} {'delay':>6s} {'port%':>6s} {'taken':>5s}")
for r in sorted(out, key=lambda r: (r["cohort"], r["family"], r["role"])):
    g, n, u, d, pf = r["first"]["gross"], r["first"]["real"], r["first_unseen"], r["first_delayed_real"], r["portfolio_first_real"] or {}
    print(f"{r['cohort']:11s} {r['family']:25s} {r['role'][:24]:24s} {n.get('n',0):4d} {f(g.get('mean')):>6s} {f(n.get('mean')):>6s} {f(n.get('cluster_t')):>6s} {f(n.get('win')):>5s} {f(u.get('mean')):>7s} {f(u.get('cluster_t')):>6s} {f(d.get('mean')):>6s} {f(pf.get('total_return_pct')):>6s} {f(pf.get('trades_taken')):>5s}")
pos = [r for r in out if r["first"]["real"].get("n") and r["first"]["real"]["mean"] > 0]
neg2 = [r for r in out if (r["first"]["real"].get("cluster_t") or 0) < -2]
pos2 = [r for r in out if (r["first"]["real"].get("cluster_t") or 0) > 2]
import statistics as st
print("\narms with trades", sum(1 for r in out if r["first"]["real"].get("n")), "| positive net", len(pos), [(r["cohort"], r["family"], r["role"]) for r in pos])
print("net t<-2:", len(neg2), "| net t>+2:", len(pos2))
print("mean of arm means: gross %.2f net %.2f model %.2f" % tuple(st.mean(r["first"][k]["mean"] for r in out if r["first"][k].get("n")) for k in ("gross", "real", "model")))
print("portfolio (first trades, real costs) positive:", [(r["cohort"], r["family"], r["role"], round(r["portfolio_first_real"]["total_return_pct"], 2)) for r in out if r["portfolio_first_real"] and r["portfolio_first_real"]["total_return_pct"] > 0])
