"""Replay the corrected intraday-mechanisms.v3 cohort with the review's driver.

Diagnostic only, on public consolidated-tape bars that have already been
examined.  Identical to docs/review-2026-10-15/research/scripts/replay.py
(entry at the next bar's open, no entry at or after 15:00, flat at 15:50,
30 bps stressed-cost floor, same exit walk and costs) except that it:

* replays only the 12 v3 arms, and
* supplies the point-in-time prior-session context the v3 corrections need:
  the subject's prior sessions for same-clock volume confirmation, and SPY's
  and the subject's prior sessions for the beta-adjusted residual.  Prior
  rows are whole earlier sessions only, so nothing after the signal bar is
  visible.

usage: python -I replay_v3.py <repo_tree> <out_dir> [procs]
"""
import collections
import json
import os
import sys
import time
from multiprocessing import Pool

TREE = os.path.abspath(sys.argv[1])
SCRIPTS = os.path.join(TREE, "docs", "review-2026-10-15", "research", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, TREE)
import replay as R  # noqa: E402  (the review driver, from the frozen tree)
from agent.contracts.rule import (evaluate_rule_signal, feature_window_bars,  # noqa: E402
                                  causal_maturity_bars, entry_window_bounds)


def prior_rows(sym, sess, n):
    days = [d for d in sorted(R.B[sym]) if d < sess][-n:]
    return [r for d in days for r in R.B[sym][d]]


def rule_arm_v3(a):
    spec = a["spec"]; win = feature_window_bars(spec); need = causal_maturity_bars(spec)
    lo, hi = entry_window_bounds(spec)
    fam = spec["family"]; hold = spec["max_hold_bars"]
    trades = []; firsts = []; vetoed = 0; signals = 0; errors = collections.Counter()
    same_clock = (spec.get("relative_volume_mode") == "same_clock" or
                  spec.get("volume_confirmation_mode") == "same_clock")
    beta = spec.get("residual_mode") == "beta"
    for sym in sorted(R.B):
        if fam == "cross_sectional_residual" and sym == "SPY":
            continue
        for sess in sorted(R.B[sym]):
            sr = R.B[sym][sess]
            own_prior = None
            if same_clock:
                own_prior = prior_rows(sym, sess, int(spec.get("relative_volume_sessions") or 5))
            if beta:
                own_prior = prior_rows(sym, sess, int(spec["residual_beta_sessions"]) + 2)
                spy_prior = prior_rows("SPY", sess, int(spec["residual_beta_sessions"]) + 2)
            spy = R.B["SPY"].get(sess, []) if fam == "cross_sectional_residual" else None
            i = 0; first_done = False
            while i < len(sr) - 1:
                m = sr[i]["minute"]
                if m + 1 >= R.LATEST_ENTRY_MIN:
                    break
                if not (lo - 1 <= m < hi):
                    i += 1; continue
                pre = sr[:i + 1] if win is None else sr[max(0, i + 1 - max(need, win) - 2):i + 1]
                ctx = None
                if spy is not None:
                    current_spy = [r for r in spy if r["minute"] <= m]
                    ctx = {"SPY": (spy_prior + current_spy) if beta else current_spy}
                if own_prior is not None:
                    ctx = dict(ctx or {}); ctx[sym] = own_prior
                try:
                    s = evaluate_rule_signal(pre if fam != "cross_sectional_residual" else sr[:i + 1], spec,
                                             bars_by_symbol=ctx, symbol=sym)
                except Exception as exc:  # noqa
                    errors[type(exc).__name__] += 1; i += 1; continue
                if not s:
                    i += 1; continue
                signals += 1
                sig_close = float(s["entry_price"]); stop = float(s["stop_price"]); target = float(s["target_price"])
                if abs(sig_close - stop) / sig_close * 1e4 < R.STRESS_FLOOR_BPS - 1e-9:
                    vetoed += 1; i += 1; continue
                eb = sr[i + 1]; entry = float(eb["open"])
                try:
                    ex = R.run_exit(sr, i + 1, s["direction"], entry, stop, target, spec, hold)
                except Exception as exc:
                    errors["exit:" + type(exc).__name__] += 1; i += 1; continue
                ex["exit_minute"] = sr[ex["k"]]["minute"]
                row = R.trade_row(sym, sess, sr[i], eb, s["direction"], entry, stop, target, ex, sig_close)
                trades.append(row)
                if not first_done:
                    first_done = True
                    r2 = None
                    if i + 2 < len(sr):
                        try:
                            ex2 = R.run_exit(sr, i + 2, s["direction"], float(sr[i + 1]["close"]), stop, target, spec, hold)
                            ex2["exit_minute"] = sr[ex2["k"]]["minute"]
                            r2 = R.trade_row(sym, sess, sr[i], sr[i + 1], s["direction"], float(sr[i + 1]["close"]),
                                             stop, target, ex2, sig_close)
                        except Exception:
                            r2 = None
                    firsts.append({"open": row, "delayed": r2})
                i = ex["k"] + 1 if ex["reason"] not in ("force_flat",) else len(sr)
    return {"vid": a["variant_id"], "cohort": "mech_v3", "also_in": [], "family": fam, "role": a["role"],
            "parent": a["parent"], "signals_considered": signals, "vetoed": vetoed, "errors": dict(errors),
            "trades": trades, "firsts": firsts,
            "spec": {k: spec.get(k) for k in ("schema", "stop_atr", "stop_mode", "signal_event", "target_r",
                                              "max_hold_bars", "entry_after_minutes", "entry_before_minutes",
                                              "target_mode", "trailing_stop_r", "breakeven_r", "exit_before_minutes",
                                              "min_stop_distance_bps", "side", "confirmation",
                                              "volume_confirmation_mode", "residual_mode")}}


def job(a):
    t = time.time()
    r = rule_arm_v3(a)
    r["secs"] = round(time.time() - t, 1)
    return r


if __name__ == "__main__":
    out_dir = sys.argv[2]; procs = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    os.makedirs(out_dir, exist_ok=True)
    from research.mechanism_cohort_v3 import mechanism_cohort
    from agent.contracts.rule import validate_rule_spec
    arms = []
    for f in mechanism_cohort()["families"]:
        a = f["arms"][0]
        arms.append({"variant_id": a["variant_id"], "role": f"{f['name']}#0", "parent": a["parent_variant_id"],
                     "spec": validate_rule_spec(a["rule_spec"])})
    todo = [a for a in arms if not os.path.exists(os.path.join(out_dir, a["variant_id"] + ".json"))]
    print("arms", len(arms), "todo", len(todo), flush=True)
    with Pool(procs, initializer=R.init) as p:
        for r in p.imap_unordered(job, todo):
            json.dump(r, open(os.path.join(out_dir, r["vid"] + ".json"), "w"))
            print(r["family"], "trades", len(r["trades"]), "signals", r["signals_considered"], "vetoed", r["vetoed"],
                  "errors", r["errors"], "secs", r["secs"], flush=True)
    print("DONE", flush=True)
