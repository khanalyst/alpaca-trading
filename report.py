#!/usr/bin/env python3
"""Small, dependency-free summaries for a mode-scoped Alpaca journal.

The runtime journal is deliberately an append-only execution ledger.  This
module only reads it and emits a compact JSON or CSV summary; it does not
attempt to promote strategies, score experiments, or contact a broker.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "runtime" / "paper" / "journal.db"

def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def load_trade_events(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load journal trade rows through a stable, best-effort shape."""
    columns = _columns(db, "trades")
    if not columns:
        return []
    fields = (
        "ts", "symbol", "side", "action", "qty", "price", "notional",
        "trade_id", "order_id", "parent_trade_id", "setup_id", "setup_key",
        "requested_qty", "planned_qty", "cumulative_filled_qty",
        "fill_fraction", "filled_fraction", "position_closed",
        "realized_pnl_usd", "gross_pnl", "fees", "fee_usd", "funding_usd",
        "slippage", "slippage_usd", "net_pnl", "pnl_pct", "risk_usd",
        "pnl_semantics", "pnl_provenance", "cost_provenance",
        "fill_status", "strategy_id", "strategy_version", "setup_type",
        "variant_id", "runtime_mode",
    )
    selected = [name if name in columns else f"NULL AS {name}" for name in fields]
    db.row_factory = sqlite3.Row
    rows = db.execute(
        f"SELECT {', '.join(selected)} FROM trades ORDER BY ts, rowid"
    ).fetchall()
    return [dict(row) for row in rows]


def _equity_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _columns(db, "equity")
    if not columns:
        return []
    selected = [name if name in columns else f"NULL AS {name}"
                for name in ("ts", "equity", "state")]
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute(
        f"SELECT {', '.join(selected)} FROM equity ORDER BY ts, rowid").fetchall()]


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


_CLOSE_ACTIONS = frozenset({"close", "partial_close", "exit", "sell_to_close"})


def _close_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in events if str(row.get("action") or "").lower()
            in _CLOSE_ACTIONS]


def _parent_key(row: dict[str, Any], index: int) -> str:
    """Return the durable parent identity for one close fill.

    ``setup_id`` is the position/trade identity on current rows and survives
    partial closes.  ``trade_id`` is the next best key for deployment-era rows
    that predate setup stamping.  A row-local fallback avoids silently merging
    unrelated legacy closes when neither identity is available.
    """
    for field in ("parent_trade_id", "setup_id", "setup_key"):
        value = row.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    # Current close replay ids are ``close:<entry-order>:<close-order>`` and
    # append ``:increment:<cumulative-fill>`` when a terminal snapshot grows.
    # The close order changes on a cancel/retry, but the entry order remains
    # the durable parent identity.  Normalize that legacy shape so partial
    # close fills from retries aggregate with their one entry.
    trade_id = str(row.get("trade_id") or "")
    if trade_id.startswith("close:"):
        parts = trade_id.split(":")
        if len(parts) > 1 and parts[1]:
            return f"legacy-entry-order:{parts[1]}"
    if _is_open(row):
        order_id = row.get("order_id")
        if order_id not in (None, ""):
            return f"legacy-entry-order:{order_id}"
    if trade_id:
        return f"trade:{trade_id}"
    order_id = row.get("order_id")
    if order_id not in (None, ""):
        return f"order:{order_id}"
    return f"legacy-row:{index}"


def _is_close(row: dict[str, Any]) -> bool:
    return str(row.get("action") or "").strip().lower() in _CLOSE_ACTIONS


def _is_open(row: dict[str, Any]) -> bool:
    return str(row.get("action") or "").strip().lower() in {
        "open", "buy_to_open", "sell_to_open"
    }


def _quantity(row: dict[str, Any], *fields: str) -> float | None:
    for field in fields:
        value = _finite(row.get(field))
        if value is not None and value > 0:
            return value
    return None


def _explicit_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "closed"}


def _risk_for_parent(rows: list[dict[str, Any]],
                     close_rows: list[dict[str, Any]]) -> float | None:
    """Use the whole-entry denominator when fill telemetry varies by retry."""
    opened = [_finite(row.get("risk_usd")) for row in rows if _is_open(row)]
    opened = [value for value in opened if value is not None and value > 0]
    if opened:
        # Entry fills are incremental in the runtime journal, so their risks
        # add to the delivered risk for the parent position.
        return sum(opened)
    closes = [_finite(row.get("risk_usd")) for row in close_rows]
    closes = [value for value in closes if value is not None and value > 0]
    # Close retries may carry the residual risk.  The largest observed value
    # is the conservative whole-entry denominator.
    return max(closes) if closes else None


