"""Initial-breakout-range (IBR) alpha contract.

Inputs are intentionally plain mappings so this module can be used by live
market collection, replay, and unit tests without importing the engine.  A
bar's timestamp is its *open* timestamp; a one-minute bar is therefore
eligible only after ``timestamp + 1 minute`` has completed.  All comparisons
use completed closes, never a wick.

Signal timestamps.  An IBR signal's ``signal_ts`` is the *close* of the
completed signal bar (its open plus one minute), and it keeps that meaning:
the setup plan, its idempotency key and the shadow signatures all read it.
Rule signals (``agent.contracts.rule``) use ``signal_ts`` for the signal
bar's *open*, so the same field name differs by one bar between the two
families.  Every IBR signal therefore also carries the unambiguous pair
``signal_bar_open_ts`` and ``signal_bar_close_ts``; a consumer that compares
signals across families should read those instead of ``signal_ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from . import finite, register
from .rule import MIN_STOP_DISTANCE_FRACTION

DEFAULT_IBR_TIMEZONE = "America/New_York"
DEFAULT_SESSION_START = "09:30"
DEFAULT_SESSION_END = "09:45"

# Relative-volume baselines.  ``opening_range_mean`` is the registered
# behaviour: the candidate bar's volume over the mean volume of the opening
# range minutes.  Intraday volume is U-shaped, so that ratio mostly measures
# how far the candidate is from the open.  ``same_clock`` is an explicit
# opt-in that divides by the mean volume of the same clock minute over the
# exact prior sessions, the time-of-day adjusted baseline the rule family's
# v6 ``relative_volume_mode="same_clock"`` already uses.
RELATIVE_VOLUME_OPENING_RANGE_MEAN = "opening_range_mean"
RELATIVE_VOLUME_SAME_CLOCK = "same_clock"
RELATIVE_VOLUME_MODES = (RELATIVE_VOLUME_OPENING_RANGE_MEAN,
                         RELATIVE_VOLUME_SAME_CLOCK)
DEFAULT_RELATIVE_VOLUME_SESSIONS = 5
RELATIVE_VOLUME_SESSIONS_BOUNDS = (5, 20)


def _dt(value, tz: ZoneInfo = ZoneInfo(DEFAULT_IBR_TIMEZONE)) -> datetime | None:
    if isinstance(value, datetime):
        out = value
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(tz)
    if isinstance(value, date):
        return datetime.combine(value, time(), tzinfo=tz)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    # Accept either seconds or milliseconds since epoch.
    if abs(number) > 100_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None


def _strict_dt(value, tz: ZoneInfo = ZoneInfo(DEFAULT_IBR_TIMEZONE)) -> datetime | None:
    """Parse a supplied instant without inventing a timezone.

    Runtime callers must provide an aware instant.  ``_dt`` intentionally
    remains permissive for legacy session/date configuration, while market
    rows and ``now`` use this fail-closed parser.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        try:
            return value.astimezone(tz)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        try:
            return parsed.astimezone(tz)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if abs(number) > 100_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None


def _bar_time(bar: Mapping) -> datetime | None:
    for key in ("timestamp", "ts", "time", "datetime", "start"):
        if key in bar:
            return _strict_dt(bar.get(key))
    return None


def _bar_value(bar: Mapping, *keys: str) -> float | None:
    for key in keys:
        if key in bar:
            return finite(bar.get(key))
    return None


def _raw_number(bar: Mapping, *keys: str) -> tuple[bool, float | None]:
    """Return (valid, value), preserving missing-vs-invalid input."""
    for key in keys:
        if key not in bar:
            continue
        value = bar.get(key)
        if isinstance(value, bool):
            return False, None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return False, None
        if not math.isfinite(number):
            return False, None
        return True, number
    return True, None


def _valid_bar_payload(bar: Mapping) -> bool:
    """Validate direct OHLCV input without changing omitted-field semantics."""
    if not isinstance(bar, Mapping):
        return False
    if "interval_seconds" in bar:
        interval = bar.get("interval_seconds")
        try:
            interval_number = float(interval)
        except (TypeError, ValueError, OverflowError):
            return False
        if (isinstance(interval, bool) or not isinstance(interval, (int, float)) or
                not math.isfinite(interval_number) or interval_number != 60.0):
            return False
    valid_high, high = _raw_number(bar, "high", "h")
    valid_low, low = _raw_number(bar, "low", "l")
    valid_close, close = _raw_number(bar, "close", "c")
    if (not valid_high or not valid_low or not valid_close or
            high is None or low is None or close is None or
            high <= 0 or low <= 0 or close <= 0):
        return False
    valid_open, opening = _raw_number(bar, "open", "o")
    if not valid_open or (opening is not None and opening <= 0):
        return False
    valid_volume, volume = _raw_number(bar, "volume", "v")
    if not valid_volume or (volume is not None and volume < 0):
        return False
    if high < low or high < close or low > close:
        return False
    if opening is not None and (high < opening or low > opening):
        return False
    # Optional numeric fields are safety assertions when supplied.  They are
    # not required for legacy direct fixtures.
    for names, lower in (
            (("atr", "atr_1m", "atr_pct"), 0.0),
            (("relative_volume",), 0.0),
            (("spread_bps", "spread_pct"), 0.0),
            (("data_age_seconds", "stale_seconds"), 0.0),
            (("bid",), 0.0), (("ask",), 0.0)):
        valid, number = _raw_number(bar, *names)
        if not valid or (number is not None and number < lower):
            return False
        if names in (("bid",), ("ask",)) and number is not None and number <= 0:
            return False
    return True


