import sys, time
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.config import load_config
from agent.risk import RiskEngine
cfg = load_config("/home/user/alpaca-trading/config.yaml")
r = RiskEngine(cfg)
equity = 100_000.0
close = 100.00
stop = round(close * (1 - 0.003), 2)   # 99.70, the 30 bps floor
target = round(close * (1 + 0.006), 2) # 100.60, target_r 2.0
now = time.time()
for ask in (99.98, 100.00, 100.02, 100.25, 100.50):
    decision = {"symbol": "XYZ", "direction": "long", "entry_price": ask,
                "stop_price": stop, "target_price": target,
                "stop_distance": close - stop, "execution_profile": "shares"}
    market = {"spread_bps": 2.0, "stale": False, "quote_stale": False,
              "quote": {"ask": ask, "bid": ask - 0.02}}
    plan, why = r.vet_open(decision, equity, [], {"XYZ": market}, {}, 0.0,
                           active_trades={}, now=now, cost_cfg=cfg)
    if plan is None:
        print(f"ask={ask}: REJECT {why}")
    else:
        print(f"ask={ask}: shares={plan['shares']} notional={plan['notional']:.0f} "
              f"risk_usd={plan['risk_usd']:.2f} ({plan['risk_usd']/equity*100:.3f}% eq) "
              f"budget={plan['configured_risk_budget_usd']:.0f} stop_dist={plan['stop_distance']:.2f} "
              f"reward={plan['target_price']-ask:.2f} R={(plan['target_price']-ask)/plan['stop_distance']:.2f}")
# second and third positions vs 50% gross cap
pos = [{"symbol": "AAA", "market_value": 24_990.0}]
active = {"AAA": {"risk_usd": 75.0}}
for gross in (24_990.0, 25_100.0, 49_980.0):
    pos_list = [{"symbol": "AAA", "market_value": gross}] if gross < 30000 else [{"symbol": "AAA", "market_value": 24_990.0}, {"symbol": "BBB", "market_value": 24_990.0}]
    act = {p["symbol"]: {"risk_usd": 75.0} for p in pos_list}
    decision = {"symbol": "XYZ", "direction": "long", "entry_price": 100.02,
                "stop_price": stop, "target_price": target,
                "stop_distance": close - stop, "execution_profile": "shares"}
    market = {"spread_bps": 2.0, "stale": False, "quote_stale": False,
              "quote": {"ask": 100.02, "bid": 100.00}}
    plan, why = r.vet_open(decision, equity, pos_list, {"XYZ": market}, {}, gross,
                           active_trades=act, now=now, cost_cfg=cfg)
    print(f"gross={gross}: {'REJECT ' + why if plan is None else 'OK shares=%d' % plan['shares']}")
