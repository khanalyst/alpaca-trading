"""Compare the modelled cost of a fill against the fills actually recorded.

The cost model in :mod:`research.costs` is an assumption until something
measures it.  This module reads the runtime's own append-only journal and asks
one question per entry fill: how far did the fill land from the price the plan
was priced at, and is that inside what the model expected?

It never writes to the journal, never adjusts the model, and never invents a
reference price.  A fill whose reference cannot be reconstructed is counted as
unreferenced rather than scored against a guess, and an empty journal reports
``insufficient_data`` rather than a flattering verdict.  Deciding what to do
with a biased model stays a human decision.
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

SCHEMA = 1
# Below this many referenced fills the sample cannot separate the model from
# noise, so no verdict is issued at all.
MIN_FILLS = 20


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def load_entry_fills(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load entry fills joined to the planned notional that priced them.

    ``trades.notional`` is the risk plan's ``shares * plan entry price`` and
    ``orders.qty`` is the share count that plan submitted, so their ratio
    recovers the plan's entry price exactly -- including for a partial fill,
    where dividing by the filled quantity would not.
    """
    if not {"ts", "price", "notional", "order_id", "action"} <= _columns(db, "trades"):
        return []
    if not {"order_id", "qty"} <= _columns(db, "orders"):
        return []
    db.row_factory = sqlite3.Row
    columns = _columns(db, "trades")
    optional = {key: (f"t.{key}" if key in columns else "NULL")
                for key in ("runtime_mode", "variant_id", "vehicle",
                            "execution_profile", "reference_price",
                            "entry_reference", "exit_reference", "action",
                            "mid_price", "market_price", "drift_bps")}
    rows = db.execute(
        "SELECT t.ts AS ts, t.symbol AS symbol, t.side AS side, t.qty AS qty, "
        "t.price AS price, t.notional AS notional, t.order_id AS order_id, "
        f"{optional['runtime_mode']} AS runtime_mode, {optional['variant_id']} AS variant_id, "
        f"{optional['vehicle']} AS vehicle, {optional['execution_profile']} AS execution_profile, "
        f"{optional['reference_price']} AS reference_price, {optional['entry_reference']} AS entry_reference, "
        f"{optional['exit_reference']} AS exit_reference, {optional['action']} AS action, "
        f"{optional['mid_price']} AS mid_price, {optional['market_price']} AS market_price, "
        f"{optional['drift_bps']} AS drift_bps, "
        "MIN(o.qty) AS planned_qty "
        "FROM trades t LEFT JOIN orders o ON o.order_id = t.order_id "
        "WHERE lower(t.action) = 'open' "
        "GROUP BY t.rowid ORDER BY t.ts, t.rowid").fetchall()
    return [dict(row) for row in rows]


