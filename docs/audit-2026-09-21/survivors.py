import sys,os,json,math
SP=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,SP); sys.path.insert(0,'/home/user/alpaca-trading')
from harness import arms, replay
from multiprocessing import Pool
KEEP={"rule.momentum-continuation.fe74f73cfc7d082a","rule.momentum-continuation.984cbcedd4a2846a",
      "rule.volatility-breakout.651520055477ae38","rule.volatility-breakout.30aa361de5e11ca7",
      "rule.volume-breakout.30fece6e3f9b9fd6","rule.volume-breakout.a68b65c3c8e96ff4",
      "rule.trend-pullback.a2b51fa10dd145fd","rule.vwap-reversion.ab97bfe87ed566de"}
def job(a):
    lane,fam,role,vid,ax,val,spec=a
    if vid not in KEEP: return None
    fires=replay(spec)
    out=[]
    for f in fires:
        d=1 if f["dir"]=="long" else -1; e=f["entry"]
        r30=None
        if len(f["future"])>=30: r30=(f["future"][29][2]-e)/e*1e4*d
        out.append({"sym":f["sym"],"sess":f["sess"],"min":f["minute"],"r30":r30})
    return {"vid":vid,"rows":out}
if __name__=="__main__":
    with Pool(6) as p: res=[r for r in p.map(job,arms()) if r]
    json.dump(res,open(SP+"/survivors.json","w"))
    print("done",len(res))
