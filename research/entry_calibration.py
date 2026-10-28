"""Entry-cost calibration against the decision-time mid, with missed fills.

``research.calibration`` scores a fill against the journal reference.  For
equity entries that reference is the limit price itself (the IEX ask or bid
the plan was overwritten with), and a DAY limit fills only at or better than
its limit, so every scored entry costs zero or less and the comparison with
the model's ``entry_cost_bps`` (half spread plus slippage) can never veto.
Orders that never filled are not scored at all (review finding D1-06).

This module scores like with like:

- each filled entry order against the decision-time mid, so the half spread
  the model charges is in the measurement, and against the signal bar's
  close, the price the signal was computed from;
- every entry order that ended without a fill as a missed fill, with its
  opportunity cost (how far the mid moved away by the time the order ended)
  when the journal records the mid at that point.

It reads the runtime journal only.  The runtime does not yet journal the
fields it needs (``decision_mid`` or ``decision_bid``/``decision_ask``,
``signal_close`` and, for unfilled orders, ``expiry_mid``); until it does,
the report is ``insufficient_data`` and fails closed.  ``mid_price`` is used
as the decision mid when present, since that is what the plan records.
"""

from __future__ import annotations

from contextlib import closing
import math
import sqlite3
from statistics import median
from typing import Any, Mapping, Sequence

from .calibration import MIN_FILLS
from .costs import CostModel, cost_model_for_vehicle

ENTRY_CALIBRATION_SCHEMA = "entry-calibration.v1"
ENTRY_ACTIONS = frozenset({"open", "entry"})
# Mirrors research.calibration.MAX_PARTIAL_CANCEL_RATE: a lane that misses
# more than one entry in five is not executing the strategy it was proved on.
MAX_MISSED_FILL_RATE = 0.20
_MISSED_STATUSES = frozenset({
    "canceled", "cancelled", "expired", "rejected", "stopped", "suspended",
    "failed", "done_for_day",
})
_DECISION_MID_FIELDS = ("decision_mid", "mid_price")
_SIGNAL_FIELDS = ("signal_close", "signal_reference_price", "signal_price")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(row: Mapping[str, Any], names: Sequence[str]) -> float | None:
    for name in names:
        value = _finite(row.get(name))
        if value is not None and value > 0:
            return value
    return None


def decision_mid(row: Mapping[str, Any]) -> float | None:
    """The mid at decision time: recorded directly, or from the quote sides."""
    direct = _positive(row, _DECISION_MID_FIELDS)
    if direct is not None:
        return direct
    bid, ask = _finite(row.get("decision_bid")), _finite(row.get("decision_ask"))
    if bid is not None and ask is not None and 0 < bid <= ask:
        return (bid + ask) / 2.0
    return None


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _rows(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    columns = _columns(db, table)
    if not columns or "ts" not in columns:
        return []
    names = ["_rowid", *columns]
    try:
        raw = db.execute(f"SELECT rowid AS _rowid, * FROM {table} "
                         "ORDER BY ts, _rowid").fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(names, values)) for values in raw]


