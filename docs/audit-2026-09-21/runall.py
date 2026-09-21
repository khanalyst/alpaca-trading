import sys, json, os, math
SP=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,SP); sys.path.insert(0,'/home/user/alpaca-trading')
from harness import arms, replay
from multiprocessing import Pool

RT_MODELLED=17.0   # repo cost model round trip, bps
RT_REAL=1.5        # realistic SPY/QQQ round trip, bps

def evaluate(job):
    lane, fam, role, vid, axis, val, spec = job
    fires = replay(spec)
    n=len(fires)
    res={"lane":lane,"family":fam,"role":role,"variant_id":vid,"axis":axis,"value":val,
         "spec":{k:spec[k] for k in ("threshold_bps","zscore","volume_multiplier","compression_bps",
                                     "target_r","max_hold_bars","confirmation","range_minutes",
                                     "lookback","entry_trigger","regime_mode","target_mode")
                 if k in spec},
         "fires":n}
    if not n:
        res["status"]="no_signal"; return res
    hold=int(spec["max_hold_bars"])
    stop_bps=[];tgt_bps=[];outs=[];rets={h:[] for h in (1,5,15,30,60,90)};mfe=[];mae=[]
    floor_bind=0
    for f in fires:
        e=f["entry"]; d=1 if f["dir"]=="long" else -1
        sd=abs(e-f["stop"])/e*1e4; td=abs(f["target"]-e)/e*1e4
        stop_bps.append(sd); tgt_bps.append(td)
        if f["atr"] and (f["atr"]/e*1e4)*1.0 < 30.0: floor_bind+=1
        path=f["future"][:hold]
        # outcome walk: stop first on tie
        out="time"; hi=-9e9; lo=9e9
        for (h,l,c) in path:
            up=(h-e)/e*1e4*d if d>0 else (e-l)/e*1e4
            dn=(e-l)/e*1e4*d if d>0 else (h-e)/e*1e4
            up = (h-e)/e*1e4 if d>0 else (e-l)/e*1e4
            dn = (e-l)/e*1e4 if d>0 else (h-e)/e*1e4
            hi=max(hi,up); lo=min(lo,-dn)
            if dn>=sd: out="stop"; break
            if up>=td: out="target"; break
        outs.append(out); mfe.append(hi); mae.append(lo)
        for h in rets:
            if len(f["future"])>=h:
                c=f["future"][h-1][2]; rets[h].append((c-e)/e*1e4*d)
    def mu(x): return sum(x)/len(x) if x else None
    def sd_(x):
        if len(x)<2: return None
        m=mu(x); return math.sqrt(sum((v-m)**2 for v in x)/(len(x)-1))
    res.update({
      "status":"traded",
      "stop_bps_med": sorted(stop_bps)[len(stop_bps)//2],
      "target_bps_med": sorted(tgt_bps)[len(tgt_bps)//2],
      "atr_floor_would_bind_pct": 100.0*floor_bind/n,
      "outcome_pct": {k: round(100.0*outs.count(k)/n,2) for k in ("stop","target","time")},
      "mfe_med": sorted(mfe)[len(mfe)//2], "mae_med": sorted(mae)[len(mae)//2],
      "fwd_bps": {},
    })
    for h,v in rets.items():
        m=mu(v); s=sd_(v)
        res["fwd_bps"][h]={"n":len(v),"mean":m,"sd":s,
            "t": (m/(s/math.sqrt(len(v))) if s and s>0 and len(v)>1 else None),
            "net_modelled": (m-RT_MODELLED) if m is not None else None,
            "net_real": (m-RT_REAL) if m is not None else None}
    return res

if __name__=="__main__":
    A=arms()
    with Pool(6) as p:
        out=p.map(evaluate, A)
    json.dump(out, open(SP+"/results.json","w"), indent=1)
    print("DONE", len(out))
