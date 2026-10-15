"""Trade-level replay of every registered arm on fresh public one-minute bars.

Diagnostic only (consolidated tape, not the deployment's Alpaca IEX feed).

Signals: the repository's own evaluators (agent.contracts.rule.evaluate_rule_signal,
agent.contracts.ibr.generate_ibr_signal). Exits: agent.contracts.rule
initialize_exit_state / completed_bar_exit_transition (gap first, stop-first ties,
breakeven/trailing on completed closes). Runtime rules mirrored:
  * entry at the open of the bar after the signal bar (optimistic: the trader polls
    every 60 s; a one-bar-delay sensitivity is reported for first trades);
  * no entry at or after 15:00 New York (strategy.latest_entry_time, engine_cycle.py);
  * force flat at 15:50 (10 minutes before close), at that bar's open;
  * time exit after max_hold_bars completed bars, at the last bar's close;
  * exit_before_minutes deadline (v4+), at the close of the bar ending at the deadline;
  * stressed-cost veto: stop distance from the signal close must be >= the effective
    floor (9 bps / 0.30 = 30 bps), as in agent/risk.py;
  * one position per symbol at a time, re-entry allowed after an exit.
Costs (round trip, bps): gross; fees 0.28 (SEC/TAF upper bound used by the audit);
real = max(one tick, Corwin-Schultz median from docs spread-reference) + 1.0 slippage
+ 0.28 fees; model = 17.0 (shipped costs block).
"""
import sys, os, json, time, math, collections, collections.abc
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_bars, arm_registry
sys.path.insert(0, REPO_ROOT)
import agent.contracts.rule as rule
from agent.contracts.rule import (evaluate_rule_signal, feature_window_bars, causal_maturity_bars,
                                  entry_window_bounds, initialize_exit_state, completed_bar_exit_transition)
from agent.contracts.ibr import generate_ibr_signal, IBRConfig
from multiprocessing import Pool

rule.Mapping = collections.abc.Mapping   # speed only; verified identical signals
NY = ZoneInfo("America/New_York")
SPREAD = json.load(open(os.path.join(REPO_ROOT, "docs", "audit-2026-09-21", "spread-reference.json")))["symbols"]
FEES_RT = 0.28; SLIP_RT = 1.0; MODEL_RT = 17.0
STRESS_FLOOR_BPS = 9.0 / 0.30
LATEST_ENTRY_MIN = 330      # 15:00
FLAT_MIN = 380              # 15:50
B = None

def real_rt(sym, price):
    return max(100.0 / price, SPREAD[sym]["corwin_schultz_median_bps"]) + SLIP_RT + FEES_RT

def ts(b):
    return datetime.fromisoformat(b["timestamp"])

def run_exit(sr, j, direction, entry, stop, target, spec, hold_bars):
    """Walk bars from index j (entry bar, entered at its open). Return exit dict."""
    exit_before = spec.get("exit_before_minutes") if spec else None
    day = ts(sr[j]).date()
    deadline_ts = None
    if exit_before is not None:
        # rule.py:1103-1107: the thesis deadline is 16:00 New York MINUS exit_before_minutes
        deadline_ts = (datetime.combine(day, datetime.min.time(), tzinfo=NY) + timedelta(hours=16)
                       - timedelta(minutes=int(exit_before))).timestamp()
        if ts(sr[j]).timestamp() >= deadline_ts:
            raise ValueError("entry_after_exit_deadline")
    state = initialize_exit_state(
        direction, entry, stop, target,
        breakeven_r=(spec or {}).get("breakeven_r"),
        trailing_stop_r=(spec or {}).get("trailing_stop_r"),
        target_mode=(spec or {}).get("target_mode") or "fixed_r",
        target_lookback=(spec or {}).get("target_lookback") if spec and spec.get("schema") not in ("rule-strategy.v1", "rule-strategy.v2", "rule-strategy.v3") else None,
        exit_before_ts=deadline_ts)
    held = 0
    for k in range(j, len(sr)):
        bar = sr[k]
        if bar["minute"] >= FLAT_MIN:
            return {"reason": "force_flat", "price": bar["open"], "k": k, "held": held}
        if deadline_ts is not None and ts(bar).timestamp() >= deadline_ts:
            return {"reason": "deadline", "price": bar["open"], "k": k, "held": held}
        out = completed_bar_exit_transition(state, bar)
        state = out["state"]; held += 1
        if out["exit"] is not None:
            return {"reason": out["exit"]["reason"], "price": out["exit"]["price"], "k": k, "held": held,
                    "gapped": out["exit"].get("gapped"), "tie": out["exit"].get("tie_broken")}
        # rule.py:1164-1176: entry bar plus max_hold_bars further bars, exit at the last bar's close
        if hold_bars is not None and held >= int(hold_bars) + 1:
            return {"reason": "time", "price": bar["close"], "k": k, "held": held}
    last = sr[-1]
    return {"reason": "session_end", "price": last["close"], "k": len(sr) - 1, "held": held}