def _bar_available(bar: Mapping, timestamp: datetime) -> datetime | None:
    """Return the earliest instant a completed direct bar is usable."""
    values = [timestamp + timedelta(minutes=1)]
    for key in ("as_of", "asof", "observed_at", "received_at", "ingested_at",
                "available_at"):
        if key not in bar:
            continue
        if bar.get(key) is None:
            # Provider DTOs commonly expose optional provenance attributes as
            # None; this is equivalent to omission at the direct boundary.
            continue
        boundary = _strict_dt(bar.get(key))
        if boundary is None:
            return None
        values.append(boundary)
    return max(values)


def close_breaks_range(close: float, boundary: float, breakout_buffer_bps: float,
                       direction: str) -> bool:
    """Apply the live close-confirmed breakout predicate.

    The buffer is relative to the completed signal close, with the range
    boundary remaining the comparison anchor.  Keeping this pure predicate in
    the contract lets replay and live signal generation share exact parity.
    """
    if direction not in {"long", "short"}:
        return False
    try:
        close = float(close)
        boundary = float(boundary)
        bps = float(breakout_buffer_bps)
    except (TypeError, ValueError, OverflowError):
        return False
    if (not math.isfinite(close) or not math.isfinite(boundary) or
            not math.isfinite(bps) or close <= 0 or boundary <= 0 or bps < 0):
        return False
    buffer = close * bps / 10000.0
    return close > boundary + buffer if direction == "long" else close < boundary - buffer


def wilder_atr_series(bars: Iterable[Mapping],
                      period: int = 14) -> list[float | None]:
    """Return Wilder ATR values using each bar and its prior close only.

    ATR is intentionally computed here as a strategy input rather than read
    from a model response.  The first value appears only after ``period``
    complete bars; a signal bar therefore cannot borrow a future close.

    Definition (pinned by ``tests/research/test_ibr_review_gaps.py``):

    * true range ``TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)``,
      and ``H_0 - L_0`` for the first row, which has no prior close;
    * the first value, at index ``period - 1``, is the simple mean of the
      first ``period`` true ranges;
    * every later value is Wilder's recursion
      ``ATR_i = (ATR_{i-1} * (period - 1) + TR_i) / period``;
    * a malformed row contributes a NaN true range, so no value is emitted
      until ``period`` finite true ranges follow it, after which the
      recursion resumes from the last emitted value;
    * there is no session reset: rows from several sessions seed across the
      overnight gap.  Non-mapping rows are skipped entirely.

    Callers: ``generate_ibr_signal`` (through ``_bars_with_atr``, which
    overwrites any ``atr`` the market boundary attached),
    ``evaluate_ibr_breakout`` (its ``history`` fallback) and the research
    replay's width/ATR admission in ``research.ibr``.  The same recursion,
    returned as ``Decimal``, is ``agent.market.wilder_atr_decimal_series``.
    The rule families use a different definition, the simple mean of the
    last ``period`` true ranges (``agent.contracts.rule._atr``), so an
    ``atr_period`` of 14 does not mean the same average in the two families.
    """
    period = int(period)
    if period <= 0:
        raise ValueError("ATR period must be positive")
    rows = [row for row in (bars or ()) if isinstance(row, Mapping)]
    values: list[float | None] = [None] * len(rows)
    ranges: list[float] = []
    previous_close: float | None = None
    current_atr: float | None = None
    for index, row in enumerate(rows):
        high = _bar_value(row, "high", "h")
        low = _bar_value(row, "low", "l")
        close = _bar_value(row, "close", "c")
        if high is None or low is None or close is None or high < low:
            ranges.append(float("nan")); previous_close = close; continue
        tr = high - low if previous_close is None else max(
            high - low, abs(high - previous_close), abs(low - previous_close))
        ranges.append(tr)
        window = ranges[index + 1 - period:index + 1]
        if index + 1 >= period and all(math.isfinite(item) for item in window):
            current_atr = (sum(window) / period if current_atr is None else
                           ((current_atr * (period - 1)) + tr) / period)
            values[index] = current_atr
        previous_close = close
    return values


# Historical name kept for existing callers; it is the same function object.
atr_series = wilder_atr_series


