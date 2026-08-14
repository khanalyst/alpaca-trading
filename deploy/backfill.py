#!/usr/bin/env python3
"""Seed the recorder's corpus from Alpaca historical bars.

The recorder samples forward in real time, so a fresh deployment starts with an
empty corpus and cannot clear the research floors — a hundred held-out trades
across ten sessions, and then a *strictly later* forward window — for months.
That delay is an artefact of how the corpus is acquired, not of the evidence
standard, and backfilling removes it without weakening a single gate.

What this writes is deliberately identical to what the recorder writes: the
same normalized CSV fields, the same ``event_key``, the same one-partition-per-
New-York-session layout, and the same sidecar index.  Research therefore cannot
tell a backfilled session from a recorded one, and nothing downstream needs a
special case.

Three boundaries keep it honest:

* Only *completed* sessions are written.  A partial session would leave the
  recorder's continuity check staring at a mid-session hole, and a partial
  session is not a research observation anyway.
* ``as_of`` is the bar's completed one-minute boundary, exactly as the recorder
  records it, so research cannot see the bar's high, low, close, or volume
  before that minute has finished.
* Options are not backfilled.  The recorder's option rows are sampled chain
  snapshots with quote-age semantics that a historical endpoint cannot
  reconstruct, so inventing them would fabricate the one thing option research
  depends on.  The option lane still needs recorded sessions.
* Quotes are opt-in (``--quotes``) and are not optional evidence.  Strict
  replay -- the default, and everything the equity gates assume -- refuses to
  price a fill it has no recorded quote for, so a bars-only corpus executes no
  trades whatsoever.  Backfill quotes, or set ``execution.strict_market_data``
  to false and accept bar prints marked up by the modelled half-spread, which
  the proof then records as such under ``fill_quality``.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.alpaca_session import AlpacaError, normalize_calendar_day  # noqa: E402
from agent.instruments import validate_equity_symbol  # noqa: E402
from deploy.recorder import (  # noqa: E402
    NEW_YORK,
    _append_partitions,
    _partition_path,
    _save_index,
    _scan_corpus,
    audit_corpus,
    corpus_partitions,
)
from deploy.recorder_market import (  # noqa: E402
    _call_market_data,
    _call_quotes,
    _event_key,
    _feed,
    _iso,
    _point_in_time,
    _timestamp,
    _value,
)

MAX_BACKFILL_DAYS = 1_095
DEFAULT_BACKFILL_DAYS = 90


class BackfillError(RuntimeError):
    """Raised when a backfill cannot produce a corpus research may trust."""


def completed_sessions(provider, start: date, end: date) -> list[date]:
    """Return the completed NYSE session dates in ``[start, end]``.

    The Alpaca calendar is the authority.  When it is unavailable this refuses
    rather than falling back to a weekday guess: a backfill that silently
    invents sessions on holidays would put fabricated gaps into the one corpus
    every downstream statistic is computed from.
    """
    method = getattr(provider, "calendar", None)
    if not callable(method):
        raise BackfillError("provider exposes no trading calendar")
    try:
        rows = method(start=start, end=end) or []
    except (AlpacaError, TypeError, ValueError, OSError) as exc:
        raise BackfillError(f"trading calendar unavailable: {exc}") from exc
    sessions = []
    for row in rows:
        try:
            day = normalize_calendar_day(row)
        except (TypeError, ValueError):
            continue
        if start <= day.date <= end:
            sessions.append(day.date)
    return sorted(set(sessions))


def last_completed_session(now: datetime) -> date:
    """The most recent session date whose regular hours are certainly over.

    Today counts only once it is past 20:00 New York — comfortably beyond any
    late close, and past the window in which a consolidated feed is still
    revising the tape.
    """
    local = now.astimezone(NEW_YORK)
    if local.time() >= time(20, 0):
        return local.date()
    return local.date() - timedelta(days=1)


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    """Fetch the whole New York day; the calendar decides what is a session."""
    start = datetime.combine(day, time(0, 0), tzinfo=NEW_YORK)
    return start, start + timedelta(days=1)


def _bar_rows(provider, symbols, day: date, *, feed: str, observed: str):
    start, end = _session_bounds(day)
    bars = _call_market_data(provider.bars, symbols, start=start, end=end,
                             feed=feed) or {}
    for raw_symbol, values in bars.items():
        symbol = validate_equity_symbol(raw_symbol)
        for bar in values or ():
            timestamp = _iso(getattr(bar, "timestamp", None))
            if not _point_in_time(timestamp):
                raise BackfillError(
                    f"backfilled bar {symbol!r} has no point-in-time timestamp")
            parsed = _timestamp(timestamp)
            # A provider that returns a neighbouring day's bars would put rows
            # in the wrong partition and corrupt the session index.
            if parsed.astimezone(NEW_YORK).date() != day:
                continue
            yield {
                "event_key": _event_key("bar_1m", symbol, timestamp),
                "observed_at": observed, "provider": "alpaca", "feed": feed,
                "event_type": "bar_1m", "symbol": symbol, "contract": "",
                # A one-minute OHLC row is knowable only after its closing
                # boundary, even when the provider timestamps it at the open.
                "timestamp": timestamp,
                "as_of": (parsed + timedelta(minutes=1)).isoformat(),
                "open": _value(bar.open), "high": _value(bar.high),
                "low": _value(bar.low), "close": _value(bar.close),
                "volume": _value(bar.volume), "bid": "", "ask": "", "last": "",
            }


def _quote_rows(provider, symbols, day: date, *, feed: str, observed: str):
    start, end = _session_bounds(day)
    quotes = _call_quotes(provider.quotes, symbols, start=start, end=end,
                          feed=feed) or {}
    for raw_symbol, values in quotes.items():
        symbol = validate_equity_symbol(raw_symbol)
        for quote in values or ():
            timestamp = _iso(getattr(quote, "timestamp", None))
            if not _point_in_time(timestamp):
                continue
            parsed = _timestamp(timestamp)
            if parsed.astimezone(NEW_YORK).date() != day:
                continue
            yield {
                "event_key": _event_key("quote", symbol, timestamp),
                "observed_at": observed, "provider": "alpaca", "feed": feed,
                "event_type": "quote", "symbol": symbol, "contract": "",
                "timestamp": timestamp, "as_of": timestamp, "open": "",
                "high": "", "low": "", "close": "", "volume": "",
                "bid": _value(quote.bid), "ask": _value(quote.ask),
                "last": _value(quote.last),
            }


def backfill(provider, symbols, output: Path, *, days: int = DEFAULT_BACKFILL_DAYS,
             feed: str | None = None, include_quotes: bool = False,
             now: datetime | None = None, overwrite: bool = False,
             progress=None) -> dict:
    """Write completed historical sessions into the recorder's corpus.

    Sessions that already have a partition are skipped, so an interrupted run
    resumes and a repeated run is a no-op.  Each session is written as one
    partition before the next is fetched, which bounds memory and makes partial
    progress durable.
    """
    symbols = [validate_equity_symbol(symbol) for symbol in symbols]
    if not symbols:
        raise BackfillError("at least one US equity symbol is required")
    if not 1 <= int(days) <= MAX_BACKFILL_DAYS:
        raise BackfillError(f"days must be between 1 and {MAX_BACKFILL_DAYS}")
    now = now or datetime.now(timezone.utc)
    resolved_feed = str(feed if feed is not None else
                        getattr(provider, "data_feed", None) or _feed()
                        ).strip().lower() or "iex"
    end = last_completed_session(now)
    start = end - timedelta(days=int(days) - 1)
    sessions = completed_sessions(provider, start, end)
    existing = {path.name for path in corpus_partitions(output)}
    written_rows = 0
    written_sessions: list[str] = []
    skipped: list[str] = []
    observed = now.isoformat()
    for day in sessions:
        partition = _partition_path(output, day)
        if partition.name in existing:
            if not overwrite:
                skipped.append(day.isoformat())
                continue
            # Partitions are append-only, so overwriting means replacing the
            # file. Appending instead would duplicate every event key and make
            # the corpus unreadable at the recorder's next scan.
            partition.unlink()
        rows = list(_bar_rows(provider, symbols, day,
                              feed=resolved_feed, observed=observed))
        if include_quotes:
            rows.extend(_quote_rows(provider, symbols, day,
                                    feed=resolved_feed, observed=observed))
        if not rows:
            # A scheduled session with no bars is a data hole, not a holiday;
            # record it as skipped rather than writing an empty partition that
            # would later read as a silent session.
            skipped.append(day.isoformat())
            continue
        # One key per row, within the session being written.  The recorder's
        # own scan enforces this across the whole corpus afterwards.
        unique: dict[str, dict] = {}
        for row in rows:
            unique.setdefault(row["event_key"], row)
        _append_partitions(output, list(unique.values()))
        written_rows += len(unique)
        written_sessions.append(day.isoformat())
        if progress is not None:
            progress(day, len(unique))
    # Backfill is an explicit offline operation, so retain the full duplicate
    # audit before rebuilding the bounded live index.
    audit_corpus(output)
    _save_index(output, _scan_corpus(output))
    return {
        "schema": "recorder-backfill.v1", "feed": resolved_feed,
        "symbols": symbols, "requested_days": int(days),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "calendar_sessions": len(sessions),
        "written_sessions": written_sessions, "skipped_sessions": skipped,
        "rows": written_rows, "quotes": bool(include_quotes),
        "options": False,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="seed the research corpus from Alpaca historical bars")
    p.add_argument("--out", default="runtime/research/recorded")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--days", type=int, default=DEFAULT_BACKFILL_DAYS,
                   help=f"calendar days back from the last completed session "
                        f"(max {MAX_BACKFILL_DAYS})")
    p.add_argument("--quotes", action="store_true",
                   help="also backfill quotes; far larger, and required unless "
                        "execution.strict_market_data is false. Strict replay "
                        "(the default) refuses to price a fill with no recorded "
                        "quote, so a bars-only corpus executes no trades at all")
    p.add_argument("--overwrite", action="store_true",
                   help="rewrite sessions that already have a partition")
    return p


def main(argv=None) -> int:
    import json

    args = parser().parse_args(argv)
    env_file = os.getenv("ALPACA_AGENT_SECRETS_FILE")
    if env_file:
        from deploy.recorder import load_dotenv
        load_dotenv(env_file, override=False)
    from main import load_cfg
    from agent.alpaca_provider import AlpacaProvider

    cfg = load_cfg(args.config)
    symbols = list(cfg.get("universe", {}).get("symbols") or [])
    if not symbols:
        raise SystemExit("config.universe.symbols is empty")
    provider = AlpacaProvider(cfg)
    output = Path(args.out) / "market.csv"
    try:
        result = backfill(
            provider, symbols, output, days=args.days,
            include_quotes=bool(args.quotes), overwrite=bool(args.overwrite),
            progress=lambda day, count: print(
                f"backfilled {count} rows for {day.isoformat()}", flush=True))
    except BackfillError as exc:
        print(json.dumps({"schema": "recorder-backfill.v1", "status": "failed",
                          "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