def _parent_is_complete(rows: list[dict[str, Any]], close_rows: list[dict[str, Any]]) -> bool:
    """Require evidence that a grouped position is flat before counting it.

    Current runtime rows carry the same setup id on entry and every close fill,
    so cumulative quantities provide a durable lifecycle boundary.  Explicit
    ``position_closed`` remains authoritative when present.  Legacy close-only
    rows have no open quantity to reconcile and retain their historical
    one-row-is-one-completed-trade interpretation.
    """
    if any(_explicit_true(row.get("position_closed")) for row in close_rows):
        return True
    opened = sum(_quantity(row, "qty", "cumulative_filled_qty") or 0.0
                 for row in rows if _is_open(row))
    closed = sum(_quantity(row, "qty", "cumulative_filled_qty") or 0.0
                 for row in close_rows)
    if opened > 0:
        return closed + max(1e-9, opened * 1e-9) >= opened
    requested = [value for row in close_rows
                 for value in [_quantity(row, "requested_qty", "planned_qty")]
                 if value is not None]
    if requested:
        # A close fill explicitly marked below its requested quantity is an
        # incomplete lifecycle unless later fills cover that request.
        return closed + max(1e-9, max(requested) * 1e-9) >= max(requested)
    fractions = [_finite(row.get("filled_fraction", row.get("fill_fraction")))
                 for row in close_rows]
    if fractions and all(value is not None for value in fractions):
        return any(value >= 1.0 - 1e-9 for value in fractions)
    return not any(str(row.get("action") or "").lower() == "partial_close"
                   for row in close_rows)


def _pnl_attribution(row: dict[str, Any]) -> dict[str, Any]:
    """Resolve one row's gross/fee/net values without inventing net P&L."""
    gross = _finite(row.get("gross_pnl"))
    if gross is None:
        # This is a historical fill-only field.  It remains usable as gross,
        # but is never relabeled as net when no cost information exists.
        gross = _finite(row.get("realized_pnl_usd"))
    fees = _finite(row.get("fees"))
    if fees is None:
        fees = _finite(row.get("fee_usd"))
    funding = _finite(row.get("funding_usd"))
    slippage = _finite(row.get("slippage"))
    if slippage is None:
        slippage = _finite(row.get("slippage_usd"))
    net = _finite(row.get("net_pnl"))
    semantics = str(row.get("pnl_semantics") or "").strip().lower()
    fill_price_net = semantics in {
        "broker_fill_pnl_minus_fees", "fill_pnl_minus_fees",
        "gross_pnl_minus_fees", "gross_minus_fees",
    }
    net_basis = "explicit_net_pnl" if net is not None else None
    if net is None and gross is not None and fees is not None and fill_price_net:
        # Match the execution writer's semantics: broker fill prices already
        # contain spread/slippage, so only known fees are subtracted here.
        net = gross - fees
        net_basis = "derived_gross_minus_known_fees"
    if gross is None and net is not None and fees is not None and fill_price_net:
        gross = net + fees
    raw_cost = row.get("cost_provenance")
    if raw_cost not in (None, ""):
        cost_provenance = raw_cost
    elif fees is not None:
        cost_provenance = "fee_field"
    else:
        cost_provenance = None
    return {"gross": gross, "fees": fees, "funding": funding,
            "slippage": slippage, "net": net, "net_basis": net_basis,
            "cost_provenance": cost_provenance,
            "risk": _finite(row.get("risk_usd")),
            "pnl_pct": _finite(row.get("pnl_pct"))}