def trade_row(sym, sess, sig_bar, entry_bar, direction, entry, stop, target, ex, sig_close):
    d = 1 if direction == "long" else -1
    gross = d * (ex["price"] / entry - 1) * 1e4
    risk_bps = abs(entry - stop) / entry * 1e4
    rr = real_rt(sym, entry)
    return {"sym": sym, "sess": sess, "sig_min": sig_bar["minute"], "entry_min": entry_bar["minute"],
            "exit_min": None if ex["k"] is None else ex.get("exit_minute"), "dir": direction,
            "entry": entry, "stop": stop, "target": target, "exit": ex["price"], "reason": ex["reason"],
            "held": ex["held"], "gross": gross, "risk_bps": risk_bps,
            "stop_bps_from_signal": abs(sig_close - stop) / sig_close * 1e4,
            "target_bps": abs(target - entry) / entry * 1e4,
            "fees": gross - FEES_RT, "real": gross - rr, "model": gross - MODEL_RT, "real_rt": rr,
            "R_gross": gross / risk_bps if risk_bps > 0 else None}

def ctx_for(spec, sym, sess, sr_prefix_minute):
    fam = spec["family"]
    ctx = None
    if fam == "cross_sectional_residual":
        spy = B["SPY"].get(sess, [])
        ctx = {"SPY": [r for r in spy if r["minute"] <= sr_prefix_minute]}
    if spec.get("relative_volume_mode") == "same_clock":
        prior = [r for d, rows in sorted(B[sym].items()) if d < sess for r in rows]
        ctx = dict(ctx or {}); ctx[sym] = prior
    return ctx

def rule_arm(a):
    spec = a["spec"]; win = feature_window_bars(spec); need = causal_maturity_bars(spec)
    lo, hi = entry_window_bounds(spec)
    fam = spec["family"]; hold = spec["max_hold_bars"]
    floor = float(spec.get("min_stop_distance_bps") or 0.0)
    trades = []; firsts = []; vetoed = 0; signals = 0; errors = collections.Counter()
    static_ctx = {}
    for sym in sorted(B):
        if fam == "cross_sectional_residual" and sym == "SPY":
            continue
        for sess in sorted(B[sym]):
            sr = B[sym][sess]
            prior_ctx = None
            if spec.get("relative_volume_mode") == "same_clock":
                n_sess = int(spec.get("relative_volume_sessions") or 5)
                prior_days = [d for d in sorted(B[sym]) if d < sess][-n_sess:]
                prior_ctx = [r for d in prior_days for r in B[sym][d]]
            spy = B["SPY"].get(sess, []) if fam == "cross_sectional_residual" else None
            i = 0; first_done = False
            while i < len(sr) - 1:
                m = sr[i]["minute"]
                if m + 1 >= LATEST_ENTRY_MIN:
                    break
                if not (lo - 1 <= m < hi):
                    i += 1; continue
                pre = sr[:i + 1] if win is None else sr[max(0, i + 1 - max(need, win) - 2):i + 1]
                ctx = None
                if spy is not None:
                    ctx = {"SPY": [r for r in spy if r["minute"] <= m]}
                if prior_ctx is not None:
                    ctx = dict(ctx or {}); ctx[sym] = prior_ctx
                try:
                    s = evaluate_rule_signal(pre if fam != "cross_sectional_residual" else sr[:i + 1], spec,
                                             bars_by_symbol=ctx, symbol=sym)
                except Exception as exc:  # noqa
                    errors[type(exc).__name__] += 1; i += 1; continue
                if not s:
                    i += 1; continue
                signals += 1
                sig_close = float(s["entry_price"]); stop = float(s["stop_price"]); target = float(s["target_price"])
                sd = abs(sig_close - stop) / sig_close * 1e4
                if sd < STRESS_FLOOR_BPS - 1e-9:
                    vetoed += 1; i += 1; continue
                eb = sr[i + 1]; entry = float(eb["open"])
                try:
                    ex = run_exit(sr, i + 1, s["direction"], entry, stop, target, spec, hold)
                except Exception as exc:  # invalid geometry after the gap, etc.
                    errors["exit:" + type(exc).__name__] += 1; i += 1; continue
                ex["exit_minute"] = sr[ex["k"]]["minute"]
                row = trade_row(sym, sess, sr[i], eb, s["direction"], entry, stop, target, ex, sig_close)
                trades.append(row)
                if not first_done:
                    first_done = True
                    # one-bar-delay sensitivity: enter at the close of the entry bar
                    if i + 2 < len(sr):
                        try:
                            ex2 = run_exit(sr, i + 2, s["direction"], float(sr[i + 1]["close"]), stop, target, spec, hold)
                            ex2["exit_minute"] = sr[ex2["k"]]["minute"]
                            r2 = trade_row(sym, sess, sr[i], sr[i + 1], s["direction"], float(sr[i + 1]["close"]),
                                           stop, target, ex2, sig_close)
                        except Exception:
                            r2 = None
                    else:
                        r2 = None
                    firsts.append({"open": row, "delayed": r2})
                i = ex["k"] + 1 if ex["reason"] not in ("force_flat",) else len(sr)
    return {"vid": a["variant_id"], "cohort": a["cohort"], "also_in": a["also_in"], "family": fam, "role": a["role"],
            "signals_considered": signals, "vetoed": vetoed, "errors": dict(errors), "trades": trades, "firsts": firsts,
            "spec": {k: spec.get(k) for k in ("schema", "stop_atr", "target_r", "max_hold_bars", "entry_after_minutes",
                                              "entry_before_minutes", "target_mode", "trailing_stop_r", "breakeven_r",
                                              "exit_before_minutes", "min_stop_distance_bps", "side", "confirmation")}}

