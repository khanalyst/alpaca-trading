"""Shared loading and arm registry for the fresh replay (diagnostic only)."""
import os as _os
REPO_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "..", ".."))
import json, os, sys, datetime as dt
from zoneinfo import ZoneInfo
sys.path.insert(0, REPO_ROOT)
NY = ZoneInfo("America/New_York")
DATA = os.environ.get("REVIEW_BARS_1M", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bars_1m.json"))

def load_bars(path=DATA):
    raw = json.load(open(path))["bars"]
    out = {}
    for sym, rows in raw.items():
        sess = {}
        for r in rows:
            b = dict(r)
            b["feed"] = "delayed_sip"; b["provider"] = "public_chart"; b["source_mode"] = "historical_backfill"
            sess.setdefault(r["session"], []).append(b)
        out[sym] = sess
    return out

def arm_registry():
    from research.diagnostic_shadow import _logical_arms
    from research.mechanism_cohort import mechanism_cohort as m1
    from research.mechanism_cohort_v2 import mechanism_cohort as m2
    from research.mechanism_cost_cohort import mechanism_cost_cohort as mc
    from research.mechanism_deadline_cohort import mechanism_deadline_cohort as md
    from research.mechanism_refresh_cohort import mechanism_refresh_cohort as mr
    from agent.contracts.rule import validate_rule_spec
    arms = []
    for a in _logical_arms():
        arms.append({"cohort": "diag24", "family": a["family"], "role": a["role"],
                     "variant_id": a["variant_id"], "spec": validate_rule_spec(a["rule_spec"])})
    for tag, fn in (("mech_v1", m1), ("mech_v2", m2), ("cost_v1", mc), ("deadline_v1", md), ("refresh_v1", mr)):
        c = fn()
        for f in c["families"]:
            for a in f["arms"]:
                arms.append({"cohort": tag, "family": a["rule_spec"]["family"], "role": f"{f['name']}#{a['arm']}",
                             "variant_id": a["variant_id"], "spec": validate_rule_spec(a["rule_spec"])})
    # de-duplicate identical specs across cohorts (keep first registration)
    seen = {}; uniq = []
    for a in arms:
        if a["variant_id"] in seen:
            seen[a["variant_id"]]["also_in"].append(a["cohort"]); continue
        a["also_in"] = []; seen[a["variant_id"]] = a; uniq.append(a)
    return uniq
