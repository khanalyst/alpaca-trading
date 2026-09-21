"""Replay every registered arm over real 1-minute bars using the repo's own evaluator."""
import sys, json, os
sys.path.insert(0,'/home/user/alpaca-trading')
from agent.contracts.rule import evaluate_rule_signal, validate_rule_spec
from research.diagnostic_shadow import _logical_arms
from research.mechanism_cohort import mechanism_cohort

SP=os.path.dirname(os.path.abspath(__file__))
U=json.load(open(SP+"/universe.json"))

def arms():
    out=[]
    for a in _logical_arms():
        out.append(("rule24", a["family"], a["role"], a["variant_id"],
                    a.get("variant_axis"), a.get("variant_value"), validate_rule_spec(a["rule_spec"])))
    for fam in mechanism_cohort()["families"]:
        for a in fam["arms"]:
            out.append(("mech12", fam["name"], f"arm{a['arm']}", a["variant_id"],
                        None, None, validate_rule_spec(a["rule_spec"])))
    return out

def sessions(rows):
    s={}
    for r in rows: s.setdefault(r["session"],[]).append(r)
    return s

def replay(spec, warm=60):
    """Fire the arm on every bar; record signal + realised forward path."""
    fires=[]
    xs = U.get("SPY")
    spy_by_sess = sessions(xs)
    for sym, rows in U.items():
        for sess, srows in sessions(rows).items():
            bench = {"SPY": spy_by_sess.get(sess, [])} if spec["family"]=="cross_sectional_residual" else None
            if spec["family"]=="cross_sectional_residual" and sym=="SPY": continue
            for i in range(warm, len(srows)-1):
                prefix = srows[:i+1]
                try:
                    if bench is not None:
                        b = {"SPY":[r for r in bench["SPY"] if r["minute"]<=srows[i]["minute"]]}
                        sig = evaluate_rule_signal(prefix, spec, bars_by_symbol=b, symbol=sym)
                    else:
                        sig = evaluate_rule_signal(prefix, spec)
                except Exception:
                    continue
                if not sig: continue
                entry_bar = srows[i+1]
                entry = float(entry_bar["open"])
                fires.append({"sym":sym,"sess":sess,"i":i+1,"minute":entry_bar["minute"],
                              "dir":sig["direction"],"entry":entry,
                              "stop":float(sig["stop_price"]),"target":float(sig["target_price"]),
                              "atr":float(sig.get("atr") or 0.0),
                              "future":[(r["high"],r["low"],r["close"]) for r in srows[i+1:]]})
    return fires
