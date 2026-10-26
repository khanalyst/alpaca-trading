"""Scorecard from the refreshed runtime-faithful view: all 30 sessions, the review's 22, and the fresh 8."""
import json, sys, math, collections
import numpy as np
rf = json.load(open(sys.argv[1]))
old = {r["vid"]: r for r in json.load(open(sys.argv[2]))}
def fmt(x, p=1):
    return "n/a" if x is None else f"{x:+.{p}f}"
rows = []
for r in rf:
    a, fr, o = r["first"]["real"], r.get("first_fresh") or {}, old.get(r["vid"], {}).get("first", {}).get("real", {})
    rows.append({"cohort": r["cohort"], "family": r["family"], "role": r["role"], "vid": r["vid"],
                 "n": a.get("n", 0), "gross": r["first"]["gross"].get("mean"), "net": a.get("mean"),
                 "t": a.get("cluster_t"), "win": a.get("win"),
                 "fresh_n": fr.get("n", 0), "fresh_net": fr.get("mean"), "fresh_t": fr.get("cluster_t"),
                 "old_net": o.get("mean"), "old_t": o.get("cluster_t"),
                 "exits": r.get("exit_mix_first", {})})
json.dump(rows, open(sys.argv[3], "w"), indent=1)
traded = [r for r in rows if r["n"]]
fresh = [r for r in rows if r["fresh_n"]]
print("arms", len(rows), "traded", len(traded))
print("positive net (30 sessions):", sum(r["net"] > 0 for r in traded), "| t>+2:", sum((r["t"] or 0) > 2 for r in traded), "| t<-2:", sum((r["t"] or 0) < -2 for r in traded))
print("fresh window: arms trading", len(fresh), "| positive net", sum(r["fresh_net"] > 0 for r in fresh), "| fresh t>+2:", sum((r["fresh_t"] or 0) > 2 for r in fresh), "| fresh t<-2:", sum((r["fresh_t"] or 0) < -2 for r in fresh))
print("mean of arm means net: 30s %.2f | old 22s %.2f | fresh 8s %.2f" % (np.mean([r["net"] for r in traded]), np.mean([r["old_net"] for r in traded if r["old_net"] is not None]), np.mean([r["fresh_net"] for r in fresh])))
# sign agreement between old and fresh
pairs = [(r["old_net"], r["fresh_net"]) for r in fresh if r["old_net"] is not None]
print("sign agreement old vs fresh:", sum((a > 0) == (b > 0) for a, b in pairs), "of", len(pairs))
print("rank correlation old vs fresh net:", round(float(np.corrcoef(np.argsort(np.argsort([a for a, _ in pairs])), np.argsort(np.argsort([b for _, b in pairs])))[0, 1]), 3))
print()
print("| family | arm | trades | gross | net bps (t) | win | review 22s net | fresh 8s net (t, n) |")
print("| --- | --- | --- | --- | --- | --- | --- | --- |")
for r in sorted([r for r in rows if r["cohort"] in ("diag24", "ibr7")], key=lambda r: (r["cohort"], r["family"], r["role"])):
    if r["cohort"] == "ibr7" and r["role"] != "ibr.baseline": continue
    print(f"| {r['family']} | {r['role']} | {r['n']} | {fmt(r['gross'])} | {fmt(r['net'])} ({fmt(r['t'],2)}) | {('n/a' if r['win'] is None else f'{100*r[chr(119)+chr(105)+chr(110)]:.0f}%')} | {fmt(r['old_net'])} | {fmt(r['fresh_net'])} ({fmt(r['fresh_t'],2)}, {r['fresh_n']}) |")
print()
print("positive in the fresh window:")
for r in sorted(fresh, key=lambda r: -r["fresh_net"])[:12]:
    print(f"  {r['cohort']:11s} {r['family']:25s} {r['role'][:26]:26s} fresh {fmt(r['fresh_net'])} t {fmt(r['fresh_t'],2)} n {r['fresh_n']} | 30s {fmt(r['net'])} t {fmt(r['t'],2)}")
