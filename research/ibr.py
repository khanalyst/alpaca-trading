"""Deterministic Initial Balance Range (IBR) replay for US sessions.

IBR is deliberately a small replay harness rather than a strategy framework:
the range is made only from completed one-minute bars, a breakout is observed
at bar close, and the order is entered on the following bar.  This makes the
look-ahead boundary explicit and keeps equity and option vehicles as separate
populations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .costs import BAR, QUOTE, CostModel, ReplayPolicy, index_quotes, quote_fill

RUNTIME_MAX_MARKET_DATA_AGE_SECONDS = 30.0
from .market_data import (OptionSnapshot, QuoteSnapshot, UnderlyingBar,
                          option_has_liquidity)


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


@dataclass
class IBRResult:
    vehicle: str
    trades: list[IBRTrade] = field(default_factory=list)

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
        return {
            "vehicle": self.vehicle,
            "trades": len(self.trades),
            "gross_pnl": self.gross_pnl,
            "costs": self.costs,
            "net_pnl": self.net_pnl,
        }


def _local(ts: datetime, zone: ZoneInfo) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ReplayError("bars must have timezone-aware timestamps")
    return ts.astimezone(zone)


def _option_liquid(snapshot: OptionSnapshot) -> bool:
    """Backward-compatible private alias for the shared runtime rule."""
    return option_has_liquidity(snapshot)


def _session_start(day: date, cfg: IBRConfig, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, cfg.session_open, tzinfo=zone)


def _visible(record: object, event_time: datetime) -> bool:
    """A value is usable only when its source was available by that instant."""
    identity = getattr(record, "identity", None)
    as_of = getattr(identity, "as_of", None)
    return as_of is not None and as_of <= event_time


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


def _trade_from_exit(*, vehicle: str, symbol: str, day: date, direction: str,
                     range_high: float, range_low: float, signal: UnderlyingBar,
                     entry_bar: UnderlyingBar, entry_reference: float,
                     exit_bar: UnderlyingBar, exit_reference: float,
                     reason: str, tie: bool, gap: bool, cfg: IBRConfig,
                     stop_price: float, target_price: float, multiplier: int = 1,
                     entry_timestamp: datetime | None = None,
                     entry_source: str = BAR, exit_source: str = BAR) -> IBRTrade:
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
    )


def _replay_session(bars: Sequence[UnderlyingBar], *, vehicle: str, symbol: str,
                    cfg: IBRConfig, option_snapshots: Mapping[datetime, OptionSnapshot] | None = None,
                    quotes: Mapping[str, Sequence[QuoteSnapshot]] | None = None,
                    multiplier: int = 1) -> IBRTrade | None:
    if not bars:
        return None
    zone = ZoneInfo(cfg.timezone)
    day = _local(bars[0].timestamp, zone).date()
    start = _session_start(day, cfg, zone)
    range_end = start + timedelta(minutes=cfg.range_minutes)
    close_at = datetime.combine(day, cfg.force_flat, tzinfo=zone)
    if cfg.policy.force_flat_time is not None:
        close_at = datetime.combine(day, cfg.policy.force_flat_time, tzinfo=zone)
    # Restrict by the source's point-in-time session identity as well as the
    # wall-clock conversion.  A late-corrected event must never migrate into a
    # neighbouring session merely because its timestamp does.
    range_bars = [b for b in bars if b.identity.session_date == day
                  and start <= _local(b.timestamp, zone) < range_end
                  and b.end <= range_end and _visible(b, b.end)]
    # A complete range is mandatory.  Missing bars must not make an apparent
    # edge by shrinking the range.
    expected = cfg.range_minutes
    if len(range_bars) != expected:
        return None
    range_bars.sort(key=lambda b: b.timestamp)
    for previous, current in zip(range_bars, range_bars[1:]):
        if current.timestamp - previous.timestamp != timedelta(minutes=1):
            return None
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
        return None
    for previous, current in zip(post, post[1:]):
        if current.timestamp - previous.timestamp != timedelta(minutes=1):
            return None
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
    if signal_idx is None or signal_idx + 1 >= len(post):
        return None
    signal = post[signal_idx]
    entry_bar = post[signal_idx + 1]
    if entry_bar.timestamp != signal.end:
        # "Next bar" means the immediate following one-minute bar; carrying a
        # signal across an outage would turn a stale breakout into an entry.
        return None
    # The completed breakout must be visible by its close.  The next bar's
    # *open* is the boundary observation; the completed bar record itself is
    # naturally not published until its end. Executable pricing below still
    # requires a point-in-time quote at the boundary.
    if not _visible(signal, signal.end):
        return None
    if (cfg.policy.latest_entry_time is not None and
            _local(entry_bar.timestamp, zone).time() > cfg.policy.latest_entry_time):
        return None
    long_break = (buffer_long(signal) if cfg.close_confirmed else signal.high > high)
    short_break = (buffer_short(signal) if cfg.close_confirmed else signal.low < low)
    # If one completed bar breaks both sides, stop-first tie semantics choose
    # the side whose stop is encountered first after the next-bar entry.
    direction = "long" if long_break and not short_break else "short"
    if long_break and short_break:
        direction = "long" if (high - signal.open) <= (signal.open - low) else "short"
    underlying_entry = entry_bar.open
    entry_ref = underlying_entry
    entry_source = BAR
    # Option snapshots are selected at/before the entry bar.  A caller may pass
    # a sparse mapping from timestamp to snapshot; absence is explicit no-data.
    selected_contract = None
    if vehicle == "equity":
        # The fill instant is the entry bar's open.  A quote recorded at that
        # instant is the real executable price; the bar open is a trade print
        # that has to be marked up by the modelled spread instead.
        quoted = quote_fill(
            quotes, symbol=symbol, at=entry_bar.timestamp,
            side="buy" if direction == "long" else "sell",
            max_age_seconds=cfg.policy.max_market_data_age_seconds,
            session_date=day)
        if quoted is not None:
            entry_ref, entry_source = quoted, QUOTE
        elif cfg.policy.strict_market_data:
            return None
    if vehicle == "option":
        option_policy = cfg.policy
        if option_snapshots is None:
            return None
        wanted_right = "call" if direction == "long" else "put"
        eligible = [s for s in option_snapshots.values()
                    if s.timestamp <= entry_bar.timestamp
                    and _visible(s, entry_bar.timestamp)
                    and s.session_date == day and s.ask > 0 and s.bid > 0
                    and entry_bar.timestamp.timestamp() - s.timestamp.timestamp() <=
                        option_policy.max_market_data_age_seconds
                    and str(s.contract.underlying).upper() == str(symbol).upper()
                    and max(1, option_policy.options_min_dte) <= (s.contract.expiration - day).days <=
                        option_policy.options_max_dte
                    and ((s.ask - s.bid) / ((s.ask + s.bid) / 2.0) * 100.0 <=
                         option_policy.options_max_spread_pct)
                    and option_has_liquidity(s)
                    and str(s.contract.right).lower() in {
                        wanted_right, wanted_right[0]}]
        if not eligible:
            return None
        spot = next((item.underlying_price for item in sorted(
            eligible, key=lambda item: (item.timestamp, item.contract.symbol), reverse=True)
            if item.underlying_price), underlying_entry)
        snap = min(eligible, key=lambda item: (
            abs(float(item.contract.strike) - float(spot)),
            (item.ask - item.bid) / ((item.ask + item.bid) / 2.0),
            -item.timestamp.timestamp(), item.contract.symbol))
        contract = snap.contract
        selected_contract = contract
        entry_ref = snap.ask
        if entry_ref <= 0:
            return None
        multiplier = snap.contract.multiplier
    # The runtime derives both levels from the completed breakout bar's close,
    # because the bracket legs are submitted with the entry and no fill price
    # exists yet.  Anchoring here to the entry bar's open instead would give
    # research a systematically different R than the deployed system.
    anchor = float(signal.close)
    if cfg.range_stop:
        stop = low if direction == "long" else high
        distance = abs(anchor - stop)
        target_r = cfg.target_r if cfg.target_r is not None else (
            cfg.target_pct / cfg.stop_pct)
        target = (anchor + target_r * distance if direction == "long"
                  else anchor - target_r * distance)
    else:
        stop = anchor * (
            1 - cfg.stop_pct if direction == "long" else 1 + cfg.stop_pct)
        target = anchor * (
            1 + cfg.target_pct if direction == "long" else 1 - cfg.target_pct)

    def option_exit_reference(cutoff: datetime) -> float | None:
        if vehicle != "option":
            return None
        eligible_exits = [s for s in option_snapshots.values()
                          if selected_contract is not None
                          and s.contract == selected_contract
                          and s.timestamp <= cutoff and _visible(s, cutoff)
                          and s.session_date == day and s.bid > 0
                          and cutoff.timestamp() - s.timestamp.timestamp() <=
                              cfg.policy.max_market_data_age_seconds]
        if not eligible_exits:
            return None
        return max(eligible_exits, key=lambda item: item.timestamp).bid

    exit_side = "sell" if direction == "long" else "buy"

    def boundary_exit(reference: float, at: datetime) -> tuple[float, str]:
        """Price a fill that happens at a bar boundary, quote-first."""
        if vehicle != "equity":
            return reference, BAR
        quoted = quote_fill(
            quotes, symbol=symbol, at=at, side=exit_side,
            max_age_seconds=cfg.policy.max_market_data_age_seconds,
            session_date=day)
        return (quoted, QUOTE) if quoted is not None else (reference, BAR)

    for bar in post[signal_idx + 1:]:
        if _local(bar.timestamp, zone) >= close_at:
            break
        if not _visible(bar, bar.end):
            # An exit cannot use an as-yet-unavailable candle.  Continue to a
            # later visible bar; force-flat below still requires visibility.
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
                exit_reference = option_exit_reference(bar.timestamp)
            else:
                exit_reference, exit_source = boundary_exit(bar.open, bar.timestamp)
            if (cfg.policy.strict_market_data and vehicle == "equity" and
                    exit_source != QUOTE):
                return None
            if exit_reference is None:
                return None
            return _trade_from_exit(vehicle=vehicle, symbol=symbol, day=day, direction=direction,
                                    range_high=high, range_low=low, signal=signal,
                                    entry_bar=entry_bar, entry_reference=entry_ref,
                                    exit_bar=bar, exit_reference=exit_reference,
                                    reason=reason, tie=False, gap=True, cfg=cfg,
                                    multiplier=multiplier, stop_price=stop,
                                    target_price=target, entry_source=entry_source,
                                    exit_source=exit_source)
        if hit_stop or hit_target:
            # Stop wins if both are touched by one candle.
            reason = "stop" if hit_stop else "target"
            level = stop if reason == "stop" else target
            # Intrabar trigger has no precise quote instant.  Use the latest
            # snapshot available at the bar open, never a post-trigger bar-end
            # quote that would look into the future.
            exit_reference = (option_exit_reference(
                bar.timestamp if cfg.policy.strict_market_data else bar.end)
                              if vehicle == "option" else level)
            if exit_reference is None:
                return None
            return _trade_from_exit(vehicle=vehicle, symbol=symbol, day=day, direction=direction,
                                    range_high=high, range_low=low, signal=signal,
                                    entry_bar=entry_bar, entry_reference=entry_ref,
                                    exit_bar=bar, exit_reference=exit_reference,
                                    reason=reason, tie=hit_stop and hit_target, gap=False,
                                    cfg=cfg, multiplier=multiplier,
                                    stop_price=stop, target_price=target,
                                    entry_source=entry_source)
    # Force-flat at the last completed bar before the configured close.
    boundary = next((b for b in post if _local(b.timestamp, zone) >= close_at
                     and _visible(b, b.timestamp)), None)
    exit_source = BAR
    if boundary is not None:
        last = boundary
        exit_ref, exit_source = boundary_exit(last.open, last.timestamp)
    else:
        candidates = [b for b in post if b.end <= close_at and _visible(b, b.end)]
        if not candidates:
            return None
        last = candidates[-1]
        exit_ref, exit_source = boundary_exit(last.close, last.end)
        if vehicle == "option":
            exit_ref = option_exit_reference(last.timestamp)
            if exit_ref is None:
                return None
        elif cfg.policy.strict_market_data and exit_source != QUOTE:
            return None
    return _trade_from_exit(vehicle=vehicle, symbol=symbol, day=day, direction=direction,
                            range_high=high, range_low=low, signal=signal,
                            entry_bar=entry_bar, entry_reference=entry_ref,
                            exit_bar=last, exit_reference=exit_ref,
                            reason="force_flat", tie=False, gap=False, cfg=cfg,
                            multiplier=multiplier, stop_price=stop,
                            target_price=target, entry_source=entry_source,
                            exit_source=exit_source)


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
    quote_index = index_quotes(quotes)
    rows = _validate_bars(list(bars), cfg)
    if symbol is not None:
        rows = [b for b in rows if b.symbol == symbol]
    sessions: dict[tuple[str, date], list[UnderlyingBar]] = {}
    zone = ZoneInfo(cfg.timezone)
    for bar in rows:
        sessions.setdefault((bar.symbol, _local(bar.timestamp, zone).date()), []).append(bar)
    result = IBRResult(vehicle=vehicle)
    for (sym, _), session_bars in sorted(sessions.items(), key=lambda item: item[0]):
        trade = _replay_session(session_bars, vehicle=vehicle, symbol=sym, cfg=cfg,
                                option_snapshots=option_snapshots,
                                quotes=quote_index)
        if trade is not None:
            result.trades.append(trade)
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
    materialized_quotes = list(quotes or ())
    return {
        vehicle: replay_ibr(
            materialized, symbol=symbol, config=config, vehicle=vehicle,
            option_snapshots=option_snapshots, quotes=materialized_quotes)
        for vehicle in requested
    }


__all__ = [
    "IBRConfig", "IBRResult", "IBRTrade", "ReplayError", "replay_ibr",
    "replay_ibr_vehicles",
]