def _bars_with_atr(rows: Iterable[Mapping], period: int = 14) -> list[dict]:
    source = [dict(row) for row in (rows or ()) if isinstance(row, Mapping)]
    values = wilder_atr_series(source, period=period)
    for row, atr in zip(source, values):
        if atr is not None:
            row["atr"] = atr
            row["atr_period"] = int(period)
    return source


def _parse_clock(value: str, fallback: time) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class IBRConfig:
    """IBR parameters; values may also be supplied in a strategy mapping.

    These defaults are the runtime contract.  The research replay's bare
    ``research.ibr.IBRConfig()`` defaults describe a different, legacy
    harness (a 30-minute wick breakout with a fixed 30/60 bps bracket and no
    admission filters); research callers that mean this contract build it
    with ``research.ibr.IBRConfig.from_runtime_contract``, which resolves
    every absent key through :meth:`from_mapping` and therefore through the
    defaults below.

    ``relative_volume_mode`` and ``relative_volume_sessions`` are opt-in:
    the default keeps the registered opening-range-mean baseline, so every
    existing configuration admits exactly the same bars as before.
    """

    timezone: str = DEFAULT_IBR_TIMEZONE
    session_start: str = DEFAULT_SESSION_START
    session_end: str = DEFAULT_SESSION_END
    range_minutes: int = 15
    breakout_buffer_bps: float = 5.0
    min_relative_volume: float = 1.0
    min_ibr_width_atr: float = 0.0
    max_ibr_width_atr: float = float("inf")
    atr_period: int = 14
    max_ibr_width_pct: float = float("inf")
    latest_entry_time: str = "11:00"
    force_flat_minutes_before_close: int = 5
    target_r: float = 2.0
    max_entry_extension_r: float = float("inf")
    stale_minutes: float = 0.5
    max_spread_bps: float = 25.0
    relative_volume_mode: str = RELATIVE_VOLUME_OPENING_RANGE_MEAN
    relative_volume_sessions: int = DEFAULT_RELATIVE_VOLUME_SESSIONS

    def __post_init__(self) -> None:
        if self.relative_volume_mode not in RELATIVE_VOLUME_MODES:
            raise ValueError(
                "relative_volume_mode must be one of "
                + ", ".join(RELATIVE_VOLUME_MODES))
        lower, upper = RELATIVE_VOLUME_SESSIONS_BOUNDS
        sessions = self.relative_volume_sessions
        if (isinstance(sessions, bool) or not isinstance(sessions, int) or
                not lower <= sessions <= upper):
            raise ValueError(
                f"relative_volume_sessions must be an integer from {lower} "
                f"to {upper}")
        # ``inf`` is the omitted/default policy.  Any supplied finite value
        # must be an actual nonnegative number; mapping callers are checked
        # before construction and direct construction is checked here.
        if (isinstance(self.max_ibr_width_pct, bool) or
                not isinstance(self.max_ibr_width_pct, (int, float))):
            raise ValueError("max_ibr_width_pct must be a finite nonnegative number")
        try:
            value = float(self.max_ibr_width_pct)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "max_ibr_width_pct must be a finite nonnegative number") from exc
        if math.isnan(value) or value < 0 or (math.isinf(value) and value != float("inf")):
            raise ValueError("max_ibr_width_pct must be a finite nonnegative number")
        object.__setattr__(self, "max_ibr_width_pct", value)

    @classmethod
    def from_mapping(cls, value: Mapping | None = None) -> "IBRConfig":
        value = value if isinstance(value, Mapping) else {}
        if isinstance(value.get("strategy"), Mapping):
            value = value["strategy"]
        # The strategy config uses breakout_buffer_bps, while a few callers
        # historically used breakout_buffer_pct. Supporting both is harmless
        # and keeps replay records portable.
        kwargs = {}
        for field_name in cls.__dataclass_fields__:
            if field_name in value:
                kwargs[field_name] = value[field_name]
        if "breakout_buffer_bps" not in kwargs and "breakout_buffer_pct" in value:
            kwargs["breakout_buffer_bps"] = float(value["breakout_buffer_pct"]) * 100
        if "max_ibr_width_pct" not in kwargs and "max_range_width_pct" in value:
            kwargs["max_ibr_width_pct"] = value["max_range_width_pct"]
        if "max_ibr_width_pct" in kwargs:
            supplied = kwargs["max_ibr_width_pct"]
            try:
                supplied_number = float(supplied)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "max_ibr_width_pct must be a finite nonnegative number") from exc
            if (isinstance(supplied, bool) or
                    not isinstance(supplied, (int, float)) or
                    not math.isfinite(supplied_number) or supplied_number < 0):
                raise ValueError(
                    "max_ibr_width_pct must be a finite nonnegative number")
            kwargs["max_ibr_width_pct"] = supplied_number
        # Config may use one nested session string ("09:30-09:45").
        session = value.get("session")
        if isinstance(session, str) and "-" in session:
            start, end = session.split("-", 1)
            kwargs.setdefault("session_start", start.strip())
            kwargs.setdefault("session_end", end.strip())
        return cls(**kwargs)

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001 - invalid config is handled safely
            return ZoneInfo(DEFAULT_IBR_TIMEZONE)

    def session_bounds(self, session_date: date | datetime | str | None = None):
        if isinstance(session_date, str):
            try:
                session_date = date.fromisoformat(session_date)
            except ValueError:
                session_date = None
        local = _dt(session_date, self.zone) if session_date is not None else None
        day = local.date() if local else datetime.now(self.zone).date()
        start_clock = _parse_clock(self.session_start, time(9, 30))
        end_clock = _parse_clock(self.session_end, time(9, 45))
        start = datetime.combine(day, start_clock, tzinfo=self.zone)
        end = datetime.combine(day, end_clock, tzinfo=self.zone)
        # ``range_minutes`` is the source of truth.  A stale/default
        # ``session_end`` must not accidentally truncate a configured 30m
        # opening range to 15m.
        if end <= start or (self.session_end == DEFAULT_SESSION_END and int(self.range_minutes) != 15):
            end = start + timedelta(minutes=int(self.range_minutes))
        return start, end