def closed_parent_trades(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate close fills into completed parent trades.

    The returned values are intentionally explicit about missing economics.
    In particular, ``net`` is ``None`` when any fill in a parent has unknown
    costs; callers can still report its gross result with provenance.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    close_groups: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(events):
        key = _parent_key(row, index)
        if _is_open(row) or _is_close(row):
            groups.setdefault(key, []).append(row)
        if _is_close(row):
            close_groups.setdefault(key, []).append(row)
    parents: list[dict[str, Any]] = []
    for key, close_rows in close_groups.items():
        rows = groups.get(key, close_rows)
        if not _parent_is_complete(rows, close_rows):
            continue
        details = [_pnl_attribution(row) for row in close_rows]

        def total(name: str) -> float | None:
            values = [item[name] for item in details]
            return sum(values) if values and all(value is not None for value in values) else None

        risk = next((item["risk"] for item in details
                     if item["risk"] is not None and item["risk"] > 0), None)
        risk = _risk_for_parent(rows, close_rows) or risk
        net = total("net")
        gross = total("gross")
        fees = total("fees")
        first = close_rows[0]
        bases = {item["net_basis"] for item in details}
        if bases == {"explicit_net_pnl"}:
            net_basis = "explicit_net_pnl"
        elif bases == {"derived_gross_minus_known_fees"}:
            net_basis = "derived_gross_minus_known_fees"
        elif net is not None and bases:
            net_basis = "mixed_net_basis"
        else:
            net_basis = None
        parent = {
            "parent_trade_id": key,
            "symbol": first.get("symbol"),
            "strategy_id": first.get("strategy_id"),
            "variant_id": first.get("variant_id"),
            "close_fills": len(close_rows),
            "closed": True,
            "gross": gross,
            "fees": fees,
            "funding": total("funding"),
            "slippage": total("slippage"),
            "net": net,
            "risk": risk,
            "r_multiple": (net / risk if net is not None and risk else None),
            "gross_r_multiple": (gross / risk if gross is not None and risk else None),
            "pnl_pct": total("pnl_pct"),
            "net_basis": net_basis,
            "cost_provenance": sorted({str(item["cost_provenance"])
                                        for item in details
                                        if item["cost_provenance"] not in (None, "")}),
            "last_ts": max(((row.get("ts") if row.get("ts") is not None
                              else row.get("when")) for row in rows
                             if row.get("ts") is not None or
                             row.get("when") is not None), default=None),
        }
        parents.append(parent)
    return parents


def _total(parents: list[dict[str, Any]], field: str) -> float | None:
    values = [parent.get(field) for parent in parents]
    return sum(values) if values and all(value is not None for value in values) else None


def _summary(events: list[dict[str, Any]], equity: list[dict[str, Any]]) -> dict[str, Any]:
    closes = _close_rows(events)
    parents = closed_parent_trades(events)
    gross = _total(parents, "gross")
    fees = _total(parents, "fees")
    net = _total(parents, "net")
    r_values = [parent["r_multiple"] for parent in parents
                if parent.get("r_multiple") is not None]
    gross_r_values = [parent["gross_r_multiple"] for parent in parents
                      if parent.get("gross_r_multiple") is not None]
    pnl_pct = [parent["pnl_pct"] for parent in parents
               if parent.get("pnl_pct") is not None]
    wins = sum(parent["net"] > 0 for parent in parents
               if parent.get("net") is not None)
    gross_wins = sum(parent["gross"] > 0 for parent in parents
                     if parent.get("gross") is not None)
    latest_equity = _finite(equity[-1].get("equity")) if equity else None
    first_equity = _finite(equity[0].get("equity")) if equity else None
    modes = {str(row.get("runtime_mode") or "").lower() for row in events
             if str(row.get("runtime_mode") or "").lower() in {"paper", "live"}}
    scope = (f"alpaca-{next(iter(modes))}" if len(modes) == 1 else
             "alpaca-mixed" if len(modes) > 1 else "alpaca-paper")
    return {
        "schema": 1,
        "scope": scope,
        "events": len(events),
        "closed_trades": len(parents),
        "close_fills": len(closes),
        # ``realized_pnl_usd`` is retained for compatibility.  Its basis is
        # explicit below; modern consumers must use ``net_pnl_usd``.
        "realized_pnl_usd": (net if net is not None else gross
                             if gross is not None else 0.0),
        "realized_pnl_basis": ("net_pnl" if net is not None else
                                "gross_legacy_unknown_cost" if gross is not None
                                else "no_closed_trades"),
        "gross_pnl_usd": gross if gross is not None else None,
        "fees_usd": fees,
        "net_pnl_usd": net if net is not None else (0.0 if not parents else None),
        "net_pnl_available": bool(net is not None),
        "cost_provenance": sorted({value for parent in parents
                                    for value in parent.get("cost_provenance", [])}),
        "average_pnl_usd": (net / len(parents) if net is not None and parents else None),
        "average_gross_pnl_usd": (gross / len(parents)
                                   if gross is not None and parents else None),
        "win_rate": (wins / len(parents) if net is not None and parents else None),
        "gross_win_rate": (gross_wins / len(parents)
                           if gross is not None and parents else None),
        "total_r": sum(r_values) if r_values and len(r_values) == len(parents) else None,
        "average_r": (sum(r_values) / len(r_values)
                      if r_values and len(r_values) == len(parents) else None),
        "gross_total_r": (sum(gross_r_values)
                          if gross_r_values and len(gross_r_values) == len(parents)
                          else None),
        "average_pnl_pct": (sum(pnl_pct) / len(pnl_pct) if pnl_pct else None),
        "latest_equity_usd": latest_equity,
        "equity_change_usd": (
            latest_equity - first_equity
            if latest_equity is not None and first_equity is not None else None),
        "latest_event_ts": events[-1].get("ts") if events else None,
    }


def json_report(db: sqlite3.Connection) -> dict[str, Any]:
    """Return a compact machine-readable journal summary."""
    events = load_trade_events(db)
    return _safe(_summary(events, _equity_rows(db)))  # type: ignore[return-value]


def csv_report(db: sqlite3.Connection) -> str:
    """Return the summary as one CSV record, suitable for cron artifacts."""
    summary = json_report(db)
    fields = list(summary)
    from io import StringIO
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerow(summary)
    return output.getvalue()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", nargs="?", type=Path, default=DEFAULT_DB)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit JSON (default)")
    output.add_argument("--csv", action="store_true", help="emit one CSV summary row")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.journal.is_file():
        print(f"journal not found: {args.journal}", file=sys.stderr)
        return 1
    try:
        with closing(sqlite3.connect(args.journal)) as db:
            if args.csv:
                print(csv_report(db), end="")
            else:
                print(json.dumps(json_report(db), sort_keys=True,
                                  allow_nan=False))
    except (OSError, sqlite3.Error) as exc:
        print(f"journal read failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
