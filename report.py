#!/usr/bin/env python3
"""Performance report over verified, trade-ID-matched journal records.

Usage:
  python3 report.py
  python3 report.py path/to/journal.db

Percentage returns from unrelated trades are never summed or averaged. The
report uses net realized USDT, original notional, planned risk, actual costs,
and transfer-adjusted equity. Old journal rows without a durable trade ID or
realized-USDT value are shown as excluded rather than guessed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parent / "runtime" / "journal.db"
TRADE_FIELDS = (
    "ts", "symbol", "side", "action", "qty", "price", "notional",
    "leverage", "reason", "confidence", "pnl_pct", "trade_id", "order_id",
    "fee_usd", "funding_usd", "realized_pnl_usd", "risk_usd",
    "fill_status", "slippage_usd",
)


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(1, 60 - len(title)))


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def load_trade_events(db: sqlite3.Connection) -> list[dict]:
    """Load current and legacy journals through one stable row shape."""
    columns = _columns(db, "trades")
    if not columns:
        return []
    selected = [name if name in columns else f"NULL AS {name}"
                for name in TRADE_FIELDS]
    db.row_factory = sqlite3.Row
    rows = db.execute(
        f"SELECT {', '.join(selected)} FROM trades ORDER BY ts"
    ).fetchall()
    return [dict(row) for row in rows]


def match_round_trips(events: list[dict]) -> tuple[list[dict], dict]:
    """Match one open and its final close using only a durable trade ID."""
    opens: dict[str, dict] = {}
    partials: dict[str, list[dict]] = defaultdict(list)
    duplicates: set[str] = set()
    for event in events:
        trade_id = event.get("trade_id")
        if event.get("action") == "partial_close" and trade_id:
            partials[str(trade_id)].append(event)
        if event.get("action") != "open" or not trade_id:
            continue
        if trade_id in opens:
            duplicates.add(str(trade_id))
        else:
            opens[str(trade_id)] = event

    matched = []
    matched_ids = set()
    closed_ids = set()
    unmatchable_closes = 0
    unscored_closes = 0
    for close in events:
        if close.get("action") != "close":
            continue
        trade_id = close.get("trade_id")
        if (not trade_id or str(trade_id) not in opens
                or str(trade_id) in duplicates
                or str(trade_id) in matched_ids):
            unmatchable_closes += 1
            continue
        closed_ids.add(str(trade_id))
        if close.get("realized_pnl_usd") is None:
            unscored_closes += 1
            continue
        opened = opens[str(trade_id)]
        notional = _number(opened.get("notional"))
        risk = _number(opened.get("risk_usd"), _number(close.get("risk_usd")))
        pnl = _number(close.get("realized_pnl_usd"))
        entry_fee = _number(opened.get("fee_usd"))
        exit_fee = _number(close.get("fee_usd"))
        entry_slippage = _number(opened.get("slippage_usd"))
        exit_slippage = _number(close.get("slippage_usd"))
        partial_fees = sum(_number(row.get("fee_usd"))
                           for row in partials.get(str(trade_id), []))
        partial_funding = sum(_number(row.get("funding_usd"))
                              for row in partials.get(str(trade_id), []))
        partial_slippage = sum(_number(row.get("slippage_usd"))
                               for row in partials.get(str(trade_id), []))
        matched.append({
            "trade_id": str(trade_id),
            "symbol": opened.get("symbol"),
            "open_ts": _number(opened.get("ts")),
            "close_ts": _number(close.get("ts")),
            "confidence": opened.get("confidence"),
            "notional_usd": notional,
            "risk_usd": risk,
            "net_pnl_usd": pnl,
            "return_on_notional_pct": pnl / notional * 100 if notional else None,
            "r_multiple": pnl / risk if risk else None,
            "fees_usd": entry_fee + partial_fees + exit_fee,
            "funding_usd": partial_funding + _number(close.get("funding_usd")),
            "slippage_usd": (entry_slippage + partial_slippage
                             + exit_slippage),
            "open_fill_status": opened.get("fill_status"),
            "close_fill_status": close.get("fill_status"),
        })
        matched_ids.add(str(trade_id))

    open_ids = set(opens) - duplicates
    diagnostics = {
        "opens": sum(1 for event in events if event.get("action") == "open"),
        "closes": sum(1 for event in events if event.get("action") == "close"),
        "partial_closes": sum(
            1 for event in events if event.get("action") == "partial_close"),
        "unmatched_opens": len(open_ids - closed_ids),
        "unmatchable_closes": unmatchable_closes,
        "unscored_closes": unscored_closes,
        "duplicate_trade_ids": len(duplicates),
    }
    return matched, diagnostics


def load_transfers(db: sqlite3.Connection) -> list[tuple[float, float]]:
    if not _columns(db, "events"):
        return []
    rows = db.execute(
        "SELECT ts, payload FROM events WHERE kind='transfer' ORDER BY ts"
    ).fetchall()
    transfers = []
    for ts, payload in rows:
        try:
            net = float(json.loads(payload).get("net_usdt"))
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            continue
        transfers.append((float(ts), net))
    return transfers


def adjusted_equity_curve(
        equity: list[tuple[float, float]],
        transfers: list[tuple[float, float]]) -> tuple[list[tuple[float, float]], float]:
    """Remove external cash flows that occurred after the first snapshot."""
    if not equity:
        return [], 0.0
    first_ts = equity[0][0]
    eligible = [(ts, net) for ts, net in transfers if ts > first_ts]
    adjusted = []
    cumulative = 0.0
    cursor = 0
    for ts, value in equity:
        while cursor < len(eligible) and eligible[cursor][0] <= ts:
            cumulative += eligible[cursor][1]
            cursor += 1
        adjusted.append((ts, value - cumulative))
    return adjusted, cumulative


def curve_stats(curve: list[tuple[float, float]]) -> dict | None:
    if not curve:
        return None
    first = float(curve[0][1])
    last = float(curve[-1][1])
    peak = first
    max_drawdown = 0.0
    for _, value in curve:
        peak = max(peak, float(value))
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - float(value)) / peak * 100)
    return {
        "first": first,
        "last": last,
        "peak": peak,
        "return_pct": (last - first) / first * 100 if first else 0.0,
        "max_drawdown_pct": max_drawdown,
    }


def print_equity(db: sqlite3.Connection, transfers: list[tuple[float, float]]) -> None:
    section("EQUITY (TRANSFER ADJUSTED)")
    if not _columns(db, "equity"):
        print("  no equity table yet")
        return
    equity = [(float(ts), float(value)) for ts, value in db.execute(
        "SELECT ts, equity FROM equity ORDER BY ts").fetchall()]
    if not equity:
        print("  no equity snapshots yet")
        return
    adjusted, net_flow = adjusted_equity_curve(equity, transfers)
    raw = curve_stats(equity)
    clean = curve_stats(adjusted)
    print(f"  {fmt_ts(equity[0][0])} -> {fmt_ts(equity[-1][0])} UTC "
          f"({len(equity)} snapshots)")
    print(f"  raw account:      {raw['first']:,.2f} -> {raw['last']:,.2f} USDT "
          f"({raw['return_pct']:+.2f}%)")
    print(f"  external net flow:{net_flow:>+13,.2f} USDT")
    print(f"  adjusted equity:  {clean['first']:,.2f} -> {clean['last']:,.2f} USDT "
          f"({clean['return_pct']:+.2f}%)")
    print(f"  adjusted peak {clean['peak']:,.2f}   "
          f"max drawdown {clean['max_drawdown_pct']:.2f}%")


def print_trades(trades: list[dict], diagnostics: dict) -> None:
    section("MATCHED ROUND TRIPS")
    print(f"  opens {diagnostics['opens']}   closes {diagnostics['closes']}   "
          f"partial closes {diagnostics['partial_closes']}   "
          f"verified matches {len(trades)}")
    excluded = (diagnostics["unmatched_opens"]
                + diagnostics["unmatchable_closes"]
                + diagnostics["unscored_closes"]
                + diagnostics["duplicate_trade_ids"])
    if excluded:
        print("  excluded: "
              f"{diagnostics['unmatched_opens']} still-open/unmatched entries, "
              f"{diagnostics['unmatchable_closes']} unmatchable closes, "
              f"{diagnostics['unscored_closes']} closes without net USDT PnL, "
              f"{diagnostics['duplicate_trade_ids']} duplicate trade IDs")
    if not trades:
        print("  no verified trade-ID-matched round trips yet")
        return

    wins = [trade for trade in trades if trade["net_pnl_usd"] > 0]
    losses = [trade for trade in trades if trade["net_pnl_usd"] <= 0]
    pnl = sum(trade["net_pnl_usd"] for trade in trades)
    notionals = sum(trade["notional_usd"] for trade in trades)
    risk_trades = [trade for trade in trades if trade["risk_usd"] > 0]
    risk = sum(trade["risk_usd"] for trade in risk_trades)
    gross_profit = sum(trade["net_pnl_usd"] for trade in wins)
    gross_loss = abs(sum(trade["net_pnl_usd"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else None
    print(f"  net realized PnL {pnl:+,.2f} USDT   "
          f"win rate {len(wins) / len(trades) * 100:.1f}%   "
          f"expectancy {pnl / len(trades):+,.2f} USDT/trade")
    factor_text = (f"{profit_factor:.2f}" if profit_factor is not None
                   else "n/a (no losing trades)")
    print(f"  notional-weighted return "
          f"{(pnl / notionals * 100 if notionals else 0):+.3f}%   "
          f"profit factor {factor_text}")
    if risk_trades:
        risk_pnl = sum(trade["net_pnl_usd"] for trade in risk_trades)
        print(f"  aggregate risk return {risk_pnl / risk:+.2f}R across "
              f"{len(risk_trades)} risk-tagged trades   "
              f"average {sum(t['r_multiple'] for t in risk_trades) / len(risk_trades):+.2f}R/trade")
    print(f"  recorded costs: fees {sum(t['fees_usd'] for t in trades):,.2f}   "
          f"funding {sum(t['funding_usd'] for t in trades):+,.2f}   "
          f"execution slippage {sum(t['slippage_usd'] for t in trades):,.2f} USDT")


def print_per_symbol(trades: list[dict]) -> None:
    if not trades:
        return
    section("PER SYMBOL (NET USDT)")
    grouped = defaultdict(list)
    for trade in trades:
        grouped[trade["symbol"]].append(trade)
    for symbol, rows in sorted(
            grouped.items(), key=lambda item: sum(
                row["net_pnl_usd"] for row in item[1]), reverse=True):
        pnl = sum(row["net_pnl_usd"] for row in rows)
        notional = sum(row["notional_usd"] for row in rows)
        wins = sum(row["net_pnl_usd"] > 0 for row in rows)
        risk_rows = [row for row in rows if row["risk_usd"] > 0]
        r_text = (f"{sum(row['r_multiple'] for row in risk_rows):+.2f}R"
                  if risk_rows else "n/a")
        print(f"  {str(symbol):<20} trades {len(rows):>3}   win {wins}/{len(rows)}   "
              f"PnL {pnl:+,.2f}   notional return "
              f"{(pnl / notional * 100 if notional else 0):+.3f}%   {r_text}")


def print_calibration(trades: list[dict]) -> None:
    section("CONFIDENCE CALIBRATION")
    scored = [trade for trade in trades if trade["confidence"] is not None]
    if not scored:
        print("  no matched confidence-tagged round trips yet")
        return
    print("  confidence   trades   win rate   net pnl   avg R")
    for lo, hi in ((0.0, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)):
        rows = [trade for trade in scored
                if lo <= _number(trade["confidence"]) < hi]
        if not rows:
            continue
        wins = sum(row["net_pnl_usd"] > 0 for row in rows)
        risk_rows = [row for row in rows if row["r_multiple"] is not None]
        avg_r = (sum(row["r_multiple"] for row in risk_rows) / len(risk_rows)
                 if risk_rows else None)
        avg_text = f"{avg_r:+.2f}" if avg_r is not None else "n/a"
        print(f"  {lo:.2f}-{min(hi, 1.0):.2f}    {len(rows):>5}     "
              f"{wins / len(rows) * 100:>5.1f}%   "
              f"{sum(row['net_pnl_usd'] for row in rows):>+9.2f}   {avg_text}")


def print_rejections(db: sqlite3.Connection) -> None:
    section("REJECTIONS (WHY PROPOSALS WERE VETOED)")
    if not _columns(db, "events"):
        print("  none recorded")
        return
    rows = db.execute(
        "SELECT payload FROM events WHERE kind='rejected'").fetchall()
    counts = defaultdict(int)
    for (payload,) in rows:
        try:
            reason = json.loads(payload).get("why", "?")
        except (TypeError, json.JSONDecodeError, AttributeError):
            reason = "?"
        counts[str(reason)] += 1
    if not counts:
        print("  none recorded")
        return
    for reason, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {count:>4}  {reason}")


def print_transfers(transfers: list[tuple[float, float]]) -> None:
    if not transfers:
        return
    section("TRANSFERS")
    for ts, net in transfers:
        print(f"  {fmt_ts(ts)} UTC   {net:+,.2f} USDT")


def main(path: Path | None = None) -> int:
    db_path = path or (Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB)
    if not db_path.exists():
        print(f"No journal at {db_path} - run the agent first.")
        return 1
    db = sqlite3.connect(db_path)
    try:
        transfers = load_transfers(db)
        events = load_trade_events(db)
        trades, diagnostics = match_round_trips(events)
        print_equity(db, transfers)
        print_trades(trades, diagnostics)
        print_per_symbol(trades)
        print_calibration(trades)
        print_rejections(db)
        print_transfers(transfers)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
