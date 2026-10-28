"""Parent (intraday-mechanisms.v2) against corrected child (v3), per family.

usage (from the score directory, after runtime_faithful.py):
    python -I compare_v3.py results/runtime_faithful.json parents.json out.json
"""
import json
import sys

rows = {r["vid"]: r for r in json.load(open(sys.argv[1]))}
parents = json.load(open(sys.argv[2]))


def cell(r, key):
    s = r[key] if key != "first" else r["first"]["real"]
    return {"n": s.get("n", 0), "mean": s.get("mean"), "t": s.get("cluster_t"), "win": s.get("win")}


out = []
f = lambda x, p=2: "-" if x is None else (f"{x:.{p}f}" if isinstance(x, float) else str(x))
print(f"{'family':25s} | {'v2 n':>5s} {'net':>6s} {'t':>6s} {'fresh':>6s} | {'v3 n':>5s} {'net':>6s} {'t':>6s} {'fresh':>6s} {'t_fr':>6s} | v3 exits")
for fam, (child, parent) in parents.items():
    c, p = rows.get(child), rows.get(parent)
    item = {"family": fam, "v2": None if p is None else {"all": cell(p, "first"), "fresh": cell(p, "first_fresh")},
            "v3": None if c is None else {"all": cell(c, "first"), "fresh": cell(c, "first_fresh"),
                                          "exit_mix": c["exit_mix_first"],
                                          "portfolio_return_pct": (c["portfolio_first_real"] or {}).get("total_return_pct")}}
    out.append(item)
    pa, pf = (item["v2"] or {}).get("all", {}), (item["v2"] or {}).get("fresh", {})
    ca, cf = (item["v3"] or {}).get("all", {}), (item["v3"] or {}).get("fresh", {})
    mix = ", ".join(f"{k} {v:.2f}" for k, v in sorted((c or {}).get("exit_mix_first", {}).items(), key=lambda kv: -kv[1])[:3])
    print(f"{fam:25s} | {pa.get('n', 0):5d} {f(pa.get('mean')):>6s} {f(pa.get('t')):>6s} {f(pf.get('mean')):>6s} | "
          f"{ca.get('n', 0):5d} {f(ca.get('mean')):>6s} {f(ca.get('t')):>6s} {f(cf.get('mean')):>6s} {f(cf.get('t')):>6s} | {mix}")
v3 = [o["v3"]["all"] for o in out if o["v3"] and o["v3"]["all"]["n"]]
print("\nv3 arms with trades", len(v3), "| positive net", sum(1 for s in v3 if s["mean"] > 0),
      "| t>+2", sum(1 for s in v3 if (s["t"] or 0) > 2), "| t<-2", sum(1 for s in v3 if (s["t"] or 0) < -2),
      "| mean of arm means %.2f" % (sum(s["mean"] for s in v3) / len(v3)))
v3f = [o["v3"]["fresh"] for o in out if o["v3"] and o["v3"]["fresh"]["n"]]
print("fresh window: positive", sum(1 for s in v3f if s["mean"] > 0), "of", len(v3f),
      "| t>+2", sum(1 for s in v3f if (s["t"] or 0) > 2), "| mean of arm means %.2f" % (sum(s["mean"] for s in v3f) / len(v3f)))
json.dump(out, open(sys.argv[3], "w"), indent=1)
