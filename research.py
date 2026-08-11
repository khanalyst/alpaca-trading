#!/usr/bin/env python3
"""Offline validation, replay, edge discovery, and autonomous strategy research."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.calibration import json_report as calibration_report
from research.costs import (CostModel, DEFAULT_FEE_BPS, DEFAULT_SLIPPAGE_BPS,
                            DEFAULT_SPREAD_BPS)
from research.ibr import IBRConfig, replay_ibr, replay_ibr_vehicles
from research.edge_lab import (EdgeLedger, DEFAULT_DB_PATH, discover,
                               init_ledger)
from research.market_data import (
    NormalizationError,
    normalize_option_snapshot,
    normalize_quote,
    normalize_underlying_bar,
    parse_timestamp,
)
from research.proof import write_proof
from research.strategy_factory import factory_status, run_factory
from agent.config import load_config as load_agent_config


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
        costs=CostModel(spread_bps=args.spread_bps,
                        slippage_bps=args.slippage_bps,
                        fee_bps=args.fee_bps),
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
    parser.add_argument("--spread-bps", type=float, default=DEFAULT_SPREAD_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--force-flat", type=_time, default=_time("15:55"))


def _quotes(args: argparse.Namespace):
    """Load the optional executable-quote view replay prices boundaries from."""
    path = getattr(args, "quotes", None)
    if not path:
        return None
    quotes = []
    for number, row in enumerate(_json_rows(path), 1):
        try:
            quotes.append(normalize_quote(
                row, provider=args.provider, feed=args.feed))
        except (NormalizationError, ValueError) as exc:
            raise SystemExit(f"quote row {number}: {exc}") from exc
    return quotes or None


def cmd_backtest_ibr(args: argparse.Namespace) -> int:
    bars = _bars(args)
    quotes = _quotes(args)
    cfg = _config(args)
    if args.vehicle == "both":
        result = replay_ibr_vehicles(bars, config=cfg, quotes=quotes,
                                     vehicles=("equity", "option"))
        print(json.dumps({key: value.summary() for key, value in result.items()},
                         sort_keys=True))
    else:
        result = replay_ibr(bars, config=cfg, vehicle=args.vehicle,
                            symbol=args.symbol, quotes=quotes)
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


def _agent_config(args: argparse.Namespace) -> dict:
    path = (getattr(args, "agent_config", None) or
            os.getenv("ALPACA_AGENT_CONFIG") or REPO / "config.yaml")
    return load_agent_config(path)


def _dataset_context(path: str | Path) -> dict[str, Any]:
    """Summarize point-in-time source identity without copying raw rows."""
    if str(path) == "-":
        return {}
    source = Path(path)
    if not source.is_file():
        return {}
    providers: set[str] = set()
    feeds: set[str] = set()
    kinds: set[str] = set()
    latest: datetime | None = None
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return {}
            if not isinstance(row, dict):
                continue
            if row.get("provider"):
                providers.add(str(row["provider"]))
            if row.get("feed"):
                feeds.add(str(row["feed"]))
            kinds.add(str(row.get("kind") or "bar").lower())
            raw_time = row.get("as_of") or row.get("observed_at") or row.get("timestamp")
            if raw_time is None:
                continue
            try:
                timestamp = parse_timestamp(raw_time)
            except (NormalizationError, ValueError):
                continue
            if latest is None or timestamp > latest:
                latest = timestamp
    result: dict[str, Any] = {
        "provider": ",".join(sorted(providers)) or None,
        "feed": ",".join(sorted(feeds)) or None,
        "schema": "normalized-market.v1:" + ",".join(sorted(kinds)),
    }
    if latest is not None:
        result.update({
            "as_of": latest.isoformat(),
            "session_timestamp": latest.isoformat(),
            "session_date": latest.astimezone(
                ZoneInfo("America/New_York")).date().isoformat(),
        })
    return result


def _emit_proofs(args: argparse.Namespace, result: dict,
                 config: dict) -> list[dict[str, Any]]:
    research = dict(config.get("research") or {})
    proof_cfg = dict(research.get("proof") or {})
    root = Path(os.getenv("ALPACA_RESEARCH_PROOF_DIR") or
                proof_cfg.get("directory") or "research/results/edges")
    if not root.is_absolute():
        root = REPO / root
    webhook = (os.getenv("ALPACA_EDGE_WEBHOOK_URL") or
               proof_cfg.get("webhook_url") or "")
    timeout = float(proof_cfg.get("webhook_timeout_seconds", 10))
    context = _dataset_context(getattr(args, "data", "-"))
    ledger = EdgeLedger(_db(args))
    emitted: list[dict[str, Any]] = []
    for candidate in ledger.status(vehicle=getattr(args, "vehicle", None)):
        if candidate.get("status") not in {"validated", "champion"}:
            continue
        eligibility = ledger.eligibility(candidate["candidate_id"])
        if not eligibility.get("eligible"):
            continue
        proof = write_proof(
            ledger, candidate["candidate_id"], context=context,
            output_root=root, webhook_url=str(webhook) or None,
            webhook_timeout_seconds=timeout)
        item = {
            "candidate_id": candidate["candidate_id"],
            "status": candidate["status"], "vehicle": candidate["vehicle"],
            "payload_hash": proof.payload_hash, "artifact": str(proof.path),
            "created": proof.created, "webhook": proof.webhook,
        }
        emitted.append(item)
        if proof.created:
            ledger.append_event(
                candidate_id=candidate["candidate_id"],
                event_type="proof_created", actor="research_cli",
                reason="content-addressed edge proof created",
                payload={"payload_hash": proof.payload_hash,
                         "artifact": str(proof.path)})
            if proof.webhook is not None:
                sent = bool(proof.webhook.get("ok"))
                ledger.append_event(
                    candidate_id=candidate["candidate_id"],
                    event_type="proof_webhook_sent" if sent else "proof_webhook_failed",
                    actor="research_cli",
                    reason=("edge proof notification sent" if sent else
                            "edge proof notification failed; artifact remains durable"),
                    payload={"payload_hash": proof.payload_hash,
                             "result": proof.webhook})
    result["proofs"] = emitted
    return emitted


def cmd_edge_init(args: argparse.Namespace) -> int:
    print(json.dumps(init_ledger(_db(args)), sort_keys=True))
    return 0


def cmd_edge_status(args: argparse.Namespace) -> int:
    print(json.dumps(EdgeLedger(_db(args)).status(vehicle=args.vehicle), sort_keys=True))
    return 0


def cmd_edge_promote(args: argparse.Namespace) -> int:
    ledger = EdgeLedger(_db(args))
    record = ledger.transition(args.candidate, args.status, reason=args.reason,
                               actor=args.actor)
    output = {"candidate": record}
    if record.get("status") in {"validated", "champion"}:
        _emit_proofs(args, output, _agent_config(args))
    print(json.dumps(output, sort_keys=True))
    return 0


def cmd_vehicles(args: argparse.Namespace) -> int:
    """Print the vehicles this deployment should research, one per line.

    The research cycle asks this rather than studying both vehicles
    unconditionally: a trader runs one execution profile, so evidence in the
    other vehicle can never be deployed and only accumulates unusable proofs.
    """
    from agent.edge import research_vehicles, runtime_vehicle

    config = _agent_config(args)
    override = (args.vehicles if getattr(args, "vehicles", None) is not None
                else os.getenv("ALPACA_RESEARCH_VEHICLES"))
    try:
        selected = research_vehicles(config, override)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if getattr(args, "json", False):
        print(json.dumps({"schema": "research-vehicles.v1",
                          "vehicles": selected,
                          "runtime_vehicle": runtime_vehicle(config),
                          "override": override or None}, sort_keys=True))
    else:
        for vehicle in selected:
            print(vehicle)
    return 0 if selected else 2


def cmd_edge_paper(args: argparse.Namespace) -> int:
    """Report how each edge is actually doing on live paper outcomes."""
    ledger = EdgeLedger(_db(args))
    if getattr(args, "candidate", None):
        report = [ledger.paper_performance(args.candidate)]
    else:
        report = ledger.paper_report(vehicle=args.vehicle,
                                     deployed_only=bool(args.deployed))
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


def cmd_edge_ingest(args: argparse.Namespace) -> int:
    ledger = EdgeLedger(_db(args))
    outcome = _read_json(args.outcome, {})
    if not isinstance(outcome, dict):
        raise ValueError("outcome JSON must be an object")
    print(json.dumps(ledger.ingest_paper_outcome(args.candidate, outcome), sort_keys=True))
    return 0


def cmd_edge_discover(args: argparse.Namespace) -> int:
    agent_config = _agent_config(args)
    config = _read_json(args.config, {})
    if not isinstance(config, dict):
        raise ValueError("--config JSON must be an object")
    result = discover(
        args.data, db_path=_db(args), vehicle=args.vehicle, lane=args.lane,
        config=config, variants_path=args.variants,
        min_trades=args.min_trades, min_sessions=args.min_sessions,
        alpha=args.alpha)
    _emit_proofs(args, result, agent_config)
    print(json.dumps(result, sort_keys=True, default=str))
    promoted = any(item.get("status") in {"validated", "champion"}
                   for item in result.get("variants", []))
    return 0 if promoted else 2


def cmd_factory_run(args: argparse.Namespace) -> int:
    agent_config = _agent_config(args)
    config = _read_json(getattr(args, "config", None), {})
    if not isinstance(config, dict):
        raise ValueError("--config JSON must be an object")
    result = run_factory(
        args.data, db_path=_db(args), vehicle=args.vehicle,
        strategies=args.strategies, variants_per_strategy=args.variants,
        workers=args.workers, starting_cash=args.starting_cash,
        min_trades=args.min_trades, min_sessions=args.min_sessions,
        alpha=args.alpha, max_generations=args.max_generations,
        costs=CostModel.from_config(config),
        strategy_llm=(agent_config.get("research") or {}).get("strategy_llm"))
    proofs = _emit_proofs(args, result, agent_config)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if proofs else 2


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Score the shared cost model against the runtime's recorded fills."""
    import sqlite3
    from contextlib import closing
    journal = Path(args.journal)
    if not journal.is_file():
        raise SystemExit(f"journal not found: {journal}")
    config = _read_json(getattr(args, "config", None), {})
    if not isinstance(config, dict):
        raise ValueError("--config JSON must be an object")
    with closing(sqlite3.connect(journal)) as db:
        report = calibration_report(db, CostModel.from_config(config))
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 2 if report["verdict"] == "optimistic" else 0


