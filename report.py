#!/usr/bin/env python3
"""Performance report over the trade journal.

  python3 report.py                # reads runtime/journal.db
  python3 report.py path/to/journal.db

Answers "is this working?" with numbers: equity curve, win rate and
expectancy, per-symbol results, confidence calibration (do the model's
0.8-confidence trades actually beat its 0.7s?), and why proposals were
rejected. PnL percentages are on margin (ccxt's convention), as journaled
at close time.
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent / "runtime" / "journal.db")


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(1, 60 - len(title)))


def main() -> int:
    if not DB.exists():
        print(f"No journal at {DB} - run the agent first.")
        return 1
    db = sqlite3.connect(DB)

    # ---------------------------------------------------------- equity
    eq = db.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()
    section("EQUITY")
    if eq:
        first_ts, first_eq = eq[0]
        last_ts, last_eq = eq[-1]
        peak, max_dd = first_eq, 0.0
        for _, e in eq:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak * 100)
        change = (last_eq - first_eq) / first_eq * 100 if first_eq else 0.0
        print(f"  {fmt_ts(first_ts)} -> {fmt_ts(last_ts)} UTC "
              f"({len(eq)} snapshots)")
        print(f"  start {first_eq:,.2f}  end {last_eq:,.2f}  "
              f"({change:+.2f}%)   peak {peak:,.2f}   "
              f"max drawdown {max_dd:.2f}%")
        print("  NOTE: deposits/withdrawals are included in raw equity; "
              "see the transfers section.")
    else:
        print("  no equity snapshots yet")

    # ---------------------------------------------------------- trades
    closes = db.execute(
        "SELECT ts, symbol, pnl_pct, reason FROM trades "
        "WHERE action='close' ORDER BY ts").fetchall()
    opens = db.execute(
        "SELECT ts, symbol, confidence, notional FROM trades "
        "WHERE action='open' ORDER BY ts").fetchall()
    section("TRADES")
    print(f"  opens: {len(opens)}   closes: {len(closes)}")
    scored = [(ts, s, p, r) for ts, s, p, r in closes if p is not None]
    if scored:
        wins = [p for _, _, p, _ in scored if p > 0]
        losses = [p for _, _, p, _ in scored if p <= 0]
        mean = sum(p for _, _, p, _ in scored) / len(scored)
        print(f"  scored closes: {len(scored)}   "
              f"win rate {len(wins)/len(scored)*100:.0f}%   "
              f"avg win {sum(wins)/len(wins):+.2f}%" if wins else
              f"  scored closes: {len(scored)}   win rate 0%", end="")
        if losses:
            print(f"   avg loss {sum(losses)/len(losses):+.2f}%", end="")
        print(f"   expectancy {mean:+.2f}%/trade (on margin)")
    elif closes:
        print("  closes exist but carry no pnl_pct - they predate the "
              "confidence/pnl journaling feature")

    # ------------------------------------------------------- per symbol
    if scored:
        section("PER SYMBOL")
        by_sym: dict = {}
        for _, s, p, _ in scored:
            by_sym.setdefault(s, []).append(p)
        for s, ps in sorted(by_sym.items(),
                            key=lambda kv: sum(kv[1]), reverse=True):
            w = sum(1 for p in ps if p > 0)
            print(f"  {s:<20} trades {len(ps):>3}   win {w}/{len(ps)}   "
                  f"total {sum(ps):+.2f}%   avg {sum(ps)/len(ps):+.2f}%")

    # ------------------------------------------- confidence calibration
    # Pair each scored close with the most recent prior open on the same
    # symbol, then bucket by the open's confidence.
    section("CONFIDENCE CALIBRATION")
    pairs = []
    for cts, sym, pnl, _ in scored:
        prior = [o for o in opens if o[1] == sym and o[0] <= cts
                 and o[2] is not None]
        if prior:
            pairs.append((prior[-1][2], pnl))
    if pairs:
        buckets = [(0.65, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]
        print("  confidence   trades   win rate   avg pnl")
        for lo, hi in buckets:
            ps = [p for c, p in pairs if lo <= c < hi]
            if not ps:
                continue
            w = sum(1 for p in ps if p > 0)
            print(f"  {lo:.2f}-{min(hi, 1.0):.2f}    {len(ps):>5}     "
                  f"{w/len(ps)*100:>5.0f}%    {sum(ps)/len(ps):+.2f}%")
        print("  (healthy calibration: higher buckets should win more "
              "often; if not, the floor is set wrong or confidence is "
              "noise)")
    else:
        print("  no confidence-tagged open/close pairs yet - data "
              "accumulates from now on")

    # ------------------------------------------------------- rejections
    section("REJECTIONS (why proposals were vetoed)")
    rej = db.execute(
        "SELECT payload FROM events WHERE kind='rejected'").fetchall()
    if rej:
        import json
        counts: dict = {}
        for (payload,) in rej:
            try:
                why = json.loads(payload).get("why", "?")
            except Exception:
                why = "?"
            counts[why] = counts.get(why, 0) + 1
        for why, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4}  {why}")
    else:
        print("  none recorded")

    # -------------------------------------------------------- transfers
    tr = db.execute(
        "SELECT ts, payload FROM events WHERE kind='transfer' "
        "ORDER BY ts").fetchall()
    if tr:
        section("TRANSFERS (deposits/withdrawals, excluded from PnL "
                "benchmarks)")
        import json
        for ts, payload in tr:
            try:
                net = json.loads(payload).get("net_usdt")
                print(f"  {fmt_ts(ts)} UTC   {net:+,.2f} USDT")
            except Exception:
                pass
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
