"""Read-only execution calibration from the runtime's append-only journal.

Calibration deliberately uses the plan/reference fields written by the
runtime. A partial fill's actual notional is not a plan price, and rows from
an initialized journal with missing reference fields are therefore
unreferenced rather than scored against a reconstructed guess.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
import sqlite3
import sys
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from .costs import CostModel

SCHEMA = 2
MIN_FILLS = 20
# Preregistered execution authorization policy. A terminal order that got
# less than this fraction while its remainder was canceled is material
# underfill evidence. In-flight orders are excluded from this calculation.
MATERIAL_FILL_FRACTION = 0.80
MAX_PARTIAL_CANCEL_RATE = 0.20
_TERMINAL_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "replaced",
    "stopped", "suspended", "failed", "not_found", "done", "closed",
    "done_for_day",
}
_CANCEL_STATUSES = {
    "canceled", "cancelled", "expired", "rejected", "replaced", "stopped",
    "suspended", "failed", "not_found",
}
_EVIDENCE_COLUMNS = {
    # ``variant_id`` predates this calibration evidence contract in some
    # deployed trades tables, so it is intentionally not a migration marker.
    "execution_profile", "vehicle", "reference_price",
    "entry_reference", "exit_reference", "market_price", "mid_price",
    "requested_qty", "planned_qty", "cumulative_filled_qty", "fill_fraction",
    "filled_fraction",
}


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _latest_orders(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Return the latest append-only order snapshot for every broker id."""
    columns = _columns(db, "orders")
    if not columns or "order_id" not in columns:
        return {}
    selected = [name for name in (
        "order_id", "ts", "qty", "status", "filled_qty", "filled_avg_price",
        "execution_profile", "vehicle", "variant_id", "reference_price",
        "entry_reference", "exit_reference", "market_price", "mid_price",
        "requested_qty", "planned_qty", "cumulative_filled_qty",
        "fill_fraction", "filled_fraction",
    ) if name in columns]
    try:
        order_sort = "ts, _rowid" if "ts" in columns else "_rowid"
        rows = db.execute(
            "SELECT rowid AS _rowid, " + ",".join(selected) +
            " FROM orders ORDER BY " + order_sort).fetchall()
    except sqlite3.Error:
        return {}
    names = ["_rowid", *selected]
    latest: dict[str, dict[str, Any]] = {}
    for values in rows:
        row = dict(zip(names, values))
        key = row.get("order_id")
        if key is None:
            continue
        latest[str(key)] = row
    return latest


def _load_rows(db: sqlite3.Connection, actions: set[str]) -> list[dict[str, Any]]:
    columns = _columns(db, "trades")
    required = {"ts", "price", "qty", "action"}
    if not required <= columns:
        return []
    selected = [name for name in (
        "ts", "symbol", "side", "action", "qty", "price", "notional",
        "order_id", "runtime_mode", "variant_id", "execution_profile", "vehicle",
        "reference_price", "entry_reference", "exit_reference", "market_price",
        "mid_price", "drift_bps", "requested_qty", "planned_qty",
        "cumulative_filled_qty", "fill_fraction", "filled_fraction",
    ) if name in columns]
    try:
        raw = db.execute(
            "SELECT rowid AS _rowid, " + ",".join(selected) +
            " FROM trades ORDER BY ts, _rowid").fetchall()
    except sqlite3.Error:
        return []
    names = ["_rowid", *selected]
    orders = _latest_orders(db)
    # A custom pre-migration table used by old callers has no new evidence
    # columns. Keep its historical notional/qty compatibility seam, while a
    # migrated table with nullable evidence fields fails closed instead.
    legacy_schema = not bool(_EVIDENCE_COLUMNS & columns)
    result: list[dict[str, Any]] = []
    for values in raw:
        row = dict(zip(names, values))
        action = str(row.get("action") or "").lower()
        if action not in actions:
            continue
        order = orders.get(str(row.get("order_id"))) if row.get("order_id") is not None else None
        if order:
            for name in (
                "status", "qty", "filled_qty", "filled_avg_price", "requested_qty", "planned_qty",
                "execution_profile", "vehicle", "variant_id", "reference_price",
                "entry_reference", "exit_reference", "market_price", "mid_price",
                "cumulative_filled_qty", "fill_fraction", "filled_fraction",
            ):
                row[f"order_{name}"] = order.get(name)
        row["order_status"] = str((order or {}).get("status") or "").lower()
        row["_legacy_schema"] = legacy_schema
        # Explicit order/trade fields take precedence over old aliases.
        row["requested_qty"] = (row.get("requested_qty")
                                 if row.get("requested_qty") is not None
                                 else (row.get("order_requested_qty")
                                       if row.get("order_requested_qty") is not None
                                       else row.get("order_qty")))
        row["planned_qty"] = (row.get("planned_qty")
                               if row.get("planned_qty") is not None
                               else (row.get("order_planned_qty")
                                     if row.get("order_planned_qty") is not None
                                     else row.get("requested_qty")))
        for name in ("execution_profile", "vehicle", "variant_id", "reference_price",
                     "entry_reference", "exit_reference", "market_price", "mid_price"):
            if row.get(name) is None and row.get(f"order_{name}") is not None:
                row[name] = row[f"order_{name}"]
        result.append(row)
    return result


