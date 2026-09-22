# PROVENANCE CORRECTION (22 September 2026).  This is the script as it was
# actually run for SIGNAL-VALUE.md, preserved unchanged below this note.  It
# labels public consolidated-tape bars as provider "alpaca", feed "iex", and
# leaves source_mode to its forward_observed default.  Those labels were
# wrong: the data is third-party consolidated tape fetched after the fact.
# Labels do not enter the arithmetic, so the published numbers stand, but the
# strict policy only admitted these rows because of the false labels.
# step6c.py is the corrected version: delayed_sip, historical_backfill, the
# explicit diagnostic policy, and session-clustered inference.
"""Step 6: signal quality as the primary instrument, clock-matched control."""
import sys,os,json
SP=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,SP); sys.path.insert(0,'/home/user/alpaca-trading')
from research.market_data import normalize_underlying_bar
from research.signal_quality import measure_signal_quality
from research.diagnostic_shadow import _logical_arms
from multiprocessing import Pool

U=json.load(open(SP+"/universe60.json"))
BARS=[normalize_underlying_bar({**r,"provider":"alpaca","feed":"iex"},
                               provider="alpaca", feed="iex")
      for sym in sorted(U) for r in U[sym]]
BY={sym:[normalize_underlying_bar({**r,"provider":"alpaca","feed":"iex"},
                                  provider="alpaca", feed="iex") for r in U[sym]]
    for sym in ("SPY",)}
H=(5,15,30,60,120)

def job(a):
    try:
        res=measure_signal_quality(BARS, a["rule_spec"], horizons=H,
                                   cost_hurdle_bps=1.2, bars_by_symbol=BY)
    except Exception as exc:
        return {"vid":a["variant_id"],"family":a["family"],"role":a["role"],
                "error":f"{type(exc).__name__}: {exc}"}
    out={"vid":a["variant_id"],"family":a["family"],"role":a["role"],
         "events":res.get("event_count"),"sessions":res.get("session_count"),
         "control_policy":res.get("control_policy"),"horizons":{}}
    for key,m in (res.get("horizon_metrics") or {}).items():
        if not isinstance(m,dict): continue
        out["horizons"][key]={
            "n":m.get("candidate_count"),
            "cand":m.get("mean_forward_return_bps"),
            "ctrl":m.get("control_mean_forward_return_bps"),
            "delta":m.get("candidate_minus_control_bps"),
            "delta_sd":m.get("candidate_minus_control_stdev_bps"),
            "matched":m.get("matched_count"),
            "pos_rate":m.get("positive_rate"),
            "t":m.get("candidate_minus_control_t_stat"),
            "clock_gap":(None if m.get("candidate_mean_session_minute") is None
                         or m.get("control_mean_session_minute") is None else
                         m["candidate_mean_session_minute"]-m["control_mean_session_minute"]),
            "session_clusters":m.get("session_clusters"),
            "after_hurdle":m.get("mean_after_hurdle_bps"),
            "after_hurdle_t":m.get("after_hurdle_t_stat")}
    return out

if __name__=="__main__":
    with Pool(4) as p: res=p.map(job,_logical_arms())
    json.dump(res,open(SP+"/step6b.json","w"))
    print("DONE",len(res),"arms")