def load_execution_fills(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load both entry and exit fills when the journal records close rows.

    The legacy ``load_entry_fills`` API remains entry-only for compatibility;
    this explicit API prevents accidental mixing of entry/exit references.
    """
    if not {"ts", "price", "notional", "order_id", "action"} <= _columns(db, "trades"):
        return load_entry_fills(db)
    # Reuse the same query shape, then replace its open-only predicate without
    # duplicating the optional-column migration logic.
    db.row_factory = sqlite3.Row
    columns = _columns(db, "trades")
    optional = {key: (f"t.{key}" if key in columns else "NULL")
                for key in ("runtime_mode", "variant_id", "vehicle",
            "execution_profile", "reference_price",
                            "entry_reference", "exit_reference", "action",
                            "mid_price", "market_price", "drift_bps")}
    query = (
        "SELECT t.ts AS ts, t.symbol AS symbol, t.side AS side, t.qty AS qty, "
        "t.price AS price, t.notional AS notional, t.order_id AS order_id, "
        f"{optional['runtime_mode']} AS runtime_mode, {optional['variant_id']} AS variant_id, "
        f"{optional['vehicle']} AS vehicle, {optional['execution_profile']} AS execution_profile, "
        f"{optional['reference_price']} AS reference_price, {optional['entry_reference']} AS entry_reference, "
        f"{optional['exit_reference']} AS exit_reference, {optional['action']} AS action, "
        f"{optional['mid_price']} AS mid_price, {optional['market_price']} AS market_price, "
        f"{optional['drift_bps']} AS drift_bps, "
        "MIN(o.qty) AS planned_qty FROM trades t LEFT JOIN orders o ON o.order_id=t.order_id "
        "WHERE lower(t.action) IN ('open','close','entry','exit','sell_to_close') "
        "GROUP BY t.rowid ORDER BY t.ts, t.rowid")
    return [dict(row) for row in db.execute(query).fetchall()]


def _measure(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return one fill's adverse cost in bps, or ``None`` when unreferenced."""
    fill = _finite(row.get("price"))
    notional = _finite(row.get("notional"))
    planned_qty = _finite(row.get("planned_qty"))
    side = str(row.get("side") or "").lower()
    if fill is None or fill <= 0:
        return None
    if side not in {"buy", "sell"}:
        return None
    explicit_reference = (_finite(row.get("reference_price")) or
                          _finite(row.get("entry_reference")) or
                          _finite(row.get("exit_reference")))
    if explicit_reference is None and (notional is None or notional <= 0 or
                                        planned_qty is None or planned_qty <= 0):
        return None
    reference = (explicit_reference if explicit_reference and explicit_reference > 0
                 else notional / planned_qty)
    if not math.isfinite(reference) or reference <= 0:
        return None
    # Adverse means paid-above-reference when buying and received-below when
    # selling.  A favourable fill is a negative cost, not a zero: clamping it
    # would make the measured mean look worse than the account experienced.
    kind = str(row.get("action") or "open").lower()
    adverse = (fill - reference) if side == "buy" else (reference - fill)
    drift = _finite(row.get("drift_bps"))
    if drift is None:
        market = _finite(row.get("market_price")) or _finite(row.get("mid_price"))
        if market is not None and market > 0:
            drift = ((market - reference) if side == "buy" else
                     (reference - market)) / reference * 10_000.0
    adverse_bps = adverse / reference * 10_000.0
    execution = adverse_bps - drift if drift is not None else None
    profile = str(row.get("execution_profile") or "").lower()
    vehicle = str(row.get("vehicle") or ("option" if profile in {"option", "options"} else "equity"))
    return {"symbol": str(row.get("symbol") or ""), "ts": _finite(row.get("ts")),
            "side": side, "reference_price": reference, "fill_price": fill,
            "adverse_bps": adverse_bps,
            "drift_bps": drift,
            "execution_slippage_bps": execution,
            "variant_id": row.get("variant_id"),
            "runtime_mode": row.get("runtime_mode") or "unknown",
            "vehicle": vehicle, "execution_profile": profile or
            ("options" if vehicle == "option" else "shares"),
            "kind": "exit" if kind in {"close", "exit", "sell_to_close"} else "entry"}


def calibrate(rows: Sequence[Mapping[str, Any]],
              costs: CostModel | None = None, *,
              runtime_mode: str | None = None,
              vehicle: str | None = None,
              execution_profile: str | None = None) -> dict[str, Any]:
    """Score fills with explicit mode/vehicle/profile strata.

    Entry and exit slippage are reported separately when journal rows carry
    exit references.  Missing strata or thin samples remain ``insufficient``;
    no pooled equity/options verdict is emitted.
    """
    model = costs or CostModel()
    measured = [item for item in (_measure(row) for row in rows) if item is not None]
    if runtime_mode is not None:
        measured = [item for item in measured if item["runtime_mode"] == str(runtime_mode)]
    if vehicle is not None:
        measured = [item for item in measured if item["vehicle"] == str(vehicle)]
    if execution_profile is not None:
        measured = [item for item in measured if item["execution_profile"] == str(execution_profile)]
    expected = model.entry_cost_bps
    report: dict[str, Any] = {
        "schema": SCHEMA, "model": model.as_dict(), "journal_fills": len(rows),
        "referenced_fills": len(measured),
        "unreferenced_fills": len(rows) - len(measured),
        "expected_entry_cost_bps": expected,
    }
    if not measured:
        return {**report, "verdict": "insufficient_data",
                "observed_mean_bps": None, "observed_median_bps": None,
                "bias_bps": None, "within_model_rate": None,
                "over_runtime_cap": 0, "worst_bps": None, "by_symbol": {},
                "strata": {}, "by_kind": {"entry": {"verdict": "insufficient_data"},
                                             "exit": {"verdict": "insufficient_data"}}}
    distinct_vehicles = {item["vehicle"] for item in measured}
    if len(distinct_vehicles) > 1:
        # Equity and option fills have different multipliers, liquidity and
        # execution profiles; a pooled verdict would be uninterpretable.
        strata = {}
        for key in sorted({(item["runtime_mode"], item["vehicle"], item["execution_profile"])
                           for item in measured}):
            values = [item["adverse_bps"] for item in measured
                      if (item["runtime_mode"], item["vehicle"], item["execution_profile"]) == key]
            strata["|".join(key)] = {"runtime_mode": key[0], "vehicle": key[1],
                                     "execution_profile": key[2], "fills": len(values),
                                     "mean_bps": sum(values) / len(values)}
        return {**report, "verdict": "insufficient_data", "mixed_vehicles": True,
                "observed_mean_bps": None, "observed_median_bps": None,
                "bias_bps": None, "within_model_rate": None,
                "over_runtime_cap": 0, "worst_bps": None, "by_symbol": {},
                "strata": strata, "by_kind": {"entry": {"verdict": "insufficient_data"},
                                                 "exit": {"verdict": "insufficient_data"}}}
    observed = [item["adverse_bps"] for item in measured]
    mean_bps = sum(observed) / len(observed)
    within = sum(value <= expected + 1e-9 for value in observed)
    # A fill past the runtime's own rejection cap means the pre-trade slippage
    # check and the realized fill disagree.  That is an execution finding, not
    # a cost-model finding, and it is reported separately.
    over_cap = sum(value > model.max_slippage_bps for value in observed)
    by_symbol: dict[str, Any] = {}
    for symbol in sorted({item["symbol"] for item in measured}):
        values = [item["adverse_bps"] for item in measured if item["symbol"] == symbol]
        by_symbol[symbol] = {"fills": len(values),
                             "mean_bps": sum(values) / len(values),
                             "median_bps": median(values)}
    if len(measured) < MIN_FILLS:
        verdict = "insufficient_data"
    elif mean_bps > expected:
        verdict = "optimistic"
    else:
        verdict = "conservative"
    strata: dict[str, Any] = {}
    for key in sorted({(item["runtime_mode"], item["vehicle"], item["execution_profile"])
                       for item in measured}):
        values = [item["adverse_bps"] for item in measured
                  if (item["runtime_mode"], item["vehicle"], item["execution_profile"]) == key]
        strata["|".join(key)] = {"runtime_mode": key[0], "vehicle": key[1],
                                 "execution_profile": key[2], "fills": len(values),
                                 "mean_bps": sum(values) / len(values),
                                 "verdict": ("insufficient_data" if len(values) < MIN_FILLS
                                             else ("optimistic" if sum(values) / len(values) > expected
                                                   else "conservative"))}
    by_kind: dict[str, Any] = {}
    for kind in ("entry", "exit"):
        values = [item["adverse_bps"] for item in measured if item["kind"] == kind]
        drift_values = [float(item["drift_bps"]) for item in measured
                        if item["kind"] == kind and item.get("drift_bps") is not None]
        execution_values = [float(item["execution_slippage_bps"]) for item in measured
                            if item["kind"] == kind and item.get("execution_slippage_bps") is not None]
        mean_kind = sum(values) / len(values) if values else None
        by_kind[kind] = {"fills": len(values), "observed_mean_bps": mean_kind,
                         "drift_mean_bps": (sum(drift_values) / len(drift_values)
                                            if drift_values else None),
                         "execution_slippage_mean_bps": (
                             sum(execution_values) / len(execution_values)
                             if execution_values else None),
                         "verdict": ("insufficient_data" if len(values) < MIN_FILLS else
                                     "optimistic" if mean_kind > expected else "conservative")}
    return {**report,
            "observed_mean_bps": mean_bps,
            "observed_median_bps": median(observed),
            # Positive bias means the model charges more than the account paid.
            "bias_bps": expected - mean_bps,
            "within_model_rate": within / len(measured),
            "over_runtime_cap": over_cap,
            "worst_bps": max(observed),
            "by_symbol": by_symbol,
            "strata": strata,
            "by_kind": by_kind,
            "verdict": verdict}


def json_report(db: sqlite3.Connection, costs: CostModel | None = None) -> dict[str, Any]:
    """Return the calibration summary for one mode-scoped journal."""
    return calibrate(load_entry_fills(db), costs)


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
    # An optimistic model is a finding the caller must be able to see in an
    # exit status, not only in a log somebody reads later.
    return 2 if report["verdict"] == "optimistic" else 0


__all__ = ["MIN_FILLS", "SCHEMA", "calibrate", "json_report",
           "load_entry_fills", "load_execution_fills", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