def load_entry_evidence(db: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Return entry fill rows and the latest snapshot of every entry order.

    Every column the journal has is read, so fields a newer runtime adds are
    picked up without a migration here.  A fill row inherits any field it
    lacks from its order's latest snapshot.
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in _rows(db, "orders"):
        key = row.get("order_id")
        if key is not None:
            merged = dict(latest.get(str(key), {}))
            merged.update({name: value for name, value in row.items()
                           if value is not None})
            latest[str(key)] = merged
    fills = []
    for row in _rows(db, "trades"):
        if str(row.get("action") or "").lower() not in ENTRY_ACTIONS:
            continue
        order = latest.get(str(row.get("order_id")), {})
        fills.append({**{name: value for name, value in order.items()
                         if name not in {"_rowid", "ts", "qty", "status"}},
                      **{name: value for name, value in row.items()
                         if value is not None},
                      "order_status": str(order.get("status") or "").lower()})
    orders = [order for order in latest.values()
              if str(order.get("action") or "").lower() in ENTRY_ACTIONS]
    return fills, orders


def _vehicle(row: Mapping[str, Any]) -> str:
    value = str(row.get("vehicle") or row.get("execution_profile") or "").lower()
    return "option" if value in {"option", "options"} else "equity"


def _signed_bps(side: str, price: float, reference: float) -> float:
    adverse = (price - reference) if side == "buy" else (reference - price)
    return adverse / reference * 10_000.0


def _summary(values: Sequence[float]) -> dict[str, Any]:
    clean = [value for value in values if math.isfinite(value)]
    return {"orders": len(clean),
            "mean_bps": sum(clean) / len(clean) if clean else None,
            "median_bps": median(clean) if clean else None}


def score_entries(fills: Sequence[Mapping[str, Any]],
                  orders: Sequence[Mapping[str, Any]] = (),
                  costs: CostModel | Mapping | None = None, *,
                  vehicle: str = "equity",
                  runtime_mode: str | None = None) -> dict[str, Any]:
    """Score entry orders against the decision-time mid and the signal close."""
    model = cost_model_for_vehicle(costs, vehicle)
    selected = [row for row in fills if _vehicle(row) == vehicle and (
        runtime_mode is None or str(row.get("runtime_mode") or "unknown") ==
        str(runtime_mode))]
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, row in enumerate(selected):
        grouped.setdefault(str(row.get("order_id") or f"row:{index}"), []).append(row)
    versus_mid: list[float] = []
    versus_signal: list[float] = []
    versus_limit: list[float] = []
    unscored = 0
    for items in grouped.values():
        side = str(items[0].get("side") or "").lower()
        quantities = [_finite(item.get("qty")) for item in items]
        prices = [_finite(item.get("price")) for item in items]
        if (side not in {"buy", "sell"} or
                any(value is None or value <= 0 for value in quantities + prices)):
            unscored += 1
            continue
        quantity = sum(quantities)
        fill = sum(price * qty for price, qty in zip(prices, quantities)) / quantity
        mid = next((decision_mid(item) for item in items
                    if decision_mid(item) is not None), None)
        signal = next((_positive(item, _SIGNAL_FIELDS) for item in items
                       if _positive(item, _SIGNAL_FIELDS) is not None), None)
        limit = next((_positive(item, ("entry_reference", "reference_price"))
                      for item in items
                      if _positive(item, ("entry_reference", "reference_price"))
                      is not None), None)
        if mid is None:
            unscored += 1
        else:
            versus_mid.append(_signed_bps(side, fill, mid))
        if signal is not None:
            versus_signal.append(_signed_bps(side, fill, signal))
        if limit is not None:
            versus_limit.append(_signed_bps(side, fill, limit))
    filled_orders = {str(key) for key in grouped}
    missed = [order for order in orders
              if _vehicle(order) == vehicle and
              (runtime_mode is None or str(order.get("runtime_mode") or "unknown")
               == str(runtime_mode)) and
              str(order.get("order_id")) not in filled_orders and
              str(order.get("status") or "").lower() in _MISSED_STATUSES and
              not (_finite(order.get("filled_qty")) or 0.0) > 0]
    opportunity = []
    for order in missed:
        side = str(order.get("side") or "").lower()
        mid, later = decision_mid(order), _positive(order, ("expiry_mid",))
        if side in {"buy", "sell"} and mid is not None and later is not None:
            # Positive when the price moved away from the order: the move the
            # strategy would have captured had it been filled at the mid.
            opportunity.append(_signed_bps(side, later, mid))
    attempted = len(grouped) + len(missed)
    missed_rate = len(missed) / attempted if attempted else None
    expected = model.entry_cost_bps
    mid_summary = _summary(versus_mid)
    if len(versus_mid) < MIN_FILLS:
        verdict = "insufficient_data"
    elif mid_summary["mean_bps"] > expected:
        verdict = "optimistic"
    else:
        verdict = "conservative"
    missed_veto = bool(missed_rate is not None and missed_rate > MAX_MISSED_FILL_RATE)
    if verdict == "optimistic":
        authorization = "veto_optimistic_cost"
    elif missed_veto:
        authorization = "veto_missed_fills"
    elif verdict == "conservative":
        authorization = "authorized"
    else:
        authorization = "insufficient_data"
    return {
        "schema": ENTRY_CALIBRATION_SCHEMA, "vehicle": vehicle,
        "reference": "decision_time_mid",
        "expected_entry_cost_bps": expected,
        "filled_orders": len(grouped), "unscored_orders": unscored,
        "cost_versus_decision_mid": mid_summary,
        "cost_versus_signal_close": _summary(versus_signal),
        "cost_versus_limit_reference": _summary(versus_limit),
        "missed_orders": len(missed), "missed_fill_rate": missed_rate,
        "max_missed_fill_rate": MAX_MISSED_FILL_RATE,
        "missed_opportunity_cost": {
            **_summary(opportunity),
            "unmeasured_orders": len(missed) - len(opportunity)},
        "verdict": verdict, "missed_fill_veto": missed_veto,
        "authorization_verdict": authorization,
        "authorization_exit_code": 0 if authorization == "authorized" else 2,
        "missing_runtime_fields": sorted(
            ({"decision_mid"} if len(versus_mid) < len(grouped) else set()) |
            ({"signal_close"} if len(versus_signal) < len(grouped) else set()) |
            ({"expiry_mid"} if len(opportunity) < len(missed) else set())),
    }


def entry_calibration_report(db: sqlite3.Connection,
                             costs: CostModel | Mapping | None = None, *,
                             vehicle: str = "equity",
                             runtime_mode: str | None = None) -> dict[str, Any]:
    """Read a runtime journal and score its entries; see :func:`score_entries`."""
    fills, orders = load_entry_evidence(db)
    return score_entries(fills, orders, costs, vehicle=vehicle,
                         runtime_mode=runtime_mode)


def entry_calibration_from_path(path: str, costs: CostModel | Mapping | None = None,
                                **options: Any) -> dict[str, Any]:
    with closing(sqlite3.connect(path)) as db:
        return entry_calibration_report(db, costs, **options)


__all__ = ["ENTRY_CALIBRATION_SCHEMA", "MAX_MISSED_FILL_RATE", "decision_mid",
           "entry_calibration_from_path", "entry_calibration_report",
           "load_entry_evidence", "score_entries"]
