"""Fit an execution-cost schedule to the recorded quote corpus.

The shipped cost model carries two constants — a 4 bps quoted spread and a
6 bps adverse-slippage charge — applied uniformly to every symbol at every
minute of the session.  They are an assumption, and on the configured ETF
universe they are the dominant term in every replayed result: 17 bps round
trip on bar references and 13 bps when executable quote prices already carry
the spread, before comparing either charge with authored trade risk.

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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .costs import (CostError, CostModel, DEFAULT_FEE_BPS,
                    RUNTIME_MAX_SLIPPAGE_BPS, RUNTIME_MAX_SPREAD_BPS,
                    static_cost_config)
from .edge_ledger import content_hash


QUOTE_COST_SCHEMA = "quote-cost-schedule.v1"
MEASURED_QUOTE_CONFIG_SCHEMA = "measured-quote-cost.v1"
MEASURED_QUOTE_COVERAGE_POLICIES = ("strict",)
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
_DEFAULT_PROVIDER = "alpaca"


class QuoteCostError(ValueError):
    """Raised for a corpus that cannot support a cost measurement."""


# Backward-compatible private name retained for the authorizing lanes added
# with measured-cost support.  The projection itself lives beside CostModel so
# older flat-cost readers can opt into the same explicit boundary without a
# quote-cost import cycle.
_static_cost_config = static_cost_config


def _canonical_identity(value: Any) -> str | None:
    """Normalize one optional persisted feed/provider identity."""
    if value in (None, ""):
        return None
    return _normalized_identity(value)


def _positive_integer(value: Any, name: str) -> int:
    """Return one positive integer metadata value or refuse it."""
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = -1
    if (isinstance(value, bool) or result < 1 or result != value):
        raise QuoteCostError(f"{name} must be a positive integer")
    return result


def _normalized_identity(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    # The runtime names this entitlement ``delayed_sip`` while some corpus
    # rows/configs use the short provider label ``delayed``.  Treat the alias
    # identically at the schedule/config boundary so a feed mismatch cannot
    # be hidden by spelling alone.
    return "delayed_sip" if normalized == "delayed" else normalized


def _authoritative_provider(config: Mapping[str, Any] | None) -> str:
    """Resolve the one configured market-data provider identity.

    ``broker.provider`` is canonical.  ``data.provider`` is retained as a
    compatibility alias for pre-provider configs, but it may not silently
    override a broker declaration.  This helper is intentionally local to the
    measured-cost boundary so a schedule's persisted provider can only be
    compared with authority; it can never become authority itself.
    """
    source = config if isinstance(config, Mapping) else {}
    broker = source.get("broker")
    data = source.get("data")
    if broker is not None and not isinstance(broker, Mapping):
        raise QuoteCostError("broker must be a mapping")
    if data is not None and not isinstance(data, Mapping):
        raise QuoteCostError("data must be a mapping")

    def declared(block: Mapping[str, Any] | None, path: str) -> str | None:
        if not isinstance(block, Mapping) or "provider" not in block:
            return None
        value = block.get("provider")
        if not isinstance(value, str) or not value.strip():
            raise QuoteCostError(f"{path} must be a non-empty string")
        return _normalized_identity(value)

    broker_provider = declared(broker, "broker.provider")
    data_provider = declared(data, "data.provider")
    if (broker_provider is not None and data_provider is not None and
            broker_provider != data_provider):
        raise QuoteCostError(
            "broker.provider and data.provider must match")
    return broker_provider or data_provider or _DEFAULT_PROVIDER


def _read_schedule_path(path: str) -> Mapping[str, Any]:
    """Load one immutable JSON schedule referenced by configuration."""
    if not isinstance(path, str) or not path.strip():
        raise QuoteCostError("measured_quote.schedule_path must be non-empty")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise QuoteCostError(
            f"measured quote schedule could not be loaded: {path!r}") from exc
    if not isinstance(value, Mapping):
        raise QuoteCostError("measured quote schedule file must contain a mapping")
    return dict(value)


def validate_measured_quote_config(
        value: Mapping[str, Any], *,
        expected_feed: str | None = None,
        expected_provider: str | None = None,
        embed_schedule: bool = True) -> dict[str, Any]:
    """Validate and normalize the ``costs.measured_quote`` config block.

    An enabled schedule must be frozen either inline or through a path whose
    content hash is declared and verified immediately.  ``strict`` is the
    only coverage policy: a requested symbol/time cell may not fall back to a
    broader aggregate.  The normalized result retains the schedule inline so
    candidate assumptions can bind the exact evidence rather than a mutable
    filename.  ``embed_schedule=False`` is available to a lightweight config
    boundary that wants to retain only an immutable path/hash reference.
    """
    if not isinstance(value, Mapping):
        raise QuoteCostError("costs.measured_quote must be a mapping")
    allowed = {
        "schema", "enabled", "schedule", "schedule_path", "schedule_hash",
        "percentile", "depth_percentile", "min_quotes_per_cell",
        "max_impact_half_spreads", "coverage_policy", "feed", "provider",
    }
    unknown = sorted(set(value) - allowed, key=str)
    if unknown:
        raise QuoteCostError(
            "costs.measured_quote has unknown field(s): " +
            ", ".join(unknown))
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise QuoteCostError("costs.measured_quote.enabled must be true or false")
    result: dict[str, Any] = {
        "schema": str(value.get("schema") or MEASURED_QUOTE_CONFIG_SCHEMA),
        "enabled": enabled,
        "percentile": str(value.get("percentile", "p75")).strip().lower(),
        "depth_percentile": str(value.get("depth_percentile", "p25")).strip().lower(),
        "min_quotes_per_cell": value.get("min_quotes_per_cell", 500),
        "max_impact_half_spreads": value.get("max_impact_half_spreads", 4.0),
        "coverage_policy": str(value.get("coverage_policy", "strict")).strip().lower(),
    }
    if result["schema"] != MEASURED_QUOTE_CONFIG_SCHEMA:
        raise QuoteCostError(
            f"expected {MEASURED_QUOTE_CONFIG_SCHEMA}, got {result['schema']!r}")
    if result["percentile"] not in PERCENTILES:
        raise QuoteCostError(
            f"measured_quote.percentile must be one of {PERCENTILES}")
    if result["depth_percentile"] not in PERCENTILES:
        raise QuoteCostError(
            f"measured_quote.depth_percentile must be one of {PERCENTILES}")
    try:
        floor = int(result["min_quotes_per_cell"])
    except (TypeError, ValueError, OverflowError):
        floor = -1
    if isinstance(result["min_quotes_per_cell"], bool) or floor < 1 or floor != result["min_quotes_per_cell"]:
        raise QuoteCostError(
            "measured_quote.min_quotes_per_cell must be a positive integer")
    result["min_quotes_per_cell"] = floor
    impact = _number(result["max_impact_half_spreads"])
    if impact is None or impact < 0:
        raise QuoteCostError(
            "measured_quote.max_impact_half_spreads must be non-negative")
    result["max_impact_half_spreads"] = impact
    if result["coverage_policy"] not in MEASURED_QUOTE_COVERAGE_POLICIES:
        raise QuoteCostError(
            "measured_quote.coverage_policy must be 'strict'")

    declared_feed = (_normalized_identity(value.get("feed"))
                     if value.get("feed") not in (None, "") else None)
    declared_provider = (_normalized_identity(value.get("provider"))
                         if value.get("provider") not in (None, "") else None)
    schedule_value = value.get("schedule")
    schedule_path = value.get("schedule_path")
    if schedule_value is not None and schedule_path not in (None, ""):
        raise QuoteCostError(
            "measured_quote must provide schedule or schedule_path, not both")
    if not enabled:
        # A disabled block is operator documentation for the static fallback,
        # not a feed entitlement.  Validate its own shape but do not make an
        # inactive IEX placeholder prevent a legitimate SIP configuration.
        if declared_feed:
            result["feed"] = declared_feed
        if declared_provider:
            result["provider"] = declared_provider
        if schedule_path not in (None, ""):
            if not isinstance(schedule_path, str):
                raise QuoteCostError("measured_quote.schedule_path must be a string")
            result["schedule_path"] = schedule_path
        if value.get("schedule_hash") not in (None, ""):
            result["schedule_hash"] = str(value["schedule_hash"])
        return result

    normalized_feed = (_normalized_identity(expected_feed)
                       if expected_feed not in (None, "") else declared_feed)
    # Missing authority means the canonical broker default, never the
    # schedule's declaration.  The latter is evidence to compare, not a
    # permission to authorize its own provider.
    normalized_provider = (_normalized_identity(expected_provider)
                           if expected_provider not in (None, "")
                           else _DEFAULT_PROVIDER)
    if normalized_feed:
        result["feed"] = normalized_feed
    if normalized_provider:
        result["provider"] = normalized_provider
    if declared_feed and normalized_feed and declared_feed != normalized_feed:
        raise QuoteCostError(
            f"measured_quote feed {declared_feed!r} does not match {normalized_feed!r}")
    if declared_provider and normalized_provider and declared_provider != normalized_provider:
        raise QuoteCostError(
            f"measured_quote provider {declared_provider!r} does not match {normalized_provider!r}")

    if schedule_value is None and schedule_path not in (None, ""):
        schedule_value = _read_schedule_path(schedule_path)
    if schedule_value is None:
        raise QuoteCostError(
            "enabled measured_quote requires an embedded schedule or schedule_path")
    if not isinstance(schedule_value, Mapping):
        raise QuoteCostError("measured_quote.schedule must be a mapping")
    schedule = dict(schedule_value)
    try:
        schedule_hash = str(schedule.get("schedule_hash") or "")
        body = dict(schedule)
        body.pop("schedule_hash", None)
        if not schedule_hash or schedule_hash != content_hash(body):
            raise QuoteCostError("measured_quote schedule hash is missing or invalid")
        declared_hash = value.get("schedule_hash")
        if (declared_hash not in (None, "") and
                str(declared_hash) != schedule_hash):
            raise QuoteCostError(
                "measured_quote declared schedule_hash does not match schedule")
        measured_meta = schedule.get("measured")
        if not isinstance(measured_meta, Mapping):
            raise QuoteCostError("measured_quote schedule metadata is missing")
        if str(schedule.get("schema")) != QUOTE_COST_SCHEMA:
            raise QuoteCostError("measured_quote schedule schema is invalid")
        schedule_feed = _normalized_identity(measured_meta.get("feed"))
        schedule_provider = _normalized_identity(measured_meta.get("provider"))
        if not schedule_feed or not schedule_provider:
            raise QuoteCostError(
                "measured_quote schedule must declare one feed and provider")
        if normalized_feed and schedule_feed != normalized_feed:
            raise QuoteCostError(
                f"schedule feed {schedule_feed!r} does not match {normalized_feed!r}")
        if normalized_provider and schedule_provider != normalized_provider:
            raise QuoteCostError(
                f"schedule provider {schedule_provider!r} does not match {normalized_provider!r}")
        schedule_floor = measured_meta.get("min_quotes_per_cell")
        if schedule_floor != result["min_quotes_per_cell"]:
            raise QuoteCostError(
                "measured_quote min_quotes_per_cell does not match schedule")
        # Validate the selected universe model now, including runtime caps.
        cost_model_from_schedule(
            schedule, percentile=result["percentile"],
            depth_percentile=result["depth_percentile"],
            max_impact_half_spreads=result["max_impact_half_spreads"],
            expected_feed=normalized_feed,
            expected_provider=normalized_provider)
    except QuoteCostError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuoteCostError("measured_quote schedule is invalid") from exc
    result.update({
        "schedule_hash": schedule_hash,
        "feed": schedule_feed,
        "provider": schedule_provider,
    })
    # Embedding the verified object is the preferred immutable candidate
    # representation.  Do not retain the path alongside it: a later replay
    # must not re-open a mutable file or reject its own normalized config as
    # ambiguously specifying two schedules.  A lightweight caller can opt out
    # of embedding and retain the path/hash reference instead.
    if schedule_path not in (None, "") and not embed_schedule:
        if not isinstance(schedule_path, str):
            raise QuoteCostError("measured_quote.schedule_path must be a string")
        result["schedule_path"] = schedule_path
    if embed_schedule:
        result["schedule"] = schedule
    return result


@dataclass(frozen=True)
class CostResolverSetup:
    """The one fallback model and optional measured resolver for a replay."""

    model: CostModel
    resolver: Any = None
    measured: Mapping[str, Any] | None = None


def reprice_ibr_result(result: Any, *, resolver: Any,
                       vehicle: str = "equity") -> Any:
    """Apply the causal resolver to an already-mechanically replayed IBR run.

    ``research.ibr`` intentionally accepts one immutable ``CostModel`` for
    backwards-compatible direct callers.  Its signal/exit mechanics do not
    depend on expected costs, so an authorizing facade can replay mechanics
    once and then recompute each realized trade's two cost legs at their own
    symbol/time cells.  Quote sources remain executable prices; the helper
    therefore uses the model's slippage-only leg cost for those sources and
    never charges the measured spread twice.
    """
    if resolver is None:
        return result
    if vehicle != "equity":
        raise QuoteCostError(
            "measured quote-cost resolver currently supports equity only")
    trades = []
    for trade in getattr(result, "trades", ()):
        entry_source = str(getattr(trade, "entry_fill_source", "") or "").lower()
        exit_source = str(getattr(trade, "exit_fill_source", "") or "").lower()
        # Quote provenance belongs to the fill leg, not to the underlying bar
        # used by a bar fallback or resting bracket. Normalize it once at the
        # repricing boundary so resolver contexts and durable rows carry the
        # same canonical identity.
        entry_feed = _canonical_identity(getattr(trade, "entry_feed", None))
        exit_feed = _canonical_identity(getattr(trade, "exit_feed", None))
        entry_provider = _canonical_identity(
            getattr(trade, "entry_provider", None))
        exit_provider = _canonical_identity(
            getattr(trade, "exit_provider", None))
        quantity = float(getattr(trade, "quantity", 1.0))
        multiplier = int(getattr(trade, "contract_multiplier", 1))
        direction = str(getattr(trade, "direction", "long"))
        execution_direction = direction
        entry_timestamp = getattr(trade, "entry_timestamp", None)
        exit_timestamp = getattr(trade, "exit_timestamp", None)
        common = {
            "vehicle": vehicle, "symbol": str(getattr(trade, "symbol", "")),
            "quantity": quantity, "shares": quantity,
            "entry_timestamp": (entry_timestamp.isoformat()
                                 if hasattr(entry_timestamp, "isoformat")
                                 else entry_timestamp),
            "exit_timestamp": (exit_timestamp.isoformat()
                                if hasattr(exit_timestamp, "isoformat")
                                else exit_timestamp),
        }
        entry_context = {
            **common, "cost_leg": "entry",
            "feed": entry_feed, "provider": entry_provider,
            "entry_feed": entry_feed, "exit_feed": exit_feed,
            "entry_provider": entry_provider, "exit_provider": exit_provider,
            "cost_timestamp": common["entry_timestamp"],
        }
        exit_context = {
            **common, "cost_leg": "exit",
            "feed": exit_feed, "provider": exit_provider,
            "entry_feed": entry_feed, "exit_feed": exit_feed,
            "entry_provider": entry_provider, "exit_provider": exit_provider,
            "cost_timestamp": common["exit_timestamp"],
        }
        entry_model = resolver(entry_context)
        exit_model = resolver(exit_context)
        if not isinstance(entry_model, CostModel) or not isinstance(exit_model, CostModel):
            raise QuoteCostError("measured cost resolver returned an invalid model")
        entry_price = entry_model.execution_price(
            float(trade.entry_reference), execution_direction, entry=True,
            executable_quote=entry_source == "quote")
        exit_price = exit_model.execution_price(
            float(trade.exit_reference), execution_direction, entry=False,
            executable_quote=exit_source == "quote")
        gross = ((exit_price - entry_price)
                 if execution_direction == "long" else
                 (entry_price - exit_price)) * quantity * multiplier
        fees = (entry_model.fees(entry_price, entry_price, quantity, multiplier,
                                 vehicle=vehicle) / 2.0 +
                exit_model.fees(exit_price, exit_price, quantity, multiplier,
                                vehicle=vehicle) / 2.0)
        if vehicle == "option":
            # Options are long-premium positions in IBR.  The measured
            # executable entry price is therefore also the risk unit.
            realized_risk_per_unit = entry_price * multiplier
            risk_per_unit = realized_risk_per_unit
        else:
            # Preserve the authored/planned risk distance while refreshing
            # realized fill risk to the same measured entry economics used by
            # the account replay.
            risk_per_unit = getattr(trade, "risk_per_unit", None)
            if risk_per_unit is None:
                risk_per_unit = abs(entry_price - float(trade.stop_price))
            realized_risk_per_unit = max(
                0.0,
                (entry_price - float(trade.stop_price)
                 if execution_direction == "long" else
                 float(trade.stop_price) - entry_price),
            )
        risk_usd = quantity * realized_risk_per_unit
        # Persist the exact per-leg model selection and the complete measured
        # round-trip arithmetic.  The flat fields intentionally match the
        # account/factory row contract; ``cost_economics`` is the one
        # versioned claim that gates can recompute and cross-check.
        entry_model_fields = {
            "entry_cost_model_provenance": entry_model.provenance,
            "entry_cost_model_spread_bps": entry_model.spread_bps,
            "entry_cost_model_slippage_bps": entry_model.slippage_bps,
            "entry_cost_model_fee_bps": entry_model.fee_bps,
            "exit_cost_model_provenance": exit_model.provenance,
            "exit_cost_model_spread_bps": exit_model.spread_bps,
            "exit_cost_model_slippage_bps": exit_model.slippage_bps,
            "exit_cost_model_fee_bps": exit_model.fee_bps,
        }
        round_trip_cost = (
            abs(entry_price - float(trade.entry_reference)) * quantity * multiplier +
            abs(exit_price - float(trade.exit_reference)) * quantity * multiplier +
            fees)
        cost_economics = {
            "schema": "ibr-cost-economics.v1",
            "vehicle": vehicle,
            "direction": direction,
            "quantity": quantity,
            "contract_multiplier": multiplier,
            "entry_reference": float(trade.entry_reference),
            "exit_reference": float(trade.exit_reference),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl": gross,
            "costs": fees,
            "net_pnl": gross - fees,
            "round_trip_cost": round_trip_cost,
            "risk_per_unit": risk_per_unit,
            "realized_risk_per_unit": realized_risk_per_unit,
            "risk_usd": risk_usd,
        }
        trades.append(replace(
            trade, entry_price=entry_price, exit_price=exit_price,
            gross_pnl=gross, costs=fees, net_pnl=gross - fees,
            entry_feed=entry_feed, exit_feed=exit_feed,
            entry_provider=entry_provider, exit_provider=exit_provider,
            risk_per_unit=risk_per_unit,
            realized_risk_per_unit=realized_risk_per_unit,
            risk_usd=risk_usd, **entry_model_fields,
            cost_economics=cost_economics))
    try:
        return replace(result, trades=trades)
    except TypeError:
        # Keep a clear hard failure for an unexpected result shape; silently
        # returning an un-repriced result would turn measured mode into a
        # static-cost claim.
        raise QuoteCostError("IBR result cannot carry measured cost repricing")


def cost_resolver_setup(config: Mapping[str, Any] | None, *,
                        vehicle: str = "equity",
                        base_model: CostModel | None = None) -> CostResolverSetup:
    """Resolve configured static/measured economics for one replay lane.

    Disabled measurement returns the ordinary static model and no resolver.
    Enabled measurement is equity-only and returns a fail-closed per-opportunity
    resolver.  All lanes call this function so candidate, control, null,
    qualification, and shadow arithmetic have identical provenance.
    """
    from .costs import CostModel
    static = (base_model if isinstance(base_model, CostModel) else
              CostModel.from_config(_static_cost_config(config), vehicle=vehicle))
    block = ((config or {}).get("costs") if isinstance(config, Mapping) else None)
    measured_raw = block.get("measured_quote") if isinstance(block, Mapping) else None
    if measured_raw is None:
        return CostResolverSetup(model=static)
    broker = (config or {}).get("broker") if isinstance(config, Mapping) else None
    expected_feed = (broker.get("data_feed") if isinstance(broker, Mapping)
                     else None)
    expected_provider = _authoritative_provider(config)
    normalized = validate_measured_quote_config(
        measured_raw, expected_feed=expected_feed,
        expected_provider=expected_provider)
    if not normalized.get("enabled"):
        # Disabled measurement is behaviorally the ordinary static model.  Do
        # not bind operator-placeholder metadata into candidate identities or
        # force a proof epoch when no measured economics were applied.
        return CostResolverSetup(model=static)
    if vehicle != "equity":
        raise QuoteCostError(
            "measured quote-cost resolver currently supports equity only")
    schedule = normalized.get("schedule")
    if not isinstance(schedule, Mapping):
        # This branch is only reachable for a caller deliberately asking for
        # a path-only reference; load and revalidate before constructing a
        # resolver rather than trusting the path's current contents.
        schedule = _read_schedule_path(str(normalized.get("schedule_path") or ""))
        normalized = validate_measured_quote_config(
            {**normalized, "schedule": schedule, "schedule_path": None},
            expected_feed=expected_feed, expected_provider=expected_provider)
        schedule = normalized["schedule"]
    resolver = measured_cost_resolver(
        schedule, percentile=normalized["percentile"], vehicle=vehicle,
        depth_percentile=normalized["depth_percentile"],
        max_impact_half_spreads=normalized["max_impact_half_spreads"],
        fee_bps=static.fee_bps,
        max_spread_bps=static.max_spread_bps,
        max_slippage_bps=static.max_slippage_bps,
        expected_feed=normalized.get("feed"),
        expected_provider=expected_provider,
        coverage_policy=normalized["coverage_policy"])
    return CostResolverSetup(model=static, resolver=resolver, measured=normalized)


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
            # Keep the legacy quote_count field, but make the two coverage
            # populations explicit. A spread quote is not necessarily an
            # executable-depth quote: both displayed sizes must be present
            # and positive before it enters the depth histogram.
            "spread_quote_count": self.spread.count,
            "depth_quote_count": self.depth.count,
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
    held in memory. A quote is used for spread measurement only when its
    bid/ask are two-sided and positive; missing or malformed displayed sizes
    leave that spread observation intact but do not shape executable depth.
    """
    min_quotes = _positive_integer(min_quotes_per_cell,
                                   "min_quotes_per_cell")
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
        if str(_value(row, "kind", "quote")).strip().lower() not in {
                "quote", "quote_snapshot", "equity_quote",
                "underlying_quote", ""}:
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
        # A one-sided size is not executable depth. The spread remains a
        # legitimate observation, but positive-size pricing must not infer
        # capacity from the one side that happened to be reported.
        sizes = ([bid_size, ask_size]
                 if (bid_size is not None and bid_size > 0 and
                     ask_size is not None and ask_size > 0)
                 else [])
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
            cell.spread.count >= min_quotes}
        symbols[symbol] = {**accumulator.summary(), "buckets": buckets,
                           "sparse_buckets": sum(
                               1 for (cell_symbol, _bucket), cell
                               in by_cell.items()
                               if cell_symbol == symbol and
                               cell.spread.count < min_quotes),
                           "depth_sparse_buckets": sum(
                               1 for (cell_symbol, _bucket), cell
                               in by_cell.items()
                               if cell_symbol == symbol and
                               cell.depth.count < min_quotes)}
    schedule = {
        "schema": QUOTE_COST_SCHEMA,
        "measured": {
            "quote_rows_seen": seen,
            "quote_rows_used": universe.spread.count,
            "depth_rows_used": universe.depth.count,
            "quote_rows_rejected": universe.rejected,
            "first_session": first_session, "last_session": last_session,
            "feeds": sorted(feeds), "providers": sorted(providers),
            "feed": next(iter(feeds)) if len(feeds) == 1 else None,
            "provider": next(iter(providers)) if len(providers) == 1 else None,
            "missing_feed_rows": int(missing_feed_rows),
            "missing_provider_rows": int(missing_provider_rows),
            "min_quotes_per_cell": min_quotes,
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


def _section_count(section: Mapping[str, Any], *, depth: bool) -> int | None:
    """Read and cross-check a section's spread or depth observation count.

    ``quote_count`` and ``touch_shares.count`` are retained for compatibility
    with v1 schedules. New schedules also expose explicit count names so a
    caller cannot mistake spread coverage for executable-depth coverage.
    """
    if not isinstance(section, Mapping):
        return None
    if depth:
        touch = section.get("touch_shares")
        legacy = (touch.get("count")
                  if isinstance(touch, Mapping) else None)
        explicit = section.get("depth_quote_count")
        name = "depth quote count"
    else:
        legacy = section.get("quote_count")
        explicit = section.get("spread_quote_count")
        name = "spread quote count"
    histogram = section.get("spread_bps")
    histogram_count = (histogram.get("count")
                        if not depth and isinstance(histogram, Mapping)
                        else None)
    values = [(name, value) for name, value in (
        ("explicit", explicit), ("legacy", legacy),
        ("histogram", histogram_count)) if value is not None]
    if not values:
        return None
    parsed: list[int] = []
    for source, value in values:
        try:
            count = int(value)
        except (TypeError, ValueError, OverflowError):
            raise QuoteCostError(f"cost schedule has invalid {name}")
        if isinstance(value, bool) or count < 0 or count != value:
            raise QuoteCostError(f"cost schedule has invalid {name}")
        parsed.append(count)
    if any(count != parsed[0] for count in parsed[1:]):
        raise QuoteCostError(
            f"cost schedule {name} metadata disagrees across section fields")
    return parsed[0]


def _required_quote_coverage(measured_meta: Mapping[str, Any]) -> int:
    """Validate the immutable cell-coverage metadata on a public schedule."""
    required = _positive_integer(measured_meta.get("min_quotes_per_cell"),
                                 "cost schedule min_quotes_per_cell")
    bucket_minutes = _number(measured_meta.get("bucket_minutes"))
    if bucket_minutes != BUCKET_MINUTES:
        raise QuoteCostError(
            "cost schedule bucket_minutes does not match configured "
            f"BUCKET_MINUTES ({BUCKET_MINUTES})")
    return required


def _cell(schedule: Mapping[str, Any], symbol: str | None,
          bucket: str | None, *, required_quotes: int | None = None
          ) -> tuple[Mapping[str, Any], str]:
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
                if required_quotes is None:
                    measured_meta = schedule.get("measured")
                    required_quotes = (_required_quote_coverage(measured_meta)
                                       if isinstance(measured_meta, Mapping)
                                       else None)
                observed_quotes = (_section_count(measured, depth=False)
                                   if isinstance(measured, Mapping) else None)
                if (isinstance(measured, Mapping) and measured and
                        required_quotes is not None and
                        observed_quotes is not None and
                        observed_quotes >= required_quotes):
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
        max_slippage_bps: float = RUNTIME_MAX_SLIPPAGE_BPS,
        expected_feed: str | None = None,
        expected_provider: str | None = None,
        coverage_policy: str = "strict") -> CostModel:
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
    if coverage_policy not in MEASURED_QUOTE_COVERAGE_POLICIES:
        raise QuoteCostError("coverage_policy must be 'strict'")
    impact_cap = _number(max_impact_half_spreads)
    if impact_cap is None or impact_cap < 0:
        raise QuoteCostError("max_impact_half_spreads must be non-negative")
    measured_meta = schedule.get("measured")
    if not isinstance(measured_meta, Mapping):
        raise QuoteCostError("cost schedule metadata is missing")
    required_quotes = _required_quote_coverage(measured_meta)
    schedule_feed = _normalized_identity(measured_meta.get("feed"))
    schedule_provider = _normalized_identity(measured_meta.get("provider"))
    normalized_feed = (_normalized_identity(expected_feed)
                       if expected_feed not in (None, "") else None)
    normalized_provider = (_normalized_identity(expected_provider)
                           if expected_provider not in (None, "") else None)
    if normalized_feed and schedule_feed != normalized_feed:
        raise QuoteCostError(
            f"schedule feed {schedule_feed!r} does not match {normalized_feed!r}")
    if normalized_provider and schedule_provider != normalized_provider:
        raise QuoteCostError(
            f"schedule provider {schedule_provider!r} does not match {normalized_provider!r}")
    section, origin = _cell(schedule, symbol, bucket,
                            required_quotes=required_quotes)
    spread_bps = _percentile(section.get("spread_bps"), percentile)
    half_spread = spread_bps / 2.0

    impact_bps = 0.0
    shares = _number(order_shares)
    if shares is not None and shares > 0:
        spread_count = _section_count(section, depth=False)
        if spread_count is None or spread_count < required_quotes:
            raise QuoteCostError(
                f"chosen measured section {origin} has insufficient spread "
                f"coverage for positive order_shares ({spread_count or 0} "
                f"< {required_quotes})")
        depth_count = _section_count(section, depth=True)
        if depth_count is None:
            raise QuoteCostError(
                f"chosen measured section {origin} has no depth coverage "
                "metadata for positive order_shares")
        if depth_count > spread_count:
            raise QuoteCostError(
                f"chosen measured section {origin} has invalid depth "
                f"coverage ({depth_count} > spread coverage {spread_count})")
        if depth_count < required_quotes:
            raise QuoteCostError(
                f"chosen measured section {origin} has under-covered depth "
                f"for positive order_shares ({depth_count} < {required_quotes})")
        touch_shares = section.get("touch_shares")
        depth_values = (touch_shares if isinstance(touch_shares, Mapping)
                        else {})
        depth_shares = _number(depth_values.get(depth_percentile))
        if depth_shares is None or depth_shares <= 0:
            raise QuoteCostError(
                f"chosen measured section {origin} has no usable depth "
                f"measurement at {depth_percentile}")
        multiple = max(0.0, shares / depth_shares - 1.0)
        impact_bps = half_spread * min(multiple, impact_cap)
    return CostModel(
        spread_bps=spread_bps, slippage_bps=impact_bps, fee_bps=fee_bps,
        max_spread_bps=max_spread_bps, max_slippage_bps=max_slippage_bps,
        provenance=(f"measured:{schedule.get('schedule_hash', '')[:12]}"
                    f":{origin}:spread-{percentile}:depth-{depth_percentile}"
                    f":feed-{schedule_feed}:provider-{schedule_provider}"
                    f":coverage-{coverage_policy}"))


def schedule_costs_block(schedule: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """The measured model as a ``costs`` config block, for a replay run."""
    model = cost_model_from_schedule(schedule, **kwargs)
    block = model.as_dict()
    return {name: block[name] for name in
            ("spread_bps", "slippage_bps", "fee_bps",
             "option_fee_per_contract_side", "provenance")}


def measured_cost_resolver(schedule: Mapping[str, Any], *,
                           percentile: str = "p75",
                           vehicle: str = "equity",
                           depth_percentile: str = "p25",
                           max_impact_half_spreads: float = 4.0,
                           fee_bps: float = DEFAULT_FEE_BPS,
                           max_spread_bps: float = RUNTIME_MAX_SPREAD_BPS,
                           max_slippage_bps: float = RUNTIME_MAX_SLIPPAGE_BPS,
                           expected_feed: str | None = None,
                           expected_provider: str | None = None,
                           coverage_policy: str = "strict"):
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
    measured_meta = schedule.get("measured")
    schedule_feed = (_normalized_identity(measured_meta.get("feed"))
                     if isinstance(measured_meta, Mapping) else None)
    schedule_provider = (_normalized_identity(measured_meta.get("provider"))
                         if isinstance(measured_meta, Mapping) else None)
    configured_feed = (_normalized_identity(expected_feed)
                       if expected_feed not in (None, "") else schedule_feed)
    configured_provider = (_normalized_identity(expected_provider)
                           if expected_provider not in (None, "")
                           else schedule_provider)

    def resolve(row: Mapping[str, Any] | None = None, *,
                symbol: str | None = None, bucket: str | None = None,
                order_shares: float | None = None) -> CostModel:
        item = row if isinstance(row, Mapping) else {}
        # Opportunity contexts do not always duplicate source identity, but
        # any explicit top-level or per-leg identity may never contradict the
        # configured broker/schedule. Check both legs even when resolving one
        # leg so a mixed-provenance row cannot be partially repriced.
        for field, expected, label in (
                ("feed", configured_feed, "feed"),
                ("provider", configured_provider, "provider"),
                ("entry_feed", configured_feed, "entry feed"),
                ("exit_feed", configured_feed, "exit feed"),
                ("entry_provider", configured_provider, "entry provider"),
                ("exit_provider", configured_provider, "exit provider")):
            supplied = item.get(field)
            if (supplied not in (None, "") and expected not in (None, "") and
                    _normalized_identity(supplied) != expected):
                raise QuoteCostError(
                    f"measured cost row {label} "
                    f"{_normalized_identity(supplied)!r} does not match "
                    f"{expected!r}")
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
            order_shares=order_shares, depth_percentile=depth_percentile,
            max_impact_half_spreads=max_impact_half_spreads, fee_bps=fee_bps,
            max_spread_bps=max_spread_bps, max_slippage_bps=max_slippage_bps,
            expected_feed=expected_feed, expected_provider=expected_provider,
            coverage_policy=coverage_policy)
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
           "MEASURED_QUOTE_CONFIG_SCHEMA", "MEASURED_QUOTE_COVERAGE_POLICIES",
           "QuoteCostError", "bucket_label", "cost_model_from_schedule",
           "measure_quote_costs", "measured_cost_resolver", "schedule_costs_block",
           "validate_measured_quote_config", "cost_resolver_setup",
           "CostResolverSetup", "reprice_ibr_result", "_static_cost_config",
           "DEFAULT_FALLBACK_SCENARIO_BPS", "DEFAULT_MIN_SESSIONS_PER_CELL",
           "STRESS_CALIBRATION_SCHEMA", "StressCalibrationError",
           "calibrate_stress_schedule", "calibrate_stressed_cost",
           "empirical_stress_calibration"]