def cmd_factory_status(args: argparse.Namespace) -> int:
    print(json.dumps(factory_status(_db(args)), sort_keys=True, default=str))
    return 0


def _factory_parser(sub: argparse._SubParsersAction, name: str, command: str):
    parser = sub.add_parser(name, help=f"autonomous strategy factory {command}")
    parser.add_argument("--db", default=None)
    if command == "status":
        parser.set_defaults(func=cmd_factory_status)
    else:
        parser.add_argument("--data", required=True, help="normalized mixed market JSONL")
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
        parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
        parser.add_argument("--strategies", type=int, default=7)
        parser.add_argument("--variants", type=int, default=4,
                            help="isolated variants/accounts per strategy")
        parser.add_argument("--workers", type=int, default=7)
        parser.add_argument("--starting-cash", type=float, default=100000.0)
        parser.add_argument("--min-trades", type=int, default=100)
        parser.add_argument("--min-sessions", type=int, default=10)
        parser.add_argument("--alpha", type=float, default=.05)
        parser.add_argument("--max-generations", type=int, default=5)
        parser.add_argument("--config", default=None,
                            help="optional base runtime config JSON")
        parser.set_defaults(func=cmd_factory_run)
    return parser


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
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
        parser.set_defaults(func=cmd_edge_promote)
    elif command == "ingest":
        parser.add_argument("candidate")
        parser.add_argument("outcome")
        parser.set_defaults(func=cmd_edge_ingest)
    elif command == "paper":
        parser.add_argument("candidate", nargs="?", default=None,
                            help="one candidate id (default: every candidate)")
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.add_argument("--deployed", action="store_true",
                            help="only validated/champion edges")
        parser.set_defaults(func=cmd_edge_paper)
    elif command == "discover":
        parser.add_argument("--data", required=True,
                            help="normalized market JSONL corpus")
        parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
        parser.add_argument("--lane", choices=("auto", "backtest", "shadow"), default="auto")
        parser.add_argument("--config", help="optional base runtime config JSON")
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
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
    ibr.add_argument("--quotes", default=None,
                     help="normalized quote JSONL used for boundary fills")
    ibr.add_argument("--provider", default=None)
    ibr.add_argument("--feed", default=None)
    ibr.add_argument("--vehicle", choices=("equity", "option", "both"), default="equity")
    _add_cost_flags(ibr)
    ibr.set_defaults(func=cmd_backtest_ibr)
    # Both ``edge init`` and flat ``edge-init`` forms are accepted so cron
    # jobs can stay terse while operators retain a discoverable command tree.
    edge = sub.add_parser("edge", help="auditable edge discovery ledger")
    edge_sub = edge.add_subparsers(dest="edge_command", required=True)
    for name in ("init", "status", "promote", "ingest", "paper", "discover"):
        _edge_parser(edge_sub, name, name)
    for name in ("edge-init", "edge-status", "edge-promote", "edge-ingest",
                 "edge-paper", "edge-discover"):
        _edge_parser(sub, name, name.split("-", 1)[1])
    vehicles = sub.add_parser(
        "vehicles", help="print the vehicles this deployment should research")
    vehicles.add_argument("--agent-config", default=None,
                          help="validated agent config (default: config.yaml)")
    vehicles.add_argument("--vehicles", default=None,
                          help="override: 'all' or a comma-separated subset "
                               "(default: ALPACA_RESEARCH_VEHICLES, else the "
                               "configured execution profile)")
    vehicles.add_argument("--json", action="store_true")
    vehicles.set_defaults(func=cmd_vehicles)
    calibrate = sub.add_parser(
        "calibrate", help="compare modelled fill costs against journaled fills")
    calibrate.add_argument("journal")
    calibrate.add_argument("--config", default=None)
    calibrate.set_defaults(func=cmd_calibrate)
    factory = sub.add_parser("factory", help="parallel autonomous strategy factory")
    factory_sub = factory.add_subparsers(dest="factory_command", required=True)
    for name in ("run", "status"):
        _factory_parser(factory_sub, name, name)
    for name in ("factory-run", "factory-status"):
        _factory_parser(sub, name, name.split("-", 1)[1])
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
