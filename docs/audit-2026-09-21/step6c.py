"""Q1 re-run: signal quality with session-clustered inference, examined window only.

Public consolidated-tape bars, labelled honestly as historical_backfill on
delayed_sip, evaluated through the explicit diagnostic policy.  Sessions after
2026-09-21 are excluded: they belong to the sealed preregistered window.
"""
import sys, os, json
from datetime import date
SP = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, '/home/user/alpaca-trading')
from research.market_data import normalize_underlying_bar
from research.signal_quality import measure_signal_quality
from research.costs import diagnostic_backfill_policy
from research.diagnostic_shadow import _logical_arms
from multiprocessing import Pool

BOUNDARY = date(2026, 9, 21)
U = json.load(open(SP + "/universe60.json"))
def norm(r):
    return normalize_underlying_bar({**r, "provider": "public_chart",
        "feed": "delayed_sip", "source_mode": "historical_backfill"})
BARS = [b for s in sorted(U) for b in map(norm, U[s]) if b.identity.session_date <= BOUNDARY]
BY = {"SPY": [b for b in BARS if b.symbol == "SPY"]}
POLICY = diagnostic_backfill_policy()
H = (5, 15, 30, 60, 120)
KEYS = {"n": "candidate_count", "matched": "matched_count",
        "delta": "candidate_minus_control_bps",
        "event_t": "candidate_minus_control_t_stat",
        "cluster_se": "candidate_minus_control_cluster_stderr_bps",
        "cluster_t": "candidate_minus_control_cluster_t_stat",
        "cluster_df": "candidate_minus_control_cluster_df",
        "sign_flip_p": "candidate_minus_control_cluster_sign_flip_p_value",
        "clusters": "session_clusters"}

def job(a):
    try:
        res = measure_signal_quality(BARS, a["rule_spec"], policy=POLICY,
                                     horizons=H, bars_by_symbol=BY)
    except Exception as exc:
        return {"vid": a["variant_id"], "family": a["family"], "error": repr(exc)}
    return {"vid": a["variant_id"], "family": a["family"], "role": a["role"],
            "events": res.get("event_count"),
            "horizons": {k: {n: m.get(src) for n, src in KEYS.items()}
                         for k, m in (res.get("horizon_metrics") or {}).items()}}

if __name__ == "__main__":
    with Pool(4) as p: out = p.map(job, _logical_arms())
    json.dump(out, open(SP + "/step6c.json", "w"), indent=1)
    print("DONE", len(out), "arms", len({b.identity.session_date for b in BARS}), "examined sessions")
