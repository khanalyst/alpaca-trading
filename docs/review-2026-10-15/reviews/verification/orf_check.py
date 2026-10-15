from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from agent.contracts.rule import evaluate_rule_signal, validate_rule_spec
z=ZoneInfo('America/New_York')
t0=datetime(2026,10,13,9,30,tzinfo=z)
def bar(i,o,h,l,c,v=1000):
    return {'timestamp':(t0+timedelta(minutes=i)).isoformat(),'open':o,'high':h,'low':l,'close':c,'volume':v}
# 20-minute opening range between 99.80 and 100.00
bars=[bar(i,99.9,100.00 if i==5 else 99.95,99.80 if i==7 else 99.85,99.9) for i in range(20)]
# signal bar 09:50: pokes 40 bps above OR high, closes back inside
bars.append(bar(20,99.95,100.40,99.94,99.99))
orf={'schema':'rule-strategy.v4','family':'opening_range_fade','range_minutes':20,'threshold_bps':8.0,'target_r':1.5,'confirmation':'none'}
s=evaluate_rule_signal(bars,orf)
print('ORF signal:',s['direction'],'entry',s['entry_price'],'stop',round(s['stop_price'],4),'failed-breakout high',100.40,'stop below wick high:',s['stop_price']<100.40)
# ORB repeated firing: after 15-min OR, price closes above OR high for several bars
bars2=[bar(i,99.9,100.00,99.80,99.9) for i in range(15)]
fires=[]
for k in range(6):
    px=100.10+0.02*k
    bars2.append(bar(15+k,px-0.01,px+0.01,px-0.02,px,v=5000))
    orb={'schema':'rule-strategy.v4','family':'opening_range_breakout','range_minutes':15,'threshold_bps':5.0,'confirmation':'none'}
    sig=evaluate_rule_signal(bars2,orb)
    fires.append((bars2[-1]['timestamp'][11:16], sig and sig['direction']))
print('ORB fires on consecutive bars:',fires)
