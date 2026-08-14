#!/usr/bin/env python3
"""Build temporary research views from the append-only recorder corpus.

Recorder releases before the post-fetch observation fix could write otherwise
well-formed rows whose ``as_of`` timestamp is later than ``observed_at``. Such
rows cannot be used as point-in-time evidence. This module leaves the source
corpus untouched, excludes only that precisely identified legacy defect from
the temporary research views, and reports the quarantine explicitly.

Every other row is preserved for ``research.py validate-data`` so malformed
timestamps, symbols, prices, kinds, and other integrity violations still fail
the cycle closed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable, Mapping


BAR_KINDS = {"bar", "bar_1m", "underlying", "underlying_bar"}
QUOTE_KINDS = {"quote", "quote_snapshot", "equity_quote", "underlying_quote"}
OPTION_KINDS = {"option", "option_snapshot", "option_quote"}


def _clean(value):
    return None if value in (None, "") else value


def _csv_payload(row: Mapping[str, object]) -> dict:
    event = str(row.get("event_type") or "").lower()
    common = {
        "provider": row.get("provider") or "alpaca",
        "feed": row.get("feed") or "iex",
        "symbol": _clean(row.get("symbol")),
        "timestamp": _clean(row.get("timestamp")),
        "observed_at": _clean(row.get("observed_at") or row.get("timestamp")),
        "as_of": _clean(row.get("as_of") or row.get("timestamp")),
    }
    if event in {"bar", "bar_1m"}:
        return {
            "kind": "bar", **common,
            "open": _clean(row.get("open")),
            "high": _clean(row.get("high")),
            "low": _clean(row.get("low")),
            "close": _clean(row.get("close")),
            "volume": _clean(row.get("volume")),
        }
    if event == "quote":
        return {
            "kind": "quote", **common,
            "bid": _clean(row.get("bid")),
            "ask": _clean(row.get("ask")),
            "bid_size": _clean(row.get("bid_size")),
            "ask_size": _clean(row.get("ask_size")),
        }
    if event in OPTION_KINDS:
        return {
            "kind": "option_snapshot", **common,
            "contract": _clean(row.get("contract")),
            "underlying": _clean(row.get("underlying")),
            "expiration": _clean(row.get("expiration")),
            "strike": _clean(row.get("strike")),
            "right": _clean(row.get("right")),
            "multiplier": _clean(row.get("multiplier")),
            "bid": _clean(row.get("bid")),
            "ask": _clean(row.get("ask")),
            "last": _clean(row.get("last")),
            "bid_size": _clean(row.get("bid_size")),
            "ask_size": _clean(row.get("ask_size")),
            "volume": _clean(row.get("volume")),
            "open_interest": _clean(row.get("open_interest")),
            "underlying_price": _clean(row.get("underlying_price")),
        }
    # Preserve unknown recorder event types so validate-data names them instead
    # of silently dropping evidence from the cycle.
    return {"kind": event, **common}


def _timestamp(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        # Validation owns malformed timestamps. Returning None deliberately
        # keeps the row in the temporary dataset so it remains a hard failure.
        return None


def _legacy_observation_inversion(payload: Mapping[str, object]) -> bool:
    as_of = _timestamp(payload.get("as_of"))
    observed = _timestamp(payload.get("observed_at"))
    return as_of is not None and observed is not None and as_of > observed


def _jsonl_rows(path: Path) -> Iterable[tuple[int, dict]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: expected an object")
            yield number, value


def _csv_rows(path: Path) -> Iterable[tuple[int, dict]]:
    with path.open(newline="", encoding="utf-8") as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            yield number, _csv_payload(row)


def build_views(source: Path, *, input_format: str, normalized: Path,
                bars: Path, quotes: Path, options: Path,
                replay: Path | None = None) -> dict:
    rows = _csv_rows(source) if input_format == "csv" else _jsonl_rows(source)
    quarantined = Counter()
    kept = 0
    view_counts = {"normalized": 0, "bars": 0, "quotes": 0,
                   "options": 0, "replay": 0}
    first_source_row = None
    last_source_row = None
    with ExitStack() as stack:
        normalized_output = stack.enter_context(normalized.open("w", encoding="utf-8"))
        bars_output = stack.enter_context(bars.open("w", encoding="utf-8"))
        quotes_output = stack.enter_context(quotes.open("w", encoding="utf-8"))
        options_output = stack.enter_context(options.open("w", encoding="utf-8"))
        replay_output = (stack.enter_context(replay.open("w", encoding="utf-8"))
                         if replay is not None else None)
        for source_row, payload in rows:
            kind = str(payload.get("kind", "bar")).lower()
            if _legacy_observation_inversion(payload):
                quarantined[kind or "unknown"] += 1
                if first_source_row is None:
                    first_source_row = source_row
                last_source_row = source_row
                continue
            serialized = json.dumps(payload, sort_keys=True) + "\n"
            normalized_output.write(serialized)
            kept += 1
            view_counts["normalized"] += 1
            if kind in BAR_KINDS:
                bars_output.write(serialized)
                view_counts["bars"] += 1
                if replay_output is not None:
                    replay_output.write(serialized)
                    view_counts["replay"] += 1
            elif kind in QUOTE_KINDS:
                quotes_output.write(serialized)
                view_counts["quotes"] += 1
            elif kind in OPTION_KINDS:
                options_output.write(serialized)
                view_counts["options"] += 1
                if replay_output is not None:
                    replay_output.write(serialized)
                    view_counts["replay"] += 1
    skipped = sum(quarantined.values())
    return {
        "schema": "research-cycle-quarantine.v1",
        "status": "quarantined" if skipped else "clean",
        "reason": "as_of_after_observed_at" if skipped else None,
        "rows": skipped,
        "kept_rows": kept,
        "view_counts": view_counts,
        "by_kind": dict(sorted(quarantined.items())),
        "first_source_row": first_source_row,
        "last_source_row": last_source_row,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--format", choices=("csv", "jsonl"), required=True)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--options", type=Path, required=True)
    parser.add_argument("--replay", dest="replay", type=Path,
                        help="optional bar+option JSONL view for replay workers")
    args = parser.parse_args()
    result = build_views(
        args.source, input_format=args.format, normalized=args.normalized,
        bars=args.bars, quotes=args.quotes, options=args.options,
        replay=args.replay)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
