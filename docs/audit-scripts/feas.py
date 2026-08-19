import sys, math
sys.path.insert(0, "/home/user/alpaca-trading")
from research.costs import CostModel
from agent.contracts.rule import MIN_STOP_DISTANCE_BPS
from research.factory_core import NOTIONAL_CAP_PCT

m = CostModel()
print("=== COST MODEL (shipped defaults) ===")
print(f"spread={m.spread_bps} slippage={m.slippage_bps} fee={m.fee_bps} -> per-side entry_cost={m.entry_cost_bps} bps")

price, qty = 600.0, 1.0
rt = m.round_trip_cost(price, price, qty, 1.0, vehicle="equity")
rt_bps = rt / price * 10_000
print(f"round-trip cost on flat trade = ${rt:.4f} on ${price} = {rt_bps:.2f} bps of notional")

R_bps = MIN_STOP_DISTANCE_BPS
print(f"\n=== R (risk unit) AT THE FLOOR ===")
print(f"MIN_STOP_DISTANCE_BPS = {R_bps} bps")
print(f"cost / R           = {rt_bps/R_bps:.3f} R  <-- every trade starts down this much")
for s in (9.0, 15.0, 25.0, 50.0):
    print(f"  stress {s:>4} bps (entry notional) -> {s/R_bps:.3f} R" + ("   <-- AUTHORIZING check" if s==25.0 else ""))

print(f"\n=== BREAK-EVEN WIN RATE (2R target, 1R stop) ===")
for name, c in (("baseline 17bps", rt_bps/R_bps), ("25bps stress", 25.0/R_bps)):
    p = (1.0 + c) / 3.0
    print(f"  {name:>15}: need {p*100:.1f}% win rate just to break even")

print(f"\n=== SIZING: does the notional cap bind? ===")
risk_pct, cap_pct = 0.005, NOTIONAL_CAP_PCT/100.0
cash = 100_000.0
stop_frac = R_bps/10_000
q_risk = cash*risk_pct/(price*stop_frac)
q_cap  = cash*cap_pct/price
print(f"  risk-based qty  = {q_risk:.1f} shares")
print(f"  notional-cap qty= {q_cap:.1f} shares   (cap={NOTIONAL_CAP_PCT}%)")
print(f"  binding         = {'NOTIONAL CAP' if q_cap<q_risk else 'risk'}")
delivered = min(q_risk,q_cap)*price*stop_frac
print(f"  intended risk   = ${cash*risk_pct:,.0f}  ({risk_pct*100}% of equity)")
print(f"  DELIVERED risk  = ${delivered:,.0f}  ({delivered/cash*100:.3f}% of equity)")
print(f"  shortfall factor= {cash*risk_pct/delivered:.1f}x under-risked")
print(f"  cap binds for any stop tighter than {risk_pct/cap_pct*10_000:.0f} bps of price")

print(f"\n=== ARE THE RISK LIMITS REACHABLE? ===")
dr = delivered/cash*100
print(f"  delivered risk/trade      = {dr:.3f}%")
print(f"  3 concurrent positions    = {3*dr:.3f}% open risk vs max_open_risk_pct=2.0  -> {'DEAD' if 3*dr<2.0 else 'live'}")
print(f"  stop-outs to hit 2% daily = {2.0/dr:.1f} consecutive losses  -> {'DEAD' if 2.0/dr>10 else 'live'}")

print(f"\n=== WHEN DOES THE 30bps FLOOR OVERRIDE stop_atr? ===")
for atr_bps in (3,5,8,15,30):
    thr = R_bps/atr_bps
    frac = (min(thr,10.0)-0.2)/(10.0-0.2)
    print(f"  1-min ATR={atr_bps:>2} bps -> floor binds for stop_atr < {thr:5.1f}  = {frac*100:5.1f}% of the [0.2,10] grammar range is a NO-OP")

print(f"\n=== STATISTICAL POWER (min useful edge = 0.05R) ===")
for sd in (1.0, 1.43):
    for n in (100,150,6437):
        se = sd/math.sqrt(n); mde = 2.80*se
        print(f"  sd={sd}R n={n:>5}: minimum detectable edge = {mde:.3f}R" + ("  <-- shadow floor" if n==150 else ""))
    need = (2.80*sd/0.05)**2
    print(f"  sd={sd}R -> trades needed to detect 0.05R at 80% power/5%: {need:,.0f}\n")
