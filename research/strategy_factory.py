"""Autonomous, parallel strategy research with isolated simulated accounts.

Seven bounded hypotheses are evaluated in separate worker processes by
default.  A worker may mutate only the audited rule grammar; it cannot write
or execute source code.  Mutations are diagnosed from the chronological fit
partition and judged on untouched held-out or later forward data.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
import sqlite3
from statistics import mean
from typing import Any, Mapping, Sequence
import uuid
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_FAMILIES, evaluate_rule_signal, rule_variant_id, validate_rule_spec,
)
from .edge_lab import (
    DEFAULT_DB_PATH, EdgeLedger, _read_discovery_rows, canonical_json,
    content_hash,
)
from .gates import AcceptanceFloor, chronological_split, max_drawdown_of
from .market_data import OptionSnapshot, UnderlyingBar
from .stats import benjamini_hochberg, paired_cluster_sign_flip


DEFAULT_STRATEGIES = 7
DEFAULT_VARIANTS = 4
DEFAULT_WORKERS = 7
MAX_STRATEGIES = 16
MAX_VARIANTS = 8
MAX_WORKERS = 16
FACTORY_SCHEMA = "strategy-factory.v1"
ACTIVE_HYPOTHESIS_STATES = {"queued", "testing", "backtest_passed"}


class FactoryError(ValueError):
    """Raised when a factory cycle cannot preserve its research boundaries."""


@dataclass(frozen=True)
class StrategyHypothesis:
    hypothesis_id: str
    slot: int
    generation: int
    vehicle: str
    family: str
    thesis: str
    falsification: str
    rule_spec: dict[str, Any]
    parent_hypothesis_id: str | None = None
    not_before: str | None = None


def _hypothesis_id(vehicle: str, slot: int, generation: int,
                   spec: Mapping[str, Any]) -> str:
    return f"hyp.{vehicle}.{slot:02d}.{generation:02d}.{content_hash(spec)[:12]}"


def _thesis(spec: Mapping[str, Any]) -> str:
    family = str(spec["family"]).replace("_", " ")
    confirmation = str(spec.get("confirmation", "none")).replace("_", " ")
    suffix = "" if confirmation == "none" else f" confirmed by {confirmation}"
    return f"A completed-bar {family}{suffix} has positive expectancy after executable costs."


def _falsification(spec: Mapping[str, Any]) -> str:
    return (
        f"The {str(spec['family']).replace('_', ' ')} rule has no positive "
        "held-out and forward expectancy after costs and multiple-test correction."
    )


def initial_hypotheses(count: int = DEFAULT_STRATEGIES, *,
                       vehicle: str = "equity") -> list[StrategyHypothesis]:
    if not 1 <= int(count) <= MAX_STRATEGIES:
        raise FactoryError(f"strategies must be between 1 and {MAX_STRATEGIES}")
    templates = [
        {"family": "opening_range_breakout", "range_minutes": 15,
         "threshold_bps": 5.0, "confirmation": "volume"},
        {"family": "opening_range_fade", "range_minutes": 20,
         "threshold_bps": 8.0, "target_r": 1.5, "confirmation": "none"},
        {"family": "momentum_continuation", "lookback": 12,
         "threshold_bps": 18.0, "confirmation": "volume"},
        {"family": "mean_reversion", "lookback": 20,
         "zscore": 1.5, "target_r": 1.5, "confirmation": "volatility"},
        {"family": "trend_pullback", "lookback": 10,
         "slow_lookback": 35, "threshold_bps": 15.0, "confirmation": "trend"},
        {"family": "volatility_breakout", "lookback": 12,
         "compression_bps": 55.0, "threshold_bps": 5.0, "confirmation": "volume"},
        {"family": "volume_breakout", "lookback": 15,
         "volume_multiplier": 1.5, "threshold_bps": 5.0, "confirmation": "trend"},
    ]
    output = []
    for slot in range(int(count)):
        raw = templates[slot % len(templates)]
        if slot >= len(templates):
            raw = {**raw, "lookback": min(120, int(raw.get("lookback", 15)) + slot)}
        spec = validate_rule_spec(raw)
        output.append(StrategyHypothesis(
            _hypothesis_id(vehicle, slot, 0, spec), slot, 0, vehicle, spec["family"],
            _thesis(spec), _falsification(spec), spec, None, None,
        ))
    return output


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


class FactoryLedger:
    """Append-only factory lineage stored beside the edge evidence ledger."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        EdgeLedger(self.path)
        with closing(_connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS factory_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    slot INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    parent_hypothesis_id TEXT,
                    family TEXT NOT NULL,
                    thesis TEXT NOT NULL,
                    falsification TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    not_before TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(vehicle,slot,generation)
                );
                CREATE TABLE IF NOT EXISTS factory_events (
                    event_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS factory_accounts (
                    account_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    variant_id TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    starting_cash REAL NOT NULL,
                    ending_equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(cycle_id,variant_id,vehicle)
                );
                CREATE TABLE IF NOT EXISTS factory_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    dataset_hash TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    workers INTEGER NOT NULL,
                    strategies INTEGER NOT NULL,
                    variants INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(dataset_hash,vehicle)
                );
                CREATE TRIGGER IF NOT EXISTS factory_hypotheses_no_update
                    BEFORE UPDATE ON factory_hypotheses BEGIN
                    SELECT RAISE(ABORT, 'factory hypotheses are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_hypotheses_no_delete
                    BEFORE DELETE ON factory_hypotheses BEGIN
                    SELECT RAISE(ABORT, 'factory hypotheses are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_accounts_no_update
                    BEFORE UPDATE ON factory_accounts BEGIN
                    SELECT RAISE(ABORT, 'factory accounts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_accounts_no_delete
                    BEFORE DELETE ON factory_accounts BEGIN
                    SELECT RAISE(ABORT, 'factory accounts are immutable');
                END;
            """)

    def register(self, hypothesis: StrategyHypothesis) -> dict:
        now = datetime.now().timestamp()
        with closing(_connect(self.path)) as db, db:
            row = db.execute("SELECT * FROM factory_hypotheses WHERE hypothesis_id=?",
                             (hypothesis.hypothesis_id,)).fetchone()
            if row is None:
                db.execute("""INSERT INTO factory_hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    hypothesis.hypothesis_id, hypothesis.slot, hypothesis.generation,
                    hypothesis.vehicle, hypothesis.parent_hypothesis_id,
                    hypothesis.family, hypothesis.thesis,
                    hypothesis.falsification, canonical_json(hypothesis.rule_spec),
                    hypothesis.not_before, now,
                ))
                db.execute("INSERT INTO factory_events VALUES(?,?,?,?,?,?)", (
                    uuid.uuid4().hex, hypothesis.hypothesis_id, "queued",
                    "autonomous hypothesis registered", canonical_json({}), now,
                ))
            return self.hypothesis(hypothesis.hypothesis_id) or {}

    def event(self, hypothesis_id: str, status: str, reason: str,
              payload: Mapping | None = None) -> None:
        if not reason.strip():
            raise FactoryError("factory event reason is required")
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_events VALUES(?,?,?,?,?,?)", (
                uuid.uuid4().hex, hypothesis_id, status, reason,
                canonical_json(dict(payload or {})), datetime.now().timestamp(),
            ))

    def hypothesis(self, hypothesis_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT h.*, (
                SELECT status FROM factory_events e WHERE e.hypothesis_id=h.hypothesis_id
                ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1) AS status
                FROM factory_hypotheses h WHERE h.hypothesis_id=?""",
                (hypothesis_id,)).fetchone()
        if row is None:
            return None
        item = dict(row); item["rule_spec"] = json.loads(item.pop("spec_json"))
        return item

    def hypotheses(self, *, vehicle: str | None = None) -> list[dict]:
        where = " WHERE h.vehicle=?" if vehicle else ""
        parameters = (vehicle,) if vehicle else ()
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT h.*, (
                SELECT status FROM factory_events e WHERE e.hypothesis_id=h.hypothesis_id
                ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1) AS status
                FROM factory_hypotheses h""" + where +
                " ORDER BY h.vehicle,h.slot,h.generation", parameters).fetchall()
        output = []
        for row in rows:
            item = dict(row); item["rule_spec"] = json.loads(item.pop("spec_json")); output.append(item)
        return output

    def active(self, vehicle: str) -> list[dict]:
        latest: dict[int, dict] = {}
        for item in self.hypotheses(vehicle=vehicle):
            if item.get("status") in ACTIVE_HYPOTHESIS_STATES:
                latest[int(item["slot"])] = item
        return [latest[key] for key in sorted(latest)]

    def add_account(self, cycle_id: str, hypothesis_id: str, result: Mapping) -> None:
        account = result["account"]
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO factory_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                account["account_id"], cycle_id, hypothesis_id, result["variant_id"],
                result["vehicle"], account["starting_cash"], account["ending_equity"],
                account["realized_pnl"], account["max_drawdown"], account["trades"],
                result["worker_pid"], canonical_json(dict(result)), datetime.now().timestamp(),
            ))

    def last_boundary(self, hypothesis_id: str, vehicle: str) -> str | None:
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT result_json FROM factory_accounts
                WHERE hypothesis_id=? AND vehicle=? ORDER BY created_at DESC""",
                (hypothesis_id, vehicle)).fetchall()
        values = []
        for row in rows:
            try:
                value = json.loads(row["result_json"]).get("evaluation_end")
                if value:
                    values.append(str(value))
            except json.JSONDecodeError:
                continue
        return max(values) if values else None

    def existing_cycle(self, dataset_hash: str, vehicle: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("SELECT result_json FROM factory_cycles WHERE dataset_hash=? AND vehicle=?",
                             (dataset_hash, vehicle)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def add_cycle(self, cycle_id: str, dataset_hash: str, vehicle: str,
                  workers: int, strategies: int, variants: int, result: Mapping) -> None:
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_cycles VALUES(?,?,?,?,?,?,?,?)", (
                cycle_id, dataset_hash, vehicle, workers, strategies, variants,
                canonical_json(dict(result)), datetime.now().timestamp(),
            ))

    def status(self) -> dict:
        hypotheses = self.hypotheses()
        with closing(_connect(self.path)) as db:
            accounts = db.execute("SELECT COUNT(*) AS n FROM factory_accounts").fetchone()["n"]
            cycles = db.execute("SELECT COUNT(*) AS n FROM factory_cycles").fetchone()["n"]
        return {"schema": FACTORY_SCHEMA, "hypotheses": hypotheses,
                "accounts": int(accounts), "cycles": int(cycles)}


def _session(row: UnderlyingBar) -> str:
    return row.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _visible(row: Any, cutoff: datetime) -> bool:
    identity = getattr(row, "identity", None)
    return bool(identity is not None and identity.as_of <= cutoff)


def _option_at(snapshots: Sequence[OptionSnapshot], *, symbol: str, day: date,
               direction: str, cutoff: datetime, contract_symbol: str | None = None) -> OptionSnapshot | None:
    right = "call" if direction == "long" else "put"
    eligible = [snap for snap in snapshots
                if snap.contract.underlying.upper() == symbol.upper()
                and snap.session_date == day and snap.timestamp <= cutoff
                and _visible(snap, cutoff) and snap.bid > 0 and snap.ask > 0
                and (contract_symbol is None
                     or snap.contract.symbol == contract_symbol)
                and (contract_symbol is not None or snap.contract.right.lower() == right)]
    if not eligible:
        return None
    if contract_symbol is not None:
        return max(eligible, key=lambda item: item.timestamp)
    spot = eligible[-1].underlying_price
    return min(eligible, key=lambda item: (
        abs(item.contract.strike - (spot or item.contract.strike)),
        (item.ask - item.bid) / item.ask, -item.timestamp.timestamp(),
        item.contract.symbol))


def _simulate_trade(session_bars: Sequence[UnderlyingBar], spec: Mapping[str, Any],
                    snapshots: Sequence[OptionSnapshot], vehicle: str) -> dict | None:
    for index in range(1, len(session_bars) - 1):
        signal_bar = session_bars[index]
        if not _visible(signal_bar, signal_bar.end):
            continue
        signal = evaluate_rule_signal(session_bars[:index + 1], spec)
        if signal is None:
            continue
        entry_bar = session_bars[index + 1]
        if not _visible(entry_bar, entry_bar.end):
            continue
        direction = signal["direction"]
        entry_underlying = float(entry_bar.open)
        distance = float(signal["stop_distance"])
        stop = entry_underlying - distance if direction == "long" else entry_underlying + distance
        target = entry_underlying + distance * float(spec["target_r"]) if direction == "long" else entry_underlying - distance * float(spec["target_r"])
        last_index = min(len(session_bars) - 1, index + 1 + int(spec["max_hold_bars"]))
        exit_bar = session_bars[last_index]
        exit_ref = float(exit_bar.close)
        reason = "time"
        tie = False
        for bar in session_bars[index + 2:last_index + 1]:
            if not _visible(bar, bar.end):
                continue
            if direction == "long":
                hit_stop, hit_target = bar.low <= stop, bar.high >= target
            else:
                hit_stop, hit_target = bar.high >= stop, bar.low <= target
            if hit_stop or hit_target:
                tie = hit_stop and hit_target
                reason = "stop" if hit_stop else "target"
                exit_ref = stop if hit_stop else target
                exit_bar = bar
                break
        day = signal_bar.session_date
        multiplier = 1
        contract = None
        entry_ref = entry_underlying
        if vehicle == "option":
            entry_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                    direction=direction, cutoff=entry_bar.end)
            if entry_snap is None:
                return None
            exit_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                   direction=direction, cutoff=exit_bar.end,
                                   contract_symbol=entry_snap.contract.symbol)
            if exit_snap is None:
                return None
            contract = entry_snap.contract.symbol
            entry_ref = entry_snap.ask
            exit_ref = exit_snap.bid
            multiplier = entry_snap.contract.multiplier
        return {
            "vehicle": vehicle, "symbol": signal_bar.symbol,
            "session_date": day.isoformat(), "direction": direction,
            "signal_timestamp": signal_bar.end.isoformat(),
            "entry_timestamp": entry_bar.timestamp.isoformat(),
            "exit_timestamp": exit_bar.end.isoformat(),
            "entry_reference": entry_ref, "exit_reference": exit_ref,
            "underlying_entry": entry_underlying, "stop_price": stop,
            "target_price": target, "exit_reason": reason, "tie_broken": tie,
            "contract": contract, "contract_multiplier": multiplier,
            "risk_per_unit": (entry_ref * multiplier if vehicle == "option" else distance),
        }
    return None


def simulate_account(bars: Sequence[UnderlyingBar], snapshots: Sequence[OptionSnapshot],
                     spec: Mapping[str, Any], *, vehicle: str, account_id: str,
                     starting_cash: float = 100_000.0, risk_pct: float = .5,
                     spread_bps: float = 1.0, slippage_bps: float = 1.0,
                     fee_bps: float = .5) -> dict:
    """Replay one variant in a completely isolated cash/equity book."""
    spec = validate_rule_spec(spec)
    grouped: dict[tuple[str, date], list[UnderlyingBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.timestamp, item.symbol)):
        grouped.setdefault((bar.symbol, bar.session_date), []).append(bar)
    cash = float(starting_cash)
    peak = cash
    drawdown = 0.0
    rows = []
    for (symbol, day), session_bars in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0])):
        opportunity = f"{rule_variant_id(spec)}:{vehicle}:{symbol}:{day.isoformat()}"
        raw = _simulate_trade(session_bars, spec, snapshots, vehicle)
        if raw is None:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(), "opportunity_id": opportunity,
                         "net_pnl": 0.0, "return_value": 0.0, "no_trade": True})
            continue
        risk_budget = max(0.0, cash * float(risk_pct) / 100.0)
        per_unit = max(float(raw["risk_per_unit"]), 1e-9)
        quantity = math.floor(risk_budget / per_unit)
        if vehicle == "equity":
            notional_cap = max(0.0, cash * .25)
            quantity = min(quantity, math.floor(notional_cap / max(float(raw["entry_reference"]), 1e-9)))
        if quantity <= 0:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(), "opportunity_id": opportunity,
                         "net_pnl": 0.0, "return_value": 0.0, "no_trade": True,
                         "reject_reason": "isolated account risk budget cannot fund one unit"})
            continue
        execution_direction = "long" if vehicle == "option" else raw["direction"]
        spread = 0.0 if vehicle == "option" else spread_bps / 20_000.0
        slip = slippage_bps / 10_000.0
        entry_sign = 1.0 if execution_direction == "long" else -1.0
        exit_sign = -entry_sign
        entry = float(raw["entry_reference"]) * (1 + entry_sign * (spread + slip))
        exit_price = float(raw["exit_reference"]) * (1 + exit_sign * (spread + slip))
        multiplier = int(raw["contract_multiplier"])
        gross = ((exit_price - entry) if execution_direction == "long" else
                 (entry - exit_price)) * quantity * multiplier
        costs = (abs(entry) + abs(exit_price)) * quantity * multiplier * fee_bps / 10_000.0
        net = gross - costs
        before = cash
        cash += net
        peak = max(peak, cash)
        drawdown = max(drawdown, peak - cash)
        rows.append({**raw, "opportunity_id": opportunity, "quantity": quantity,
                     "entry_price": entry, "exit_price": exit_price,
                     "gross_pnl": gross, "costs": costs, "net_pnl": net,
                     "return_value": net / before if before > 0 else 0.0,
                     "no_trade": False})
    executed = [row for row in rows if row.get("no_trade") is not True]
    return {
        "account_id": account_id, "starting_cash": float(starting_cash),
        "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
        "max_drawdown": drawdown, "trades": len(executed), "rows": rows,
    }


def diagnose(rows: Sequence[Mapping], *, starting_cash: float = 100_000.0) -> dict:
    trades = [row for row in rows if row.get("no_trade") is not True]
    pnl = [float(row.get("net_pnl", 0.0)) for row in trades]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    sessions = {row.get("session_date") for row in rows}
    expectancy = mean(pnl) if pnl else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    drawdown = max_drawdown_of(rows)
    if len(trades) < max(3, len(sessions) // 3):
        failure = "insufficient_signals"
    elif expectancy <= 0:
        failure = "negative_expectancy"
    elif len(wins) / len(trades) < .35:
        failure = "low_win_rate"
    elif profit_factor < 1.1:
        failure = "poor_payoff"
    elif drawdown > starting_cash * .05:
        failure = "excess_drawdown"
    else:
        failure = "none"
    return {
        "primary_failure": failure, "trades": len(trades),
        "sessions": len(sessions), "net_pnl": sum(pnl), "expectancy": expectancy,
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor if math.isfinite(profit_factor) else 999.0,
        "max_drawdown": drawdown,
        "stop_rate": (sum(row.get("exit_reason") == "stop" for row in trades) / len(trades)
                      if trades else 0.0),
        "target_rate": (sum(row.get("exit_reason") == "target" for row in trades) / len(trades)
                        if trades else 0.0),
    }


def _safe_variant(spec: Mapping[str, Any], **changes) -> dict:
    candidate = dict(spec); candidate.update(changes)
    return validate_rule_spec(candidate)


def mutate_from_diagnosis(spec: Mapping[str, Any], diagnostic: Mapping[str, Any],
                          count: int = DEFAULT_VARIANTS) -> list[dict]:
    """Create bounded variants from an explicit failure diagnosis."""
    if not 2 <= int(count) <= MAX_VARIANTS:
        raise FactoryError(f"variants must be between 2 and {MAX_VARIANTS}")
    root = validate_rule_spec(spec)
    failure = str(diagnostic.get("primary_failure") or "none")
    changes: list[dict] = []
    if failure == "insufficient_signals":
        changes = [
            {"threshold_bps": max(0.0, root["threshold_bps"] * .65),
             "zscore": max(.25, root["zscore"] * .8), "confirmation": "none"},
            {"lookback": max(3, root["lookback"] - 3),
             "slow_lookback": max(root["lookback"] + 2, root["slow_lookback"] - 5)},
            {"volume_multiplier": max(.25, root["volume_multiplier"] * .8),
             "range_minutes": max(3, root["range_minutes"] - 5)},
        ]
    elif failure in {"negative_expectancy", "poor_payoff"}:
        changes = [
            {"threshold_bps": min(500.0, root["threshold_bps"] * 1.35 + 1),
             "confirmation": "trend"},
            {"target_r": max(.25, root["target_r"] * .8),
             "stop_atr": min(10.0, root["stop_atr"] * 1.15)},
            {"volume_multiplier": min(10.0, root["volume_multiplier"] * 1.25),
             "confirmation": "volume"},
        ]
    elif failure == "low_win_rate":
        changes = [
            {"target_r": max(.25, root["target_r"] * .7)},
            {"threshold_bps": min(500.0, root["threshold_bps"] * 1.5 + 1),
             "confirmation": "trend"},
            {"max_hold_bars": max(1, int(root["max_hold_bars"] * .65)),
             "confirmation": "volume"},
        ]
    elif failure == "excess_drawdown":
        changes = [
            {"threshold_bps": min(500.0, root["threshold_bps"] * 1.5 + 1),
             "confirmation": "trend"},
            {"stop_atr": max(.2, root["stop_atr"] * .8),
             "max_hold_bars": max(1, int(root["max_hold_bars"] * .7))},
            {"side": "long", "confirmation": "volume"},
        ]
    else:
        changes = [
            {"target_r": min(10.0, root["target_r"] * 1.25)},
            {"threshold_bps": min(500.0, root["threshold_bps"] + 5),
             "confirmation": "trend"},
            {"lookback": min(120, root["lookback"] + 5),
             "slow_lookback": min(240, max(root["slow_lookback"] + 8,
                                             root["lookback"] + 6))},
        ]
    variants = [root]
    for change in changes:
        if len(variants) >= int(count):
            break
        try:
            candidate = _safe_variant(root, **change)
        except ValueError:
            continue
        if rule_variant_id(candidate) not in {rule_variant_id(item) for item in variants}:
            variants.append(candidate)
    while len(variants) < int(count):
        step = len(variants)
        candidate = _safe_variant(
            root, threshold_bps=min(500.0, root["threshold_bps"] + step * 3),
            target_r=max(.25, min(10.0, root["target_r"] + (step - 1) * .25)),
        )
        if rule_variant_id(candidate) in {rule_variant_id(item) for item in variants}:
            break
        variants.append(candidate)
    return variants


def replacement_hypothesis(previous: Mapping[str, Any], diagnostic: Mapping[str, Any],
                           *, max_generations: int,
                           not_before: str | None = None) -> StrategyHypothesis | None:
    generation = int(previous["generation"]) + 1
    if generation >= int(max_generations):
        return None
    slot = int(previous["slot"])
    current = str(previous["family"])
    offset = 2 if diagnostic.get("primary_failure") == "insufficient_signals" else 1
    family = RULE_FAMILIES[(RULE_FAMILIES.index(current) + generation + offset) % len(RULE_FAMILIES)]
    seed = dict(previous["rule_spec"])
    seed.update({
        "family": family,
        "confirmation": ("volume" if diagnostic.get("primary_failure") in
                         {"negative_expectancy", "low_win_rate"} else "trend"),
        "threshold_bps": min(500.0, max(0.0, float(seed["threshold_bps"]) + 3 * generation)),
        "lookback": min(120, max(3, int(seed["lookback"]) + generation)),
    })
    seed["slow_lookback"] = max(int(seed["slow_lookback"]), int(seed["lookback"]) + 5)
    spec = validate_rule_spec(seed)
    vehicle = str(previous["vehicle"])
    hid = _hypothesis_id(vehicle, slot, generation, spec)
    return StrategyHypothesis(
        hid, slot, generation, vehicle, family, _thesis(spec), _falsification(spec), spec,
        str(previous["hypothesis_id"]), not_before,
    )


def _worker(payload: Mapping[str, Any]) -> dict:
    hypothesis = dict(payload["hypothesis"])
    bars = list(payload["bars"])
    snapshots = list(payload["snapshots"])
    vehicle = str(payload["vehicle"])
    mode = str(payload["mode"])
    starting_cash = float(payload["starting_cash"])
    if mode == "backtest":
        sessions = sorted({_session(bar) for bar in bars})
        cut = max(1, min(len(sessions) - 1, int(len(sessions) * .7))) if len(sessions) > 1 else len(sessions)
        fit_sessions = set(sessions[:cut])
        fit_bars = [bar for bar in bars if _session(bar) in fit_sessions]
        root_account = simulate_account(
            fit_bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
            account_id=f"diagnostic:{hypothesis['hypothesis_id']}",
            starting_cash=starting_cash,
        )
        diagnostic = diagnose(root_account["rows"], starting_cash=starting_cash)
        specs = mutate_from_diagnosis(
            hypothesis["rule_spec"], diagnostic, int(payload["variants_per_strategy"]))
    else:
        diagnostic = {"primary_failure": "forward_validation"}
        specs = [validate_rule_spec(item) for item in payload["existing_specs"]]
    variants = []
    for spec in specs:
        variant_id = rule_variant_id(spec)
        account_id = f"sim:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}:{uuid.uuid4().hex[:8]}"
        account = simulate_account(
            bars, snapshots, spec, vehicle=vehicle, account_id=account_id,
            starting_cash=starting_cash,
        )
        variants.append({
            "variant_id": variant_id, "rule_spec": spec, "vehicle": vehicle,
            "account": account, "diagnostic": diagnose(account["rows"], starting_cash=starting_cash),
            "worker_pid": os.getpid(),
        })
    sessions = sorted({_session(bar) for bar in bars})
    return {"hypothesis": hypothesis, "mode": mode, "diagnostic": diagnostic,
            "evaluation_start": sessions[0] if sessions else None,
            "evaluation_end": sessions[-1] if sessions else None,
            "variants": variants, "worker_pid": os.getpid()}


def _gate(rows: Sequence[Mapping], *, vehicle: str, mode: str,
          min_trades: int, min_sessions: int, alpha: float) -> dict:
    ordered = sorted(rows, key=lambda row: (str(row.get("session_date", "")),
                                             str(row.get("entry_timestamp", ""))))
    fit, heldout = ([], ordered) if mode == "shadow" else chronological_split(ordered, fit_fraction=.7)
    floor = AcceptanceFloor(min_trades=min_trades, min_sessions=min_sessions).check(ordered, vehicle=vehicle)
    held_floor = AcceptanceFloor(min_trades=min_trades, min_sessions=min_sessions).check(heldout, vehicle=vehicle)
    sample_adequate = bool(floor["checks"]["trades"] and floor["checks"]["sessions"])
    heldout_adequate = bool(
        held_floor["checks"]["trades"] and held_floor["checks"]["sessions"])
    pairs = []
    for index, row in enumerate(heldout):
        stamp = row.get("entry_timestamp") or row.get("session_date")
        try:
            parsed = datetime.fromisoformat(str(stamp))
            timestamp = parsed.timestamp()
        except (TypeError, ValueError):
            timestamp = float(index)
        pairs.append((timestamp, float(row.get("net_pnl", 0.0)), 0.0))
    test = paired_cluster_sign_flip(pairs)
    fit_net = sum(float(row.get("net_pnl", 0.0)) for row in fit)
    held_net = sum(float(row.get("net_pnl", 0.0)) for row in heldout)
    adequate = bool(sample_adequate and heldout_adequate and heldout)
    return {
        "passes_without_family": bool(
            adequate and held_net > 0 and (mode == "shadow" or fit_net > 0)
            and float(test["p_value"]) <= float(alpha)),
        "passes": False, "p_raw": float(test["p_value"]),
        "sample_adequate": sample_adequate,
        "heldout_sample_adequate": heldout_adequate,
        "confidence": 1.0 - float(test["p_value"]),
        "floor": floor, "heldout_floor": held_floor,
        "fit_net_pnl": fit_net, "heldout_net_pnl": held_net,
        "fit_trades": len([r for r in fit if r.get("no_trade") is not True]),
        "heldout_trades": len([r for r in heldout if r.get("no_trade") is not True]),
        "max_drawdown": max_drawdown_of(ordered), "test": test,
        "mode": mode, "alpha": float(alpha),
    }


def _existing_specs(edge: EdgeLedger, hypothesis_id: str, vehicle: str) -> list[dict]:
    specs = []
    for candidate in edge.status(vehicle=vehicle):
        if candidate.get("strategy_id") != "rule" or candidate.get("status") not in {
                "backtest_passed", "shadow", "validated", "champion"}:
            continue
        try:
            axes = json.loads(candidate.get("axes_json") or "{}")
            config = json.loads(candidate.get("config_json") or "{}")
        except json.JSONDecodeError:
            continue
        if axes.get("hypothesis_id") == hypothesis_id:
            spec = (config.get("strategy") or {}).get("rule_spec")
            if isinstance(spec, Mapping):
                specs.append(validate_rule_spec(spec))
    return specs


def run_factory(data: str | Path | Sequence[Mapping], *,
                db_path: str | Path = DEFAULT_DB_PATH, vehicle: str = "equity",
                strategies: int = DEFAULT_STRATEGIES,
                variants_per_strategy: int = DEFAULT_VARIANTS,
                workers: int = DEFAULT_WORKERS, starting_cash: float = 100_000.0,
                min_trades: int = 100, min_sessions: int = 10,
                alpha: float = .05, max_generations: int = 5) -> dict:
    """Run one autonomous cycle and persist every account, diagnosis and edge."""
    if vehicle not in {"equity", "option"}:
        raise FactoryError("vehicle must be equity or option")
    if not 1 <= int(strategies) <= MAX_STRATEGIES:
        raise FactoryError(f"strategies must be between 1 and {MAX_STRATEGIES}")
    if not 2 <= int(variants_per_strategy) <= MAX_VARIANTS:
        raise FactoryError(f"variants_per_strategy must be between 2 and {MAX_VARIANTS}")
    if not 1 <= int(workers) <= MAX_WORKERS:
        raise FactoryError(f"workers must be between 1 and {MAX_WORKERS}")
    if starting_cash <= 0 or min_trades < 1 or min_sessions < 1:
        raise FactoryError("starting_cash, min_trades and min_sessions must be positive")
    if not 0 < alpha <= 1:
        raise FactoryError("alpha must be in (0,1]")
    raw_rows, bars, snapshot_map = _read_discovery_rows(data)
    dataset_hash = content_hash(raw_rows)
    factory = FactoryLedger(db_path)
    duplicate = factory.existing_cycle(dataset_hash, vehicle)
    if duplicate is not None:
        return {**duplicate, "duplicate": True}
    if not factory.hypotheses(vehicle=vehicle):
        for hypothesis in initial_hypotheses(strategies, vehicle=vehicle):
            factory.register(hypothesis)
    active = factory.active(vehicle)[:int(strategies)]
    if not active:
        return {"schema": FACTORY_SCHEMA, "status": "exhausted", "dataset_hash": dataset_hash,
                "vehicle": vehicle, "strategies": 0, "variants": 0, "accounts": 0}
    edge = EdgeLedger(db_path)
    tasks = []
    snapshots = list(snapshot_map.values())
    for hypothesis in active:
        mode = "shadow" if hypothesis.get("status") == "backtest_passed" else "backtest"
        boundary = (factory.last_boundary(hypothesis["hypothesis_id"], vehicle)
                    if mode == "shadow" else hypothesis.get("not_before"))
        selected_bars = [bar for bar in bars if boundary is None or _session(bar) > boundary]
        selected_snapshots = [snap for snap in snapshots if boundary is None or snap.session_date.isoformat() > boundary]
        specs = _existing_specs(edge, hypothesis["hypothesis_id"], vehicle) if mode == "shadow" else []
        if mode == "shadow" and not specs:
            factory.event(hypothesis["hypothesis_id"], "retired",
                          "forward validation had no persisted backtest-passed variants")
            continue
        if not selected_bars:
            factory.event(hypothesis["hypothesis_id"], hypothesis["status"],
                          "no unseen sessions; forward boundary was not consumed",
                          {"boundary": boundary, "dataset_hash": dataset_hash})
            continue
        factory.event(hypothesis["hypothesis_id"], "testing",
                      f"{mode} evaluation started", {"dataset_hash": dataset_hash})
        tasks.append({
            "hypothesis": hypothesis, "bars": selected_bars,
            "snapshots": selected_snapshots, "vehicle": vehicle, "mode": mode,
            "existing_specs": specs, "variants_per_strategy": variants_per_strategy,
            "starting_cash": starting_cash,
        })
    if not tasks:
        return {"schema": FACTORY_SCHEMA, "status": "waiting_for_new_data",
                "dataset_hash": dataset_hash, "vehicle": vehicle,
                "strategies": len(active), "variants": 0, "accounts": 0}

    max_workers = min(int(workers), len(tasks))
    worker_results = []
    backend = "process"
    try:
        pool = ProcessPoolExecutor(max_workers=max_workers)
    except (OSError, PermissionError):
        # Some restricted containers disable POSIX semaphores.  Preserve
        # bounded parallel scheduling there; normal deployments use processes.
        pool = ThreadPoolExecutor(max_workers=max_workers)
        backend = "thread_fallback"
    with pool:
        futures = [pool.submit(_worker, task) for task in tasks]
        for future in as_completed(futures):
            worker_results.append(future.result())

    variant_rows = []
    for worker in worker_results:
        for variant in worker["variants"]:
            gate = _gate(variant["account"]["rows"], vehicle=vehicle,
                         mode=worker["mode"], min_trades=min_trades,
                         min_sessions=min_sessions, alpha=alpha)
            variant_rows.append((worker, variant, gate))
    correction = benjamini_hochberg(
        {variant["variant_id"]: gate["p_raw"] for _, variant, gate in variant_rows},
        alpha=alpha)
    for _, variant, gate in variant_rows:
        family = correction[variant["variant_id"]]
        gate["multiple_tests"] = {**family, "method": "benjamini_hochberg"}
        gate["passes"] = bool(gate["passes_without_family"] and family["significant"])
        gate["confidence"] = 1.0 - float(family["p_adjusted"])

    cycle_id = uuid.uuid4().hex
    summaries = []
    replacements = []
    for worker in worker_results:
        hypothesis = worker["hypothesis"]
        local = [(variant, gate) for owner, variant, gate in variant_rows
                 if owner is worker]
        adequate = [item for item in local
                    if item[1]["sample_adequate"] and
                    item[1]["heldout_sample_adequate"]]
        passing = [item for item in local if item[1]["passes"]]
        for variant, gate in local:
            result = {**variant, "evaluation_start": worker["evaluation_start"],
                      "evaluation_end": worker["evaluation_end"], "mode": worker["mode"],
                      "gate": gate}
            factory.add_account(cycle_id, hypothesis["hypothesis_id"], result)
            config = {"strategy": {"id": "rule", "version": "v1",
                                     "variant_id": variant["variant_id"],
                                     "rule_spec": variant["rule_spec"]}}
            candidate = edge.register_candidate(
                variant["variant_id"], strategy_id="rule", vehicle=vehicle,
                base_version="v1", hypothesis=hypothesis["thesis"], config=config,
                axes={"hypothesis_id": hypothesis["hypothesis_id"],
                      "slot": hypothesis["slot"], "generation": hypothesis["generation"],
                      "diagnostic": variant["diagnostic"],
                      "simulated_account_id": variant["account"]["account_id"]},
                dataset=raw_rows, code=Path(__file__),
                provenance={"factory": FACTORY_SCHEMA, "mode": worker["mode"],
                            "worker_pid": variant["worker_pid"]})
            run = None
            if gate["sample_adequate"] and gate["heldout_sample_adequate"]:
                fit, held = ([], variant["account"]["rows"]) if worker["mode"] == "shadow" else \
                    chronological_split(variant["account"]["rows"], fit_fraction=.7)
                run = edge.append_run(
                    candidate["candidate_id"], lane=worker["mode"], vehicle=vehicle,
                    dataset=raw_rows, config=config, code=Path(__file__),
                    provenance={"factory": FACTORY_SCHEMA,
                                "simulated_account_id": variant["account"]["account_id"]},
                    fit=fit, heldout=held,
                    metrics={"gate": gate, "account": {k: v for k, v in variant["account"].items()
                                                       if k != "rows"},
                             "diagnostic": variant["diagnostic"],
                             "confidence": gate["confidence"],
                             "heldout_ci_low": gate["heldout_net_pnl"],
                             "max_drawdown": gate["max_drawdown"]})
                for trade in variant["account"]["rows"]:
                    edge.append_trade(run["run_id"], trade)
                edge.append_evidence(candidate["candidate_id"], "autonomous_diagnosis", {
                    "fit_diagnosis": worker["diagnostic"],
                    "variant_diagnosis": variant["diagnostic"], "gate": gate,
                }, run_id=run["run_id"])
                current = edge.candidate(candidate["candidate_id"])["status"]
                if gate["passes"] and worker["mode"] == "backtest" and current == "candidate":
                    edge.transition(candidate["candidate_id"], "backtest_passed",
                                    reason="autonomous held-out gate passed")
                elif gate["passes"] and worker["mode"] == "shadow":
                    if current == "backtest_passed":
                        edge.transition(candidate["candidate_id"], "shadow",
                                        reason="later unseen simulated paper gate passed")
                        current = "shadow"
                    if current == "shadow":
                        edge.transition(candidate["candidate_id"], "validated",
                                        reason="backtest and forward simulated paper gates passed")
                elif not gate["passes"] and current in {"candidate", "backtest_passed", "shadow"}:
                    edge.transition(candidate["candidate_id"], "retired",
                                    reason="adequately powered autonomous gate failed")
            summaries.append({
                "hypothesis_id": hypothesis["hypothesis_id"],
                "variant_id": variant["variant_id"], "mode": worker["mode"],
                "evaluation_start": worker["evaluation_start"],
                "evaluation_end": worker["evaluation_end"],
                "account_id": variant["account"]["account_id"],
                "worker_pid": variant["worker_pid"], "gate": gate,
                "status": (edge.candidate(candidate["candidate_id"]) or {}).get("status"),
                "run_id": run.get("run_id") if run else None,
            })
        if passing:
            new_state = "validated" if worker["mode"] == "shadow" else "backtest_passed"
            factory.event(hypothesis["hypothesis_id"], new_state,
                          f"{len(passing)} autonomous variant(s) passed {worker['mode']}",
                          {"passing": [item[0]["variant_id"] for item in passing]})
        elif any(variant["variant_id"] == rule_variant_id(hypothesis["rule_spec"])
                 for variant, _gate_result in adequate):
            aggregate = max((item[0]["diagnostic"] for item in local),
                            key=lambda value: abs(float(value.get("net_pnl", 0.0))))
            factory.event(hypothesis["hypothesis_id"], "retired",
                          "all adequately powered variants failed to prove positive edge",
                          {"diagnostic": aggregate})
            replacement = replacement_hypothesis(
                hypothesis, aggregate, max_generations=max_generations,
                not_before=worker["evaluation_end"])
            if replacement is not None:
                factory.register(replacement)
                replacements.append(asdict(replacement))
        else:
            factory.event(hypothesis["hypothesis_id"], hypothesis.get("status", "queued"),
                          "sample floor not met; observations were not treated as failure",
                          {"evaluated_variants": len(local), "adequate_variants": len(adequate)})

    validated = [row for row in summaries if row["status"] in {"validated", "champion"}]
    champion = None
    if validated:
        champion = edge.select_champion(vehicle=vehicle, min_confidence=1.0 - alpha,
                                        strategy_id="rule")
    result = {
        "schema": FACTORY_SCHEMA, "status": "complete", "cycle_id": cycle_id,
        "dataset_hash": dataset_hash, "vehicle": vehicle,
        "parallel_workers": max_workers,
        "parallel_backend": backend,
        "worker_pids": sorted({row["worker_pid"] for row in summaries}),
        "strategies": len(worker_results), "variants": len(summaries),
        "accounts": len(summaries), "results": summaries,
        "replacements": replacements,
        "champion": ({key: champion.get(key) for key in
                      ("candidate_id", "variant_id", "strategy_id", "vehicle", "status")}
                     if champion else None),
    }
    factory.add_cycle(cycle_id, dataset_hash, vehicle, max_workers,
                      len(worker_results), len(summaries), result)
    return result


def factory_status(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    return FactoryLedger(db_path).status()


__all__ = [
    "DEFAULT_STRATEGIES", "DEFAULT_VARIANTS", "DEFAULT_WORKERS", "FactoryError",
    "FactoryLedger", "StrategyHypothesis", "diagnose", "factory_status",
    "initial_hypotheses", "mutate_from_diagnosis", "replacement_hypothesis",
    "run_factory", "simulate_account",
]