def _session_key(ts: datetime) -> str:
    return ts.astimezone(ZoneInfo(DEFAULT_IBR_TIMEZONE)).date().isoformat()


def build_ibr_range(
    bars: Iterable[Mapping],
    *,
    session_date: date | datetime | str | None = None,
    config: IBRConfig | Mapping | None = None,
) -> dict | None:
    """Construct a completed IBR from one-minute regular-session bars.

    Returns ``None`` when the range is incomplete or malformed.  The result
    carries all timestamps and source bars needed to audit no-lookahead.
    """
    cfg = config if isinstance(config, IBRConfig) else IBRConfig.from_mapping(config)
    source = [raw for raw in (bars or ()) if isinstance(raw, Mapping)]
    # Replay callers commonly pass one session without a separate date. Infer
    # it from the first timestamp instead of silently looking at today's date.
    if session_date is None:
        for raw in source:
            inferred = _bar_time(raw)
            if inferred is not None:
                session_date = inferred.date()
                break
    start, end = cfg.session_bounds(session_date)
    rows = []
    for raw in source:
        if not isinstance(raw, Mapping):
            continue
        ts = _bar_time(raw)
        high = _bar_value(raw, "high", "h")
        low = _bar_value(raw, "low", "l")
        close = _bar_value(raw, "close", "c")
        volume = _bar_value(raw, "volume", "v")
        if ts is None or not _valid_bar_payload(raw):
            continue
        if _bar_available(raw, ts) is None:
            continue
        # Timestamp is bar open; only bars wholly inside the opening window
        # and already closed at evaluation time can contribute.
        close_ts = ts + timedelta(minutes=1)
        if ts < start or close_ts > end:
            continue
        rows.append((ts, high, low, close, volume if volume is not None else 0.0))
    rows.sort(key=lambda item: item[0])
    expected = max(1, int(cfg.range_minutes))
    # A complete IBR needs every minute; accepting a sparse range would make
    # its width and relative-volume denominator path dependent.
    if len(rows) < expected:
        return None
    rows = rows[:expected]
    for previous, current in zip(rows, rows[1:]):
        if current[0] - previous[0] != timedelta(minutes=1):
            return None
    high = max(item[1] for item in rows)
    low = min(item[2] for item in rows)
    width = high - low
    result = {
        "session": rows[0][0].date().isoformat(),
        "timezone": cfg.timezone,
        "range_start": rows[0][0].isoformat(),
        "range_end": (rows[-1][0] + timedelta(minutes=1)).isoformat(),
        "range_end_ts": (rows[-1][0] + timedelta(minutes=1)).timestamp(),
        "high": high,
        "low": low,
        "width": width,
        "width_pct": width / low * 100 if low else None,
        "volume_mean": sum(item[4] for item in rows) / len(rows),
        "bars": len(rows),
        "complete": True,
    }
    return result


def _strict_nonnegative(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def ibr_range_summary(value: Mapping) -> dict | None:
    """Validate and summarize an externally supplied IBR range.

    Range width is always recomputed from the validated positive boundaries.
    Optional numeric metadata is preserved when valid, but an explicitly
    malformed value fails closed rather than being silently discarded.
    """
    if not isinstance(value, Mapping):
        return None
    high_key = "high" if "high" in value else "ibr_high"
    low_key = "low" if "low" in value else "ibr_low"
    if high_key not in value or low_key not in value:
        return None
    high = value.get(high_key)
    low = value.get(low_key)
    if isinstance(high, bool) or isinstance(low, bool):
        return None
    try:
        high = float(high)
        low = float(low)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(high) or not math.isfinite(low) or
            high <= 0 or low <= 0 or high <= low):
        return None
    for key in ("width", "volume_mean", "width_pct", "atr"):
        if key not in value:
            continue
        metadata = _strict_nonnegative(value.get(key))
        if metadata is None or (key == "width" and metadata <= 0):
            return None
    out = dict(value)
    out.update({"high": high, "low": low, "width": high - low})
    for key in ("volume_mean", "width_pct", "atr"):
        if key in out:
            out[key] = float(out[key])
    return out