def ibr_arm(a):
    strategy = dict(a["config"]["strategy"])
    cfg = IBRConfig.from_mapping(strategy)
    trades = []; firsts = []; vetoed = 0; signals = 0; errors = collections.Counter()
    for sym in sorted(B):
        for sess in sorted(B[sym]):
            sr = B[sym][sess]
            try:
                s = generate_ibr_signal(sym, sr, config=cfg, session_state={"signals": set()})
            except Exception as exc:
                errors[type(exc).__name__] += 1; continue
            if not s:
                continue
            signals += 1
            # signal_ts is the close of the signal bar; the entry bar starts there
            idx = next((k for k, r in enumerate(sr) if ts(r).timestamp() >= s["signal_ts"] - 1e-6), None)
            if idx is None or idx >= len(sr) or sr[idx]["minute"] >= LATEST_ENTRY_MIN:
                continue
            sig_close = float(s["entry_price"]); stop = float(s["stop_price"]); target = float(s["target_price"])
            if abs(sig_close - stop) / sig_close * 1e4 < STRESS_FLOOR_BPS - 1e-9:
                vetoed += 1; continue
            entry = float(sr[idx]["open"])
            try:
                ex = run_exit(sr, idx, s["direction"], entry, stop, target, None, None)
            except Exception as exc:
                errors["exit:" + type(exc).__name__] += 1; continue
            ex["exit_minute"] = sr[ex["k"]]["minute"]
            row = trade_row(sym, sess, sr[idx - 1], sr[idx], s["direction"], entry, stop, target, ex, sig_close)
            trades.append(row); firsts.append({"open": row, "delayed": None})
    return {"vid": a["variant_id"], "cohort": "ibr7", "also_in": [], "family": "ibr", "role": a["role"],
            "signals_considered": signals, "vetoed": vetoed, "errors": dict(errors), "trades": trades, "firsts": firsts,
            "spec": {"strategy": {k: strategy.get(k) for k in ("range_minutes", "breakout_buffer_bps", "target_r",
                                                                 "min_relative_volume", "min_ibr_width_atr",
                                                                 "max_ibr_width_atr", "latest_entry_time",
                                                                 "max_entry_extension_r")}}}

def init():
    global B
    B = load_bars()

def job(a):
    t = time.time()
    r = ibr_arm(a) if a.get("cohort") == "ibr7" else rule_arm(a)
    r["secs"] = round(time.time() - t, 1)
    return r

if __name__ == "__main__":
    out_dir = sys.argv[1]; procs = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    os.makedirs(out_dir, exist_ok=True)
    arms = arm_registry()
    from agent.config import load_config
    from research.diagnostic_shadow import _ibr_logical_arms
    runtime = load_config(os.path.join(REPO_ROOT, "config.yaml"))
    for a in _ibr_logical_arms(runtime):
        arms.append({"cohort": "ibr7", "family": "ibr", "role": a["variant_id"], "variant_id": a["variant_id"],
                     "config": a["config"], "also_in": []})
    todo = [a for a in arms if not os.path.exists(os.path.join(out_dir, a["variant_id"] + ".json"))]
    print("arms", len(arms), "todo", len(todo), flush=True)
    with Pool(procs, initializer=init) as p:
        for r in p.imap_unordered(job, todo):
            json.dump(r, open(os.path.join(out_dir, r["vid"] + ".json"), "w"))
            print(r["cohort"], r["family"], r["role"], "trades", len(r["trades"]), "vetoed", r["vetoed"],
                  "errors", r["errors"], "secs", r["secs"], flush=True)
    print("DONE", flush=True)
