"""Step 5: does a different exit geometry help? Sweep the repo's own ladders."""
import sys,os,json,math
SP=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,SP); sys.path.insert(0,'/home/user/alpaca-trading')
from agent.config import load_config
from agent.contracts.rule import (evaluate_rule_signal, validate_rule_spec,
                                  feature_window_bars, causal_maturity_bars)
from research.diagnostic_shadow import _logical_arms
from research.path_telemetry import TARGET_HOLD_TARGET_LADDER, TARGET_HOLD_HOLD_LADDER
TARGET_HOLD_TARGET_LADDER = tuple(sorted(set(TARGET_HOLD_TARGET_LADDER) | {1.5}))
from multiprocessing import Pool

U=json.load(open(SP+"/universe60.json"))
CFG=load_config("/home/user/alpaca-trading/config.yaml")
REQ=float(CFG["risk"]["stressed_cost_scenario_bps"])/float(CFG["risk"]["max_stressed_cost_to_risk_ratio"])
RT_M, RT_R = 17.0, 1.2

def sessions(rows):
    s={}
    for r in rows: s.setdefault(r["session"],[]).append(r)
    return s

def job(a):
    spec=validate_rule_spec(a["rule_spec"])
    win=feature_window_bars(spec)
    need=max(causal_maturity_bars(spec), win or 0, int(spec["atr_period"])+1)+2
    cross = spec["family"]=="cross_sectional_residual"
    spy=sessions(U["SPY"])
    sigs=[]
    for sym,rows in U.items():
        if cross and sym=="SPY": continue
        for sess,sr in sessions(rows).items():
            taken=False
            for i in range(60,len(sr)-1):
                if taken: break
                prefix = sr[:i+1] if (win is None or cross) else sr[max(0,i+1-need):i+1]
                try:
                    if cross:
                        b={"SPY":[r for r in spy.get(sess,[]) if r["minute"]<=sr[i]["minute"]]}
                        s=evaluate_rule_signal(prefix, spec, bars_by_symbol=b, symbol=sym)
                    else:
                        s=evaluate_rule_signal(prefix, spec)
                except Exception: continue
                if not s: continue
                e=float(sr[i+1]["open"]); d=1 if s["direction"]=="long" else -1
                sd=abs(float(s["entry_price"])-float(s["stop_price"]))/float(s["entry_price"])*1e4
                if sd < REQ-1e-9: continue
                path=[((r["high"]-e)/e*1e4 if d>0 else (e-r["low"])/e*1e4,
                       (e-r["low"])/e*1e4 if d>0 else (r["high"]-e)/e*1e4,
                       (r["close"]-e)/e*1e4*d) for r in sr[i+1:i+1+400]]
                sigs.append({"sess":sess,"sd":sd,"path":path})
                taken=True
    if not sigs: return {"vid":a["variant_id"],"family":a["family"],"role":a["role"],"n":0,"grid":{}}
    grid={}
    for tr in TARGET_HOLD_TARGET_LADDER:
        for hb in TARGET_HOLD_HOLD_LADDER:
            per={}; outs={"stop":0,"target":0,"time":0}
            for g in sigs:
                sd=g["sd"]; td=sd*tr; res=None
                for (up,dn,cl) in g["path"][:hb]:
                    if dn>=sd: res=-sd; outs["stop"]+=1; break
                    if up>=td: res=td; outs["target"]+=1; break
                if res is None:
                    res=g["path"][min(hb,len(g["path"]))-1][2] if g["path"] else 0.0
                    outs["time"]+=1
                per.setdefault(g["sess"],[]).append(res)
            sm=[sum(v)/len(v) for v in per.values()]
            if len(sm)<2: continue
            mu=sum(sm)/len(sm)
            sd_=math.sqrt(sum((x-mu)**2 for x in sm)/(len(sm)-1))
            grid[f"{tr}|{hb}"]={"gross":mu,"t":(mu/(sd_/math.sqrt(len(sm))) if sd_>0 else 0.0),
                                "net_real":mu-RT_R,"net_model":mu-RT_M,
                                "target_pct":100*outs["target"]/len(sigs),
                                "time_pct":100*outs["time"]/len(sigs),
                                "per_session":{k:sum(v)/len(v) for k,v in sorted(per.items())}}
    return {"vid":a["variant_id"],"family":a["family"],"role":a["role"],"n":len(sigs),"grid":grid}

if __name__=="__main__":
    with Pool(2) as p: res=p.map(job,_logical_arms())
    json.dump(res,open(SP+"/step5b.json","w"))
    print("DONE",len(res),"arms;",sum(r["n"] for r in res),"signals;",
          len(TARGET_HOLD_TARGET_LADDER)*len(TARGET_HOLD_HOLD_LADDER),"grid points each")