def _range_from_mapping(value: Mapping) -> dict | None:
    return ibr_range_summary(value)


def _row_label(row: Mapping, key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "_")
    return text or None


def same_clock_prior_row(row: object, *, feed: str | None,
                         provider: str | None, available_by: datetime,
                         before: date, zone: ZoneInfo) -> datetime | None:
    """Return a prior row's timestamp when it may enter a same-clock baseline.

    The row must be a valid completed bar mapping, available by
    ``available_by``, dated (in ``zone``) before ``before``, carry ``feed``
    and, when ``provider`` is given, that provider.  This is the one filter
    :func:`same_clock_relative_volume` applies, exposed so a caller that
    selects prior sessions (the research replay) uses exactly the same rule.
    """
    if not isinstance(row, Mapping):
        return None
    row_ts = _bar_time(row)
    if row_ts is None or not _valid_bar_payload(row):
        return None
    row_available = _bar_available(row, row_ts)
    if row_available is None or row_available > available_by:
        return None
    if row_ts.astimezone(zone).date() >= before:
        return None
    if _row_label(row, "feed") != feed:
        return None
    if provider is not None and _row_label(row, "provider") != provider:
        return None
    return row_ts


def same_clock_relative_volume(
    bar: Mapping,
    prior_session_bars: Iterable[Mapping] | None,
    *,
    sessions: int = DEFAULT_RELATIVE_VOLUME_SESSIONS,
    session_start: str = DEFAULT_SESSION_START,
    timezone_name: str = DEFAULT_IBR_TIMEZONE,
    available_by: datetime | None = None,
) -> float | None:
    """Return the candidate's volume over its same-clock prior-session mean.

    This is the IBR counterpart of the rule family's v6 same-clock RVOL
    (``agent.contracts.rule._same_clock_relative_volume``).  The candidate's
    offset from its own session's local ``session_start`` is matched in the
    ``sessions`` most recent prior session dates found in
    ``prior_session_bars``.  Each of those sessions must contain exactly one
    valid bar at that clock minute, with the candidate's feed (and provider,
    when the candidate names one) and a positive volume; anything else fails
    closed with ``None`` instead of quietly changing the baseline population.
    Matching is by each session's own local open, so a daylight-saving change
    between sessions does not shift the clock.  A prior row is used only once
    it was available by ``available_by`` (the candidate bar's close when
    omitted); rows on or after the candidate's date are ignored.
    """
    if not isinstance(bar, Mapping):
        return None
    lower, upper = RELATIVE_VOLUME_SESSIONS_BOUNDS
    if (isinstance(sessions, bool) or not isinstance(sessions, int) or
            not lower <= sessions <= upper):
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 - invalid config fails closed
        return None
    stamp = _bar_time(bar)
    if stamp is None:
        return None
    local = stamp.astimezone(zone)
    clock = _parse_clock(session_start, time(9, 30))
    opened = datetime.combine(local.date(), clock, tzinfo=zone)
    elapsed_seconds = (local - opened).total_seconds()
    if (elapsed_seconds < 0 or elapsed_seconds % 60 != 0 or
            local.second or local.microsecond):
        return None
    elapsed_minutes = int(elapsed_seconds // 60)
    boundary = (available_by if available_by is not None
                else stamp + timedelta(minutes=1))
    feed = _row_label(bar, "feed")
    provider = _row_label(bar, "provider")
    grouped: dict[date, list[tuple[datetime, Mapping]]] = {}
    for row in prior_session_bars or ():
        row_ts = same_clock_prior_row(
            row, feed=feed, provider=provider, available_by=boundary,
            before=local.date(), zone=zone)
        if row_ts is None:
            continue
        grouped.setdefault(row_ts.astimezone(zone).date(), []).append(
            (row_ts, row))
    prior_days = sorted(grouped)
    if len(prior_days) < sessions:
        return None
    volumes: list[float] = []
    for prior_day in prior_days[-sessions:]:
        target = (datetime.combine(prior_day, clock, tzinfo=zone) +
                  timedelta(minutes=elapsed_minutes))
        matches = [row for row_ts, row in grouped[prior_day]
                   if row_ts == target]
        if len(matches) != 1:
            return None
        volume = _bar_value(matches[0], "volume", "v")
        if volume is None or volume <= 0:
            return None
        volumes.append(volume)
    current_volume = _bar_value(bar, "volume", "v")
    if current_volume is None or current_volume <= 0:
        return None
    baseline = math.fsum(volumes) / len(volumes)
    if not math.isfinite(baseline) or baseline <= 0:
        return None
    return current_volume / baseline


def evaluate_ibr_breakout(
    ibr: Mapping,
    bar: Mapping,
    *,
    config: IBRConfig | Mapping | None = None,
    symbol: str | None = None,
    session_state: Mapping | None = None,
    now=None,
    prior_session_bars: Iterable[Mapping] | None = None,
) -> dict | None:
    """Evaluate one completed post-range bar and return a signal or ``None``.

    ``prior_session_bars`` is read only when the configuration opts into
    ``relative_volume_mode="same_clock"``; it holds earlier sessions' bars
    for the same symbol.
    """
    cfg = config if isinstance(config, IBRConfig) else IBRConfig.from_mapping(config)
    rng = _range_from_mapping(ibr)
    if rng is None or rng.get("complete") is False:
        return None
    ts = _bar_time(bar)
    close = _bar_value(bar, "close", "c")
    high = _bar_value(bar, "high", "h")
    low = _bar_value(bar, "low", "l")
    volume = _bar_value(bar, "volume", "v")
    if ts is None or not _valid_bar_payload(bar):
        return None
    available = _bar_available(bar, ts)
    if available is None:
        return None
    close_ts = ts + timedelta(minutes=1)
    end = _dt(rng.get("range_end_ts") or rng.get("range_end"), cfg.zone)
    if end is None or close_ts <= end:
        return None  # next-bar eligibility and no lookahead
    session = str(rng.get("session") or ts.date().isoformat())
    state = session_state if isinstance(session_state, Mapping) else {}
    seen = state.get("signals") if isinstance(state.get("signals"), (set, list, tuple)) else state
    already_seen = (symbol or "", session) in seen
    if isinstance(seen, Mapping):
        already_seen = already_seen or seen.get(symbol or "") == session
    if already_seen:
        return None
    # Regular-session bars only; a stale bar, halt, or wide spread cannot arm.
    latest = _parse_clock(cfg.latest_entry_time, time(11, 0))
    local_ts = ts.astimezone(cfg.zone)
    session_start, _ = cfg.session_bounds(session)
    if local_ts.date().isoformat() != session or local_ts < session_start or local_ts.time() > latest:
        return None
    current = None
    if now is not None:
        current = _strict_dt(now, cfg.zone)
        if current is None or available > current or close_ts > current:
            return None
        if (current - close_ts).total_seconds() > cfg.stale_minutes * 60:
            return None
    data_age = finite(bar.get("data_age_seconds", bar.get("stale_seconds")))
    if data_age is not None and data_age > cfg.stale_minutes * 60:
        return None
    if bool(bar.get("halt") or bar.get("halted")):
        return None
    spread = finite(bar.get("spread_bps", bar.get("spread_pct")))
    if spread is None:
        bid, ask = finite(bar.get("bid")), finite(bar.get("ask"))
        if bid is not None and ask is not None and bid > 0:
            spread = (ask - bid) / ((ask + bid) / 2) * 10000.0
    if spread is not None:
        if "spread_pct" in bar and "spread_bps" not in bar:
            spread *= 100.0
        if spread > cfg.max_spread_bps:
            return None
    width = float(rng["width"])
    width_pct = finite(rng.get("width_pct"))
    if width <= 0:
        return None
    if width_pct is not None and width_pct > cfg.max_ibr_width_pct:
        return None
    atr = finite(bar.get("atr", bar.get("atr_1m", bar.get("atr_pct", rng.get("atr")))))
    if atr is None:
        # Standalone evaluators may provide the completed history alongside
        # the candidate bar.  Compute only through that bar; never inspect a
        # future row from the caller's source sequence.
        history = bar.get("history") or bar.get("bars") or bar.get("_bars")
        if isinstance(history, Iterable) and not isinstance(history, (str, bytes, Mapping)):
            bounded_history = []
            previous_history_ts = None
            history_valid = True
            for item in history:
                if not isinstance(item, Mapping):
                    history_valid = False
                    break
                item_ts = _bar_time(item)
                if item_ts is None:
                    history_valid = False
                    break
                if item_ts > ts:
                    continue
                if (previous_history_ts is not None and
                        item_ts < previous_history_ts):
                    history_valid = False
                    break
                previous_history_ts = item_ts
                if not _valid_bar_payload(item):
                    history_valid = False
                    break
                item_available = _bar_available(item, item_ts)
                if item_available is None:
                    history_valid = False
                    break
                if current is not None and item_available > current:
                    continue
                bounded_history.append(item)
            values = (wilder_atr_series(
                bounded_history, period=int(getattr(cfg, "atr_period", 14)))
                      if history_valid else [])
            if values:
                atr = values[-1]
    if atr is not None and "atr_pct" in bar:
        atr = close * atr / 100.0
    if atr and atr > 0:
        # ``atr`` is a one-minute ATR while ``width`` spans ``range_minutes``
        # of those minutes, so a raw quotient grows with the opening window
        # and carries a unit the authored band was never written in.  On the
        # shipped 24-ETF universe the raw ratio runs 3.6-14 at 15 minutes and
        # 6.6-23 at 45, so the authored [0.25, 3.0] band admitted nothing at
        # any range length.  Scale the one-minute volatility to the range
        # horizon the way a diffusion does.  The quotient is then
        # horizon-invariant (measured p5 0.93-0.98 and median 1.57-1.86 at 15,
        # 30, 45 and 60 minutes) and the authored band selects the intended
        # tail: unusually wide or unusually narrow openings relative to the
        # session's own volatility.
        ratio = width / (atr * math.sqrt(max(1, int(cfg.range_minutes))))
        if ratio < cfg.min_ibr_width_atr or ratio > cfg.max_ibr_width_atr:
            return None
    elif cfg.min_ibr_width_atr > 0 or cfg.max_ibr_width_atr < float("inf"):
        return None
    same_clock = cfg.relative_volume_mode == RELATIVE_VOLUME_SAME_CLOCK
    if same_clock:
        # Opt-in only.  A bar-supplied ``relative_volume`` is ignored here:
        # it may have been computed against another baseline, and a signal
        # labelled same-clock must carry a same-clock value.
        relvol = same_clock_relative_volume(
            bar, prior_session_bars,
            sessions=cfg.relative_volume_sessions,
            session_start=cfg.session_start, timezone_name=cfg.timezone,
            available_by=current if current is not None else available)
    else:
        mean_volume = finite(rng.get("volume_mean"))
        relvol = finite(bar.get("relative_volume"))
        if relvol is None and mean_volume and mean_volume > 0 and volume is not None:
            relvol = volume / mean_volume
    if relvol is None or relvol < cfg.min_relative_volume:
        return None
    direction = ("long" if close_breaks_range(
        close, float(rng["high"]), cfg.breakout_buffer_bps, "long") else
        "short" if close_breaks_range(
            close, float(rng["low"]), cfg.breakout_buffer_bps, "short") else None)
    if direction is None:
        return None
    entry = close
    natural_stop = float(rng["low"] if direction == "long" else rng["high"])
    distance = max(abs(entry - natural_stop),
                   entry * MIN_STOP_DISTANCE_FRACTION)
    if distance <= 0:
        return None
    stop = entry - distance if direction == "long" else entry + distance
    extension = max(0.0, (entry - float(rng["high"])) / width if direction == "long"
                    else (float(rng["low"]) - entry) / width)
    if extension > cfg.max_entry_extension_r:
        return None
    target = entry + cfg.target_r * distance if direction == "long" else entry - cfg.target_r * distance
    flat_at = _force_flat_at(local_ts, cfg)
    signal = {
        "action": "open", "symbol": symbol, "direction": direction,
        "setup_type": "ibr_breakout", "session": session,
        # ``signal_ts`` is the signal bar's close and keeps that meaning
        # (rule signals use the bar open); the explicit pair below removes
        # the ambiguity for any consumer that compares the two families.
        "signal_ts": close_ts.timestamp(), "signal_time": close_ts.isoformat(),
        "signal_bar_open_ts": ts.timestamp(),
        "signal_bar_close_ts": close_ts.timestamp(),
        "entry_price": entry, "stop_price": stop, "target_price": target,
        "stop_distance": distance, "target_r": cfg.target_r,
        "relative_volume": relvol, "range_high": float(rng["high"]),
        "range_low": float(rng["low"]), "range_width": width,
        "extension_r": extension, "force_flat": True,
        "force_flat_at": flat_at,
        "force_flat_ts": _dt(flat_at, cfg.zone).timestamp() if _dt(flat_at, cfg.zone) else None,
        "force_flat_reason": "regular_session_close",
    }
    if same_clock:
        signal["relative_volume_mode"] = RELATIVE_VOLUME_SAME_CLOCK
        signal["relative_volume_sessions"] = cfg.relative_volume_sessions
    if isinstance(session_state, dict):
        if isinstance(session_state.get("signals"), set):
            session_state["signals"].add((symbol, session))
        elif isinstance(session_state.get("signals"), list):
            session_state["signals"].append((symbol, session))
        else:
            session_state[(symbol, session)] = True
    return signal


def evaluate_exit(direction: str, bar: Mapping, *, stop_price: float,
                  target_price: float, force_flat: bool = False,
                  force_flat_at: str | None = None) -> dict | None:
    """Resolve one completed bar's exits with an explicit stop-first tie.

    If both stop and target are touched by the same OHLC bar, intrabar order
    is unknowable; conservative accounting marks the stop first and records
    ``tie=True`` for audit/replay consumers.
    """
    high = finite(bar.get("high", bar.get("h"))); low = finite(bar.get("low", bar.get("l")))
    if high is None or low is None:
        return None
    if direction == "long":
        hit_stop, hit_target = low <= stop_price, high >= target_price
    elif direction == "short":
        hit_stop, hit_target = high >= stop_price, low <= target_price
    else:
        return None
    if hit_stop:
        return {"exit_reason": "stop", "exit_price": float(stop_price),
                "stop_first": True, "tie": bool(hit_target),
                "force_flat": bool(force_flat), "force_flat_at": force_flat_at}
    if hit_target:
        return {"exit_reason": "target", "exit_price": float(target_price),
                "stop_first": False, "tie": False,
                "force_flat": bool(force_flat), "force_flat_at": force_flat_at}
    return None


def _force_flat_at(signal_ts: datetime, cfg: IBRConfig) -> str:
    # US regular session ends at 16:00 local. Keep this metadata explicit so
    # options and shares share exactly the same underlying exit clock.
    close = datetime.combine(signal_ts.date(), time(16, 0), tzinfo=cfg.zone)
    return (close - timedelta(minutes=max(0, cfg.force_flat_minutes_before_close))).isoformat()


def generate_ibr_signal(
    symbol: str,
    bars: Iterable[Mapping],
    *,
    config: IBRConfig | Mapping | None = None,
    session_state: dict | None = None,
    now=None,
    prior_session_bars: Iterable[Mapping] | None = None,
) -> dict | None:
    """Build the opening range and scan completed bars for the first signal.

    ``bars`` is the current session.  ``prior_session_bars`` (earlier
    sessions of the same symbol) is consulted only by the opt-in
    ``relative_volume_mode="same_clock"`` baseline.
    """
    cfg = config if isinstance(config, IBRConfig) else IBRConfig.from_mapping(config)
    current = None
    if now is not None:
        current = _strict_dt(now, cfg.zone)
        if current is None:
            return None
    raw_rows = [bar for bar in (bars or ()) if isinstance(bar, Mapping)]
    session_date = None
    for bar in raw_rows:
        timestamp = _bar_time(bar)
        if timestamp is not None:
            session_date = timestamp.astimezone(cfg.zone).date()
            break
    if session_date is None:
        return None
    session_start, _ = cfg.session_bounds(session_date)
    latest = _parse_clock(cfg.latest_entry_time, time(11, 0))
    rows = []
    for bar in raw_rows:
        timestamp = _bar_time(bar)
        if timestamp is None:
            return None
        available = _bar_available(bar, timestamp)
        if available is None:
            return None
        # A future recorder row is simply not in the current feature prefix;
        # its malformed OHLC must not become observable before availability.
        if current is not None and available > current:
            continue
        local_timestamp = timestamp.astimezone(cfg.zone)
        eligible_current_session = (
            local_timestamp.date() == session_date and
            local_timestamp >= session_start and
            local_timestamp.time() <= latest)
        if not _valid_bar_payload(bar):
            if eligible_current_session:
                return None
            continue
        rows.append(bar)
    rows.sort(key=lambda row: _bar_time(row) or datetime.min.replace(tzinfo=timezone.utc))
    # Production OHLCV adapters may return plain dictionaries.  Attach ATR
    # before scanning so configured width bounds have a point-in-time value.
    rows = _bars_with_atr(rows, period=int(getattr(cfg, "atr_period", 14)))
    rng = build_ibr_range(rows, config=cfg)
    if rng is None:
        return None
    candidates = sorted(rows, key=lambda row: _bar_time(row) or datetime.min.replace(tzinfo=timezone.utc))
    for bar in candidates:
        signal = evaluate_ibr_breakout(rng, bar, config=cfg, symbol=symbol,
                                       session_state=session_state, now=now,
                                       prior_session_bars=prior_session_bars)
        if signal is not None:
            if session_state is not None:
                if isinstance(session_state.get("signals"), set):
                    session_state["signals"].add((symbol, signal["session"]))
                elif isinstance(session_state.get("signals"), list):
                    session_state["signals"].append((symbol, signal["session"]))
                else:
                    session_state[(symbol, signal["session"])] = True
            return signal
    return None


def setup_evidence(snapshot: Mapping, cfg: Mapping | None = None) -> dict:
    """Expose deterministic evidence in the shape used by the core prompt."""
    source = snapshot if isinstance(snapshot, Mapping) else {}
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    rng = source.get("ibr_range") or source.get("ibr") or source.get("range")
    if not isinstance(rng, Mapping):
        rng = {"high": source.get("ibr_high"), "low": source.get("ibr_low"),
               "complete": source.get("ibr_complete", False)}
    out = {"ibr_range": dict(rng), "ibr_breakout": {"long": False, "short": False}}
    bars = source.get("bars") or source.get("candles")
    if isinstance(bars, Iterable) and not isinstance(bars, (str, bytes, Mapping)):
        built = build_ibr_range(bars, config=strategy)
        if built is not None:
            out["ibr_range"] = built
            for bar in sorted(bars, key=lambda row: _bar_time(row) or datetime.min.replace(tzinfo=timezone.utc)):
                signal = evaluate_ibr_breakout(built, bar, config=strategy)
                if signal:
                    out["ibr_breakout"][signal["direction"]] = True
                    out["signal"] = signal
                    break
    return out


register("ibr", setup_evidence)
