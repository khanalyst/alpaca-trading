"""Q2: does the momentum/reversion split persist beyond one session?"""
import sys,os,json,math
SP=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,SP); sys.path.insert(0,'/home/user/alpaca-trading')
from agent.contracts.rule import evaluate_rule_signal, validate_rule_spec, feature_window_bars, causal_maturity_bars
from research.diagnostic_shadow import _logical_arms
from multiprocessing import Pool

U=json.load(open(SP+"/universe60.json"))
KEEP={"rule.momentum-continuation.fe74f73cfc7d082a","rule.momentum-continuation.984cbcedd4a2846a",
      "rule.volatility-breakout.651520055477ae38","rule.volatility-breakout.30aa361de5e11ca7",
      "rule.volume-breakout.30fece6e3f9b9fd6","rule.volume-breakout.a68b65c3c8e96ff4",
      "rule.vwap-reversion.ab97bfe87ed566de","rule.vwap-reversion.cf8b38b12c9bf952",
      "rule.mean-reversion.e8a26abe2aef631e","rule.trend-pullback.a2b51fa10dd145fd",
      "rule.opening-range-breakout.0eb200d3136d80ee","rule.range-expansion.a9871756f22463a5"}

def sessions(rows):
    s={}
    for r in rows: s.setdefault(r["session"],[]).append(r)
    return s

def job(a):
    if a["variant_id"] not in KEEP: return None
    spec=validate_rule_spec(a["rule_spec"])
    # bound the prefix for non session-anchored families
    win=feature_window_bars(spec)
    need=max(causal_maturity_bars(spec), win or 0, int(spec["atr_period"])+1)+2
    out=[]
    for sym,rows in U.items():
        for sess,sr in sessions(rows).items():
            for i in range(60,len(sr)-1):
                prefix = sr[:i+1] if win is None else sr[max(0,i+1-need):i+1]
                try: sig=evaluate_rule_signal(prefix, spec)
                except Exception: continue
                if not sig: continue
                eb=sr[i+1]; e=float(eb["open"]); d=1 if sig["direction"]=="long" else -1
                rec={"sym":sym,"sess":sess}
                for h in (15,30,60):
                    rec[f"r{h}"]=((sr[i+h][ "close"]-e)/e*1e4*d) if i+h<len(sr) else None
                out.append(rec)
    return {"vid":a["variant_id"],"family":a["family"],"role":a["role"],"rows":out}

if __name__=="__main__":
    with Pool(3) as p: res=[r for r in p.map(job,_logical_arms()) if r]
    json.dump(res,open(SP+"/q2.json","w"))
    print("DONE",len(res),"arms", sum(len(r["rows"]) for r in res),"signals")
