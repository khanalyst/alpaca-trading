"""Repo signal_quality for every registered rule arm on fresh public one-minute bars.

Diagnostic only: consolidated-tape bars labelled historical_backfill on delayed_sip,
evaluated through research.costs.diagnostic_backfill_policy, exactly as the
September audit's step6c.py did. Windows: ALL (22 sessions, 2026-09-14..10-13) and
UNSEEN (2026-09-24..10-13, the 14 sessions after the audit's last forward day).
"""
import sys, os, json, time
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import arm_registry, DATA
sys.path.insert(0, REPO_ROOT)
from research.market_data import normalize_underlying_bar
from research.signal_quality import measure_signal_quality
from research.costs import diagnostic_backfill_policy
from multiprocessing import Pool

OUT = sys.argv[1]
RAW = json.load(open(DATA))["bars"]
def norm(r):
    return normalize_underlying_bar({**r, "provider": "public_chart", "feed": "delayed_sip",
                                     "source_mode": "historical_backfill"})
ALL = [b for s in sorted(RAW) for b in map(norm, RAW[s])]
UNSEEN_FROM = date(2026, 9, 24)
WINDOWS = {"all": ALL, "unseen": [b for b in ALL if b.identity.session_date >= UNSEEN_FROM]}
POLICY = diagnostic_backfill_policy()
H = (5, 15, 30, 60, 120)
KEYS = {"n": "candidate_count", "matched": "matched_count", "delta": "candidate_minus_control_bps",
        "event_t": "candidate_minus_control_t_stat",
        "cluster_t": "candidate_minus_control_cluster_t_stat",
        "cluster_df": "candidate_minus_control_cluster_df",
        "sign_flip_p": "candidate_minus_control_cluster_sign_flip_p_value",
        "clusters": "session_clusters"}

def job(item):
    a, wname = item
    bars = WINDOWS[wname]
    by = {}
    for b in bars: by.setdefault(b.symbol, []).append(b)
    t = time.time()
    try:
        res = measure_signal_quality(bars, a["spec"], policy=POLICY, horizons=H, bars_by_symbol=by,
                                     measurement_scope="previously_examined_retrospective_diagnostic")
    except Exception as exc:
        return {"vid": a["variant_id"], "cohort": a["cohort"], "family": a["family"], "role": a["role"],
                "window": wname, "error": repr(exc)}
    hm = res.get("horizon_metrics") or {}
    return {"vid": a["variant_id"], "cohort": a["cohort"], "also_in": a["also_in"], "family": a["family"],
            "role": a["role"], "window": wname, "events": res.get("event_count"), "secs": round(time.time() - t, 1),
            "horizons": {k: {n: m.get(src) for n, src in KEYS.items()} for k, m in hm.items()},
            "top_keys": sorted(res.keys())}

if __name__ == "__main__":
    arms = arm_registry()
    items = [(a, w) for w in ("all", "unseen") for a in arms]
    out = []
    with Pool(int(sys.argv[2]) if len(sys.argv) > 2 else 2) as p:
        for r in p.imap_unordered(job, items):
            out.append(r); print(len(out), r["window"], r["cohort"], r["family"], r.get("events"), r.get("secs"), r.get("error", ""), flush=True)
            json.dump(out, open(OUT, "w"), indent=1)
    print("DONE", len(out))
