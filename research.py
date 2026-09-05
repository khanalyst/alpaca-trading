#!/usr/bin/env python3
"""Offline validation, replay, edge discovery, and autonomous strategy research."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research.calibration import json_report as calibration_report
from research.costs import (CostModel, DEFAULT_FEE_BPS, DEFAULT_SLIPPAGE_BPS,
                            DEFAULT_SPREAD_BPS, ReplayPolicy, SQLiteQuoteIndex,
                            static_cost_config)
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
from research.source_validation import (
    SourceValidationError,
    scan_source,
    source_paths,
    validate_source,
)
from research.gates import (
    PROTOCOL_BACKTEST_MIN_SESSIONS, PROTOCOL_BACKTEST_MIN_TRADES,
    PROTOCOL_SHADOW_MIN_SESSIONS, PROTOCOL_SHADOW_MIN_TRADES,
    unevaluable_reason,
)
from research.proof import write_proof
from research.strategy_factory import (DEFAULT_STRATEGIES, DEFAULT_WORKERS,
                                       factory_status, run_factory)
from research.llm_strategy import RuleProposalAdapter, _safe_error
from research.paper_epoch_cli import add_paper_epoch_parser
from agent.config import load_config as load_agent_config


def _iter_json_rows(path: str | Path):
    """Yield JSONL objects without retaining the complete source in memory."""
    source = Path(path)
    if source == Path("-"):
        sources = [source]
    elif source.is_dir():
        sources = source_paths(source)
    else:
        sources = [source]
    offset = 0
    for current in sources:
        lines = sys.stdin if current == Path("-") else current.open(
            encoding="utf-8")
        try:
            rows_seen = 0
            for number, line in enumerate(lines, offset + 1):
                rows_seen += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(
                        f"{current}:{number}: invalid JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise SystemExit(f"{current}:{number}: expected an object")
                yield value
            offset += rows_seen
        finally:
            if current != Path("-"):
                lines.close()


def _json_rows(path: str | Path) -> list[dict[str, Any]]:
    """Load JSONL rows for the small callers that need random access."""
    return list(_iter_json_rows(path))


@contextmanager
def _replayable_json_source(path: str | Path):
    """Yield a path that can be scanned and normalized in two bounded passes.

    File-backed validation streams the source directly.  Stdin is copied to a
    temporary JSONL spool so provenance validation and normalization consume
    the exact same bytes without retaining the corpus in memory.
    """
    if str(path) != "-":
        yield Path(path)
        return
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".jsonl") as spool:
        for line in sys.stdin:
            spool.write(line)
        spool.flush()
        yield Path(spool.name)


def _validated_row_provenance(row: Mapping[str, Any], *, kind: str,
                              configured_provider: str | None = None,
                              configured_feed: str | None = None,
                              diagnostic_only: bool = False) -> tuple[str, str]:
    """Require provenance to be present on the row before normalizing it.

    CLI provider/feed values describe the configured adapter; they are not
    permission to rewrite an external row's identity.  Equity evidence is
    authorizable only from the exact configured real-time feed (IEX or SIP),
    while delayed SIP remains diagnostic-only and options keep their
    independent OPRA requirement.
    """
    provider_value = row.get("provider")
    feed_value = row.get("feed", row.get("feed_id"))
    provider = "" if provider_value is None else str(provider_value).strip()
    feed = ("" if feed_value is None else
            str(feed_value).strip().lower().replace("-", "_"))
    if feed == "delayed":
        feed = "delayed_sip"
    if not provider:
        raise NormalizationError(f"{kind} row requires explicit provider provenance")
    expected_provider = (None if configured_provider in (None, "")
                         else str(configured_provider).strip())
    if expected_provider and provider != expected_provider:
        raise NormalizationError(
            f"{kind} row provider {provider!r} does not match configured provider")
    if kind in {"option", "option_snapshot", "option_quote"}:
        if feed != "opra":
            raise NormalizationError(
                f"{kind} row feed {feed or '[missing]'!r} is not executable; OPRA is required")
        return provider, feed
    expected_feed = ("iex" if configured_feed in (None, "") else
                     str(configured_feed).strip().lower().replace("-", "_"))
    if expected_feed == "delayed":
        expected_feed = "delayed_sip"
    # ``validate-data`` is the research authorization preflight.  Keep the
    # exact equity requirement explicit even when a caller supplies a
    # delayed-SIP hint for diagnostics.
    allowed_feeds = ({"iex", "sip", "delayed_sip"}
                     if diagnostic_only else {"iex", "sip"})
    if expected_feed not in allowed_feeds:
        raise NormalizationError(
            "equity research requires configured real-time feed (iex or sip); "
            f"{expected_feed or '[missing]'} is diagnostic-only")
    if feed != expected_feed:
        qualifier = ("; delayed_sip is diagnostic-only"
                     if feed == "delayed_sip" else "")
        raise NormalizationError(
            f"{kind} row feed {feed or '[missing]'!r} does not match configured "
            f"executable feed {expected_feed!r}{qualifier}")
    return provider, feed


def _config(args: argparse.Namespace) -> IBRConfig:
    return IBRConfig(
        range_minutes=args.range_minutes,
        stop_pct=args.stop_pct,
        target_pct=args.target_pct,
        costs=CostModel(spread_bps=args.spread_bps,
                        slippage_bps=args.slippage_bps,
                        fee_bps=args.fee_bps),
        policy=ReplayPolicy(strict_market_data=bool(
            getattr(args, "strict_market_data", False)),
            allow_historical_backfill_diagnostics=bool(
                getattr(args, "diagnostic_only", False))),
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
    with _replayable_json_source(args.input) as source:
        try:
            scan = validate_source(
                source,
                diagnostic_only=bool(getattr(args, "diagnostic_only", False)))
            errors: list[str] = []
        except SourceValidationError as exc:
            scan = exc.report
            errors = [f"source: {item}" for item in scan.get("errors", ())]
        for number, row in enumerate(_iter_json_rows(source), 1):
            kind = str(row.get("kind", "bar")).lower()
            supported = {"bar", "underlying_bar", "underlying", "quote",
                         "quote_snapshot", "equity_quote", "underlying_quote",
                         "option", "option_snapshot", "option_quote"}
            if kind not in supported:
                errors.append(f"row {number}: unsupported kind {kind!r}")
                continue
            # Run structural normalization against the row's own metadata first,
            # so diagnostics retain useful symbol/value errors even when a second
            # provenance check also rejects a mismatched or missing-feed row. Missing
            # fields use CLI values only for this diagnostic attempt; a row cannot
            # count as valid unless the explicit check below succeeds.
            raw_provider = row.get("provider")
            raw_feed = row.get("feed", row.get("feed_id"))
            diagnostic_provider = (raw_provider if raw_provider not in (None, "")
                                   else getattr(args, "provider", None))
            diagnostic_feed = (raw_feed if raw_feed not in (None, "")
                               else getattr(args, "feed", None))
            structural_error = None
            try:
                if kind in {"bar", "underlying_bar", "underlying"}:
                    normalize_underlying_bar(row, provider=diagnostic_provider,
                                             feed=diagnostic_feed)
                elif kind in {"quote", "quote_snapshot", "equity_quote", "underlying_quote"}:
                    normalize_quote(row, provider=diagnostic_provider,
                                    feed=diagnostic_feed)
                else:
                    normalize_option_snapshot(row, provider=diagnostic_provider,
                                              feed=diagnostic_feed)
            except (NormalizationError, ValueError) as exc:
                structural_error = exc
                errors.append(f"row {number}: {exc}")
            provenance_error = None
            try:
                provider, feed = _validated_row_provenance(
                    row, kind=kind,
                    configured_provider=getattr(args, "provider", None),
                    configured_feed=getattr(args, "feed", None) or "iex",
                    diagnostic_only=bool(getattr(args, "diagnostic_only", False)))
            except (NormalizationError, ValueError) as exc:
                provenance_error = exc
                errors.append(f"row {number}: {exc}")
            if structural_error is None and provenance_error is None:
                if kind in {"bar", "underlying_bar", "underlying"}:
                    counts["bars"] += 1
                elif kind in {"quote", "quote_snapshot", "equity_quote", "underlying_quote"}:
                    counts["quotes"] += 1
                else:
                    counts["options"] += 1
        # The provenance scan and structural normalization are separate
        # bounded passes.  Recheck the exact ordered content afterwards so a
        # mutable file/directory cannot swap bytes between those passes and
        # receive a validation result for a different corpus snapshot.
        try:
            validate_source(
                source,
                diagnostic_only=bool(getattr(args, "diagnostic_only", False)),
                expected_content_hash=str(scan.get("content_hash")),
            )
        except SourceValidationError as exc:
            for item in exc.report.get("errors", ()):
                message = f"source: {item}"
                if message not in errors:
                    errors.append(message)
    output = {"counts": counts, "errors": errors, "valid": not errors,
              "source_mode_counts": scan.get("source_mode_counts", {}),
              "future_observed_rows": int(scan.get("future_observed_rows") or 0),
              "late_forward_observation_rows": int(
                  scan.get("late_forward_observation_rows") or 0)}
    print(json.dumps(output, sort_keys=True))
    return 0 if not errors else 2


def _bars(args: argparse.Namespace):
    rows = _json_rows(args.bars)
    try:
        validate_source(rows, diagnostic_only=bool(
            getattr(args, "diagnostic_only", False)))
    except SourceValidationError as exc:
        raise SystemExit(str(exc)) from exc
    bars = []
    for number, row in enumerate(rows, 1):
        try:
            provider, feed = _validated_row_provenance(
                row, kind=str(row.get("kind", "bar")).lower(),
                configured_provider=getattr(args, "provider", None),
                configured_feed=getattr(args, "feed", None) or "iex",
                diagnostic_only=bool(getattr(args, "diagnostic_only", False)))
            bars.append(normalize_underlying_bar(
                row, provider=provider, feed=feed))
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
    parser.add_argument("--strict-market-data", action="store_true",
                        help="require executable quotes for every boundary fill")


def _quotes(args: argparse.Namespace):
    """Build a bounded-memory executable-quote view for replay boundaries."""
    path = getattr(args, "quotes", None)
    if not path:
        return None
    source = Path(path)
    diagnostic_only = bool(getattr(args, "diagnostic_only", False))
    source_report = None
    if str(path) == "-":
        rows = _json_rows(path)
        try:
            source_report = validate_source(rows, diagnostic_only=diagnostic_only)
        except SourceValidationError as exc:
            raise SystemExit(str(exc)) from exc
        row_source = enumerate(rows, 1)
    else:
        try:
            source_report = validate_source(source, diagnostic_only=diagnostic_only)
        except SourceValidationError as exc:
            raise SystemExit(str(exc)) from exc

        def file_rows():
            number = 0
            for partition in source_paths(source):
                with partition.open(encoding="utf-8") as handle:
                    for line in handle:
                        number += 1
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"invalid JSON: {exc}") from exc
                        yield number, row

        row_source = file_rows()
    mixed_source = bool(getattr(args, "quotes_from_mixed", False))
    quote_kinds = {"quote", "quote_snapshot", "equity_quote",
                   "underlying_quote"}
    index = SQLiteQuoteIndex()
    try:
        for number, row in row_source:
            try:
                if not isinstance(row, dict):
                    raise ValueError("expected an object")
                kind = str(row.get("kind", "quote")).lower()
                if mixed_source and kind not in quote_kinds:
                    continue
                provider, feed = _validated_row_provenance(
                    row, kind=kind,
                    configured_provider=getattr(args, "provider", None),
                    configured_feed=getattr(args, "feed", None) or "iex",
                    diagnostic_only=diagnostic_only)
                index.add(normalize_quote(
                    row, provider=provider, feed=feed))
            except (json.JSONDecodeError, NormalizationError, ValueError) as exc:
                index.close()
                raise SystemExit(f"{source}:{number}: invalid quote: {exc}") from exc
        if str(path) != "-":
            validate_source(
                source, diagnostic_only=diagnostic_only,
                expected_content_hash=str(source_report.get("content_hash")))
    except (OSError, SourceValidationError) as exc:
        index.close()
        raise SystemExit(f"{source}: unable to read quotes: {exc}") from exc
    if not index:
        index.close()
        return None
    return index


def cmd_backtest_ibr(args: argparse.Namespace) -> int:
    bars = _bars(args)
    quotes = _quotes(args)
    cfg = _config(args)
    try:
        if args.vehicle == "both":
            result = replay_ibr_vehicles(bars, config=cfg, quotes=quotes,
                                         vehicles=("equity", "option"))
            summary: Any = {
                key: value.summary() for key, value in result.items()}
        else:
            result = replay_ibr(bars, config=cfg, vehicle=args.vehicle,
                                symbol=args.symbol, quotes=quotes)
            summary = result.summary()
        if bool(getattr(args, "diagnostic_only", False)):
            modes: dict[str, int] = {}
            for bar in bars:
                mode = str(getattr(
                    getattr(bar, "identity", None), "source_mode",
                    "forward_observed") or "forward_observed")
                modes[mode] = modes.get(mode, 0) + 1
            quote_mode_counts = getattr(quotes, "source_mode_counts", None)
            if callable(quote_mode_counts):
                for mode, count in quote_mode_counts().items():
                    modes[str(mode)] = modes.get(str(mode), 0) + int(count)
            summary = {
                "schema": "ibr-backtest-diagnostic.v1",
                "authorizing": False,
                "diagnostic_only": True,
                "vehicle": args.vehicle,
                "source_mode_counts": dict(sorted(modes.items())),
                "result": summary,
            }
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        if quotes is not None and callable(getattr(quotes, "close", None)):
            quotes.close()


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


def _dataset_context(path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Summarize point-in-time source identity without copying raw rows."""
    if str(path) == "-":
        return {}
    source = Path(path)
    if not source.is_file() and not source.is_dir():
        return {}
    scan = scan_source(path, now=now)
    providers = list(scan.get("providers") or ())
    feeds = list(scan.get("feeds") or ())
    kinds = list(scan.get("kinds") or ())
    result: dict[str, Any] = {
        "provider": ",".join(providers) or None,
        "feed": ",".join(feeds) or None,
        "schema": "normalized-market.v1:" + ",".join(kinds),
        "source_mode_counts": dict(scan.get("source_mode_counts") or {}),
        "future_observed_rows": int(scan.get("future_observed_rows") or 0),
        "late_forward_observation_rows": int(
            scan.get("late_forward_observation_rows") or 0),
        "content_hash": scan.get("content_hash"),
    }
    latest_raw = scan.get("latest_observed_at")
    if latest_raw:
        latest = parse_timestamp(latest_raw)
        result.update({
            "as_of": latest.isoformat(),
            "session_timestamp": latest.isoformat(),
            "session_date": latest.astimezone(
                ZoneInfo("America/New_York")).date().isoformat(),
        })
    return result


