# Record script: reads universe60.json (public consolidated tape, not committed) from its own directory.
"""Exit research for the reversion arms, walk-forward.

Predeclared exit set (written before running):
  B     the registered bracket as is
  T30   target_r 10 (unreachable), max_hold 30
  T60   target_r 10, max_hold 60
  T120  target_r 10, max_hold 120
  T60W  target_r 10, max_hold 60, stop_atr x3
  V     target_mode session_vwap, max_hold as registered
Selection: best mean net R at realistic cost on the first 9 examined sessions.
Check: the same exit on the last 9 examined sessions. 2026-09-22 reported only.
Force-flat at minute 380; entry at next open; stop-first ties; one trade per symbol-session.
"""
import sys, os, json
SP = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, '/home/user/alpaca-trading')
from agent.contracts.rule import evaluate_rule_signal, validate_rule_spec, feature_window_bars, causal_maturity_bars, rule_variant_id
from research.diagnostic_shadow import _logical_arms
from multiprocessing import Pool
U = json.load(open(SP + "/universe60.json"))
SPREAD = json.load(open("/home/user/alpaca-trading/docs/audit-2026-09-21/spread-reference.json"))["symbols"]
def real_cost(sym):
    s = SPREAD[sym]; return max(s["one_tick_bps"], s["corwin_schultz_median_bps"]) + 1.0 + 0.28
REQ = 30.0
EXITS = {"B": {}, "T30": {"target_r": 10.0, "max_hold_bars": 30}, "T60": {"target_r": 10.0, "max_hold_bars": 60},
         "T120": {"target_r": 10.0, "max_hold_bars": 120}, "T60W": {"target_r": 10.0, "max_hold_bars": 60, "stop_atr": "x3"},
         "V": {"target_mode": "session_vwap"}}
FAMS = ("vwap_reversion", "opening_range_fade", "mean_reversion")
def make(spec, ex):
    s = dict(spec)
    for k, v in ex.items():
        s[k] = min(10.0, spec["stop_atr"] * 3) if v == "x3" else v
    return validate_rule_spec(s)
def job(args):
    arm, ename = args
    spec = make(arm["rule_spec"], EXITS[ename]); win = feature_window_bars(spec)
    need = max(causal_maturity_bars(spec), win or 0, int(spec["atr_period"]) + 1) + 2
    hold = int(spec["max_hold_bars"]); trades = []
    for sym, rows in U.items():
        by = {}
        for r in rows: by.setdefault(r["session"], []).append(r)
        for sess, sr in by.items():
            for i in range(60, len(sr) - 1):
                prefix = sr[:i + 1] if win is None else sr[max(0, i + 1 - need):i + 1]
                try: s = evaluate_rule_signal(prefix, spec)
                except Exception: continue
                if not s: continue
                e = float(sr[i + 1]["open"]); d = 1 if s["direction"] == "long" else -1
                c = float(s["entry_price"])
                sd = abs(c - float(s["stop_price"])) / c * 1e4
                if sd < REQ - 1e-9: continue
                tgt = float(s["target_price"])
                td = (tgt - e) / e * 1e4 * d   # target distance from actual entry (bps)
                if td <= 0: break              # target already reached at entry: skip session
                gross = None; out = "time"
                last_i = min(i + hold, len(sr) - 1)
                for j in range(i + 1, last_i + 1):
                    r = sr[j]
                    if r["minute"] >= 380: gross = (r["open"] - e) / e * 1e4 * d; out = "flat"; break
                    up = (r["high"] - e) / e * 1e4 if d > 0 else (e - r["low"]) / e * 1e4
                    dn = (e - r["low"]) / e * 1e4 if d > 0 else (r["high"] - e) / e * 1e4
                    if dn >= sd: gross = -sd; out = "stop"; break
                    if up >= td: gross = td; out = "target"; break
                if gross is None: gross = (sr[last_i]["close"] - e) / e * 1e4 * d
                cost = real_cost(sym)
                trades.append({"sess": sess, "sym": sym, "gross": gross, "net": gross - cost,
                               "R": (gross - cost) / sd, "stop": sd, "out": out})
                break
    return {"family": arm["family"], "role": arm["role"], "base_vid": arm["variant_id"],
            "exit": ename, "vid": rule_variant_id(spec), "trades": trades}
if __name__ == "__main__":
    arms = [a for a in _logical_arms() if a["family"] in FAMS]
    with Pool(4) as p: res = p.map(job, [(a, e) for a in arms for e in EXITS])
    json.dump(res, open(SP + "/exit_research.json", "w")); print("DONE", len(res))
