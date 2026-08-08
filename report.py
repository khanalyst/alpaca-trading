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
import math
import numbers
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent


# Live and replay reports share one expectancy implementation.
from research.score import match_round_trips  # noqa: E402,F401


def configured_default_db() -> Path:
    """Use the same demo/live runtime scope selected by config.yaml."""
    try:
        mode = str(
            (yaml.safe_load((ROOT / "config.yaml").read_text()) or {})
            .get("mode"))
    except Exception:
        mode = "_unconfigured"
    if mode not in {"demo", "live"}:
        mode = "_unconfigured"
    return ROOT / "runtime" / mode / "journal.db"


DEFAULT_DB = configured_default_db()
TRADE_FIELDS = (
    "ts", "symbol", "side", "action", "qty", "price", "notional",
    "leverage", "reason", "confidence", "pnl_pct", "trade_id", "order_id",
    "fee_usd", "funding_usd", "realized_pnl_usd", "risk_usd",
    "fill_status", "slippage_usd", "adverse_slippage_usd",
    "funding_status", "pnl_semantics", "strategy_id", "strategy_version",
    "setup_id", "setup_key", "setup_type", "signal_ts", "exit_policy",
    "invalidation_anchor", "run_id", "cycle_id", "prompt_version",
    "config_version", "code_version", "equity_basis_id",
    "entry_equity_usd", "close_trigger", "close_evidence",
    "runtime_mode", "account_fingerprint", "variant_id",
    "strategy_config_version",
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


def _json_safe(value: object) -> object:
    """Return standards-compliant report data, using null for infinities."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _strategy_contract_identity(trade: dict) -> dict:
    """Resolve the canonical executable contract for one report row.

    The live journal predates contract columns, so identity is derived from
    the registered strategy and explicit immutable variant when possible.
    Legacy/parameter-only rows remain visible but are marked quarantined
    rather than being presented as a canonical contract.
    """
    strategy_id = str(trade.get("strategy_id") or "").strip()
    requested_variant = str(trade.get("variant_id") or "").strip()
    unknown = {
        "strategy_contract_hash": None,
        "strategy_contract_variant_id": None,
        "strategy_contract_model_id": None,
        "strategy_contract_status": "UNKNOWN",
        "strategy_contract_exclusion_reason": (
            "legacy row has no strategy identity"),
    }
    if not strategy_id:
        return unknown
    if requested_variant in {"", "live", "legacy", "legacy_baseline"}:
        contract_variant = None
    else:
        contract_variant = requested_variant
    try:
        from agent import registry as strategy_registry
        contract = (strategy_registry.contract_for(strategy_id)
                    if contract_variant is None else
                    strategy_registry.contract_for_variant(
                        strategy_id, contract_variant))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return {
            **unknown,
            "strategy_contract_status": "QUARANTINED",
            "strategy_contract_exclusion_reason": (
                f"no canonical StrategyContract for {strategy_id}"
                + (f" variant {requested_variant}" if requested_variant
                   else "")
                + f": {exc}"),
        }
    persisted_version = str(trade.get("strategy_version") or "").strip()
    # ``match_round_trips`` uses ``legacy`` when an old journal row has no
    # version at all.  That sentinel is absence, not a claimed executable
    # version; any other persisted value must agree with the canonical
    # contract or the row is not safe to label as current.
    if (persisted_version and persisted_version != "legacy"
            and persisted_version != contract.version):
        return {
            "strategy_contract_hash": contract.semantic_hash,
            "strategy_contract_variant_id": contract.variant_id,
            "strategy_contract_model_id": contract.outcome_model.model_id,
            "strategy_contract_status": "QUARANTINED",
            "strategy_contract_exclusion_reason": (
                f"strategy version {persisted_version!r} does not match "
                f"canonical StrategyContract version {contract.version!r}"),
        }
    return {
        "strategy_contract_hash": contract.semantic_hash,
        "strategy_contract_variant_id": contract.variant_id,
        "strategy_contract_model_id": contract.outcome_model.model_id,
        "strategy_contract_status": "VERIFIED",
        "strategy_contract_exclusion_reason": None,
    }


def _annotate_strategy_contracts(trades: list[dict]) -> dict:
    """Attach contract identity and return compact report diagnostics."""
    reasons = defaultdict(int)
    verified = quarantined = 0
    for trade in trades:
        identity = _strategy_contract_identity(trade)
        trade.update(identity)
        if identity["strategy_contract_status"] == "VERIFIED":
            verified += 1
        else:
            quarantined += 1
            reasons[identity["strategy_contract_exclusion_reason"]] += 1
    return {
        "strategy_contract_verified": verified,
        "strategy_contract_quarantined": quarantined,
        "strategy_contract_quarantine_reasons": dict(sorted(reasons.items())),
    }


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
    section("EQUITY BY VALUATION BASIS (TRANSFER ADJUSTED)")
    if not _columns(db, "equity"):
        print("  no equity table yet")
        return
    columns = _columns(db, "equity")
    basis_expr = (
        "basis_id" if "basis_id" in columns else "NULL AS basis_id")
    rows = db.execute(
        f"SELECT ts, equity, {basis_expr} FROM equity ORDER BY ts"
    ).fetchall()
    if not rows:
        print("  no equity snapshots yet")
        return
    segments: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for ts, value, basis_id in rows:
        key = str(basis_id) if basis_id else "legacy_unsegmented"
        segments[key].append((float(ts), float(value)))
    ordered = sorted(segments.items(), key=lambda item: item[1][0][0])
    if len(ordered) > 1:
        print("  Returns are intentionally not chained across valuation bases.")
    for basis_id, equity in ordered:
        first_ts, last_ts = equity[0][0], equity[-1][0]
        segment_transfers = [
            (ts, net) for ts, net in transfers
            if first_ts < ts <= last_ts
        ]
        adjusted, net_flow = adjusted_equity_curve(
            equity, segment_transfers)
        raw = curve_stats(equity)
        clean = curve_stats(adjusted)
        print(f"\n  basis {basis_id}")
        if basis_id == "legacy_unsegmented":
            print("    warning: valuation basis is unknown; do not compare this "
                  "segment with later USDT-only equity")
        print(f"    {fmt_ts(first_ts)} -> {fmt_ts(last_ts)} UTC "
              f"({len(equity)} snapshots)")
        print(f"    raw account:      {raw['first']:,.2f} -> "
              f"{raw['last']:,.2f} USDT ({raw['return_pct']:+.2f}%)")
        print(f"    external net flow:{net_flow:>+13,.2f} USDT")
        print(f"    adjusted equity:  {clean['first']:,.2f} -> "
              f"{clean['last']:,.2f} USDT ({clean['return_pct']:+.2f}%)")
        print(f"    adjusted peak {clean['peak']:,.2f}   "
              f"max drawdown {clean['max_drawdown_pct']:.2f}%")


def print_trades(trades: list[dict], diagnostics: dict) -> None:
    from research.score import verified_round_trips

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
    if diagnostics.get("legacy_pnl_semantics"):
        print(f"  warning: {diagnostics['legacy_pnl_semantics']} matched trade(s) "
              "use legacy cumulative-PnL semantics")
    if diagnostics.get("funding_incomplete"):
        print(f"  warning: funding was unavailable or unverified for "
              f"{diagnostics['funding_incomplete']} matched trade(s)")
    contract_rows = {
        (
            row.get("strategy_contract_status"),
            row.get("strategy_contract_variant_id"),
            row.get("strategy_contract_model_id"),
            row.get("strategy_contract_hash"),
            row.get("strategy_contract_exclusion_reason"),
        )
        for row in trades if row.get("strategy_contract_status")
    }
    for status, variant, model, contract_hash, reason in sorted(
            contract_rows, key=lambda item: tuple(
                "" if value is None else str(value) for value in item)):
        print(f"  StrategyContract {status}: variant={variant or 'UNKNOWN'} "
              f"model={model or 'UNKNOWN'} hash={contract_hash or 'UNKNOWN'}")
        if reason:
            print(f"    quarantine reason: {reason}")
    verified = verified_round_trips(trades)
    if not verified:
        print("  no verified trade-ID-matched round trips yet")
        return

    wins = [trade for trade in verified if trade["net_pnl_usd"] > 0]
    losses = [trade for trade in verified if trade["net_pnl_usd"] <= 0]
    pnl = sum(trade["net_pnl_usd"] for trade in verified)
    notionals = sum(trade["notional_usd"] for trade in verified)
    risk_trades = [trade for trade in verified if trade["risk_usd"] > 0]
    risk = sum(trade["risk_usd"] for trade in risk_trades)
    gross_profit = sum(trade["net_pnl_usd"] for trade in wins)
    gross_loss = abs(sum(trade["net_pnl_usd"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else None
    print(f"  net realized PnL {pnl:+,.2f} USDT   "
          f"win rate {len(wins) / len(verified) * 100:.1f}%   "
          f"expectancy {pnl / len(verified):+,.2f} USDT/trade")
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
    print(f"  recorded costs: fees {sum(t['fees_usd'] for t in verified):,.2f}   "
          f"funding {sum(t['funding_usd'] for t in verified):+,.2f}   "
          f"implementation shortfall "
          f"{sum(t['slippage_usd'] for t in verified):+,.2f}   "
          f"adverse slippage "
          f"{sum(t['adverse_slippage_usd'] for t in verified):,.2f} USDT")


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


def _synthetic_strategy_drawdown(rows: list[dict]) -> tuple[float, float | None]:
    """Return max drawdown in USDT and percent for a virtual strategy ledger."""
    ordered = sorted(rows, key=lambda row: row["close_ts"])
    starts = [
        row["entry_equity_usd"] for row in ordered
        if row.get("entry_equity_usd", 0) > 0
    ]
    capital = starts[0] if starts else 0.0
    cumulative = 0.0
    peak = capital
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for row in ordered:
        cumulative += row["net_pnl_usd"]
        value = capital + cumulative
        peak = max(peak, value)
        drawdown = peak - value
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(
                max_drawdown_pct, drawdown / peak * 100)
    return max_drawdown, max_drawdown_pct if starts else None


def print_per_strategy(trades: list[dict]) -> None:
    section("PER STRATEGY (ISOLATED ATTRIBUTION)")
    if not trades:
        print("  no verified strategy-attributed round trips yet")
        return
    _annotate_strategy_contracts(trades)
    grouped = defaultdict(list)
    for trade in trades:
        grouped[(
            trade["runtime_mode"],
            trade["account_fingerprint"],
            trade["strategy_id"],
            trade["strategy_version"],
            trade["prompt_version"],
            trade["config_version"],
            trade["code_version"],
            trade["variant_id"],
            trade["strategy_config_version"],
            trade["strategy_contract_hash"],
            trade["strategy_contract_variant_id"],
        )].append(trade)
    for (
            runtime_mode, account_fingerprint, strategy_id, version,
            prompt_version, config_version, code_version, variant_id,
            strategy_config_version, contract_hash, contract_variant_id
    ), rows in sorted(
            grouped.items(),
            key=lambda item: tuple("" if value is None else str(value)
                                   for value in item[0])):
        pnl = sum(row["net_pnl_usd"] for row in rows)
        wins = [row for row in rows if row["net_pnl_usd"] > 0]
        losses = [row for row in rows if row["net_pnl_usd"] <= 0]
        gross_profit = sum(row["net_pnl_usd"] for row in wins)
        gross_loss = abs(sum(row["net_pnl_usd"] for row in losses))
        factor = gross_profit / gross_loss if gross_loss else None
        risk_rows = [row for row in rows if row["r_multiple"] is not None]
        drawdown_usd, drawdown_pct = _synthetic_strategy_drawdown(rows)
        factor_text = f"{factor:.2f}" if factor is not None else "n/a"
        drawdown_text = (
            f"{drawdown_pct:.2f}%" if drawdown_pct is not None
            else "n/a (legacy entries lack entry equity)")
        avg_r_text = (
            f"{sum(row['r_multiple'] for row in risk_rows) / len(risk_rows):+.2f}"
            if risk_rows else "n/a")
        print(f"\n  {strategy_id} / {version}")
        print(f"    runtime {runtime_mode} / {account_fingerprint}")
        print(f"    variant prompt={prompt_version} config={config_version} "
              f"code={code_version} parameter={variant_id} "
              f"strategy-config={strategy_config_version}")
        contract_status = rows[0]["strategy_contract_status"]
        print(f"    StrategyContract status={contract_status} "
              f"variant={contract_variant_id or 'UNKNOWN'} "
              f"model={rows[0]['strategy_contract_model_id'] or 'UNKNOWN'} "
              f"hash={contract_hash or 'UNKNOWN'}")
        if contract_status != "VERIFIED":
            print("    warning: row is quarantined from canonical "
                  f"contract identity ({rows[0]['strategy_contract_exclusion_reason']})")
        print(f"    trades {len(rows)}   win rate "
              f"{len(wins) / len(rows) * 100:.1f}%   "
              f"net realized {pnl:+,.2f} USDT")
        print(f"    expectancy {pnl / len(rows):+,.2f} USDT/trade   "
              f"profit factor {factor_text}   avg R {avg_r_text}")
        print(f"    synthetic drawdown {drawdown_usd:,.2f} USDT / "
              f"{drawdown_text}")
        print(f"    fees {sum(row['fees_usd'] for row in rows):,.2f}   "
              f"funding {sum(row['funding_usd'] for row in rows):+,.2f}   "
              f"signed shortfall "
              f"{sum(row['slippage_usd'] for row in rows):+,.2f}   "
              f"adverse slippage "
              f"{sum(row['adverse_slippage_usd'] for row in rows):,.2f} USDT")
        incomplete_funding = sum(
            not row["funding_complete"] for row in rows)
        if incomplete_funding:
            print(f"    warning: funding unavailable/unverified for "
                  f"{incomplete_funding} trade(s)")
        setups = defaultdict(list)
        for row in rows:
            setups[row["setup_type"]].append(row)
        for setup_type, setup_rows in sorted(setups.items()):
            setup_pnl = sum(row["net_pnl_usd"] for row in setup_rows)
            print(f"      setup {setup_type:<22} trades "
                  f"{len(setup_rows):>3}   PnL {setup_pnl:+,.2f}")


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
    section("UNIVERSE EXCLUSIONS AND REJECTIONS")
    if not _columns(db, "events"):
        print("  none recorded")
        return
    rows = db.execute(
        "SELECT ts, kind, payload FROM events WHERE kind IN "
        "('rejected','entry_execution_failed','entry_liquidity_rejected',"
        "'universe_selection') ORDER BY ts").fetchall()
    rejection_counts = defaultdict(int)
    exchange_counts = defaultdict(int)
    liquidity_counts = defaultdict(int)
    latest_universe = None
    for ts, kind, payload in rows:
        try:
            record = json.loads(payload)
        except (TypeError, json.JSONDecodeError, AttributeError):
            record = {}
        if kind == "rejected":
            rejection_counts[str(record.get("why", "?"))] += 1
        elif kind == "entry_liquidity_rejected":
            liquidity_counts[str(record.get("reason", "insufficient_depth"))] += 1
        elif kind == "entry_execution_failed":
            diagnostics = record.get("diagnostics") or {}
            result_rows = diagnostics.get("result_rows") or []
            codes = sorted({
                str(item.get("code"))
                for item in result_rows
                if isinstance(item, dict) and item.get("code")
            })
            code = record.get("error_code") or (
                ",".join(codes) if codes else "no-code")
            key = (
                str(record.get("stage") or "?"),
                str(code),
                str(record.get("classification") or "?"),
                str(record.get("error_message") or "?")[:120],
            )
            exchange_counts[key] += 1
        elif kind == "universe_selection":
            latest_universe = (float(ts), record)

    if latest_universe is not None:
        ts, audit = latest_universe
        selected = audit.get("selected") or []
        print(f"  latest universe {fmt_ts(ts)} UTC: "
              f"{', '.join(selected) if selected else 'empty'}")
        exclusions = defaultdict(int)
        for candidate in audit.get("candidates") or []:
            if isinstance(candidate, dict) and not candidate.get("selected"):
                exclusions[str(candidate.get("reason") or "?")] += 1
        for reason, count in sorted(
                exclusions.items(), key=lambda item: (-item[1], item[0])):
            print(f"    excluded {count:>4}  {reason}")

    if rejection_counts:
        print("  deterministic proposal vetoes:")
        for reason, count in sorted(
                rejection_counts.items(), key=lambda item: -item[1]):
            print(f"    {count:>4}  {reason}")
    if liquidity_counts:
        print("  liquidity rejections:")
        for reason, count in sorted(
                liquidity_counts.items(), key=lambda item: -item[1]):
            print(f"    {count:>4}  {reason}")
    if exchange_counts:
        print("  exchange execution failures:")
        for (stage, code, classification, message), count in sorted(
                exchange_counts.items(), key=lambda item: -item[1]):
            print(f"    {count:>4}  stage={stage} code={code} "
                  f"class={classification}  {message}")
    if not (latest_universe or rejection_counts
            or liquidity_counts or exchange_counts):
        print("  none recorded")


def print_transfers(transfers: list[tuple[float, float]]) -> None:
    if not transfers:
        return
    section("TRANSFERS")
    for ts, net in transfers:
        print(f"  {fmt_ts(ts)} UTC   {net:+,.2f} USDT")


def json_report(db: sqlite3.Connection) -> dict:
    """The same numbers the text output shows, machine-readable.

    Added because intention #5 needs results that survive the terminal
    scrollback, and because a human reading a table cannot be diffed against
    last week's table. Everything here is derived from the same
    match_round_trips call the printed report uses, so the two cannot drift.
    """
    from research.score import score_matched_trades

    transfers = load_transfers(db)
    events = load_trade_events(db)
    trades, diagnostics = match_round_trips(events)
    diagnostics.update(_annotate_strategy_contracts(trades))
    grouped: dict = {}
    for trade in trades:
        provenance = (
            trade["runtime_mode"], trade["account_fingerprint"],
            trade["strategy_id"], trade["strategy_version"],
            trade["prompt_version"], trade["config_version"],
            trade["code_version"], trade["variant_id"],
            trade["strategy_config_version"],
            trade["strategy_contract_hash"],
            trade["strategy_contract_variant_id"],
            trade["strategy_contract_model_id"],
            trade["strategy_contract_status"],
            trade["strategy_contract_exclusion_reason"],
        )
        grouped.setdefault(provenance, []).append(trade)

    groups = []
    fields = (
        "runtime_mode", "account_fingerprint", "strategy_id",
        "strategy_version", "prompt_version", "config_version",
        "code_version", "variant_id", "strategy_config_version",
        "strategy_contract_hash", "strategy_contract_variant_id",
        "strategy_contract_model_id", "strategy_contract_status",
        "strategy_contract_exclusion_reason",
    )
    for provenance, rows in sorted(
            grouped.items(),
            key=lambda item: tuple("" if value is None else str(value)
                                   for value in item[0])):
        identity = dict(zip(fields, provenance))
        label = "/".join(str(identity[name]) for name in fields)
        groups.append({
            "provenance": identity,
            "score": score_matched_trades(rows, label=label),
        })

    return {
        "schema": 2,
        "round_trips": len(trades),
        "diagnostics": diagnostics,
        "overall": score_matched_trades(trades, label="all"),
        "groups": groups,
        "transfers": {
            "count": len(transfers),
            "net_usdt": sum(net for _, net in transfers),
        },
    }


def main(path: Path | None = None, as_json: bool = False) -> int:
    argv = [a for a in sys.argv[1:] if a != "--json"]
    as_json = as_json or "--json" in sys.argv
    db_path = path or (Path(argv[0]) if argv else DEFAULT_DB)
    if not db_path.exists():
        print(f"No journal at {db_path} - run the agent first.")
        return 1
    db = sqlite3.connect(db_path)
    if as_json:
        try:
            print(json.dumps(_json_safe(json_report(db)), indent=2,
                             default=str, allow_nan=False))
        finally:
            db.close()
        return 0
    try:
        transfers = load_transfers(db)
        events = load_trade_events(db)
        trades, diagnostics = match_round_trips(events)
        diagnostics.update(_annotate_strategy_contracts(trades))
        from research.score import verified_round_trips
        scored_trades = verified_round_trips(trades)
        print_equity(db, transfers)
        print_trades(trades, diagnostics)
        print_per_strategy(scored_trades)
        print_per_symbol(scored_trades)
        print_calibration(scored_trades)
        print_rejections(db)
        print_transfers(transfers)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