_EQUITY_FACTORY_KINDS = frozenset({
    "bar", "bar_1m", "underlying", "underlying_bar",
    "quote", "quote_snapshot", "equity_quote", "underlying_quote",
})
_OPTION_FACTORY_KINDS = frozenset({
    "option", "option_snapshot", "option_quote",
})


def _factory_feed_contract(config: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return the configured factory feed and optional provider identity.

    ``broker.data_feed`` is the canonical runtime setting.  The provider is
    not part of the shipped config (the Alpaca adapter is implied), but keep
    support for an explicit provider field in externally supplied configs so
    a CLI corpus cannot be mixed across adapters when one is declared.
    """
    broker = config.get("broker")
    broker = broker if isinstance(broker, Mapping) else {}
    data = config.get("data")
    data = data if isinstance(data, Mapping) else {}
    feed = (broker.get("data_feed") or data.get("feed") or "iex")
    provider = (broker.get("provider") or data.get("provider") or
                config.get("provider") or os.getenv("ALPACA_DATA_PROVIDER"))
    feed = str(feed).strip().lower().replace("-", "_")
    if feed == "delayed":
        feed = "delayed_sip"
    return feed, (
        str(provider).strip() if provider not in (None, "") else None)


def _factory_dataset_preflight(
        args: argparse.Namespace, config: Mapping[str, Any]) -> dict[str, Any]:
    """Check CLI factory input provenance before invoking ``run_factory``.

    Both the public factory API and this CLI require row-owned provenance for
    authorizing corpora.  Command-line metadata must never relabel a
    differently sourced or unlabelled row as configured evidence.

    The returned context is suitable for a diagnostic result.  File-backed
    factory runs fail closed when the source cannot be inspected; callers that
    intentionally want to study an unavailable/partial source must opt into
    ``--diagnostic-only``.
    """
    source = getattr(args, "data", None)
    diagnostic_only = bool(getattr(args, "diagnostic_only", False))
    if source in (None, "-"):
        raise ValueError(
            "factory data provenance preflight requires a readable JSONL path; "
            "materialize stdin before a factory run")
    path = Path(source)
    if not path.is_file() and not path.is_dir():
        raise ValueError(
            f"factory data provenance preflight cannot read {path}; "
            "use --diagnostic-only for a non-authorizing run")

    try:
        source_scan = validate_source(path, diagnostic_only=diagnostic_only)
    except SourceValidationError as exc:
        raise ValueError(str(exc)) from exc
    context = _dataset_context(path)
    if diagnostic_only:
        return {
            "authorizing": False,
            "diagnostic_only": True,
            "source": context,
            "source_validation": source_scan,
        }

    expected_feed, expected_provider = _factory_feed_contract(config)
    errors: list[str] = list(source_scan.get("errors") or [])
    rows = 0
    equity_rows = 0
    try:
        number = 0
        for partition in source_paths(path):
            with partition.open(encoding="utf-8") as stream:
                for line in stream:
                    number += 1
                    if not line.strip():
                        continue
                    rows += 1
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        errors.append(f"row {number}: invalid JSON: {exc}")
                        continue
                    if not isinstance(row, Mapping):
                        errors.append(f"row {number}: expected an object")
                        continue
                    kind = str(row.get("kind") or "bar").strip().lower()
                    if kind in _EQUITY_FACTORY_KINDS:
                        equity_rows += 1
                        provider = str(row.get("provider") or "").strip()
                        feed = (str(row.get("feed", row.get("feed_id")) or "")
                                .strip().lower().replace("-", "_"))
                        if feed == "delayed":
                            feed = "delayed_sip"
                        if not provider:
                            errors.append(f"row {number}: {kind} requires explicit provider provenance")
                        elif expected_provider and provider != expected_provider:
                            errors.append(
                                f"row {number}: {kind} provider {provider!r} does not match "
                                f"configured provider {expected_provider!r}")
                        if expected_feed not in {"iex", "sip"}:
                            errors.append(
                                "configured equity research feed must be real-time "
                                "(iex or sip); "
                                f"{expected_feed or '[missing]'} is diagnostic-only")
                        elif feed != expected_feed:
                            qualifier = ("; delayed_sip is diagnostic-only"
                                         if feed == "delayed_sip" else "")
                            errors.append(
                                f"row {number}: {kind} feed {feed or '[missing]'!r} "
                                f"does not match configured executable feed "
                                f"{expected_feed!r}{qualifier}")
                    elif kind in _OPTION_FACTORY_KINDS:
                        # A mixed corpus may carry option evidence while an equity
                        # factory lane is running.  Keep that evidence explicit as
                        # well, without allowing OPRA rows to satisfy the equity gate.
                        provider = str(row.get("provider") or "").strip()
                        feed = str(row.get("feed", row.get("feed_id")) or "").strip().lower()
                        if not provider:
                            errors.append(f"row {number}: {kind} requires explicit provider provenance")
                        elif expected_provider and provider != expected_provider:
                            errors.append(
                                f"row {number}: {kind} provider {provider!r} does not match "
                                f"configured provider {expected_provider!r}")
                        if feed != "opra":
                            errors.append(
                                f"row {number}: {kind} feed {feed or '[missing]'!r} "
                                "is not executable; expected 'opra'")
    except OSError as exc:
        raise ValueError(f"factory data preflight failed for {path}: {exc}") from exc

    if not rows:
        errors.append("factory data preflight found no normalized rows")
    if not equity_rows:
        errors.append("factory data preflight found no equity bars or quotes")
    if errors:
        raise ValueError(
            "factory data provenance preflight failed; use --diagnostic-only "
            "for an explicitly non-authorizing run: " + "; ".join(errors[:8]))
    return {"authorizing": True, "diagnostic_only": False,
            "source": context, "source_validation": source_scan}


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


def _pinned(config: dict) -> list[tuple[str, str]]:
    from agent.governance import pinned_variant_ids

    return sorted(pinned_variant_ids(config))


def cmd_edge_trials(args: argparse.Namespace) -> int:
    """Judge each paper-account trial, and park the ones below their floor.

    ``--dry-run`` reports the same verdicts without changing a lifecycle, so an
    operator can see what a review would do before letting it do it.
    """
    from research.trial import review_trials

    config = _agent_config(args)
    result = review_trials(_db(args), config=config, vehicle=args.vehicle,
                           pinned=_pinned(config), apply=not args.dry_run)
    print(json.dumps(result, sort_keys=True, default=str))
    # Parking an edge is a real change worth noticing in a scheduled log; it
    # is not an error, so the non-zero code is reserved for the operator's
    # attention rather than for failure.
    return 3 if result.get("parked") else 0


def cmd_edge_promotable(args: argparse.Namespace) -> int:
    """List edges whose live paper record clears the trial floor.

    This is the hand-off point: it names the variant and its edge, shows what
    it actually returned, and prints the exact configuration block to paste.
    Promotion itself stays a human action.
    """
    from research.trial import promotable_report

    config = _agent_config(args)
    rows = promotable_report(_db(args), config=config, vehicle=args.vehicle,
                             pinned=_pinned(config))
    if getattr(args, "format", "json") == "text":
        if not rows:
            print("No edge has cleared its live paper trial floor yet.")
        for row in rows:
            print(f"\n{'=' * 70}")
            print(f"Variant ID : {row['variant_id']}")
            print(f"Edge/family: {row['family']}  ({row['vehicle']},"
                  f" {row['status']})")
            print(f"Candidate  : {row['candidate_id']}")
            print(f"Live paper : {row['trades']} trades over {row['sessions']}"
                  f" sessions")
            print(f"             total R {row['total_r']:.2f},"
                  f" mean R {row['mean_r']:.3f},"
                  f" win rate {row['win_rate']}")
            print(f"             net P&L {row['net_pnl']},"
                  f" mean return per trade {row['return_pct']}% of risk")
            if row["already_pinned"]:
                print("Already pinned in config.")
            else:
                print("\nTo promote, add to config.yaml:")
                print(row["config_snippet"])
    else:
        print(json.dumps(rows, sort_keys=True, default=str))
    return 0


def cmd_edge_ingest(args: argparse.Namespace) -> int:
    ledger = EdgeLedger(_db(args))
    outcome = _read_json(args.outcome, {})
    if not isinstance(outcome, dict):
        raise ValueError("outcome JSON must be an object")
    print(json.dumps(ledger.ingest_paper_outcome(args.candidate, outcome), sort_keys=True))
    return 0


def cmd_edge_ingest_shadow(args: argparse.Namespace) -> int:
    """Consume complete parity-matched broker-free shadow replays."""
    from research.live_shadow_ingest import ShadowIngestConfig, ingest_shadow

    shadow_db = Path(args.shadow_db or os.getenv("ALPACA_SHADOW_DB") or
                     REPO / "runtime" / "research" / "shadow.sqlite3")
    config = ShadowIngestConfig(
        edge_db=_db(args), shadow_db=shadow_db,
        vehicle=getattr(args, "vehicle", None),
        candidate_id=getattr(args, "candidate", None),
        baseline_candidate_id=getattr(args, "baseline_candidate", None),
        null_candidate_id=getattr(args, "null_candidate", None),
        min_trades=args.min_trades, min_sessions=args.min_sessions,
        alpha=args.alpha)
    print(json.dumps(ingest_shadow(config), sort_keys=True, default=str))
    return 0


# Factory command exit codes are part of the scheduler boundary.  A run that
# priced nothing is not a research verdict, while a bounded search or an
# exhausted LLM provider has a useful, durable diagnosis that must not be
# collapsed into an ordinary no-proof result.  Keep proof/no-proof compatible
# with the historical contract (0/2).
UNEVALUABLE_EXIT = 4
FACTORY_BOUNDED_SPACE_EXHAUSTED_EXIT = 5
FACTORY_LLM_ALL_CALLS_FAILED_EXIT = 6
# Descriptive aliases for deployment callers that name the terminal outcome
# rather than the factory's result-status spelling.
FACTORY_SEARCH_EXHAUSTED_EXIT = FACTORY_BOUNDED_SPACE_EXHAUSTED_EXIT
FACTORY_LLM_PROVIDER_FAILURE_EXIT = FACTORY_LLM_ALL_CALLS_FAILED_EXIT


def _report_unevaluable(result: Mapping, gates: Sequence[Mapping]) -> bool:
    """Print and mark a run whose corpus could not be priced at all."""
    reason = unevaluable_reason(gates)
    if reason is None:
        return False
    result["unevaluable"] = {"schema": "research-unevaluable.v1", "reason": reason}
    return True


def cmd_edge_discover(args: argparse.Namespace) -> int:
    agent_config = _agent_config(args)
    config = _read_json(args.config, {})
    if not isinstance(config, dict):
        raise ValueError("--config JSON must be an object")
    # Discovery must replay the same costs, execution freshness, risk, session
    # calendar and strategy boundaries as the checked runtime.  Historically
    # ``--agent-config`` only supplied the bar-fallback flag, leaving scheduled
    # IBR runs on weaker library defaults even though factory runs were bound
    # to the validated configuration.
    runtime_config = dict(agent_config)
    for block in ("costs", "execution", "risk", "strategy", "session"):
        if block not in config:
            continue
        base = runtime_config.get(block)
        override = config.get(block)
        if isinstance(base, dict) and isinstance(override, dict):
            runtime_config[block] = {**base, **override}
        else:
            runtime_config[block] = override
    diagnostic_only = bool(getattr(args, "diagnostic_only", False))
    result = discover(
        args.data, db_path=_db(args), vehicle=args.vehicle, lane=args.lane,
        config=runtime_config, variants_path=args.variants,
        min_trades=args.min_trades, min_sessions=args.min_sessions,
        alpha=args.alpha,
        backtest_bar_fallback=bool((agent_config.get("research") or {}).get(
            "backtest_bar_fallback", True)),
        diagnostic_only=diagnostic_only)
    if diagnostic_only:
        result["proofs"] = []
    else:
        _emit_proofs(args, result, agent_config)
    stalled = _report_unevaluable(
        result, [item.get("gate") for item in result.get("variants", [])
                 if isinstance(item, Mapping)])
    print(json.dumps(result, sort_keys=True, default=str))
    if stalled:
        return UNEVALUABLE_EXIT
    promoted = any(item.get("status") in {"validated", "champion"}
                   for item in result.get("variants", []))
    return 0 if promoted else 2


def _write_factory_report(args: argparse.Namespace) -> str | None:
    """Archive the discovery narrative where the dashboard will list it."""
    from research.factory_report import DEFAULT_REPORT_ROOT, write_report

    root = Path(os.getenv("ALPACA_RESEARCH_REPORT_DIR") or DEFAULT_REPORT_ROOT)
    if not root.is_absolute():
        root = REPO / root
    try:
        target = write_report(_db(args), vehicle=getattr(args, "vehicle", None),
                              output_root=root)
    except (OSError, ValueError):
        # The narrative is a convenience view over ledgers that are already
        # durable.  Failing to archive it must not fail the research cycle.
        return None
    return str(target) if target is not None else None


def cmd_factory_run(args: argparse.Namespace) -> int:
    agent_config = _agent_config(args)
    preflight = _factory_dataset_preflight(args, agent_config)
    diagnostic_only = bool(preflight.get("diagnostic_only"))
    config = _read_json(getattr(args, "config", None), {})
    if not isinstance(config, dict):
        raise ValueError("--config JSON must be an object")
    # Scheduled jobs pass the validated agent config. Research must replay the
    # same execution, risk, session and strategy boundaries as runtime; an
    # explicit JSON config may override those blocks for a named offline
    # experiment, and the resulting assumptions are hashed into the cycle.
    runtime_config = dict(agent_config)
    for block in ("costs", "execution", "risk", "strategy", "session"):
        if block in config:
            base = runtime_config.get(block)
            override = config.get(block)
            if isinstance(base, dict) and isinstance(override, dict):
                runtime_config[block] = {**base, **override}
            else:
                runtime_config[block] = override
    def _research_progress(phase: str, done: int, total: int) -> None:
        units = {"diagnosing": "hypotheses", "evaluating": "accounts",
                 "aggregating": "candidates", "persisting": "accounts"}
        print(json.dumps({"schema": "research-progress.v1", "phase": phase,
                          "unit": units.get(phase, "steps"),
                          "vehicle": args.vehicle, "done": int(done),
                          "total": int(total),
                          "updated_ts": datetime.now(timezone.utc).isoformat()},
                         separators=(",", ":")), flush=True)

    result = run_factory(
        args.data, db_path=_db(args), vehicle=args.vehicle,
        strategies=args.strategies, variants_per_strategy=args.variants,
        workers=args.workers, starting_cash=args.starting_cash,
        min_trades=args.min_trades, min_sessions=args.min_sessions,
        alpha=args.alpha, max_generations=args.max_generations,
        max_confirmatory_attempts=getattr(args, "max_confirmatory_attempts", 3),
        costs=CostModel.from_config(static_cost_config(runtime_config)),
        runtime_config=runtime_config,
        diagnostic_only=diagnostic_only,
        strategy_llm=(agent_config.get("research") or {}).get("strategy_llm"),
        # A worker projection is itself a strict, authorizing view.  Do not
        # let it turn the explicit diagnostic escape hatch back into a hard
        # provenance failure when the source carries mismatched/missing metadata.
        worker_data=(None if diagnostic_only else
                     getattr(args, "worker_data", None)),
        progress_callback=_research_progress,
        backtest_bar_fallback=bool((agent_config.get("research") or {}).get(
            "backtest_bar_fallback", True)))
    if diagnostic_only:
        # Keep diagnostic output visibly non-authorizing even if an injected
        # library runner happens to return validated candidates.  In
        # particular, proof emission is never reached on this path.
        result["diagnostic_only"] = True
        result["authorizing"] = False
        result["source"] = preflight.get("source") or {}
        result["source_provenance"] = {
            key: result["source"].get(key)
            for key in ("provider", "feed")
            if key in result["source"]
        }
        result["source_provider"] = result["source"].get("provider")
        result["source_feed"] = result["source"].get("feed")
        result["provenance"] = dict(result["source_provenance"])
        result["proofs"] = []
        proofs: list[dict[str, Any]] = []
    else:
        proofs = _emit_proofs(args, result, agent_config)
    # Archive the ledger-backed narrative every authorizing cycle, not only
    # when an edge proves out. Diagnostics-only never opens or reads the
    # authorizing ledger; its bounded reports already live in this result.
    result["report"] = (None if diagnostic_only else
                        _write_factory_report(args))
    stalled = _report_unevaluable(
        result, [item.get("gate") for item in result.get("results", [])
                 if isinstance(item, Mapping)])
    configured = os.getenv("ALPACA_FACTORY_DIAGNOSTIC_REPORT")
    if diagnostic_only and configured:
        if "%s" in configured:
            configured = configured % args.vehicle
        target = Path(configured)
        if not target.is_absolute():
            target = REPO / target
        target.parent.mkdir(parents=True, exist_ok=True)
        result["report"] = str(target)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(result, sort_keys=True, default=str) + "\n",
            encoding="utf-8")
        os.replace(temporary, target)
    print(json.dumps(result, sort_keys=True, default=str))
    if proofs:
        # A proof is the authorizing result.  It takes precedence over a
        # diagnostic status that may describe another slot in the same run.
        return 0
    if diagnostic_only:
        # The explicit diagnostic escape hatch is never an authorizing
        # factory verdict, including when its underlying runner reports a
        # terminal search/provider status.
        return 2
    result_status = str(result.get("status") or "").strip().lower()
    if result_status in {"llm_all_calls_failed", "llm_provider_failure"}:
        return FACTORY_LLM_ALL_CALLS_FAILED_EXIT
    if result_status == "bounded_space_exhausted":
        return FACTORY_BOUNDED_SPACE_EXHAUSTED_EXIT
    if stalled:
        return UNEVALUABLE_EXIT
    return 2


def cmd_llm_preflight(args: argparse.Namespace) -> int:
    """Probe the configured research strategy provider once.

    The adapter owns provider/API details and returns only bounded evidence;
    this command deliberately does not load secret files or persist output.
    """
    try:
        agent_config = _agent_config(args)
    except Exception as exc:  # noqa: BLE001 - preserve the CLI JSON contract
        payload = {
            "schema": "research-llm-preflight.v1", "status": "fatal",
            "reason": f"configuration error: {_safe_error(exc)}",
            "evidence": {},
        }
        print(json.dumps(payload, sort_keys=True))
        return 3
    research_config = agent_config.get("research") or {}
    llm_config = research_config.get("strategy_llm")
    if not isinstance(llm_config, Mapping) or not llm_config.get("enabled"):
        payload = {"schema": "research-llm-preflight.v1", "status": "disabled",
                   "reason": "configuration", "evidence": {}}
        print(json.dumps(payload, sort_keys=True))
        return 0
    try:
        adapter = RuleProposalAdapter(
            provider=str(llm_config.get("provider") or "openai"),
            model=str(llm_config.get("model") or ""),
            deployment=(str(llm_config.get("deployment")).strip()
                        if llm_config.get("deployment") not in (None, "")
                        else None),
            max_attempts=int(llm_config.get("max_attempts", 1)),
            timeout_seconds=float(llm_config.get("timeout_seconds", 30)),
            max_response_bytes=int(llm_config.get("max_response_bytes", 16_384)),
            max_total_calls=int(llm_config.get("max_total_calls", 64)),
        )
    except Exception as exc:  # noqa: BLE001 - preserve the CLI JSON contract
        payload = {
            "schema": "research-llm-preflight.v1", "status": "fatal",
            "reason": f"configuration error: {_safe_error(exc)}",
            "evidence": {},
        }
        print(json.dumps(payload, sort_keys=True))
        return 3
    outcome = adapter.preflight()
    print(json.dumps(outcome.as_dict(), sort_keys=True, default=str))
    if outcome.status == "ready":
        return 0
    if outcome.status == "degraded":
        return 4
    return 3


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
        report = calibration_report(
            db, CostModel.from_config(static_cost_config(config)),
                                    vehicle=getattr(args, "vehicle", None))
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    # The calibration module owns the authorization result.  Preserve its
    # machine-readable exit code so shell callers cannot mistake insufficient
    # or otherwise blocked evidence for success merely because the cost model
    # was not classified as optimistic.
    return int(report.get("authorization_exit_code", 0) or 0)


def cmd_factory_status(args: argparse.Namespace) -> int:
    print(json.dumps(factory_status(_db(args)), sort_keys=True, default=str))
    return 0


def cmd_factory_report(args: argparse.Namespace) -> int:
    """Explain what research did: lineage, variants, verdicts, and reasons."""
    from research.factory_report import build_report, render_markdown, render_text

    report = build_report(_db(args), vehicle=args.vehicle, slot=args.slot)
    if getattr(args, "write", False):
        target = _write_factory_report(args)
        print(json.dumps({"schema": "factory-report-artifact.v1",
                          "artifact": target}, sort_keys=True), file=sys.stderr)
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, default=str))
    elif args.format == "markdown":
        print(render_markdown(report), end="")
    else:
        print(render_text(report), end="")
    return 0 if report.get("available") else 2


def _factory_parser(sub: argparse._SubParsersAction, name: str, command: str):
    parser = sub.add_parser(name, help=f"autonomous strategy factory {command}")
    parser.add_argument("--db", default=None)
    if command == "status":
        parser.set_defaults(func=cmd_factory_status)
    elif command == "report":
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.add_argument("--slot", type=int, default=None)
        parser.add_argument("--format", choices=("text", "markdown", "json"),
                            default="text")
        parser.add_argument("--write", action="store_true",
                            help="also archive Markdown under research/results")
        parser.set_defaults(func=cmd_factory_report)
    else:
        parser.add_argument("--data", required=True, help="normalized mixed market JSONL")
        parser.add_argument("--worker-data", default=None,
                            help="optional bar+option JSONL replay view for workers")
        parser.add_argument(
            "--diagnostic-only", action="store_true",
            help=("run despite SIP/delayed source provenance; the result is "
                  "non-authorizing and no edge proofs are emitted"))
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
        parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
        parser.add_argument("--strategies", type=int, default=DEFAULT_STRATEGIES)
        parser.add_argument("--variants", type=int, default=4,
                            help="isolated variants/accounts per strategy")
        parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
        parser.add_argument("--starting-cash", type=float, default=100000.0)
        parser.add_argument("--min-trades", type=int,
                            default=PROTOCOL_BACKTEST_MIN_TRADES,
                            help=("minimum executed trades (protocol floor: "
                                  f"{PROTOCOL_BACKTEST_MIN_TRADES})"))
        parser.add_argument("--min-sessions", type=int,
                            default=PROTOCOL_BACKTEST_MIN_SESSIONS,
                            help=("minimum independent sessions (protocol floor: "
                                  f"{PROTOCOL_BACKTEST_MIN_SESSIONS})"))
        parser.add_argument("--alpha", type=float, default=.05)
        parser.add_argument("--max-generations", type=int, default=5)
        parser.add_argument("--max-confirmatory-attempts", type=int, default=3,
                            help="durable confirmatory attempts per exact variant")
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
    elif command == "ingest-shadow":
        parser.add_argument("candidate", nargs="?", default=None,
                            help="one candidate id (default: every eligible candidate)")
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.add_argument("--shadow-db", default=None,
                            help="broker-free shadow WAL (default: ALPACA_SHADOW_DB)")
        parser.add_argument("--baseline-candidate", default=None,
                            help="paired baseline candidate id")
        parser.add_argument("--null-candidate", default=None,
                            help="paired randomized-null candidate id")
        parser.add_argument("--min-trades", type=int,
                            default=PROTOCOL_SHADOW_MIN_TRADES,
                            help=("minimum executed trades (protocol floor: "
                                  f"{PROTOCOL_SHADOW_MIN_TRADES})"))
        parser.add_argument("--min-sessions", type=int,
                            default=PROTOCOL_SHADOW_MIN_SESSIONS,
                            help=("minimum independent sessions (protocol floor: "
                                  f"{PROTOCOL_SHADOW_MIN_SESSIONS})"))
        parser.add_argument("--alpha", type=float, default=.05)
        parser.set_defaults(func=cmd_edge_ingest_shadow)
    elif command == "paper":
        parser.add_argument("candidate", nargs="?", default=None,
                            help="one candidate id (default: every candidate)")
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.add_argument("--deployed", action="store_true",
                            help="only validated/champion edges")
        parser.set_defaults(func=cmd_edge_paper)
    elif command == "trials":
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
        parser.add_argument("--dry-run", action="store_true",
                            help="report verdicts without parking anything")
        parser.set_defaults(func=cmd_edge_trials)
    elif command == "promotable":
        parser.add_argument("--vehicle", choices=("equity", "option"), default=None)
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.set_defaults(func=cmd_edge_promotable)
    elif command == "discover":
        parser.add_argument("--data", required=True,
                            help="normalized market JSONL corpus")
        parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
        parser.add_argument("--lane", choices=("auto", "backtest", "shadow"), default="auto")
        parser.add_argument("--config", help="optional base runtime config JSON")
        parser.add_argument("--agent-config", default=None,
                            help="validated agent config (default: config.yaml)")
        parser.add_argument(
            "--diagnostic-only", action="store_true",
            help=("inspect historical/mixed sources without opening the edge "
                  "ledger; result is non-authorizing"))
        parser.add_argument("--variants", help="optional preregistration JSON/YAML path")
        parser.add_argument("--min-trades", type=int,
                            default=PROTOCOL_BACKTEST_MIN_TRADES,
                            help=("minimum executed trades (backtest floor: "
                                  f"{PROTOCOL_BACKTEST_MIN_TRADES}; auto "
                                  "requires the shadow floor when it consumes "
                                  "a shadow tail)"))
        parser.add_argument("--min-sessions", type=int,
                            default=PROTOCOL_BACKTEST_MIN_SESSIONS,
                            help=("minimum independent sessions (protocol floor: "
                                  f"{PROTOCOL_BACKTEST_MIN_SESSIONS})"))
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
    validate.add_argument("--diagnostic-only", action="store_true",
                          help=("allow historical or mixed source modes for a "
                                "non-authorizing diagnostic; malformed and "
                                "future-observed rows still fail"))
    validate.set_defaults(func=cmd_validate_data)
    ibr = sub.add_parser("backtest-ibr", help="replay IBR on normalized bars JSONL")
    ibr.add_argument("bars", help="underlying bars JSONL, or - for stdin")
    ibr.add_argument("--symbol", default=None)
    ibr.add_argument("--quotes", default=None,
                     help="normalized quote JSONL used for boundary fills")
    ibr.add_argument(
        "--quotes-from-mixed", action="store_true",
        help=("read only quote rows from --quotes, allowing the canonical "
              "mixed market JSONL to replace a duplicate quote-only file"))
    ibr.add_argument("--provider", default=None)
    ibr.add_argument("--feed", default=None)
    ibr.add_argument(
        "--diagnostic-only", action="store_true",
        help=("allow historical/mixed sources for an explicitly non-authorizing "
              "replay"))
    ibr.add_argument("--vehicle", choices=("equity", "option", "both"), default="equity")
    _add_cost_flags(ibr)
    ibr.set_defaults(func=cmd_backtest_ibr)
    # Both ``edge init`` and flat ``edge-init`` forms are accepted so cron
    # jobs can stay terse while operators retain a discoverable command tree.
    edge = sub.add_parser("edge", help="auditable edge discovery ledger")
    edge_sub = edge.add_subparsers(dest="edge_command", required=True)
    for name in ("init", "status", "promote", "ingest", "ingest-shadow", "paper", "discover",
                 "trials", "promotable"):
        _edge_parser(edge_sub, name, name)
    for name in ("edge-init", "edge-status", "edge-promote", "edge-ingest", "edge-ingest-shadow",
                 "edge-paper", "edge-discover", "edge-trials",
                 "edge-promotable"):
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
    llm_preflight = sub.add_parser(
        "llm-preflight", help="probe the configured research strategy provider")
    llm_preflight.add_argument("--agent-config", default=None,
                               help="validated agent config (default: config.yaml)")
    llm_preflight.set_defaults(func=cmd_llm_preflight)
    add_paper_epoch_parser(sub)
    calibrate = sub.add_parser(
        "calibrate", help="compare modelled fill costs against journaled fills")
    calibrate.add_argument("journal")
    calibrate.add_argument("--config", default=None)
    calibrate.add_argument("--vehicle", choices=("equity", "option"), default=None,
                           help="scope the report to one vehicle (recommended for authorization)")
    calibrate.set_defaults(func=cmd_calibrate)
    factory = sub.add_parser("factory", help="parallel autonomous strategy factory")
    factory_sub = factory.add_subparsers(dest="factory_command", required=True)
    for name in ("run", "status", "report"):
        _factory_parser(factory_sub, name, name)
    for name in ("factory-run", "factory-status", "factory-report"):
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
