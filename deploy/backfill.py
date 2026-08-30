#!/usr/bin/env python3
"""Seed the recorder's corpus from Alpaca historical bars.

The recorder samples forward in real time, so a fresh deployment starts with an
empty corpus and cannot clear the research floors — a hundred held-out trades
across ten sessions, and then a *strictly later* forward window — for months.
That delay is an artefact of how the corpus is acquired, not of the evidence
standard, and backfilling removes it without weakening a single gate.

What this writes deliberately retains the recorder's normalized CSV contract,
``event_key``, one-partition-per-New-York-session layout, and sidecar index.
Unlike a forward observation, however, every row is explicitly labelled
``historical_backfill``.  Its truthful wall-clock ``observed_at`` is never
backdated; an explicit diagnostic replay policy is required to inspect the
historical mechanics, and those results cannot authorize a candidate.

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
* Quotes are opt-in (``--quotes``).  Historical backtest lanes default to the
  research-only ``research.backtest_bar_fallback: true`` policy, so a missing
  equity quote at a bar boundary may be priced from that bar through the
  shared conservative spread/slippage/fee model; the proof records the fill
  source.  Forward-shadow, live-shadow, and paper remain strict and require
  fresh executable quotes, so backfill quotes for those lanes.  Set
  ``research.backtest_bar_fallback: false`` when a historical backtest must
  produce strict diagnostics.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.alpaca_session import AlpacaError, normalize_calendar_day  # noqa: E402
from agent.alpaca_sdk import _canonical_feed  # noqa: E402
from agent.instruments import validate_equity_symbol  # noqa: E402
from deploy.recorder import (  # noqa: E402
    NEW_YORK,
    _append_partitions,
    _corpus_data_feed,
    _partition_calendar_path,
    _partition_path,
    _partition_source_path,
    _prepare_index,
    _load_index,
    _save_partition_calendar,
    _save_index,
    _save_partition_source,
    _scan_corpus,
    audit_corpus,
    corpus_write_lock,
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


def _calendar_sessions(provider, start: date, end: date) -> dict[date, object]:
    """Return normalized Alpaca calendar sessions in ``[start, end]``."""
    method = getattr(provider, "calendar", None)
    if not callable(method):
        raise BackfillError("provider exposes no trading calendar")
    try:
        rows = method(start=start, end=end) or []
    except (AlpacaError, TypeError, ValueError, OSError) as exc:
        raise BackfillError(f"trading calendar unavailable: {exc}") from exc
    sessions: dict[date, object] = {}
    for index, row in enumerate(rows, start=1):
        try:
            session = normalize_calendar_day(row)
        except (TypeError, ValueError) as exc:
            raise BackfillError(
                f"trading calendar row {index} is invalid: {exc}") from exc
        if start <= session.date <= end:
            sessions[session.date] = session
    return dict(sorted(sessions.items()))


def completed_sessions(provider, start: date, end: date) -> list[date]:
    """Return the completed NYSE session dates in ``[start, end]``.

    The Alpaca calendar is the authority.  When it is unavailable this refuses
    rather than falling back to a weekday guess: a backfill that silently
    invents sessions on holidays would put fabricated gaps into the one corpus
    every downstream statistic is computed from.
    """
    return list(_calendar_sessions(provider, start, end))


_PARTITION_NAME = re.compile(r"market-(\d{4}-\d{2}-\d{2})\.csv\Z")


def _partition_dates(output: Path) -> list[date]:
    """Return every session date encoded by an existing partition filename.

    Calendar repair must not inspect the (potentially multi-gigabyte) CSV
    corpus.  Partition names are the only bounded source of session dates, so
    reject anything that is not exactly ``market-YYYY-MM-DD.csv`` and require
    a canonical ISO date.
    """
    paths = corpus_partitions(output)
    if not paths:
        raise BackfillError("calendar repair requires at least one session partition")
    days: list[date] = []
    for path in paths:
        match = _PARTITION_NAME.fullmatch(path.name)
        if match is None:
            raise BackfillError(
                f"invalid recorder session partition name: {path.name}")
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError as exc:
            raise BackfillError(
                f"invalid recorder session partition date: {path.name}") from exc
        if day.isoformat() != match.group(1):
            raise BackfillError(
                f"invalid recorder session partition date: {path.name}")
        days.append(day)
    return days


def _calendar_entry(session) -> dict[str, str]:
    """Serialize one normalized Alpaca session for recorder metadata."""
    return {
        "open": session.open.astimezone(timezone.utc).isoformat(),
        "close": session.close.astimezone(timezone.utc).isoformat(),
        "source": "alpaca_calendar",
    }


def repair_calendar(provider, output: Path) -> dict:
    """Repair exact calendar metadata without scanning or relabelling a corpus.

    This is intentionally separate from :func:`backfill`: it only reads the
    already validated recorder index and bounded partition filenames, obtains
    the authoritative Alpaca calendar for their date window, then writes
    calendar markers and the aggregate cache.  Every date is validated before
    any marker is touched, so a non-session partition fails closed atomically.
    """
    with corpus_write_lock(output):
        # ``_prepare_index`` may rebuild a large corpus.  A repair is only
        # safe when the existing bounded index is already validated.
        index = _load_index(output)
        if index is None:
            raise BackfillError(
                "calendar repair requires an existing validated recorder index")
        days = _partition_dates(output)
        start, end = min(days), max(days)
        calendar = _calendar_sessions(provider, start, end)
        missing = sorted(set(days) - set(calendar))
        if missing:
            rendered = ", ".join(day.isoformat() for day in missing)
            raise BackfillError(
                f"partition dates are not authoritative trading sessions: {rendered}")

        existing_markers = sum(
            _partition_calendar_path(output, day).is_file() for day in days)
        existing_calendar = dict(index.get("session_calendar") or {})
        merged_calendar = dict(existing_calendar)
        for day in days:
            merged_calendar[day.isoformat()] = _calendar_entry(calendar[day])
        index["session_calendar"] = merged_calendar
        # Commit the aggregate first.  If marker persistence is interrupted,
        # the validated index still contains the complete exact calendar while
        # the already-written marker subset remains consistent with it.
        _save_index(output, index)
        for day in days:
            _save_partition_calendar(output, day, calendar[day])

    return {
        "schema": "recorder-calendar-repair.v1",
        "status": "ok",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "partitions": len(days),
        "calendar_sessions": len(calendar),
        "markers_written": len(days),
        "markers_repaired": len(days) - existing_markers,
        "calendar_entries_added": len(
            set(day.isoformat() for day in days) - set(existing_calendar)),
    }


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


def _bar_rows(provider, symbols, day: date, *, session, feed: str,
              observed: str):
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
            as_of = parsed + timedelta(minutes=1)
            if not session.open <= parsed < session.close or as_of > session.close:
                continue
            yield {
                "event_key": _event_key("bar_1m", symbol, timestamp),
                "observed_at": observed, "provider": "alpaca", "feed": feed,
                "event_type": "bar_1m", "symbol": symbol, "contract": "",
                # A one-minute OHLC row is knowable only after its closing
                # boundary, even when the provider timestamps it at the open.
                "timestamp": timestamp,
                "as_of": as_of.isoformat(),
                "open": _value(bar.open), "high": _value(bar.high),
                "low": _value(bar.low), "close": _value(bar.close),
                "volume": _value(bar.volume), "bid": "", "ask": "", "last": "",
            }


def _quote_rows(provider, symbols, day: date, *, session, feed: str,
                observed: str):
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
            if not session.open <= parsed < session.close:
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
    with corpus_write_lock(output):
        return _backfill_locked(
            provider, symbols, output, days=days, feed=feed,
            include_quotes=include_quotes, now=now, overwrite=overwrite,
            progress=progress)


def _backfill_locked(provider, symbols, output: Path, *,
                     days: int = DEFAULT_BACKFILL_DAYS,
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
    try:
        resolved_feed = _canonical_feed(
            feed if feed is not None else
            getattr(provider, "data_feed", None) or _feed())
    except ValueError as exc:
        raise BackfillError(str(exc)) from exc
    index = _prepare_index(output)
    existing_calendar = dict(index.get("session_calendar") or {}) \
        if index is not None else {}
    existing_sources = dict(index.get("partition_sources") or {}) \
        if index is not None else {}
    existing_feed = (str(index.get("data_feed") or "").strip().lower()
                     if index is not None else "")
    if not existing_feed and corpus_partitions(output):
        existing_feed = _corpus_data_feed(output) or ""
    if existing_feed and existing_feed != resolved_feed:
        raise BackfillError(
            f"recorder data feed changed from {existing_feed} to "
            f"{resolved_feed}; use a separate corpus")
    end = last_completed_session(now)
    start = end - timedelta(days=int(days) - 1)
    calendar = _calendar_sessions(provider, start, end)
    sessions = list(calendar)
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
            # Remove metadata first: a crash after the CSV unlink must never
            # leave an orphan marker that research could mistake for evidence.
            _partition_source_path(output, day).unlink(missing_ok=True)
            _partition_calendar_path(output, day).unlink(missing_ok=True)
            partition.unlink()
        rows = list(_bar_rows(provider, symbols, day, session=calendar[day],
                              feed=resolved_feed, observed=observed))
        if include_quotes:
            rows.extend(_quote_rows(provider, symbols, day, session=calendar[day],
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
        # Persist the non-authorizing provenance before the partition.  If the
        # process crashes before the aggregate index rewrite, corpus recovery
        # reconstructs the label from this marker instead of treating the CSV
        # as forward-observed evidence.
        _save_partition_source(output, day, "historical_backfill")
        _save_partition_calendar(output, day, calendar[day])
        _append_partitions(output, list(unique.values()))
        written_rows += len(unique)
        written_sessions.append(day.isoformat())
        if progress is not None:
            progress(day, len(unique))
    # Backfill is an explicit offline operation, so retain the full duplicate
    # audit before rebuilding the bounded live index.
    audit_corpus(output)
    index = _scan_corpus(output)
    index["session_calendar"] = {**existing_calendar, **{
        day.isoformat(): {
            "open": session.open.astimezone(timezone.utc).isoformat(),
            "close": session.close.astimezone(timezone.utc).isoformat(),
            "source": "alpaca_calendar",
        }
        for day, session in calendar.items()
    }}
    index["partition_sources"] = {**existing_sources, **{
        _partition_path(output, date.fromisoformat(day)).name: {
            "source_mode": "historical_backfill",
        }
        for day in written_sessions
    }}
    _save_index(output, index)
    return {
        "schema": "recorder-backfill.v1", "feed": resolved_feed,
        "symbols": symbols, "requested_days": int(days),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "calendar_sessions": len(sessions),
        "written_sessions": written_sessions, "skipped_sessions": skipped,
        "rows": written_rows, "quotes": bool(include_quotes),
        "options": False, "source_mode": "historical_backfill",
        "authorizing": False,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="seed the research corpus from Alpaca historical bars")
    p.add_argument("--out", default="runtime/research/recorded")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--repair-calendar", action="store_true",
                   help="repair exact Alpaca calendar metadata for existing "
                   "session partitions without reading or fetching market data")
    p.add_argument("--days", type=int, default=DEFAULT_BACKFILL_DAYS,
                   help=f"calendar days back from the last completed session "
                        f"(max {MAX_BACKFILL_DAYS})")
    p.add_argument("--quotes", action="store_true",
                   help="also backfill quotes; far larger, and required by "
                        "strict forward/live-shadow and paper lanes. Historical "
                        "backtests use research.backtest_bar_fallback (default "
                        "true) for missing equity boundary quotes")
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
    provider = AlpacaProvider(cfg)
    output = Path(args.out) / "market.csv"
    try:
        if args.repair_calendar:
            result = repair_calendar(provider, output)
        else:
            symbols = list(cfg.get("universe", {}).get("symbols") or [])
            if not symbols:
                raise SystemExit("config.universe.symbols is empty")
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
