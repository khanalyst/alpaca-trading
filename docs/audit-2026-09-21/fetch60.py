import time,json,os,urllib.request,datetime as dt
from zoneinfo import ZoneInfo
NY=ZoneInfo("America/New_York")
SP=os.path.dirname(os.path.abspath(__file__))
SYMS="SPY QQQ IWM DIA XLF XLK XLE XLV XLI XLP XLY XLU XLB XLRE VTI VO VB EFA EEM TLT HYG GLD SLV SMH".split()
now=int(time.time()); out={}
for s in SYMS:
    merged={}
    for wb in (4,3,2,1):
        p2=now-(wb-1)*7*86400; p1=p2-7*86400
        u=f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?interval=1m&period1={p1}&period2={p2}"
        for attempt in range(3):
            try:
                r=urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"})
                d=json.load(urllib.request.urlopen(r,timeout=30)); break
            except Exception:
                time.sleep(2*(attempt+1)); d=None
        if not d: continue
        res=d["chart"]["result"]
        if not res: continue
        ts=res[0].get("timestamp") or []; q=res[0]["indicators"]["quote"][0]
        for i,t in enumerate(ts):
            o,h,l,c,v=q["open"][i],q["high"][i],q["low"][i],q["close"][i],q["volume"][i]
            if None in (o,h,l,c): continue
            d2=dt.datetime.fromtimestamp(t,dt.timezone.utc).astimezone(NY)
            m=d2.hour*60+d2.minute-570
            if not (0<=m<390): continue
            merged[t]={"symbol":s,"timestamp":d2.isoformat(),"open":float(o),"high":float(h),
                       "low":float(l),"close":float(c),"volume":float(v or 0),
                       "session":d2.date().isoformat(),"minute":m}
        time.sleep(0.25)
    out[s]=[merged[k] for k in sorted(merged)]
    print(s,len(out[s]),flush=True)
json.dump(out,open(SP+"/universe60.json","w"))
sess=sorted({r["session"] for v in out.values() for r in v})
print("SESSIONS",len(sess),sess[0],"..",sess[-1])
print("BARS",sum(len(v) for v in out.values()))
