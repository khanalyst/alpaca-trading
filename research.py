#!/usr/bin/env python3
"""Offline validation, IBR replay, and bounded autonomous edge discovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.ibr import IBRConfig, replay_ibr, replay_ibr_vehicles
from research.edge_lab import (EdgeLedger, DEFAULT_DB_PATH, discover,
                               init_ledger)
from research.market_data import (
    NormalizationError,
    normalize_option_snapshot,
    normalize_quote,
    normalize_underlying_bar,
)


def _json_rows(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source == Path("-"):
        lines = sys.stdin
    else:
        lines = source.open(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    try:
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{source}:{number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"{source}:{number}: expected an object")
            rows.append(value)
    finally:
        if source != Path("-"):
            lines.close()
    return rows


def _config(args: argparse.Namespace) -> IBRConfig:
    return IBRConfig(
        range_minutes=args.range_minutes,
        stop_pct=args.stop_pct,
        target_pct=args.target_pct,
        spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps,
        fee_bps=args.fee_bps,
        force_flat=args.force_flat,
    )


def _time(value: str):
    from datetime import time
    try:
        hour, minute = (int(piece) for piece in value.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("time must be HH:MM") from exc


def cmd_validate_data(args: argparse.Namespace) -> int:
    counts = {"bars": 0, "quotes": 0, "options": 0}
    errors: list[str] = []
    for number, row in enumerate(_json_rows(args.input), 1):
        kind = str(row.get("kind", "bar")).lower()
        try:
            if kind in {"bar", "underlying_bar", "underlying"}:
                normalize_underlying_bar(row, provider=args.provider, feed=args.feed)
                counts["bars"] += 1
            elif kind in {"quote", "quote_snapshot"}:
                normalize_quote(row, provider=args.provider, feed=args.feed)
                counts["quotes"] += 1
            elif kind in {"option", "option_snapshot"}:
                normalize_option_snapshot(row, provider=args.provider, feed=args.feed)
                counts["options"] += 1
            else:
                errors.append(f"row {number}: unsupported kind {kind!r}")
        except (NormalizationError, ValueError) as exc:
            errors.append(f"row {number}: {exc}")
    output = {"counts": counts, "errors": errors, "valid": not errors}
    print(json.dumps(output, sort_keys=True))
    return 0 if not errors else 2


def _bars(args: argparse.Namespace):
    bars = []
    for number, row in enumerate(_json_rows(args.bars), 1):
        try:
            bars.append(normalize_underlying_bar(
                row, provider=args.provider, feed=args.feed))
        except (NormalizationError, ValueError) as exc:
            raise SystemExit(f"bar row {number}: {exc}") from exc
    return bars


def _add_cost_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--range-minutes", type=int, default=30)
    parser.add_argument("--stop-pct", type=float, default=.003)
    parser.add_argument("--target-pct", type=float, default=.006)
    parser.add_argument("--spread-bps", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--fee-bps", type=float, default=.5)
    parser.add_argument("--force-flat", type=_time, default=_time("15:55"))


def cmd_backtest_ibr(args: argparse.Namespace) -> int:
    bars = _bars(args)
    cfg = _config(args)
    if args.vehicle == "both":
        result = replay_ibr_vehicles(bars, config=cfg,
                                     vehicles=("equity", "option"))
        print(json.dumps({key: value.summary() for key, value in result.items()},
                         sort_keys=True))
    else:
        result = replay_ibr(bars, config=cfg, vehicle=args.vehicle,
                            symbol=args.symbol)
        print(json.dumps(result.summary(), sort_keys=True))
    return 0


def _read_json(path: str | Path | None, default):
    if path is None:
        return default
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
        value = json.loads(text)
    except json.JSONDecodeError:
        # Research trade/evidence feeds are commonly JSONL; accept that
        # append-friendly representation as well as one JSON document.
        try:
            value = [json.loads(line) for line in text.splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{source}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"{source}: invalid JSON: {exc}") from exc
    return value


def _db(args):
    return Path(getattr(args, "db", None) or DEFAULT_DB_PATH)


def cmd_edge_init(args: argparse.Namespace) -> int:
    print(json.dumps(init_ledger(_db(args)), sort_keys=True))
    return 0


def cmd_edge_status(args: argparse.Namespace) -> int:
    print(json.dumps(EdgeLedger(_db(args)).status(vehicle=args.vehicle), sort_keys=True))
    return 0


def cmd_edge_promote(args: argparse.Namespace) -> int:
    ledger = EdgeLedger(_db(args))
    record = ledger.transition(args.candidate, args.status, reason=args.reason,
                               actor=args.actor, rollback=args.rollback)
    print(json.dumps(record, sort_keys=True))
    return 0


def cmd_edge_ingest(args: argparse.Namespace) -> int:
    ledger = EdgeLedger(_db(args))
    outcome = _read_json(args.outcome, {})
    if not isinstance(outcome, dict):
        raise ValueError("outcome JSON must be an object")
    print(json.dumps(ledger.ingest_paper_outcome(args.candidate, outcome), sort_keys=True))
    return 0


def cmd_edge_discover(args: argparse.Namespace) -> int:
    config = _read_json(args.config, {})
    if not isinstance(config, dict):
        raise ValueError("--config JSON must be an object")
    result = discover(
        args.data, db_path=_db(args), vehicle=args.vehicle, lane=args.lane,
        config=config, variants_path=args.variants,
        min_trades=args.min_trades, min_sessions=args.min_sessions,
        alpha=args.alpha)
    print(json.dumps(result, sort_keys=True, default=str))
    promoted = any(item.get("status") in {"validated", "champion"}
                   for item in result.get("variants", []))
    return 0 if promoted else 2


def _edge_parser(sub: argparse._SubParsersAction, name: str, command: str):
    parser = sub.add_parser(name, help=f"edge ledger {command}")
    parser.add_argument("--db", default=None)
    if command == "init":
        parser.set_defaults(func=cmd_edge_init)
    elif command == "status":
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.set_defaults(func=cmd_edge_status)
    elif command == "promote":
        parser.add_argument("candidate")
        parser.add_argument("status", choices=("backtest_passed", "shadow", "validated", "champion", "demoted", "retired"))
        parser.add_argument("--reason", required=True)
        parser.add_argument("--actor", default="cli")
        parser.add_argument("--rollback", action="store_true")
        parser.set_defaults(func=cmd_edge_promote)
    elif command == "ingest":
        parser.add_argument("candidate")
        parser.add_argument("outcome")
        parser.set_defaults(func=cmd_edge_ingest)
    elif command == "discover":
        parser.add_argument("--data", required=True,
                            help="normalized market JSONL corpus")
        parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
        parser.add_argument("--lane", choices=("auto", "backtest", "shadow"), default="auto")
        parser.add_argument("--config", help="optional base runtime config JSON")
        parser.add_argument("--variants", help="optional preregistration JSON/YAML path")
        parser.add_argument("--min-trades", type=int, default=100)
        parser.add_argument("--min-sessions", type=int, default=10)
        parser.add_argument("--alpha", type=float, default=.05)
        parser.set_defaults(func=cmd_edge_discover)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-data", help="validate normalized input JSONL")
    validate.add_argument("input", help="JSONL file, or - for stdin")
    validate.add_argument("--provider", default=None)
    validate.add_argument("--feed", default=None)
    validate.set_defaults(func=cmd_validate_data)
    ibr = sub.add_parser("backtest-ibr", help="replay IBR on normalized bars JSONL")
    ibr.add_argument("bars", help="underlying bars JSONL, or - for stdin")
    ibr.add_argument("--symbol", default=None)
    ibr.add_argument("--provider", default=None)
    ibr.add_argument("--feed", default=None)
    ibr.add_argument("--vehicle", choices=("equity", "option", "both"), default="equity")
    _add_cost_flags(ibr)
    ibr.set_defaults(func=cmd_backtest_ibr)
    # Both ``edge init`` and flat ``edge-init`` forms are accepted so cron
    # jobs can stay terse while operators retain a discoverable command tree.
    edge = sub.add_parser("edge", help="auditable edge discovery ledger")
    edge_sub = edge.add_subparsers(dest="edge_command", required=True)
    for name in ("init", "status", "promote", "ingest", "discover"):
        _edge_parser(edge_sub, name, name)
    for name in ("edge-init", "edge-status", "edge-promote", "edge-ingest", "edge-discover"):
        _edge_parser(sub, name, name.split("-", 1)[1])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:
        # Operational callers consume JSON; a non-zero return is the signal
        # to stop a scheduler cycle rather than continue with partial evidence.
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
