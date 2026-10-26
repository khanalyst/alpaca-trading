"""Recompute first-trade stats on the review's 22 sessions from the new replay and compare."""
import json, glob, os, sys, statistics as st
new_dir, old_json = sys.argv[1], sys.argv[2]
old = {r["vid"]: r for r in json.load(open(old_json))}
diffs = []
for p in sorted(glob.glob(os.path.join(new_dir, "*.json"))):
    a = json.load(open(p))
    ft = [f["open"] for f in a["firsts"] if f["open"]["sess"] <= "2026-10-13"]
    o = old.get(a["vid"])
    if not o or not o["first"]["real"].get("n"):
        continue
    n_new = len(ft)
    m_new = st.mean(t["real"] for t in ft) if ft else None
    diffs.append((a["vid"], o["first"]["real"]["n"], n_new, round(o["first"]["real"]["mean"], 4), round(m_new, 4) if m_new is not None else None))
bad = [d for d in diffs if d[1] != d[2] or (d[4] is None) or abs(d[3] - d[4]) > 1e-6]
print("arms compared", len(diffs), "| mismatches", len(bad))
for d in bad[:15]:
    print("  ", d)
