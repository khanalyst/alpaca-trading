import sys, time, copy
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.config import load_config
from agent.risk import RiskEngine
from agent.contracts.risk_geometry import effective_stop_floor_bps
cfg = load_config("/home/user/alpaca-trading/config.yaml")
print("shipped scenario/ratio:", cfg["risk"]["stressed_cost_scenario_bps"], cfg["risk"]["max_stressed_cost_to_risk_ratio"],
      "-> floor bps", effective_stop_floor_bps(30.0, cfg["risk"]["stressed_cost_scenario_bps"], cfg["risk"]["max_stressed_cost_to_risk_ratio"]))
for scen in (9.0, 15.0, 25.0):
    c = copy.deepcopy(cfg); c["risk"]["stressed_cost_scenario_bps"] = scen
    r = RiskEngine(c)
    d = {"symbol": "XYZ", "direction": "long", "entry_price": 100.0, "stop_price": 99.70,
         "target_price": 100.60, "stop_distance": 0.30, "execution_profile": "shares"}
    m = {"spread_bps": 2.0, "stale": False, "quote_stale": False, "quote": {"ask": 100.0, "bid": 99.98}}
    plan, why = r.vet_open(d, 100_000.0, [], {"XYZ": m}, {}, 0.0, active_trades={}, now=time.time(), cost_cfg=c)
    print(f"scenario {scen} bps: floor {effective_stop_floor_bps(30.0, scen, 0.30):.1f} bps ->", why or f"OK ratio={plan['stressed_cost_to_risk_ratio']:.3f}")
