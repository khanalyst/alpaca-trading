import numpy as np
rng=np.random.default_rng(7)
med={'HYG':0.64,'SPY':1.11,'SLV':2.97}
N=40000
for name,m in med.items():
    s=m/0.674  # sigma per minute in bps under normality
    for stop,tgt,T in ((30,60,90),(30,45,90),(100,200,30),(100,200,60)):
        paths=np.cumsum(rng.normal(0,s,(N,T)),axis=1)
        up=(paths>=tgt); dn=(paths<=-stop)
        fu=np.where(up.any(1),up.argmax(1),T+1); fd=np.where(dn.any(1),dn.argmax(1),T+1)
        tg=(fu<fd)&(fu<=T); st=(fd<=fu)&(fd<=T); tm=~(tg|st)
        print(f'{name} sigma1m={s:.2f}bps stop={stop} tgt={tgt} T={T}: P(target)={tg.mean():.3f} P(stop)={st.mean():.3f} P(time)={tm.mean():.3f} sigma_T={s*np.sqrt(T):.1f}bps')
