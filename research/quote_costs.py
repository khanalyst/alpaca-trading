"""Fit an execution-cost schedule to the recorded quote corpus.

The shipped cost model carries two constants — a 4 bps quoted spread and a
6 bps adverse-slippage charge — applied uniformly to every symbol at every
minute of the session.  They are an assumption, and on the configured ETF
universe they are the dominant term in every replayed result: a 17 bps
round trip against a stop the risk gate pins near 83 bps is a fixed 0.17R
toll that no strategy in the catalog can outrun.

This module replaces the assumption with a measurement.  It streams the
recorded quotes, fits the quoted spread and displayed depth per symbol and
per half-hour of the session, and builds a :class:`~research.costs.CostModel`
from the result.

It is deliberately not a way to make costs smaller.  The schedule reports what
the corpus actually contains; if the measured spread is wide, the model is
wide.  Conservatism is an explicit, auditable choice — the caller names a
percentile of the measured distribution rather than inheriting a number nobody
can trace — and the schedule carries the counts, coverage, and feed provenance
needed to check it.  A model built here still validates against the runtime's
own rejection caps.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import math
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .costs import (CostError, CostModel, DEFAULT_FEE_BPS,
                    RUNTIME_MAX_SLIPPAGE_BPS, RUNTIME_MAX_SPREAD_BPS)
from .edge_ledger import content_hash


QUOTE_COST_SCHEMA = "quote-cost-schedule.v1"
_NY = ZoneInfo("America/New_York")
SESSION_MINUTES = 390
# Half-hour resolution over the regular session.  Finer buckets split the
# corpus thinner than the spread distribution justifies; coarser ones hide the
# open/close widening that matters most to an intraday rule.
BUCKET_MINUTES = 30
PERCENTILES = ("p25", "median", "p75", "p90", "p95")
# Spread is measured in basis points of the mid.  The ceiling is far above any
# admissible quote (the runtime rejects past 100 bps) so overflow marks
# genuinely broken data rather than a wide-but-real market.
_SPREAD_CEILING_BPS = 500.0
_SPREAD_BIN_BPS = .05
# Displayed size at the touch, log-spaced: depth spans several orders of
# magnitude across this universe and a linear grid wastes almost every bin.
_DEPTH_LOG_CEILING = 7.0          # 10**7 shares
_DEPTH_LOG_BIN = .02


class QuoteCostError(ValueError):
    """Raised for a corpus that cannot support a cost measurement."""


@dataclass
class _Histogram:
    """Streaming fixed-width histogram, for percentiles over millions of rows.

    Retaining every observation would cost gigabytes on a production corpus.
    Bin width is chosen so the quantisation error is far below the precision
    any cost decision is made at.
    """

    width: float
    ceiling: float
    floor: float = 0.0
    count: int = 0
    total: float = 0.0
    overflow: int = 0
    minimum: float = math.inf
    maximum: float = -math.inf
    bins: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        if value > self.ceiling or value < self.floor:
            self.overflow += 1
            return
        self.bins[int((value - self.floor) / self.width)] += 1

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    def quantile(self, fraction: float) -> float | None:
        """Upper edge of the bin containing *fraction* of the mass.

        Overflow observations sit above every bin, so they are counted at the
        top rather than dropped: a corpus with wide outliers reports a wider
        tail, never a narrower one.
        """
        if not self.count:
            return None
        target = fraction * self.count
        seen = 0
        for index in sorted(self.bins):
            seen += self.bins[index]
            if seen >= target:
                return self.floor + (index + 1) * self.width
        return self.maximum if math.isfinite(self.maximum) else None

    def summary(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": self.mean,
            "min": self.minimum if math.isfinite(self.minimum) else None,
            "max": self.maximum if math.isfinite(self.maximum) else None,
            "p25": self.quantile(.25), "median": self.quantile(.50),
            "p75": self.quantile(.75), "p90": self.quantile(.90),
            "p95": self.quantile(.95),
            "above_measured_range": self.overflow,
        }


def _spread_histogram() -> _Histogram:
    return _Histogram(width=_SPREAD_BIN_BPS, ceiling=_SPREAD_CEILING_BPS)


def _depth_histogram() -> _Histogram:
    return _Histogram(width=_DEPTH_LOG_BIN, ceiling=_DEPTH_LOG_CEILING,
                      floor=0.0)


@dataclass
class _Accumulator:
    spread: _Histogram = field(default_factory=_spread_histogram)
    depth: _Histogram = field(default_factory=_depth_histogram)
    sessions: set = field(default_factory=set)
    rejected: int = 0

    def summary(self) -> dict[str, Any]:
        depth = self.depth.summary()
        # Depth is accumulated in log10 space; report it back in shares.
        shares = {name: (None if depth[name] is None else 10.0 ** depth[name])
                  for name in (*PERCENTILES, "mean")}
        return {
            "quote_count": self.spread.count,
            "session_count": len(self.sessions),
            "rejected_quote_count": self.rejected,
            "spread_bps": self.spread.summary(),
            "touch_shares": {**shares, "count": self.depth.count},
        }


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(row: Any) -> datetime | None:
    raw = _value(row, "timestamp", _value(row, "ts"))
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if raw is None:
        return None
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def bucket_label(minutes: float) -> str:
    """The half-hour session bucket a quote belongs to."""
    if minutes < 0:
        return "pre_open"
    if minutes >= SESSION_MINUTES:
        return "post_close"
    start = int(minutes // BUCKET_MINUTES) * BUCKET_MINUTES
    return f"m{start:03d}_{start + BUCKET_MINUTES:03d}"


def _session_minutes(stamp: datetime) -> float:
    local = stamp.astimezone(_NY)
    opened = datetime.combine(local.date(), time(9, 30), tzinfo=_NY)
    return (local.timestamp() - opened.timestamp()) / 60.0


def measure_quote_costs(quotes: Iterable[Any], *,
                        feed: str | None = None,
                        provider: str | None = None,
                        min_quotes_per_cell: int = 500) -> dict[str, Any]:
    """Fit a spread and depth schedule from recorded two-sided quotes.

    Accepts anything iterable — normalized ``QuoteSnapshot`` records or raw
    corpus mappings — and streams it, so a production corpus never has to be
    held in memory.  A quote is used only when it is two-sided and positive;
    anything else is counted as rejected rather than silently shaping the fit.
    """
    if int(min_quotes_per_cell) < 1:
        raise QuoteCostError("min_quotes_per_cell must be positive")
    universe = _Accumulator()
    by_symbol: dict[str, _Accumulator] = defaultdict(_Accumulator)
    by_cell: dict[tuple[str, str], _Accumulator] = defaultdict(_Accumulator)
    feeds: set[str] = set()
    providers: set[str] = set()
    first_session: str | None = None
    last_session: str | None = None
    seen = 0

    for row in quotes:
        if str(_value(row, "kind", "quote")).strip().lower() not in {"quote", ""}:
            continue
        seen += 1
        symbol = str(_value(row, "symbol", "")).strip().upper()
        bid = _number(_value(row, "bid"))
        ask = _number(_value(row, "ask"))
        stamp = _timestamp(row)
        if (not symbol or stamp is None or bid is None or ask is None or
                bid <= 0 or ask < bid):
            universe.rejected += 1
            by_symbol[symbol or "?"].rejected += 1
            continue
        mid = (bid + ask) / 2.0
        if mid <= 0:
            universe.rejected += 1
            by_symbol[symbol].rejected += 1
            continue
        spread_bps = (ask - bid) / mid * 10_000.0
        minutes = _session_minutes(stamp)
        session = stamp.astimezone(_NY).date().isoformat()
        cell = (symbol, bucket_label(minutes))
        # Displayed size at the touch is the binding constraint on how much of
        # an order fills without walking the book.  Take the thinner side: an
        # entry and its exit cross in opposite directions over the position's
        # life, so the smaller of the two is the honest capacity estimate.
        bid_size = _number(_value(row, "bid_size"))
        ask_size = _number(_value(row, "ask_size"))
        sizes = [size for size in (bid_size, ask_size)
                 if size is not None and size > 0]
        for accumulator in (universe, by_symbol[symbol], by_cell[cell]):
            accumulator.spread.add(spread_bps)
            accumulator.sessions.add(session)
            if sizes:
                accumulator.depth.add(math.log10(min(sizes)))
        identity = _value(row, "identity")
        row_feed = _value(row, "feed", _value(identity, "feed"))
        row_provider = _value(row, "provider", _value(identity, "provider"))
        if row_feed:
            feeds.add(str(row_feed).strip().lower())
        if row_provider:
            providers.add(str(row_provider).strip().lower())
        if first_session is None or session < first_session:
            first_session = session
        if last_session is None or session > last_session:
            last_session = session

    if not universe.spread.count:
        raise QuoteCostError(
            "no usable two-sided quotes in the corpus; cannot fit a cost model")
    if feed is not None and feeds and {str(feed).strip().lower()} != feeds:
        raise QuoteCostError(
            f"corpus feeds {sorted(feeds)} do not match the expected {feed!r}")

    symbols = {}
    for symbol, accumulator in sorted(by_symbol.items()):
        if not accumulator.spread.count:
            continue
        buckets = {
            bucket: cell.summary()
            for (cell_symbol, bucket), cell in sorted(by_cell.items())
            if cell_symbol == symbol and
            cell.spread.count >= int(min_quotes_per_cell)}
        symbols[symbol] = {**accumulator.summary(), "buckets": buckets,
                           "sparse_buckets": sum(
                               1 for (cell_symbol, _bucket), cell
                               in by_cell.items()
                               if cell_symbol == symbol and
                               cell.spread.count < int(min_quotes_per_cell))}
    schedule = {
        "schema": QUOTE_COST_SCHEMA,
        "measured": {
            "quote_rows_seen": seen,
            "quote_rows_used": universe.spread.count,
            "quote_rows_rejected": universe.rejected,
            "first_session": first_session, "last_session": last_session,
            "feeds": sorted(feeds), "providers": sorted(providers),
            "min_quotes_per_cell": int(min_quotes_per_cell),
            "bucket_minutes": BUCKET_MINUTES,
        },
        "universe": universe.summary(),
        "symbols": symbols,
    }
    schedule["schedule_hash"] = content_hash(schedule)
    return schedule


def _cell(schedule: Mapping[str, Any], symbol: str | None,
          bucket: str | None) -> tuple[Mapping[str, Any], str]:
    """Resolve the tightest measured cell available, and say which was used."""
    symbols = schedule.get("symbols") or {}
    if symbol:
        entry = symbols.get(str(symbol).strip().upper())
        if entry is not None:
            if bucket:
                measured = (entry.get("buckets") or {}).get(str(bucket))
                if measured is not None:
                    return measured, f"symbol_bucket:{symbol}:{bucket}"
            return entry, f"symbol:{symbol}"
    return schedule["universe"], "universe"


def _percentile(section: Mapping[str, Any], name: str) -> float:
    value = _number((section or {}).get(name))
    if value is None or value < 0:
        raise QuoteCostError(
            f"cost schedule has no usable {name!r} measurement")
    return value


def cost_model_from_schedule(
        schedule: Mapping[str, Any], *, symbol: str | None = None,
        bucket: str | None = None, percentile: str = "p75",
        order_shares: float | None = None,
        depth_percentile: str = "p25",
        max_impact_half_spreads: float = 4.0,
        fee_bps: float = DEFAULT_FEE_BPS,
        max_spread_bps: float = RUNTIME_MAX_SPREAD_BPS,
        max_slippage_bps: float = RUNTIME_MAX_SLIPPAGE_BPS) -> CostModel:
    """Build a :class:`CostModel` from a measured schedule.

    ``percentile`` selects how conservative the spread assumption is; the
    default takes the 75th percentile of the measured distribution rather than
    its median, so the model sits above a typical quote without chasing the
    tail.  ``order_shares`` adds the size term: an order larger than the
    displayed depth walks the book, and with top-of-book quotes the tightest
    defensible charge for the excess is a further half spread per depth
    multiple, bounded by ``max_impact_half_spreads``.

    The result is an ordinary ``CostModel`` and is validated against the same
    runtime rejection caps as a configured one, so a schedule cannot license a
    fill the runtime would refuse to submit.
    """
    if str(schedule.get("schema")) != QUOTE_COST_SCHEMA:
        raise QuoteCostError(
            f"expected {QUOTE_COST_SCHEMA}, got {schedule.get('schema')!r}")
    if percentile not in PERCENTILES:
        raise QuoteCostError(f"percentile must be one of {PERCENTILES}")
    if depth_percentile not in PERCENTILES:
        raise QuoteCostError(f"depth_percentile must be one of {PERCENTILES}")
    impact_cap = _number(max_impact_half_spreads)
    if impact_cap is None or impact_cap < 0:
        raise QuoteCostError("max_impact_half_spreads must be non-negative")
    section, origin = _cell(schedule, symbol, bucket)
    spread_bps = _percentile(section.get("spread_bps"), percentile)
    half_spread = spread_bps / 2.0

    impact_bps = 0.0
    depth_shares = _number((section.get("touch_shares") or {}).get(depth_percentile))
    shares = _number(order_shares)
    if shares is not None and shares > 0 and depth_shares and depth_shares > 0:
        multiple = max(0.0, shares / depth_shares - 1.0)
        impact_bps = half_spread * min(multiple, impact_cap)
    return CostModel(
        spread_bps=spread_bps, slippage_bps=impact_bps, fee_bps=fee_bps,
        max_spread_bps=max_spread_bps, max_slippage_bps=max_slippage_bps,
        provenance=(f"measured:{schedule.get('schedule_hash', '')[:12]}"
                    f":{origin}:{percentile}"))


def schedule_costs_block(schedule: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """The measured model as a ``costs`` config block, for a replay run."""
    model = cost_model_from_schedule(schedule, **kwargs)
    block = model.as_dict()
    return {name: block[name] for name in
            ("spread_bps", "slippage_bps", "fee_bps",
             "option_fee_per_contract_side", "provenance")}


__all__ = ["BUCKET_MINUTES", "PERCENTILES", "QUOTE_COST_SCHEMA",
           "QuoteCostError", "bucket_label", "cost_model_from_schedule",
           "measure_quote_costs", "schedule_costs_block"]
