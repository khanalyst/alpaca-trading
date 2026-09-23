# Record script: reads universe60.json (public consolidated tape, not committed) from its own directory.
"""All 24 rule arms, bracket replay under the shipped admission gate, net of three cost levels.

Examined window: 2026-08-26..2026-09-21 (18 sessions). Sealed window: sessions after 2026-09-21.
One trade per symbol-session (first admitted signal), entry next bar open, stop-first ties.
"""
import sys, os, json
SP = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, '/home/user/alpaca-trading')
from agent.config import load_config
from agent.contracts.rule import (evaluate_rule_signal, validate_rule_spec,
                                  feature_window_bars, causal_maturity_bars)
from research.diagnostic_shadow import _logical_arms
from multiprocessing import Pool

U = json.load(open(SP + "/universe60.json"))
CFG = load_config("/home/user/alpaca-trading/config.yaml")
REQ = float(CFG["risk"]["stressed_cost_scenario_bps"]) / float(CFG["risk"]["max_stressed_cost_to_risk_ratio"])
SPREAD = json.load(open("/home/user/alpaca-trading/docs/audit-2026-09-21/spread-reference.json"))["symbols"]
FEES_RT = 0.28          # SEC Section 31 upper bound on the sell leg; commission 0; FINRA TAF ~0.003 bps
SLIP_SIDE = 0.5
def cost(sym):
    s = SPREAD[sym]; spread = max(s["one_tick_bps"], s["corwin_schultz_median_bps"])
    return {"fees": FEES_RT, "real": spread + 2 * SLIP_SIDE + FEES_RT, "model": 17.0}

def by_session(rows):
    out = {}
    for r in rows: out.setdefault(r["session"], []).append(r)
    return out

def job(a):
    spec = validate_rule_spec(a["rule_spec"]); win = feature_window_bars(spec)
    need = max(causal_maturity_bars(spec), win or 0, int(spec["atr_period"]) + 1) + 2
    hold = int(spec["max_hold_bars"]); trades = []; vetoed = 0
    for sym, rows in U.items():
        c = cost(sym)
        for sess, sr in by_session(rows).items():
            for i in range(60, len(sr) - 1):
                prefix = sr[:i + 1] if win is None else sr[max(0, i + 1 - need):i + 1]
                try: s = evaluate_rule_signal(prefix, spec)
                except Exception: continue
                if not s: continue
                e = float(sr[i + 1]["open"]); d = 1 if s["direction"] == "long" else -1
                sd = abs(float(s["entry_price"]) - float(s["stop_price"])) / float(s["entry_price"]) * 1e4
                if sd < REQ - 1e-9: vetoed += 1; continue
                td = abs(float(s["target_price"]) - float(s["entry_price"])) / float(s["entry_price"]) * 1e4
                gross = None; out = "time"
                for r in sr[i + 1:i + 1 + hold]:
                    h, l = r["high"], r["low"]
                    up = (h - e) / e * 1e4 if d > 0 else (e - l) / e * 1e4
                    dn = (e - l) / e * 1e4 if d > 0 else (h - e) / e * 1e4
                    if dn >= sd: gross = -sd; out = "stop"; break
                    if up >= td: gross = td; out = "target"; break
                if gross is None:
                    last = sr[min(i + hold, len(sr) - 1)]["close"]; gross = (last - e) / e * 1e4 * d
                trades.append({"sym": sym, "sess": sess, "out": out, "gross": gross,
                               "stop_bps": sd, **{"cost_" + k: v for k, v in c.items()}})
                break
    return {"vid": a["variant_id"], "family": a["family"], "role": a["role"],
            "vetoed": vetoed, "trades": trades}

if __name__ == "__main__":
    with Pool(4) as p: res = p.map(job, _logical_arms())
    json.dump(res, open(SP + "/forward_costs.json", "w"))
    print("DONE", len(res), sum(len(r["trades"]) for r in res))
