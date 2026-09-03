"""Deterministic Initial Balance Range (IBR) replay for US sessions.

IBR is deliberately a small replay harness rather than a strategy framework:
the range is made only from completed one-minute bars, a breakout is observed
at bar close, and the order is entered on the following bar.  This makes the
look-ahead boundary explicit and keeps equity and option vehicles as separate
populations.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
import math
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.risk_geometry import (
    RiskGeometryError, effective_stop_distance, equity_price_increment,
    quantize_equity_bracket,
)
from agent.contracts.rule import canonical_exit_reason
from agent.contracts.rule import (MIN_STOP_DISTANCE_BPS,
                                  MIN_STOP_DISTANCE_FRACTION)
from .costs import (BAR, QUOTE, RESTING_BRACKET,
                    DIAGNOSTIC_BAR_FALLBACK,
                    DIAGNOSTIC_HISTORICAL_BACKFILL,
                    RESTING_BRACKET_FILL_SCHEMA, CostError, CostModel,
                    ReplayPolicy, check_entry_slippage, index_quotes,
                    quote_fill_record, replay_policy_for_bars,
                    resting_bracket_fill_claim)

RUNTIME_MAX_MARKET_DATA_AGE_SECONDS = 30.0
from .market_data import (OptionSnapshot, QuoteSnapshot, UnderlyingBar,
                          historical_backfill_record, option_has_liquidity,
                          normalize_underlying_bar,
                          replay_available_at, replay_open_is_available,
                          replay_record_is_available)
from .source_validation import SOURCE_MODES, SourceValidationError, validate_source


class ReplayError(ValueError):
    """Raised for malformed or look-ahead-prone replay input."""


@dataclass(frozen=True)
class IBRConfig:
    range_minutes: int = 30
    session_open: time = time(9, 30)
    force_flat: time = time(15, 55)
    stop_pct: float = 0.003
    target_pct: float = 0.006
    # One shared executable-cost model; there are deliberately no per-replay
    # spread/slippage/fee knobs left to drift from the other lanes.
    costs: CostModel = field(default_factory=CostModel)
    timezone: str = "America/New_York"
    quantity: float = 1.0
    target_r: float | None = None
    range_stop: bool = False
    breakout_buffer_bps: float = 0.0
    # Legacy fixtures use wick breakouts; production parity can require the
    # completed signal close explicitly.  The option is persisted in the
    # config hash so the two interpretations cannot be mixed in one run.
    close_confirmed: bool = False
    # Replay always uses the checked runtime policy.  Diagnostics that need
    # legacy bar fallback must opt out explicitly with
    # ``ReplayPolicy(strict_market_data=False)``.
    policy: ReplayPolicy | Mapping | None = field(default_factory=ReplayPolicy)

    def __post_init__(self) -> None:
        if (isinstance(self.range_minutes, bool)
                or not isinstance(self.range_minutes, int)
                or self.range_minutes <= 0):
            raise ReplayError("range_minutes must be positive")
        for name in ("stop_pct", "target_pct"):
            if getattr(self, name) <= 0:
                raise ReplayError(f"{name} must be positive")
        if not isinstance(self.costs, CostModel):
            raise ReplayError("costs must be a CostModel")
        if self.quantity <= 0:
            raise ReplayError("quantity must be positive")
        if self.target_r is not None and self.target_r <= 0:
            raise ReplayError("target_r must be positive")
        if not isinstance(self.range_stop, bool):
            raise ReplayError("range_stop must be true or false")
        if self.breakout_buffer_bps < 0:
            raise ReplayError("breakout_buffer_bps cannot be negative")
        if not isinstance(self.close_confirmed, bool):
            raise ReplayError("close_confirmed must be true or false")
        try:
            resolved = (ReplayPolicy() if self.policy is None else
                        ReplayPolicy.from_config(self.policy)
                        if isinstance(self.policy, Mapping) else self.policy)
        except Exception as exc:
            raise ReplayError(f"invalid replay policy: {exc}") from exc
        if not isinstance(resolved, ReplayPolicy):
            raise ReplayError("policy must be a ReplayPolicy or mapping")
        object.__setattr__(self, "policy", resolved)
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ReplayError(f"unknown timezone {self.timezone!r}") from exc


@dataclass(frozen=True)
class IBRTrade:
    vehicle: str
    session_date: date
    symbol: str
    direction: str
    range_high: float
    range_low: float
    signal_timestamp: datetime
    entry_timestamp: datetime
    entry_reference: float
    entry_price: float
    stop_price: float
    target_price: float
    exit_timestamp: datetime
    exit_reference: float
    exit_price: float
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float
    tie_broken: bool = False
    gap_fill: bool = False
    contract_multiplier: int = 1
    # Which observation priced each side: a recorded quote at the fill instant,
    # or the bar it fell back to.  Calibration needs to know the difference.
    entry_fill_source: str = BAR
    exit_fill_source: str = BAR
    # Non-gap equity stop/target exits use a broker-resident resting leg.  The
    # claim is persisted as a mapping so gates can independently revalidate
    # the level, tie, and exact-feed bar evidence.
    exit_fill_schema: str | None = None
    exit_fill_claim: Mapping[str, object] | None = None
    exit_fill_bar_timestamp: str | None = None
    # Option evidence is point-in-time and executable only when it carries the
    # quote age and feed identity used by the replay.  These fields are at the
    # end of the dataclass to preserve positional construction of legacy
    # equity fixtures.
    entry_quote_age_seconds: float | None = None
    exit_quote_age_seconds: float | None = None
    entry_feed: str | None = None
    exit_feed: str | None = None
    entry_provider: str | None = None
    exit_provider: str | None = None
    quantity: float = 1.0
    risk_per_unit: float | None = None
    realized_risk_per_unit: float | None = None
    risk_usd: float | None = None
    # Underlying bar provenance is retained separately from quote-leg
    # provenance so diagnostic bar fallback cannot masquerade as an executable
    # quote.
    signal_bar_feed: str | None = None
    signal_bar_provider: str | None = None
    entry_bar_feed: str | None = None
    entry_bar_provider: str | None = None
    exit_bar_feed: str | None = None
    exit_bar_provider: str | None = None
    # The underlying anchor used to select/size the vehicle.  For options this
    # is the snapshot or policy-bound equity spot, not the option premium in
    # ``entry_reference``;
    # retaining it lets null/reference controls stay executable when the entry
    # bar's OHLC was observed after the decision boundary.
    underlying_entry: float | None = None
    underlying_quote_feed: str | None = None
    # The signal's market timestamp remains ``signal_timestamp``.  A delayed
    # recorder event may only become actionable later; retain that causal
    # decision boundary separately so runtime/shadow comparisons do not
    # silently pretend the signal was known at bar close.
    decision_timestamp: datetime | None = None
    # Derived from source provenance whenever any consumed market record was
    # labelled historical backfill. Authorization rejects this marker; replay
    # visibility remains controlled independently by the policy.
    evidence_mode: str = "forward_observed"
    # Diagnostic execution is an authorization property, not a provenance
    # label.  In particular, enabling the explicit backfill policy must not
    # relabel genuinely forward-observed quotes as historical evidence.
    diagnostic_only: bool = False
    authorizing: bool = True
    directional_authorizing: bool = True
    diagnostic_reason: str | None = None
    authored_stop_distance: float | None = None
    effective_stop_floor_bps: float | None = None
    stress_floor_binding: bool = False
    stop_geometry_scenario_bps: float | None = None
    stop_geometry_max_cost_to_risk_ratio: float | None = None
    stop_geometry_activation_reason: str | None = None
    # Canonical durable cause; ``exit_reason`` remains the compatibility
    # alias consumed by legacy research reports.
    canonical_exit_reason: str = ""


@dataclass(frozen=True)
class IBRRefusal:
    """One symbol/session the replay declined, and the reason it declined it.

    ``reason`` is a stable machine token rather than prose so downstream gates
    can aggregate it; ``detail`` carries the numbers that make the refusal
    actionable without re-running the replay.
    """

    vehicle: str
    symbol: str
    session_date: date
    reason: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"vehicle": self.vehicle, "symbol": self.symbol,
                "session_date": self.session_date.isoformat(),
                "reason": self.reason, "detail": dict(self.detail)}


@dataclass
class IBRResult:
    vehicle: str
    trades: list[IBRTrade] = field(default_factory=list)
    # Why the sessions that produced no trade produced none.  A replay that
    # refuses every opportunity is either an edgeless corpus or one the policy
    # cannot price, and those demand opposite responses; without the reason
    # the two are indistinguishable downstream.
    refusals: list["IBRRefusal"] = field(default_factory=list)
    # Source provenance is a first-class result boundary.  Diagnostic replay
    # may price mechanics, but its rows must never be mistaken for evidence
    # eligible for authorization.
    authorizing: bool = True
    diagnostic_only: bool = False
    source_report: Mapping[str, object] | None = None

    @property
    def net_pnl(self) -> float:
        return float(sum(t.net_pnl for t in self.trades))

    @property
    def gross_pnl(self) -> float:
        return float(sum(t.gross_pnl for t in self.trades))

    @property
    def costs(self) -> float:
        return float(sum(t.costs for t in self.trades))

    def summary(self) -> dict:
        """A vehicle-local summary; no cross-vehicle P&L is ever calculated."""
        canonical: dict[str, int] = {}
        for trade in self.trades:
            reason = str(getattr(trade, "canonical_exit_reason", "") or
                         canonical_exit_reason(trade.exit_reason))
            canonical[reason] = canonical.get(reason, 0) + 1
        return {
            "vehicle": self.vehicle,
            "trades": len(self.trades),
            "gross_pnl": self.gross_pnl,
            "costs": self.costs,
            "net_pnl": self.net_pnl,
            "authorizing": bool(self.authorizing),
            "diagnostic_only": bool(self.diagnostic_only),
            "canonical_exit_reasons": dict(sorted(canonical.items())),
        }


def _local(ts: datetime, zone: ZoneInfo) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ReplayError("bars must have timezone-aware timestamps")
    return ts.astimezone(zone)


def _option_liquid(snapshot: OptionSnapshot) -> bool:
    """Backward-compatible private alias for the shared runtime rule."""
    return option_has_liquidity(snapshot)


def _option_executable(snapshot: OptionSnapshot) -> bool:
    """Require the recorded option quote and contract to identify OPRA.

    Feed identity is carried in both the event and contract provenance.  A
    mismatch is treated as non-executable rather than choosing whichever
    spelling happens to be present, so an indicative snapshot cannot enter
    research evidence through an adapter seam.
    """
    identity = getattr(snapshot, "identity", None)
    contract = getattr(snapshot, "contract", None)
    return (str(getattr(identity, "feed", "")).strip().lower() == "opra" and
            str(getattr(contract, "feed", "")).strip().lower() == "opra")


def _historical_evidence(record: object | None) -> bool:
    """Recognize normalized records and retained executable quote fills."""
    return bool(record is not None and (
        historical_backfill_record(record) or
        str(getattr(record, "source_mode", "") or "").strip().lower() ==
        "historical_backfill"))


def _session_start(day: date, cfg: IBRConfig, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, cfg.session_open, tzinfo=zone)


def _available(record: object, policy: ReplayPolicy) -> datetime | None:
    return replay_available_at(
        record,
        allow_historical_backfill_diagnostics=(
            policy.allow_historical_backfill_diagnostics),
    )


def _visible(record: object, event_time: datetime,
             policy: ReplayPolicy) -> bool:
    """A value is usable only when its source was available by that instant."""
    return replay_record_is_available(
        record, event_time,
        allow_historical_backfill_diagnostics=(
            policy.allow_historical_backfill_diagnostics),
    )


def _open_visible(record: object, event_time: datetime,
                  policy: ReplayPolicy) -> bool:
    return replay_open_is_available(
        record, event_time,
        allow_historical_backfill_diagnostics=(
            policy.allow_historical_backfill_diagnostics),
    )


def _validate_bars(bars: Sequence[UnderlyingBar], cfg: IBRConfig) -> list[UnderlyingBar]:
    last_by_symbol: dict[str, datetime] = {}
    for bar in bars:
        previous = last_by_symbol.get(bar.symbol)
        if previous is not None and bar.timestamp < previous:
            raise ReplayError(
                "bars must be chronological per symbol; refusing to sort look-ahead input")
        last_by_symbol[bar.symbol] = bar.timestamp
    ordered = sorted(bars, key=lambda b: b.timestamp)
    if len(ordered) != len({(b.symbol, b.timestamp) for b in ordered}):
        raise ReplayError("duplicate bar timestamps")
    for bar in ordered:
        if bar.interval_seconds != 60:
            raise ReplayError("IBR requires one-minute bars")
        if bar.timestamp.tzinfo is None:
            raise ReplayError("bar timestamp must be timezone-aware")
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise ReplayError("invalid OHLC bounds")
    return ordered


def _source_payload(record: object, *, kind: str) -> dict[str, object]:
    """Project a normalized market record into the source preflight shape."""
    identity = getattr(record, "identity", None)
    timestamp = getattr(record, "timestamp", getattr(record, "ts", None))
    payload: dict[str, object] = {"kind": kind, "timestamp": timestamp}
    if identity is not None:
        for name in ("provider", "feed", "source_mode", "as_of",
                     "observed_at"):
            value = getattr(identity, name, None)
            if value is not None:
                payload[name] = value
    return payload


def _source_records(records: object, *, kind: str) -> list[Mapping]:
    """Materialize source rows without consuming resolver-backed quote indexes."""
    if records is None or callable(getattr(records, "quote_fill", None)):
        return []
    if isinstance(records, Mapping):
        values = records.values()
    else:
        values = records
    rows: list[Mapping] = []
    for record in values:
        if isinstance(record, Mapping):
            row = dict(record)
            row.setdefault("kind", kind)
            rows.append(row)
        else:
            rows.append(_source_payload(record, kind=kind))
    return rows


def _source_preflight(
        bars: Sequence[object], *,
        quotes: object = None,
        option_snapshots: Mapping[datetime, object] | None = None,
        diagnostic_only: bool = False,
) -> tuple[list[UnderlyingBar], dict[str, object]]:
    """Validate provenance before replay can create a top-level result.

    Raw bar mappings are accepted as a convenience for standalone callers so
    an omitted ``source_mode`` remains observable by :func:`validate_source`
    instead of being silently manufactured by the normalizer.
    """
    raw_bars = list(bars)
    source_rows: list[Mapping] = []
    normalized: list[UnderlyingBar] = []
    for item in raw_bars:
        if isinstance(item, Mapping):
            source_rows.append(dict(item))
            normalized.append(normalize_underlying_bar(item))
        else:
            source_rows.append(_source_payload(item, kind="bar"))
            normalized.append(item)
    source_rows.extend(_source_records(quotes, kind="quote"))
    if option_snapshots:
        source_rows.extend(_source_records(option_snapshots, kind="option"))
    try:
        report = validate_source(source_rows, diagnostic_only=diagnostic_only)
    except SourceValidationError as exc:
        report = dict(exc.report)
        report.setdefault("errors", [str(exc)])
        report["preflight_failed"] = True
    else:
        report = dict(report)
        report["preflight_failed"] = False
    # Resolver-backed quote indexes intentionally stay opaque to row
    # materialization.  Their compact provenance aggregate is still part of
    # the source boundary and must participate in the same authorizing-mode
    # decision as bars and in-memory quote rows.
    resolver_counts: dict[str, int] = {}
    resolver_counter = getattr(quotes, "source_mode_counts", None)
    if callable(resolver_counter):
        try:
            raw_counts = resolver_counter()
            if not isinstance(raw_counts, Mapping):
                raise TypeError("source_mode_counts must return a mapping")
            for mode, count in raw_counts.items():
                normalized_mode = (str(mode or "forward_observed").strip()
                                   .lower().replace("-", "_"))
                if normalized_mode not in SOURCE_MODES:
                    raise ValueError(
                        f"unsupported source_mode {normalized_mode!r}")
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(
                        f"source_mode_counts[{normalized_mode!r}] must be a non-negative integer")
                if count:
                    resolver_counts[normalized_mode] = count
        except Exception as exc:
            report.setdefault("errors", []).append(
                f"quote resolver provenance preflight failed: {exc}")
            report["preflight_failed"] = True
            report["authorizing"] = False
    if resolver_counts:
        merged_counts = dict(report.get("source_mode_counts") or {})
        for mode, count in resolver_counts.items():
            merged_counts[mode] = int(merged_counts.get(mode, 0)) + count
        report["source_mode_counts"] = dict(sorted(merged_counts.items()))
        if not diagnostic_only:
            errors = list(report.get("errors") or ())
            if resolver_counts.get("historical_backfill", 0):
                errors.append(
                    "historical_backfill source_mode is diagnostic-only; "
                    "use an explicit diagnostic-only run")
            if len({mode for mode, count in merged_counts.items() if count}) > 1:
                errors.append("mixed source_mode values cannot authorize evidence")
            report["errors"] = errors
            report["preflight_failed"] = bool(errors)
            if errors:
                report["authorizing"] = False
    if resolver_counts.get("historical_backfill"):
        report["authorizing"] = False
        if diagnostic_only:
            report["diagnostic_only"] = True
    report["resolver_source_mode_counts"] = resolver_counts
    return normalized, report


def _trade_from_exit(*, vehicle: str, symbol: str, day: date, direction: str,
                     range_high: float, range_low: float, signal: UnderlyingBar,
                     entry_bar: UnderlyingBar, entry_reference: float,
                     exit_bar: UnderlyingBar, exit_reference: float,
                     reason: str, tie: bool, gap: bool, cfg: IBRConfig,
                     stop_price: float, target_price: float, multiplier: int = 1,
                     entry_timestamp: datetime | None = None,
                     entry_source: str = BAR, exit_source: str = BAR,
                     exit_fill_claim: Mapping[str, object] | None = None,
                     entry_quote_age_seconds: float | None = None,
                     exit_quote_age_seconds: float | None = None,
                     entry_feed: str | None = None,
                     exit_feed: str | None = None,
                     entry_provider: str | None = None,
                     exit_provider: str | None = None,
                     planned_risk_per_unit: float | None = None,
                     decision_timestamp: datetime | None = None,
                     underlying_entry: float | None = None,
                     underlying_quote_feed: str | None = None,
                     authored_stop_distance: float | None = None,
                     effective_stop_floor_bps: float | None = None,
                     stress_floor_binding: bool = False,
                     stop_geometry_scenario_bps: float | None = None,
                     stop_geometry_max_cost_to_risk_ratio: float | None = None,
                     stop_geometry_activation_reason: str | None = None,
                     evidence_records: Sequence[object] = (),
                     ) -> IBRTrade:
    # Listed options are always bought to open in the runtime, including puts
    # used for a short-underlying thesis.  Their P&L is therefore long-option
    # P&L even when the underlying direction is short.
    execution_direction = "long" if vehicle == "option" else direction
    # An option ``ask``/``bid`` and a recorded equity quote are already
    # executable; charging a modelled half-spread on top would bill the same
    # spread twice.  A bar-derived reference still pays the modelled spread.
    entry_price = cfg.costs.execution_price(
        entry_reference, execution_direction, entry=True,
        executable_quote=vehicle == "option" or entry_source == QUOTE)
    exit_price = cfg.costs.execution_price(
        exit_reference, execution_direction, entry=False,
        executable_quote=vehicle == "option" or exit_source == QUOTE)
    signed_move = ((exit_price - entry_price) if execution_direction == "long"
                   else (entry_price - exit_price))
    gross = signed_move * cfg.quantity * multiplier
    costs = cfg.costs.fees(entry_price, exit_price, cfg.quantity, multiplier,
                           vehicle=vehicle)
    # Equity sizing risk is the stop distance authored from the signal bar.
    # A next-bar gap can move the actual fill away from that anchor, so account
    # risk uses the directional distance from the executed entry to the
    # protective stop (a fill already through its stop has zero remaining loss
    # to that stop). This mirrors the runtime/factory distinction between
    # planned sizing risk and realized fill risk.
    if vehicle == "option":
        # Listed options are long-premium positions: the premium paid is the
        # monetary risk unit, including its contract multiplier. Underlying
        # stop geometry is not an option risk proxy.
        risk_per_unit = entry_price * multiplier
        realized_risk_per_unit = risk_per_unit
    else:
        risk_per_unit = (float(planned_risk_per_unit)
                         if planned_risk_per_unit is not None else
                         abs(entry_price - float(stop_price)))
        realized_risk_per_unit = max(
            0.0,
            (entry_price - float(stop_price)
             if execution_direction == "long" else
             float(stop_price) - entry_price),
        )
    risk_usd = float(cfg.quantity) * realized_risk_per_unit
    claim = exit_fill_claim
    schema = None
    if exit_source == RESTING_BRACKET:
        claim = resting_bracket_fill_claim(
            exit_reason=reason, exit_reference=exit_reference,
            stop_price=stop_price, target_price=target_price,
            bar_timestamp=exit_bar.timestamp.isoformat(),
            bar_feed=exit_bar.feed, bar_provider=exit_bar.provider,
            tie_broken=tie)
        schema = RESTING_BRACKET_FILL_SCHEMA
    return IBRTrade(
        vehicle=vehicle, session_date=day, symbol=symbol, direction=direction,
        range_high=range_high, range_low=range_low,
        signal_timestamp=signal.end, entry_timestamp=entry_timestamp or entry_bar.timestamp,
        entry_reference=entry_reference, entry_price=entry_price,
        stop_price=float(stop_price), target_price=float(target_price),
        exit_timestamp=exit_bar.timestamp, exit_reference=exit_reference,
        exit_price=exit_price, exit_reason=reason, gross_pnl=gross,
        costs=costs, net_pnl=gross - costs, tie_broken=tie, gap_fill=gap,
        contract_multiplier=multiplier, entry_fill_source=entry_source,
        exit_fill_source=exit_source,
        exit_fill_schema=schema,
        exit_fill_claim=claim,
        exit_fill_bar_timestamp=(exit_bar.timestamp.isoformat()
                                 if exit_source == RESTING_BRACKET else None),
        entry_quote_age_seconds=entry_quote_age_seconds,
        exit_quote_age_seconds=exit_quote_age_seconds,
        entry_feed=entry_feed, exit_feed=exit_feed,
        entry_provider=entry_provider, exit_provider=exit_provider,
        quantity=float(cfg.quantity), risk_per_unit=risk_per_unit,
        realized_risk_per_unit=realized_risk_per_unit, risk_usd=risk_usd,
        signal_bar_feed=signal.feed, signal_bar_provider=signal.provider,
        entry_bar_feed=entry_bar.feed, entry_bar_provider=entry_bar.provider,
        exit_bar_feed=exit_bar.feed, exit_bar_provider=exit_bar.provider,
        decision_timestamp=decision_timestamp,
        underlying_entry=(float(underlying_entry)
                          if underlying_entry is not None else None),
        underlying_quote_feed=underlying_quote_feed,
        evidence_mode=(
            DIAGNOSTIC_HISTORICAL_BACKFILL
            if any(
                _historical_evidence(item)
                for item in (signal, entry_bar, exit_bar, *evidence_records))
            else "forward_observed"
        ),
        authored_stop_distance=authored_stop_distance,
        effective_stop_floor_bps=effective_stop_floor_bps,
        stress_floor_binding=stress_floor_binding,
        stop_geometry_scenario_bps=stop_geometry_scenario_bps,
        stop_geometry_max_cost_to_risk_ratio=(
            stop_geometry_max_cost_to_risk_ratio),
        stop_geometry_activation_reason=stop_geometry_activation_reason,
        canonical_exit_reason=canonical_exit_reason(reason),
    )


def _replay_session(bars: Sequence[UnderlyingBar], *, vehicle: str, symbol: str,
                    cfg: IBRConfig, option_snapshots: Mapping[datetime, OptionSnapshot] | None = None,
                    quotes: Mapping[str, Sequence[QuoteSnapshot]] | None = None,
                    multiplier: int = 1,
                    refusals: list[IBRRefusal] | None = None) -> IBRTrade | None:
    if not bars:
        return None
    zone = ZoneInfo(cfg.timezone)
    day = _local(bars[0].timestamp, zone).date()

    def refuse(reason: str, detail: Mapping[str, object] | None = None) -> None:
        """Record why this session yields no trade, then decline it.

        Every refusal below is a decision the replay makes on the operator's
        behalf.  Returning a bare ``None`` for all of them makes an unpriceable
        corpus and an edgeless one produce byte-identical output.
        """
        if refusals is not None:
            refusals.append(IBRRefusal(vehicle=vehicle, symbol=symbol,
                                       session_date=day, reason=reason,
                                       detail=dict(detail or {})))
        return None

    try:
        effective_policy = replay_policy_for_bars(
            cfg.policy, bars, session_date=day)
    except CostError as exc:
        return refuse(str(exc))
    cfg = replace(cfg, policy=effective_policy)

    exact_open = bars[0].session_open
    exact_close = bars[0].session_close
    if exact_open is not None and exact_close is not None:
        if any(bar.timestamp < exact_open or bar.end > exact_close
               for bar in bars):
            return refuse("bar_outside_exact_session")

    start = (_local(exact_open, zone) if exact_open is not None
             else _session_start(day, cfg, zone))
    range_end = start + timedelta(minutes=cfg.range_minutes)
    close_at = datetime.combine(day, cfg.force_flat, tzinfo=zone)
    if cfg.policy.force_flat_time is not None:
        close_at = datetime.combine(day, cfg.policy.force_flat_time, tzinfo=zone)
    # Restrict by the source's point-in-time session identity as well as the
    # wall-clock conversion.  A late-corrected event must never migrate into a
    # neighbouring session merely because its timestamp does.
    range_bars = [b for b in bars if b.identity.session_date == day
                  and start <= _local(b.timestamp, zone) < range_end
                  and b.end <= range_end
                  and _available(b, cfg.policy) is not None]
    # A complete range is mandatory.  Missing bars must not make an apparent
    # edge by shrinking the range.
    expected = cfg.range_minutes
    if len(range_bars) != expected:
        return refuse("incomplete_opening_range",
                      {"observed": len(range_bars), "expected": expected})
    range_bars.sort(key=lambda b: b.timestamp)
    for previous, current in zip(range_bars, range_bars[1:]):
        if current.timestamp - previous.timestamp != timedelta(minutes=1):
            return refuse("opening_range_gap")
    high = max(b.high for b in range_bars)
    low = min(b.low for b in range_bars)
    # Keep the first bar at the force-flat boundary so its opening price can
    # be used for the close.  Its intrabar range is never used for a target or
    # stop, since the position must be flat before that bar trades.
    post = [b for b in bars if b.identity.session_date == day
            and b.end > range_end
            and _local(b.timestamp, zone) < close_at + timedelta(minutes=1)]
    post.sort(key=lambda b: b.timestamp)
    if not post:
        return refuse("no_post_range_bars")
    # Adjacency is required where it changes an outcome — the next-bar entry
    # below, and the hold window — not across the whole post-range period.  A
    # minute missing at 15:40 does not invalidate a 10:05 breakout, and
    # rejecting the session for it deletes good observations.
    # Express the buffer against the range boundary.  Using the signal close
    # as the denominator makes the effective threshold subtly depend on the
    # size of the move (and is asymmetric with the short side); a registered
    # ``N`` bps buffer means exactly N bps beyond the completed range high/low.
    breakout_buffer = cfg.breakout_buffer_bps / 10_000.0
    buffer_long = lambda bar: bar.close > high * (1.0 + breakout_buffer)
    buffer_short = lambda bar: bar.close < low * (1.0 - breakout_buffer)
    def breaks(bar: UnderlyingBar) -> bool:
        return ((buffer_long(bar) or buffer_short(bar)) if cfg.close_confirmed
                else (bar.high > high or bar.low < low))
    signal_idx = next((i for i, b in enumerate(post) if breaks(b)), None)
    if signal_idx is None:
        return refuse("no_breakout")
    if signal_idx + 1 >= len(post):
        return refuse("breakout_on_final_bar")
    signal = post[signal_idx]
    next_bar = post[signal_idx + 1]
    if next_bar.timestamp != signal.end:
        # Preserve the next-bar contract when the signal is actionable at the
        # boundary.  If the signal was observed later, the causal entry can
        # move to the first complete bar at/after that observation instead of
        # manufacturing a fill from the stale next-bar OHLC.
        signal_ready_values = [signal.end, *[_available(item, cfg.policy)
                               for item in [*range_bars, signal]
                               if _available(item, cfg.policy) is not None]]
        signal_ready = max(signal_ready_values, default=None)
        if signal_ready is None or signal_ready <= signal.end:
            return refuse("entry_bar_not_adjacent")
    else:
        signal_ready_values = [signal.end, *[_available(item, cfg.policy)
                               for item in [*range_bars, signal]
                               if _available(item, cfg.policy) is not None]]
        signal_ready = max(signal_ready_values, default=None)
        if signal_ready is None:
            return refuse("breakout_not_visible")
    boundary = signal.end
    entry_at = boundary if signal_ready <= boundary else signal_ready
    # Do not inspect the next bar's OHLC when the completed signal was only
    # observed after that bar opened.  The first full bar at/after entry_at is
    # the provenance/exit bar; quote-backed execution can still enter at the
    # exact observation instant between bar boundaries.
    entry_index = next((i for i, bar in enumerate(post[signal_idx + 1:],
                                                  signal_idx + 1)
                        if bar.timestamp >= entry_at), None)
    if entry_index is None:
        return refuse("no_entry_bar_after_signal")
    entry_bar = post[entry_index]
    if (cfg.policy.latest_entry_time is not None and
            _local(entry_at, zone).time() > cfg.policy.latest_entry_time):
        return refuse("past_latest_entry_time")
    # The hold window begins at the first full bar whose timestamp is not
    # earlier than the causal entry instant.  A delayed signal therefore does
    # not read the already-open next bar's high/low/close as if it were known.
    hold = [entry_bar]
    for bar in post[entry_index + 1:]:
        if bar.timestamp != hold[-1].end:
            break
        hold.append(bar)
    long_break = (buffer_long(signal) if cfg.close_confirmed else signal.high > high)
    short_break = (buffer_short(signal) if cfg.close_confirmed else signal.low < low)
    # If one completed bar breaks both sides, stop-first tie semantics choose
    # the side whose stop is encountered first after the next-bar entry.
    direction = "long" if long_break and not short_break else "short"
    if long_break and short_break:
        direction = "long" if (high - signal.open) <= (signal.open - low) else "short"
    # Recorder bars carry completed OHLC and are commonly observed at their
    # close.  Only a boundary-visible bar may supply its opening print;
    # strict quote-backed execution can instead use the quote as the boundary
    # underlying anchor without consuming delayed OHLC.
    entry_bar_visible = _open_visible(
        entry_bar, entry_bar.timestamp, cfg.policy)
    underlying_entry = float(entry_bar.open) if entry_bar_visible else None
    entry_ref = underlying_entry
    entry_source = BAR
    entry_evidence: object | None = None
    underlying_quote_evidence: object | None = None
    # Option snapshots are selected at/before the entry bar.  A caller may pass
    # a sparse mapping from timestamp to snapshot; absence is explicit no-data.
    selected_contract = None
    entry_snap: OptionSnapshot | None = None
    entry_quote_age_seconds: float | None = None
    exit_quote_age_seconds: float | None = None
    entry_feed: str | None = None
    exit_feed: str | None = None
    entry_provider: str | None = None
    exit_provider: str | None = None
    underlying_quote_feed: str | None = None
    if vehicle == "equity":
        # The fill instant is the entry bar's open.  A quote recorded at that
        # instant is the real executable price; the bar open is a trade print
        # that has to be marked up by the modelled spread instead.
        quoted = quote_fill_record(
            quotes, symbol=symbol, at=entry_at,
            side="buy" if direction == "long" else "sell",
            max_age_seconds=cfg.policy.max_market_data_age_seconds,
            session_date=day,
            allow_historical_backfill_diagnostics=(
                cfg.policy.allow_historical_backfill_diagnostics))
        if quoted is not None:
            entry_evidence = quoted
            entry_side = "buy" if direction == "long" else "sell"
            # Compare executable quote evidence with the independent boundary
            # reference before replacing it as the fill anchor.  This is the
            # same fail-closed cap the runtime applies to submitted entries.
            if underlying_entry is not None:
                slippage, slippage_reason = check_entry_slippage(
                    entry_side, underlying_entry, quoted.price,
                    cfg.costs.max_slippage_bps)
                if slippage_reason is not None:
                    return refuse(slippage_reason, slippage)
            # A boundary quote is independent executable evidence.  Use it as
            # the underlying anchor too, so a completed recorder bar observed
            # after its open cannot leak its OHLC into strict replay.
            underlying_entry = quoted.price
            entry_ref, entry_source = quoted.price, QUOTE
            entry_feed, entry_provider = quoted.feed, quoted.provider
            entry_quote_age_seconds = max(
                0.0, (entry_at - quoted.timestamp).total_seconds())
        elif cfg.policy.strict_market_data:
            return refuse("no_quote_at_entry")
        elif not entry_bar_visible:
            return refuse("entry_bar_not_visible")
    if vehicle == "option":
        option_policy = cfg.policy
        if option_snapshots is None:
            return refuse("no_option_snapshots")
        wanted_right = "call" if direction == "long" else "put"
        eligible = [s for s in option_snapshots.values()
                    if s.timestamp <= entry_at
                    and _visible(s, entry_at, option_policy)
                    and s.session_date == day and s.ask > 0 and s.bid > 0
                    and entry_at.timestamp() - s.timestamp.timestamp() <=
                        option_policy.max_market_data_age_seconds
                    and str(s.contract.underlying).upper() == str(symbol).upper()
                    and max(1, option_policy.options_min_dte) <= (s.contract.expiration - day).days <=
                        option_policy.options_max_dte
                    and ((s.ask - s.bid) / ((s.ask + s.bid) / 2.0) * 100.0 <=
                         option_policy.options_max_spread_pct)
                    and option_has_liquidity(s)
                    and _option_executable(s)
                    and str(s.contract.right).lower() in {
                        wanted_right, wanted_right[0]}]
        if not eligible:
            return refuse("no_eligible_option_contract")
        spot = next((item.underlying_price for item in sorted(
            eligible, key=lambda item: (item.timestamp, item.contract.symbol), reverse=True)
            if item.underlying_price), underlying_entry)
        if spot is None or spot <= 0:
            # A delayed signal cannot use the stale next-bar OHLC as an
            # underlying spot proxy. Use an independently observed fresh
            # policy-bound equity quote at the causal entry boundary when the
            # option snapshot did not carry spot itself.
            spot_quote = quote_fill_record(
                quotes, symbol=symbol, at=entry_at,
                side="buy" if direction == "long" else "sell",
                max_age_seconds=cfg.policy.max_market_data_age_seconds,
                session_date=day,
                allow_historical_backfill_diagnostics=(
                    cfg.policy.allow_historical_backfill_diagnostics))
            if spot_quote is None:
                return refuse("entry_bar_not_visible")
            underlying_quote_evidence = spot_quote
            underlying_quote_feed = str(
                spot_quote.feed or "").strip().lower().replace("-", "_")
            if underlying_quote_feed == "delayed":
                underlying_quote_feed = "delayed_sip"
            if (cfg.policy.strict_market_data and
                    underlying_quote_feed != cfg.policy.equity_feed):
                return refuse(
                    "underlying_quote_feed_mismatch",
                    {"expected": cfg.policy.equity_feed,
                     "observed": underlying_quote_feed})
            underlying_entry = spot = spot_quote.price
        snap = min(eligible, key=lambda item: (
            abs(float(item.contract.strike) - float(spot)),
            (item.ask - item.bid) / ((item.ask + item.bid) / 2.0),
            -item.timestamp.timestamp(), item.contract.symbol))
        entry_snap = snap
        entry_evidence = snap
        contract = snap.contract
        selected_contract = contract
        entry_ref = snap.ask
        if snap.underlying_price and snap.underlying_price > 0:
            underlying_entry = float(snap.underlying_price)
        elif underlying_entry is None:
            return refuse("entry_bar_not_visible")
        if entry_ref <= 0:
            return refuse("option_entry_ask_not_positive")
        multiplier = snap.contract.multiplier
        entry_source = QUOTE
        entry_quote_age_seconds = max(
            0.0, (entry_at - snap.timestamp).total_seconds())
        entry_feed = str(snap.identity.feed)
        entry_provider = str(snap.identity.provider)
    # The runtime derives both levels from the completed breakout bar's close,
    # because the bracket legs are submitted with the entry and no fill price
    # exists yet.  Anchoring here to the entry bar's open instead would give
    # research a systematically different R than the deployed system.
    anchor = float(signal.close)
    if cfg.range_stop:
        stop = low if direction == "long" else high
        distance = max(abs(anchor - stop),
                       abs(anchor) * MIN_STOP_DISTANCE_FRACTION)
        target_r = cfg.target_r if cfg.target_r is not None else (
            cfg.target_pct / cfg.stop_pct)
    else:
        distance = max(abs(anchor) * cfg.stop_pct,
                       abs(anchor) * MIN_STOP_DISTANCE_FRACTION)
        target_distance = max(abs(anchor) * cfg.target_pct,
                              abs(anchor) * MIN_STOP_DISTANCE_FRACTION)
        target_r = target_distance / distance
    authored_distance = distance
    stop_floor_bps = MIN_STOP_DISTANCE_BPS
    stop_floor_binding = False
    stop_geometry_scenario = None
    stop_geometry_activation_reason = "stress_disabled"
    if (vehicle == "equity" and
            (cfg.policy.stressed_cost_scenario_bps is not None or
             cfg.policy.stressed_cost_calibration_enabled) and
            cfg.policy.max_stressed_cost_to_risk_ratio is not None):
        stop_geometry_scenario, stop_geometry_activation_reason = (
            cfg.policy.resolve_stress_scenario(
                symbol, entry_at, vehicle="equity"))
        if stop_geometry_scenario is None:
            return refuse("stressed_cost_invalid")
        try:
            distance, stop_floor_bps = effective_stop_distance(
                anchor, authored_distance,
                base_floor_bps=MIN_STOP_DISTANCE_BPS,
                scenario_bps=stop_geometry_scenario,
                max_cost_to_risk_ratio=(
                    cfg.policy.max_stressed_cost_to_risk_ratio),
                minimum_increment=equity_price_increment(anchor))
        except RiskGeometryError:
            return refuse("stressed_cost_invalid")
        stop_floor_binding = distance > authored_distance + 1e-12
    stop = anchor - distance if direction == "long" else anchor + distance
    target = (anchor + target_r * distance if direction == "long"
              else anchor - target_r * distance)
    if (not math.isfinite(stop) or stop <= 0 or
            not math.isfinite(target) or target <= 0 or
            (direction == "long" and not (stop < anchor < target)) or
            (direction == "short" and not (target < anchor < stop))):
        return refuse("stressed_cost_invalid")
    if vehicle == "equity":
        try:
            stop, target, distance = quantize_equity_bracket(
                anchor, stop, target, direction)
        except RiskGeometryError:
            return refuse("broker_tick_geometry_invalid")

    def option_exit_snapshot(cutoff: datetime) -> OptionSnapshot | None:
        if vehicle != "option":
            return None
        eligible_exits = [s for s in option_snapshots.values()
                          if selected_contract is not None
                          and s.contract == selected_contract
                          and s.timestamp <= cutoff and _visible(s, cutoff, cfg.policy)
                          and s.session_date == day and s.bid > 0
                          and cutoff.timestamp() - s.timestamp.timestamp() <=
                              cfg.policy.max_market_data_age_seconds
                          and _option_executable(s)]
        if not eligible_exits:
            return None
        return max(eligible_exits, key=lambda item: item.timestamp)

    def option_exit_reference(cutoff: datetime) -> float | None:
        snapshot = option_exit_snapshot(cutoff)
        return None if snapshot is None else snapshot.bid

    exit_evidence: object | None = None

    def option_exit_fill(cutoff: datetime) -> float | None:
        """Return the exit bid and retain its point-in-time provenance."""
        nonlocal exit_quote_age_seconds, exit_feed, exit_provider, exit_evidence
        snapshot = option_exit_snapshot(cutoff)
        if snapshot is None:
            return None
        exit_evidence = snapshot
        exit_quote_age_seconds = max(
            0.0, (cutoff - snapshot.timestamp).total_seconds())
        exit_feed = str(snapshot.identity.feed)
        exit_provider = str(snapshot.identity.provider)
        return snapshot.bid

    exit_side = "sell" if direction == "long" else "buy"

    def boundary_exit(reference: float, at: datetime) -> tuple[float, str, str | None, str | None]:
        """Price a fill that happens at a bar boundary, quote-first."""
        nonlocal exit_quote_age_seconds, exit_evidence
        if vehicle != "equity":
            return reference, BAR, None, None
        quoted = quote_fill_record(
            quotes, symbol=symbol, at=at, side=exit_side,
            max_age_seconds=cfg.policy.max_market_data_age_seconds,
            session_date=day,
            allow_historical_backfill_diagnostics=(
                cfg.policy.allow_historical_backfill_diagnostics))
        if quoted is None:
            return reference, BAR, None, None
        exit_evidence = quoted
        exit_quote_age_seconds = max(
            0.0, (at - quoted.timestamp).total_seconds())
        return quoted.price, QUOTE, quoted.feed, quoted.provider

    def consumed_evidence(exit_bar: UnderlyingBar) -> tuple[object, ...]:
        consumed_bars = [*range_bars, *(
            bar for bar in post if bar.timestamp <= exit_bar.timestamp)]
        return tuple(consumed_bars) + tuple(
            item for item in (
                entry_evidence, underlying_quote_evidence, exit_evidence)
            if item is not None)

    for bar in hold:
        if _local(bar.timestamp, zone) >= close_at:
            break
        if _available(bar, cfg.policy) is None:
            # A completed post-entry candle may be observed after its market
            # end.  It is still valid resting-order outcome evidence once the
            # full record exists; only the pre-entry bar is excluded above.
            continue
        # A gap through a level is filled at the bar open, not at an
        # impossible stop/target price.
        if direction == "long":
            gap_stop, gap_target = bar.open <= stop, bar.open >= target
            hit_stop, hit_target = bar.low <= stop, bar.high >= target
        else:
            gap_stop, gap_target = bar.open >= stop, bar.open <= target
            hit_stop, hit_target = bar.high >= stop, bar.low <= target
        if gap_stop or gap_target:
            reason = "stop" if gap_stop else "target"
            exit_source = BAR
            if vehicle == "option":
                exit_source = QUOTE
                exit_reference = option_exit_fill(bar.timestamp)
            else:
                (exit_reference, exit_source, exit_feed,
                 exit_provider) = boundary_exit(bar.open, bar.timestamp)
            if (cfg.policy.strict_market_data and vehicle == "equity" and
                    exit_source != QUOTE):
                return refuse("no_quote_at_gap_exit")
            if exit_reference is None:
                return refuse("no_exit_reference_at_gap")
            return _trade_from_exit(vehicle=vehicle, symbol=symbol, day=day, direction=direction,
                                    range_high=high, range_low=low, signal=signal,
                                    entry_bar=entry_bar, entry_reference=entry_ref,
                                    exit_bar=bar, exit_reference=exit_reference,
                                    reason=reason, tie=False, gap=True, cfg=cfg,
                                    multiplier=multiplier, stop_price=stop,
                                    target_price=target, entry_source=entry_source,
                                    entry_timestamp=entry_at,
                                    decision_timestamp=signal_ready,
                                    underlying_entry=underlying_entry,
                                    exit_source=exit_source,
                                    entry_quote_age_seconds=entry_quote_age_seconds,
                                    exit_quote_age_seconds=exit_quote_age_seconds,
                                    entry_feed=entry_feed, exit_feed=exit_feed,
                                    entry_provider=entry_provider,
                                    exit_provider=exit_provider,
                                    planned_risk_per_unit=distance,
                                    underlying_quote_feed=underlying_quote_feed,
                                    authored_stop_distance=authored_distance,
                                    effective_stop_floor_bps=stop_floor_bps,
                                    stress_floor_binding=stop_floor_binding,
                                    stop_geometry_scenario_bps=(
                                        stop_geometry_scenario),
                                    stop_geometry_max_cost_to_risk_ratio=(
                                        cfg.policy.max_stressed_cost_to_risk_ratio),
                                    stop_geometry_activation_reason=(
                                        stop_geometry_activation_reason),
                                    evidence_records=consumed_evidence(bar))
        if hit_stop or hit_target:
            # Stop wins if both are touched by one candle.
            reason = "stop" if hit_stop else "target"
            level = stop if reason == "stop" else target
            # Intrabar trigger has no precise quote instant.  Use the latest
            # snapshot available at the bar open, never a post-trigger bar-end
            # quote that would look into the future.
            exit_reference = (option_exit_fill(
                bar.timestamp if cfg.policy.strict_market_data else bar.end)
                              if vehicle == "option" else level)
            if exit_reference is None:
                return refuse("no_option_exit_reference_at_level")
            return _trade_from_exit(vehicle=vehicle, symbol=symbol, day=day, direction=direction,
                                    range_high=high, range_low=low, signal=signal,
                                    entry_bar=entry_bar, entry_reference=entry_ref,
                                    exit_bar=bar, exit_reference=exit_reference,
                                    reason=reason, tie=hit_stop and hit_target, gap=False,
                                    cfg=cfg, multiplier=multiplier,
                                    stop_price=stop, target_price=target,
                                    entry_source=entry_source,
                                    entry_timestamp=entry_at,
                                    decision_timestamp=signal_ready,
                                    underlying_entry=underlying_entry,
                                    # Equity non-gap level triggers are fills
                                    # of the already-resting broker bracket;
                                    # they must not masquerade as a quote or a
                                    # generic bar/time fallback.  Options
                                    # retain their executable OPRA bid path.
                                    exit_source=(QUOTE if vehicle == "option"
                                                 else RESTING_BRACKET),
                                    entry_quote_age_seconds=entry_quote_age_seconds,
                                    exit_quote_age_seconds=exit_quote_age_seconds,
                                    entry_feed=entry_feed, exit_feed=exit_feed,
                                    entry_provider=entry_provider,
                                    exit_provider=exit_provider,
                                    planned_risk_per_unit=distance,
                                    underlying_quote_feed=underlying_quote_feed,
                                    authored_stop_distance=authored_distance,
                                    effective_stop_floor_bps=stop_floor_bps,
                                    stress_floor_binding=stop_floor_binding,
                                    stop_geometry_scenario_bps=(
                                        stop_geometry_scenario),
                                    stop_geometry_max_cost_to_risk_ratio=(
                                        cfg.policy.max_stressed_cost_to_risk_ratio),
                                    stop_geometry_activation_reason=(
                                        stop_geometry_activation_reason),
                                    evidence_records=consumed_evidence(bar))
    # Force-flat at the last completed bar before the configured close.
    boundary = next((b for b in hold if _local(b.timestamp, zone) >= close_at
                     and _open_visible(b, b.timestamp, cfg.policy)), None)
    exit_source = BAR
    if boundary is not None:
        last = boundary
        exit_ref, exit_source, exit_feed, exit_provider = boundary_exit(
            last.open, last.timestamp)
    else:
        candidates = [b for b in hold if b.end <= close_at and
                      _available(b, cfg.policy) is not None]
        if not candidates:
            return refuse("no_visible_bar_before_force_flat")
        last = candidates[-1]
        exit_ref, exit_source, exit_feed, exit_provider = boundary_exit(
            last.close, last.end)
    if vehicle == "option":
        # Option liquidation is always priced from the selected contract's
        # latest visible bid, including the force-flat boundary path.
        exit_ref = option_exit_fill(last.timestamp)
        if exit_ref is None:
            return refuse("no_option_exit_reference_at_force_flat")
    elif cfg.policy.strict_market_data and exit_source != QUOTE:
        return refuse("no_quote_at_force_flat_exit")
    return _trade_from_exit(vehicle=vehicle, symbol=symbol, day=day, direction=direction,
                            range_high=high, range_low=low, signal=signal,
                            entry_bar=entry_bar, entry_reference=entry_ref,
                            exit_bar=last, exit_reference=exit_ref,
                            reason="force_flat", tie=False, gap=False, cfg=cfg,
                            multiplier=multiplier, stop_price=stop,
                            target_price=target, entry_source=entry_source,
                            entry_timestamp=entry_at,
                            decision_timestamp=signal_ready,
                            underlying_entry=underlying_entry,
                            exit_source=(QUOTE if vehicle == "option" else exit_source),
                            entry_quote_age_seconds=entry_quote_age_seconds,
                            exit_quote_age_seconds=exit_quote_age_seconds,
                            entry_feed=entry_feed, exit_feed=exit_feed,
                            entry_provider=entry_provider,
                            exit_provider=exit_provider,
                            planned_risk_per_unit=distance,
                            underlying_quote_feed=underlying_quote_feed,
                            authored_stop_distance=authored_distance,
                            effective_stop_floor_bps=stop_floor_bps,
                            stress_floor_binding=stop_floor_binding,
                            stop_geometry_scenario_bps=(
                                stop_geometry_scenario),
                            stop_geometry_max_cost_to_risk_ratio=(
                                cfg.policy.max_stressed_cost_to_risk_ratio),
                            stop_geometry_activation_reason=(
                                stop_geometry_activation_reason),
                            evidence_records=consumed_evidence(last))


def replay_ibr(bars: Iterable[UnderlyingBar], *, symbol: str | None = None,
               config: IBRConfig | None = None, vehicle: str = "equity",
               option_snapshots: Mapping[datetime, OptionSnapshot] | None = None,
               quotes: Iterable[QuoteSnapshot] | None = None) -> IBRResult:
    """Replay each session and return a result scoped to one vehicle.

    ``vehicle`` is either ``equity`` or ``option``.  To compare vehicles call
    this function twice; intentionally there is no pooled result API.
    """
    if vehicle not in {"equity", "option"}:
        raise ReplayError("vehicle must be 'equity' or 'option'")
    cfg = config or IBRConfig()
    # Non-strict bar fallback is a legacy mechanics lane and, like the
    # explicit backfill policy, cannot authorize evidence.  Keep it usable for
    # existing diagnostics while exposing the non-authorizing envelope.
    diagnostic_only = bool(
        cfg.policy.allow_historical_backfill_diagnostics or
        not cfg.policy.strict_market_data)
    # Run the shared source-mode boundary before any session can emit a trade.
    # A failed authorizing preflight is represented as an unevaluable result,
    # preserving the standalone replay API's result shape while preventing a
    # historical/unlabelled/late corpus from looking like evidence.
    materialized_bars = list(bars)
    quote_input = (quotes if quotes is not None and
                   callable(getattr(quotes, "quote_fill", None)) else
                   list(quotes or ()))
    rows, source_report = _source_preflight(
        materialized_bars, quotes=quote_input, option_snapshots=option_snapshots,
        diagnostic_only=diagnostic_only)
    # A large recorded corpus may provide a disk-backed resolver.  Passing it
    # through avoids materializing every quote just to answer boundary fills;
    # ordinary test/in-memory iterables retain the historical grouping path.
    quote_index = index_quotes(quote_input)
    rows = _validate_bars(rows, cfg)
    if symbol is not None:
        rows = [b for b in rows if b.symbol == symbol]
    sessions: dict[tuple[str, date], list[UnderlyingBar]] = {}
    zone = ZoneInfo(cfg.timezone)
    for bar in rows:
        sessions.setdefault((bar.symbol, _local(bar.timestamp, zone).date()), []).append(bar)
    source_failed = bool(source_report.get("preflight_failed"))
    source_diagnostic = bool(
        source_report.get("source_mode_counts", {}).get("historical_backfill") or
        source_report.get("implicit_source_mode_rows") or
        source_report.get("late_forward_observation_rows"))
    policy_diagnostic = bool(
        cfg.policy.allow_historical_backfill_diagnostics)
    run_diagnostic = bool(source_diagnostic or policy_diagnostic)
    if not run_diagnostic and diagnostic_only:
        # ``strict_market_data=False`` is retained for ordinary forward
        # legacy fixtures; its bar fallback alone does not make provenance
        # diagnostic when every source row is explicitly forward-observed.
        source_report = dict(source_report)
        source_report.update({"authorizing": True, "diagnostic_only": False})
    result = IBRResult(
        vehicle=vehicle,
        authorizing=not source_failed and not run_diagnostic,
        diagnostic_only=run_diagnostic,
        source_report=source_report,
    )
    if run_diagnostic:
        report = dict(result.source_report or {})
        report.update({
            "authorizing": False,
            "diagnostic_only": True,
            "diagnostic_reason": (
                "diagnostic_source_provenance" if source_diagnostic else
                "diagnostic_backfill_policy"),
        })
        result.source_report = report
    if source_failed:
        detail = {"errors": list(source_report.get("errors") or ())}
        for (sym, day), _session_bars in sorted(
                sessions.items(), key=lambda item: item[0]):
            result.refusals.append(IBRRefusal(
                vehicle=vehicle, symbol=sym, session_date=day,
                reason="source_preflight_failed", detail=detail))
        return result
    bar_fallback_diagnostic = False
    for (sym, _), session_bars in sorted(sessions.items(), key=lambda item: item[0]):
        trade = _replay_session(session_bars, vehicle=vehicle, symbol=sym, cfg=cfg,
                                option_snapshots=option_snapshots,
                                quotes=quote_index, refusals=result.refusals)
        if trade is not None:
            # A valid quote-backed entry with a broker-resident resting
            # bracket exit is still forward evidence.  Only a non-quote,
            # non-resting-bracket leg indicates actual bar fallback.
            trade_uses_bar_fallback = (
                not cfg.policy.strict_market_data and
                not (trade.entry_fill_source == QUOTE and
                     trade.exit_fill_source in {QUOTE, RESTING_BRACKET}))
            if trade_uses_bar_fallback:
                bar_fallback_diagnostic = True
            if (trade_uses_bar_fallback and
                    trade.evidence_mode != DIAGNOSTIC_HISTORICAL_BACKFILL):
                trade = replace(trade, evidence_mode=DIAGNOSTIC_BAR_FALLBACK)
            trade_mode = str(trade.evidence_mode or "").strip().lower()
            trade_diagnostic_reason = (
                trade_mode if trade_mode in {
                    DIAGNOSTIC_HISTORICAL_BACKFILL,
                    DIAGNOSTIC_BAR_FALLBACK,
                } else
                "diagnostic_source_provenance" if source_diagnostic else
                "diagnostic_backfill_policy" if policy_diagnostic else None
            )
            if trade_diagnostic_reason is not None:
                trade = replace(
                    trade,
                    diagnostic_only=True,
                    authorizing=False,
                    directional_authorizing=False,
                    diagnostic_reason=trade_diagnostic_reason,
                )
            result.trades.append(trade)
    if bar_fallback_diagnostic:
        result.authorizing = False
        result.diagnostic_only = True
        report = dict(result.source_report or {})
        report.update({"authorizing": False, "diagnostic_only": True,
                       "bar_fallback": True})
        result.source_report = report
    return result


def replay_ibr_vehicles(
        bars: Iterable[UnderlyingBar], *, symbol: str | None = None,
        config: IBRConfig | None = None,
        option_snapshots: Mapping[datetime, OptionSnapshot] | None = None,
        quotes: Iterable[QuoteSnapshot] | None = None,
        vehicles: Sequence[str] = ("equity", "option"),
    ) -> dict[str, IBRResult]:
    """Run independent vehicle books and return them keyed by vehicle.

    The return type is intentionally a mapping of separate results rather than
    an aggregate; callers must choose which vehicle's P&L to report.
    """
    requested = tuple(vehicles)
    if len(set(requested)) != len(requested):
        raise ReplayError("vehicles must be unique")
    materialized = list(bars)
    quote_index = (quotes if quotes is not None and
                   callable(getattr(quotes, "quote_fill", None)) else
                   list(quotes or ()))
    return {
        vehicle: replay_ibr(
            materialized, symbol=symbol, config=config, vehicle=vehicle,
            option_snapshots=option_snapshots, quotes=quote_index)
        for vehicle in requested
    }


__all__ = [
    "IBRConfig", "IBRResult", "IBRTrade", "ReplayError", "replay_ibr",
    "replay_ibr_vehicles",
]