def load_entry_fills(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load entry rows, preserving one row per durable incremental fill."""
    return _load_rows(db, {"open", "entry"})


def load_execution_fills(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load entry and exit rows, including order terminal evidence."""
    return _load_rows(db, {"open", "entry", "close", "exit", "sell_to_close"})


def _reference(row: Mapping[str, Any]) -> float | None:
    kind = str(row.get("action") or "open").lower()
    names = (("exit_reference", "reference_price", "entry_reference")
             if kind in {"close", "exit", "sell_to_close"}
             else ("entry_reference", "reference_price", "exit_reference"))
    for name in names:
        value = _finite(row.get(name))
        if value is not None and value > 0:
            return value
    # Compatibility is limited to genuinely pre-migration tables. Once the
    # nullable evidence columns exist, a missing explicit reference is an
    # insufficient row even if actual notional and requested qty are present.
    if row.get("_legacy_schema"):
        notional = _finite(row.get("notional"))
        planned = _finite(row.get("planned_qty"))
        if notional is not None and notional > 0 and planned is not None and planned > 0:
            return notional / planned
    return None


def _measure(row: Mapping[str, Any]) -> dict[str, Any] | None:
    fill = _finite(row.get("price"))
    qty = _finite(row.get("qty"))
    reference = _reference(row)
    side = str(row.get("side") or "").lower()
    if fill is None or fill <= 0 or qty is None or qty <= 0 or reference is None:
        return None
    if side not in {"buy", "sell"}:
        return None
    profile = str(row.get("execution_profile") or "").lower()
    vehicle = str(row.get("vehicle") or
                  ("option" if profile in {"option", "options"} else "equity"))
    kind = str(row.get("action") or "open").lower()
    adverse = (fill - reference) if side == "buy" else (reference - fill)
    drift = _finite(row.get("drift_bps"))
    if drift is None:
        market = _finite(row.get("market_price")) or _finite(row.get("mid_price"))
        if market is not None and market > 0:
            drift = ((market - reference) if side == "buy" else
                     (reference - market)) / reference * 10_000.0
    adverse_bps = adverse / reference * 10_000.0
    return {
        "symbol": str(row.get("symbol") or ""), "ts": _finite(row.get("ts")),
        "side": side, "qty": qty, "reference_price": reference,
        "fill_price": fill, "adverse_bps": adverse_bps, "drift_bps": drift,
        "execution_slippage_bps": (adverse_bps - drift if drift is not None else None),
        "variant_id": row.get("variant_id"),
        "runtime_mode": row.get("runtime_mode") or "unknown", "vehicle": vehicle,
        "execution_profile": profile or ("options" if vehicle == "option" else "shares"),
        "kind": "exit" if kind in {"close", "exit", "sell_to_close"} else "entry",
        "order_id": row.get("order_id"),
        "order_status": str(row.get("order_status") or row.get("status") or "").lower(),
        "requested_qty": (_finite(row.get("requested_qty")) or
                          _finite(row.get("planned_qty"))),
        "filled_qty_evidence": (_finite(row.get("order_filled_qty")) or
                                 _finite(row.get("cumulative_filled_qty")) or
                                 _finite(row.get("filled_qty"))),
    }


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse incremental rows to one independently scored order/kind."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for index, row in enumerate(rows):
        key = (str(row.get("order_id") or f"row:{index}"),
               "exit" if str(row.get("action") or "").lower() in
               {"close", "exit", "sell_to_close"} else "entry")
        groups.setdefault(key, []).append(row)
    measured: list[dict[str, Any]] = []
    for (order_id, kind), items in groups.items():
        measurements = [_measure(item) for item in items]
        valid = [item for item in measurements if item is not None]
        if not valid or len(valid) != len(items):
            continue
        quantity = sum(item["qty"] for item in valid)
        if quantity <= 0:
            continue
        weighted_price = sum(item["fill_price"] * item["qty"] for item in valid) / quantity
        weighted_adverse = sum(item["adverse_bps"] * item["qty"] for item in valid) / quantity
        first = dict(valid[0])
        first.update({
            "order_id": order_id, "kind": kind, "qty": quantity,
            "fill_price": weighted_price, "adverse_bps": weighted_adverse,
            "increment_count": len(items),
            "drift_bps": (sum(item["drift_bps"] * item["qty"] for item in valid
                               if item.get("drift_bps") is not None) /
                          sum(item["qty"] for item in valid if item.get("drift_bps") is not None)
                          if any(item.get("drift_bps") is not None for item in valid) else None),
            "requested_qty": next((item["requested_qty"] for item in valid
                                   if item.get("requested_qty") is not None), None),
            "filled_qty_evidence": max(
                [item["filled_qty_evidence"] for item in valid
                 if item.get("filled_qty_evidence") is not None] or [quantity]),
        })
        first["execution_slippage_bps"] = (
            first["adverse_bps"] - first["drift_bps"]
            if first.get("drift_bps") is not None else None)
        measured.append(first)
    return measured, len(groups)


def _empty_report(model: CostModel, journal_fills: int, unique_orders: int,
                  unreferenced: int, order_metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "model": model.as_dict(), "journal_fills": journal_fills,
        "unique_orders": unique_orders, "referenced_fills": 0,
        "unreferenced_fills": unreferenced, "expected_entry_cost_bps": model.entry_cost_bps,
        "verdict": "insufficient_data", "observed_mean_bps": None,
        "observed_median_bps": None, "bias_bps": None, "within_model_rate": None,
        "over_runtime_cap": 0, "worst_bps": None, "by_symbol": {}, "strata": {},
        "by_kind": {"entry": {"verdict": "insufficient_data"},
                    "exit": {"verdict": "insufficient_data"}},
        **order_metrics,
    }


def _order_metrics(measured: Sequence[Mapping[str, Any]], raw_count: int,
                   unique_orders: int) -> dict[str, Any]:
    terminal = [item for item in measured
                if str(item.get("order_status") or "") in _TERMINAL_STATUSES]
    inflight = [item for item in measured
                if str(item.get("order_status") or "") not in _TERMINAL_STATUSES]
    complete = []
    partial_cancel = []
    underfilled = []
    requested_total = 0.0
    filled_total = 0.0
    for item in measured:
        requested = _finite(item.get("requested_qty"))
        filled = _finite(item.get("filled_qty_evidence")) or _finite(item.get("qty"))
        fraction = filled / requested if requested and requested > 0 else None
        item_status = str(item.get("order_status") or "")
        if requested and requested > 0:
            requested_total += requested
            filled_total += max(0.0, filled or 0.0)
        if item_status in _TERMINAL_STATUSES and fraction is not None:
            if item_status == "filled" and fraction >= 1.0 - 1e-9:
                complete.append(item)
            if item_status in _CANCEL_STATUSES and 0 < fraction < 1.0 - 1e-9:
                partial_cancel.append(item)
            if 0 < fraction < MATERIAL_FILL_FRACTION:
                underfilled.append(item)
    terminal_count = len(terminal)
    partial_rate = len(partial_cancel) / terminal_count if terminal_count else None
    completion_rate = len(complete) / terminal_count if terminal_count else None
    return {
        "requested_qty": requested_total or None,
        "cumulative_filled_qty": filled_total or None,
        "fill_fraction": (filled_total / requested_total if requested_total > 0 else None),
        "terminal_orders": terminal_count, "inflight_orders": len(inflight),
        "completed_orders": len(complete), "partial_cancel_orders": len(partial_cancel),
        "completion_rate": completion_rate, "partial_cancel_rate": partial_rate,
        "material_underfill_orders": len(underfilled),
        "material_fill_fraction_threshold": MATERIAL_FILL_FRACTION,
        "max_partial_cancel_rate": MAX_PARTIAL_CANCEL_RATE,
        "terminal_status_evidence": bool(terminal), "raw_order_rows": raw_count,
        "partial_execution_veto": bool(underfilled) or (
            partial_rate is not None and partial_rate > MAX_PARTIAL_CANCEL_RATE),
    }


def calibrate(rows: Sequence[Mapping[str, Any]], costs: CostModel | None = None, *,
              runtime_mode: str | None = None, vehicle: str | None = None,
              execution_profile: str | None = None) -> dict[str, Any]:
    model = costs or CostModel()
    selected = list(rows)
    if runtime_mode is not None:
        selected = [row for row in selected
                    if str(row.get("runtime_mode") or "unknown") == str(runtime_mode)]
    if vehicle is not None:
        selected = [row for row in selected if str(row.get("vehicle") or "") == str(vehicle)]
    if execution_profile is not None:
        selected = [row for row in selected
                    if str(row.get("execution_profile") or "") == str(execution_profile)]
    measured, unique_orders = _aggregate(selected)
    metrics = _order_metrics(measured, len(selected), unique_orders)
    report = _empty_report(model, len(selected), unique_orders,
                           max(0, unique_orders - len(measured)), metrics)
    if not measured:
        report.update({"authorization_verdict": "insufficient_data",
                       # Calibration is advisory for offline research, but
                       # every non-authorized state must be non-zero so a
                       # promotion boundary cannot mistake an empty/thin
                       # journal for a verified execution model.
                       "authorization_exit_code": 2})
        return report

    distinct_vehicles = {str(item["vehicle"]) for item in measured}
    expected = model.entry_cost_bps
    strata: dict[str, Any] = {}
    for key in sorted({(str(item["runtime_mode"]), str(item["vehicle"]),
                        str(item["execution_profile"])) for item in measured}):
        values = [item["adverse_bps"] for item in measured
                  if (str(item["runtime_mode"]), str(item["vehicle"]),
                      str(item["execution_profile"])) == key]
        mean = sum(values) / len(values)
        stratum_items = [item for item in measured
                         if (str(item["runtime_mode"]), str(item["vehicle"]),
                             str(item["execution_profile"])) == key]
        stratum_kind: dict[str, Any] = {}
        for stratum_kind_name in ("entry", "exit"):
            kind_values = [item["adverse_bps"] for item in stratum_items
                           if item["kind"] == stratum_kind_name]
            stratum_kind[stratum_kind_name] = {
                "fills": len(kind_values),
                "mean_bps": (sum(kind_values) / len(kind_values)
                              if kind_values else None),
            }
        strata["|".join(key)] = {
            "runtime_mode": key[0], "vehicle": key[1],
            "execution_profile": key[2], "fills": len(values), "mean_bps": mean,
            "by_kind": stratum_kind,
            "verdict": ("insufficient_data" if len(values) < MIN_FILLS
                        else "optimistic" if mean > expected else "conservative"),
        }
    observed = [item["adverse_bps"] for item in measured]
    mean_bps = sum(observed) / len(observed)
    by_symbol: dict[str, Any] = {}
    for symbol in sorted({item["symbol"] for item in measured}):
        values = [item["adverse_bps"] for item in measured if item["symbol"] == symbol]
        by_symbol[symbol] = {"fills": len(values), "mean_bps": sum(values) / len(values),
                             "median_bps": median(values)}
    by_kind: dict[str, Any] = {}
    for kind in ("entry", "exit"):
        values = [item["adverse_bps"] for item in measured if item["kind"] == kind]
        drift = [item["drift_bps"] for item in measured
                 if item["kind"] == kind and item.get("drift_bps") is not None]
        execution = [item["execution_slippage_bps"] for item in measured
                     if item["kind"] == kind and item.get("execution_slippage_bps") is not None]
        kind_mean = sum(values) / len(values) if values else None
        by_kind[kind] = {
            "fills": len(values), "observed_mean_bps": kind_mean,
            "drift_mean_bps": sum(drift) / len(drift) if drift else None,
            "execution_slippage_mean_bps": sum(execution) / len(execution) if execution else None,
            "verdict": ("insufficient_data" if len(values) < MIN_FILLS else
                        "optimistic" if kind_mean is not None and kind_mean > expected
                        else "conservative"),
        }
    mixed = len(distinct_vehicles) > 1
    verdict = ("insufficient_data" if mixed or len(measured) < MIN_FILLS
               else "optimistic" if mean_bps > expected else "conservative")
    cost_veto = verdict == "optimistic" or any(
        item.get("verdict") == "optimistic" for item in strata.values())
    partial_veto = bool(metrics.get("partial_execution_veto"))
    if cost_veto:
        authorization = "veto_optimistic_cost"
    elif partial_veto:
        authorization = "veto_underfilled_execution"
    elif verdict == "conservative":
        authorization = "authorized"
    else:
        authorization = "insufficient_data"
    report.update({
        "referenced_fills": len(measured),
        "unreferenced_fills": max(0, unique_orders - len(measured)),
        "observed_mean_bps": None if mixed else mean_bps,
        "observed_median_bps": None if mixed else median(observed),
        "bias_bps": None if mixed else expected - mean_bps,
        "within_model_rate": (None if mixed else
                               sum(value <= expected + 1e-9 for value in observed) / len(observed)),
        "over_runtime_cap": sum(value > model.max_slippage_bps for value in observed),
        "worst_bps": max(observed), "by_symbol": by_symbol, "strata": strata,
        "by_kind": by_kind, "mixed_vehicles": mixed, "verdict": verdict,
        "authorization_verdict": authorization,
        # ``insufficient_data`` is deliberately fail-closed for promotion;
        # offline discovery/factory callers may still consume the diagnostic
        # report and continue generating candidates.
        "authorization_exit_code": 0 if authorization == "authorized" else 2,
    })
    return report


def json_report(db: sqlite3.Connection, costs: CostModel | None = None) -> dict[str, Any]:
    return calibrate(load_execution_fills(db), costs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--config", type=Path, default=None,
                        help="JSON config carrying the costs/execution blocks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.journal.is_file():
        print(f"journal not found: {args.journal}", file=sys.stderr)
        return 1
    config: Mapping[str, Any] = {}
    if args.config is not None:
        try:
            config = json.loads(args.config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"config read failed: {exc}", file=sys.stderr)
            return 1
        if not isinstance(config, Mapping):
            print("config must be a JSON object", file=sys.stderr)
            return 1
    try:
        with closing(sqlite3.connect(args.journal)) as db:
            report = json_report(db, CostModel.from_config(config))
    except (OSError, sqlite3.Error) as exc:
        print(f"journal read failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return int(report.get("authorization_exit_code", 0) or 0)


__all__ = ["MIN_FILLS", "SCHEMA", "MATERIAL_FILL_FRACTION",
           "MAX_PARTIAL_CANCEL_RATE", "calibrate", "json_report",
           "load_entry_fills", "load_execution_fills", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
