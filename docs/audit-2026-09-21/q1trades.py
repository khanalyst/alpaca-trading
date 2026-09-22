"""Q1: trades produced after steps 1/3/4, with the stressed-cost gate applied."""
import sys,os,json,math
SP=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,SP); sys.path.insert(0,'/home/user/alpaca-trading')
from agent.config import load_config
from agent.contracts.rule import (evaluate_rule_signal, validate_rule_spec,
                                  feature_window_bars, causal_maturity_bars)
from research.diagnostic_shadow import _logical_arms
from multiprocessing import Pool

U=json.load(open(SP+"/universe60.json"))
CFG=load_config("/home/user/alpaca-trading/config.yaml")
SCEN=float(CFG["risk"]["stressed_cost_scenario_bps"])
RATIO=float(CFG["risk"]["max_stressed_cost_to_risk_ratio"])
REQ=SCEN/RATIO                      # required stop, bps
RT_MODEL=17.0; RT_REAL=1.2          # round trip, bps

def sessions(rows):
    s={}
    for r in rows: s.setdefault(r["session"],[]).append(r)
    return s

def job(a):
    spec=validate_rule_spec(a["rule_spec"])
    win=feature_window_bars(spec)
    need=max(causal_maturity_bars(spec), win or 0, int(spec["atr_period"])+1)+2
    hold=int(spec["max_hold_bars"])
    sig_n=0; vetoed=0; trades=[]
    for sym,rows in U.items():
        for sess,sr in sessions(rows).items():
            taken=False
            for i in range(60,len(sr)-1):
                if taken: break
                prefix = sr[:i+1] if win is None else sr[max(0,i+1-need):i+1]
                try: s=evaluate_rule_signal(prefix, spec)
                except Exception: continue
                if not s: continue
                sig_n+=1
                e=float(sr[i+1]["open"]); d=1 if s["direction"]=="long" else -1
                sd=abs(float(s["entry_price"])-float(s["stop_price"]))/float(s["entry_price"])*1e4
                if sd < REQ-1e-9:            # stressed_cost_risk_limit
                    vetoed+=1; continue
                td=abs(float(s["target_price"])-float(s["entry_price"]))/float(s["entry_price"])*1e4
                out="time"; gross=None
                for k,(h,l,c) in enumerate(((r["high"],r["low"],r["close"]) for r in sr[i+1:i+1+hold])):
                    up=(h-e)/e*1e4 if d>0 else (e-l)/e*1e4
                    dn=(e-l)/e*1e4 if d>0 else (h-e)/e*1e4
                    if dn>=sd: out="stop"; gross=-sd; break
                    if up>=td: out="target"; gross=td; break
                if gross is None:
                    last=sr[min(i+hold,len(sr)-1)]["close"]; gross=(last-e)/e*1e4*d
                trades.append({"sym":sym,"sess":sess,"out":out,"gross":gross})
                taken=True
    return {"vid":a["variant_id"],"family":a["family"],"role":a["role"],
            "signals":sig_n,"vetoed":vetoed,"trades":trades,
            "required_stop_bps":REQ}

if __name__=="__main__":
    with Pool(2) as p: res=p.map(job,_logical_arms())
    json.dump(res,open(SP+"/q1trades.json","w"))
    print("DONE",len(res),"arms;",sum(len(r["trades"]) for r in res),"trades")
