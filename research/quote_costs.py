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
import hashlib
import json
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
_SUM_SCALE = 1_000_000_000_000
_DIGEST_MODULUS = 1 << 256


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
    # Fixed-point accumulation keeps summaries independent of input order.
    # That matters because schedule hashes are evidence identities, while
    # recorder partitions may be traversed in a different stable order.
    total: int = 0
    overflow: int = 0
    minimum: float = math.inf
    maximum: float = -math.inf
    bins: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        self.total += int(round(value * _SUM_SCALE))
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        if value > self.ceiling or value < self.floor:
            self.overflow += 1
            return
        self.bins[int((value - self.floor) / self.width)] += 1

    @property
    def mean(self) -> float | None:
        return (self.total / (_SUM_SCALE * self.count)
                if self.count else None)

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
            # Keep the actual session IDs alongside the count.  A count alone
            # cannot distinguish broad chronological coverage from repeated
            # observations in one session when stress calibration is persisted.
            "sessions": sorted(self.sessions),
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
    missing_feed_rows = 0
    missing_provider_rows = 0
    first_session: str | None = None
    last_session: str | None = None
    seen = 0
    # A streaming multiset digest is insensitive to row iteration order while
    # still preserving multiplicity.  Combining modular sum and xor avoids the
    # duplicate cancellation weakness of xor alone.
    quote_digest_sum = 0
    quote_digest_xor = 0
    quote_digest_count = 0

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
        identity = _value(row, "identity")
        row_feed = _value(row, "feed", _value(identity, "feed"))
        row_provider = _value(row, "provider", _value(identity, "provider"))
        # Bind the measured schedule to the exact observations that shaped it.
        # The normalized primitive projection works for both raw mappings and
        # QuoteSnapshot records and keeps the fit streaming.
        digest_row = {
            "symbol": symbol, "timestamp": (stamp.isoformat() if stamp else None),
            "bid": bid, "ask": ask,
            "bid_size": bid_size, "ask_size": ask_size,
            "provider": (None if row_provider in (None, "") else str(row_provider).strip().lower()),
            "feed": (None if row_feed in (None, "") else str(row_feed).strip().lower()),
        }
        digest_bytes = json.dumps(
            digest_row, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        row_digest = int.from_bytes(hashlib.sha256(digest_bytes).digest(), "big")
        quote_digest_sum = (quote_digest_sum + row_digest) % _DIGEST_MODULUS
        quote_digest_xor ^= row_digest
        quote_digest_count += 1
        normalized_provider = (str(row_provider).strip().lower()
                               if row_provider not in (None, "") else "")
        normalized_feed = (str(row_feed).strip().lower()
                           if row_feed not in (None, "") else "")
        if not normalized_provider or not normalized_feed:
            # A usable quote without source identity is not a measurement;
            # accepting it would make the fitted schedule impossible to tie
            # back to a recorder/feed.  Invalid bid/ask rows above remain
            # ordinary rejected observations for compatibility.
            if not normalized_feed:
                missing_feed_rows += 1
            if not normalized_provider:
                missing_provider_rows += 1
            universe.rejected += 1
            by_symbol[symbol].rejected += 1
            continue
        for accumulator in (universe, by_symbol[symbol], by_cell[cell]):
            accumulator.spread.add(spread_bps)
            accumulator.sessions.add(session)
            if sizes:
                accumulator.depth.add(math.log10(min(sizes)))
        feeds.add(normalized_feed)
        providers.add(normalized_provider)
        if first_session is None or session < first_session:
            first_session = session
        if last_session is None or session > last_session:
            last_session = session

    quote_content_hash = hashlib.sha256(
        (f"sha256-multiset-sum-xor.v1:{quote_digest_count}:"
         f"{quote_digest_sum:064x}:{quote_digest_xor:064x}").encode("ascii")
    ).hexdigest()
    if missing_feed_rows:
        raise QuoteCostError(
            "quote-cost measurement requires feed provenance on every "
            f"usable quote; missing {missing_feed_rows} row(s)")
    if missing_provider_rows:
        raise QuoteCostError(
            "quote-cost measurement requires provider provenance on every "
            f"usable quote; missing {missing_provider_rows} row(s)")
    if not universe.spread.count:
        raise QuoteCostError(
            "no usable two-sided quotes in the corpus; cannot fit a cost model")
    expected_feed = (str(feed).strip().lower()
                     if feed not in (None, "") else None)
    if expected_feed is not None and feeds != {expected_feed}:
        raise QuoteCostError(
            f"corpus feeds {sorted(feeds)} do not match the expected "
            f"{expected_feed!r}")
    if expected_feed is None and len(feeds) != 1:
        raise QuoteCostError(
            "quote-cost measurement requires one explicit feed; "
            f"got {sorted(feeds)}")
    expected_provider = (str(provider).strip().lower()
                         if provider not in (None, "") else None)
    if expected_provider is not None and providers != {expected_provider}:
        raise QuoteCostError(
            f"corpus providers {sorted(providers)} do not match the expected "
            f"{expected_provider!r}")
    if expected_provider is None and len(providers) != 1:
        raise QuoteCostError(
            "quote-cost measurement requires one explicit provider; "
            f"got {sorted(providers)}")

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
            "feed": next(iter(feeds)) if len(feeds) == 1 else None,
            "provider": next(iter(providers)) if len(providers) == 1 else None,
            "missing_feed_rows": int(missing_feed_rows),
            "missing_provider_rows": int(missing_provider_rows),
            "min_quotes_per_cell": int(min_quotes_per_cell),
            "bucket_minutes": BUCKET_MINUTES,
            "quote_content_hash": quote_content_hash,
            "quote_content_hash_algorithm": "sha256-multiset-sum-xor.v1",
            "session_hash": content_hash(sorted({
                session for accumulator in (universe,)
                for session in accumulator.sessions})),
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
    normalized_symbol = (str(symbol).strip().upper()
                         if symbol not in (None, "") else None)
    normalized_bucket = (str(bucket).strip()
                         if bucket not in (None, "") else None)
    if normalized_symbol:
        entry = symbols.get(normalized_symbol)
        if entry is not None:
            if normalized_bucket is not None:
                measured = (entry.get("buckets") or {}).get(normalized_bucket)
                measured_meta = schedule.get("measured")
                required_quotes = _number(
                    measured_meta.get("min_quotes_per_cell")
                    if isinstance(measured_meta, Mapping) else None)
                observed_quotes = (_number(measured.get("quote_count"))
                                   if isinstance(measured, Mapping) else None)
                if (isinstance(measured, Mapping) and measured and
                        (required_quotes is None or
                         (observed_quotes is not None and
                          observed_quotes >= required_quotes))):
                    return measured, (f"symbol_bucket:{normalized_symbol}:"
                                      f"{normalized_bucket}")
                # A bucket omitted by ``measure_quote_costs`` did not meet
                # its coverage floor.  Falling back to the symbol aggregate
                # would make that sparse cell look measured and can price an
                # opportunity using evidence from the wrong time of day.
                raise QuoteCostError(
                    "requested measured cost bucket "
                    f"{normalized_symbol}/{normalized_bucket} is unavailable "
                    "or under-covered; refusing symbol-wide fallback")
            return entry, f"symbol:{normalized_symbol}"
        if normalized_bucket is not None:
            raise QuoteCostError(
                "requested measured cost bucket "
                f"{normalized_symbol}/{normalized_bucket} is unavailable; "
                "refusing universe fallback")
    if normalized_bucket is not None:
        raise QuoteCostError(
            "requested measured cost bucket "
            f"{normalized_bucket} has no symbol; refusing universe fallback")
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
    schedule_hash = str(schedule.get("schedule_hash") or "")
    schedule_body = dict(schedule)
    schedule_body.pop("schedule_hash", None)
    if not schedule_hash or schedule_hash != content_hash(schedule_body):
        raise QuoteCostError("cost schedule hash is missing or invalid")
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


def measured_cost_resolver(schedule: Mapping[str, Any], *,
                           percentile: str = "p75",
                           vehicle: str = "equity"):
    """Return the causal per-opportunity schedule resolver.

    Replay/account code can call this immediately before admission and each
    execution leg.  It intentionally accepts a row-like mapping rather than
    mutating account state, making the same resolver usable by configured and
    measured arms without changing the authored rule or sizing policy.
    """
    if not isinstance(schedule, Mapping):
        raise QuoteCostError("schedule must be a mapping")
    if str(vehicle).strip().lower() != "equity":
        raise QuoteCostError(
            "measured quote-cost resolver currently supports equity only")
    def resolve(row: Mapping[str, Any] | None = None, *,
                symbol: str | None = None, bucket: str | None = None,
                order_shares: float | None = None) -> CostModel:
        item = row if isinstance(row, Mapping) else {}
        resolved_symbol = symbol or item.get("symbol")
        resolved_bucket = bucket
        if resolved_bucket in (None, ""):
            raw = next((item.get(name) for name in
                        ("cost_timestamp", "entry_timestamp", "timestamp")
                        if item.get(name) not in (None, "")), None)
            if raw not in (None, ""):
                try:
                    stamp = _timestamp({"timestamp": raw})
                except (TypeError, ValueError, OverflowError) as exc:
                    raise QuoteCostError(
                        "measured cost resolver received an unparsable "
                        "timestamp") from exc
                if stamp is None:
                    raise QuoteCostError(
                        "measured cost resolver received an unparsable "
                        "timestamp")
                local = stamp.astimezone(_NY)
                minutes = ((local.hour * 60 + local.minute +
                            local.second / 60.0) - 9 * 60 - 30)
                resolved_bucket = bucket_label(minutes)
        if order_shares is None:
            order_shares = _number(item.get("quantity", item.get("shares")))
        return cost_model_from_schedule(
            schedule, symbol=(None if resolved_symbol in (None, "") else str(resolved_symbol)),
            bucket=resolved_bucket, percentile=percentile,
            order_shares=order_shares)
    return resolve


# Re-export the diagnostic bridge from the schedule module so callers that
# already depend on ``research.quote_costs`` can discover calibration without
# importing a runtime/risk module.  The implementation remains separate to
# keep the measured schedule and stress-selection contracts distinct.
from .stressed_cost_calibration import (  # noqa: E402  (late import avoids cycles)
    DEFAULT_FALLBACK_SCENARIO_BPS, DEFAULT_MIN_SESSIONS_PER_CELL,
    STRESS_CALIBRATION_SCHEMA, StressCalibrationError,
    calibrate_stress_schedule, calibrate_stressed_cost,
    empirical_stress_calibration,
)

__all__ = ["BUCKET_MINUTES", "PERCENTILES", "QUOTE_COST_SCHEMA",
           "QuoteCostError", "bucket_label", "cost_model_from_schedule",
           "measure_quote_costs", "measured_cost_resolver", "schedule_costs_block",
           "DEFAULT_FALLBACK_SCENARIO_BPS", "DEFAULT_MIN_SESSIONS_PER_CELL",
           "STRESS_CALIBRATION_SCHEMA", "StressCalibrationError",
           "calibrate_stress_schedule", "calibrate_stressed_cost",
           "empirical_stress_calibration"]
