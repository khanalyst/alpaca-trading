"""One executable-cost and fill model for every research lane.

Three lanes used to carry their own spread/slippage/fee numbers and their own
arithmetic, so a change in one silently disagreed with the others and none of
them agreed with the deployed runtime.  This module owns both: the expected
cost parameters and the formulas that spend them.  A lane may choose a
``CostModel``; it may not re-implement one.

Expected cost is not a rejection cap.  ``execution.max_slippage_bps`` is the
worst quoted slippage the runtime will *accept* before refusing to submit;
simulating at that number prices every fill as if it were the worst tolerable
one, and simulating without it prices every fill as if the cap did not exist.
The expected values below are the cost of a normal marketable fill in the
configured liquid US ETF universe; the caps are carried alongside only so a
model that expects a cost the runtime would reject fails closed here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import math
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .market_data import (record_is_available, replay_record_is_available)

# The shipped runtime schedule assumes a conservative quoted spread for the
# configured ETF universe, including its less liquid members, not only the
# tightest name.
DEFAULT_SPREAD_BPS = 4.0
# A marketable entry and a broker-resident stop leg both execute through the
# book at whatever is resting when they arrive; a triggered stop in a moving
# market pays materially more than half the quoted spread.  Keep this aligned
# with the shipped runtime schedule so direct replay/API callers are not more
# optimistic than configured runs.
DEFAULT_SLIPPAGE_BPS = 6.0
# Regulatory and exchange fees on notional, charged on both sides.
DEFAULT_FEE_BPS = 0.5
# Conservative listed-option broker/exchange fee floor per contract per side.
# Configuration may override this, but a default zero would systematically
# overstate option expectancy relative to equity.
DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE = 0.65
# Mirrors the checked `execution` block; these are caps, never expectations.
RUNTIME_MAX_SPREAD_BPS = 100.0
RUNTIME_MAX_SLIPPAGE_BPS = 50.0
# These are the preregistered all-in cost shocks used by the research gate.
# Runtime may select one of these scenarios, but must not invent a new stress
# level that has no corresponding research evidence.
COST_STRESS_SCENARIOS_BPS = (9.0, 15.0, 25.0, 50.0)
# The stress is deliberately a cost shock, not a multiplier on configured
# replay costs or risk.  Keep this machine-readable so runtime telemetry and
# fit reports cannot describe the same arithmetic in contradictory terms.
STRESSED_COST_SCHEMA = "stressed-entry-cost.v1"
STRESSED_COST_BASIS = {
    "notional": "entry_notional",
    "notional_charge": "scenario_bps_of_entry_notional",
    "option_fees": "round_trip_two_sides_per_contract",
}

CONFIG_BLOCK = "costs"


class CostError(ValueError):
    """Raised for a malformed or internally inconsistent cost model."""


# Stable machine-readable reasons shared by runtime and replay entry checks.
# Keep the human-facing runtime error separate: it retains the historical
# wording while telemetry/replay rows can be compared without parsing text.
ENTRY_SLIPPAGE_INVALID_REASON = "entry_slippage_invalid"
ENTRY_SLIPPAGE_REJECT_REASON = "entry_slippage_exceeds_limit"


def check_entry_slippage(
        side: Any, reference_price: Any, executable_quote: Any,
        max_slippage_bps: Any) -> tuple[dict[str, Any], str | None]:
    """Validate and evaluate one executable entry quote.

    The runtime and replay lanes must make the same adverse-price decision.
    This pure helper therefore owns both fail-closed input validation and the
    single adverse-basis-point calculation.  It never raises for malformed
    caller input; callers receive stable telemetry and a machine-readable
    reason instead.  ``buy`` entries are adverse above the reference and
    ``sell`` entries are adverse below it.  A non-adverse quote is accepted
    with zero slippage.
    """
    telemetry: dict[str, Any] = {
        "side": (side.strip().lower() if isinstance(side, str) else None),
        "reference_price": None,
        "executable_quote": None,
        "adverse_bps": None,
        "slippage_bps": None,
        "max_slippage_bps": None,
        "accepted": False,
        "reason": ENTRY_SLIPPAGE_INVALID_REASON,
    }

    normalized_side = telemetry["side"]
    if normalized_side not in {"buy", "sell"}:
        return telemetry, ENTRY_SLIPPAGE_INVALID_REASON

    def finite_number(value: Any, *, positive: bool = False) -> float | None:
        # Numeric strings and booleans are configuration/data mistakes.  A
        # Decimal or other numeric scalar is fine once converted to float.
        if isinstance(value, (bool, str, bytes, bytearray)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or (number <= 0 if positive else number < 0):
            return None
        return number

    reference = finite_number(reference_price, positive=True)
    executable = finite_number(executable_quote, positive=True)
    maximum = finite_number(max_slippage_bps)
    if reference is None or executable is None or maximum is None:
        return telemetry, ENTRY_SLIPPAGE_INVALID_REASON
    telemetry.update({
        "reference_price": reference,
        "executable_quote": executable,
        "max_slippage_bps": maximum,
    })

    # Compute the adverse basis points exactly once; every caller consumes this
    # value for both the rejection decision and its telemetry.
    adverse = (max(0.0, executable - reference)
               if normalized_side == "buy" else
               max(0.0, reference - executable))
    adverse_bps = adverse / reference * 10_000.0
    accepted = adverse_bps <= maximum
    reason = None if accepted else ENTRY_SLIPPAGE_REJECT_REASON
    telemetry.update({
        "adverse_bps": adverse_bps,
        "slippage_bps": adverse_bps,
        "accepted": accepted,
        "reason": reason,
    })
    return telemetry, reason


@dataclass(frozen=True)
class ReplayPolicy:
    """Point-in-time and portfolio limits shared by replay lanes.

    The runtime owns these values.  A research caller can pass the validated
    runtime config (or this value object) through the optional ``policy`` hook;
    omitted policy retains the historical fixture behaviour for compatibility.
    """

    max_market_data_age_seconds: float = 30.0
    options_min_dte: int = 7
    options_max_dte: int = 60
    options_max_spread_pct: float = 10.0
    risk_per_trade_pct: float = 0.5
    latest_entry_time: time | None = None
    force_flat_time: time | None = None
    max_concurrent_positions: int | None = None
    max_position_notional_pct: float | None = None
    max_gross_exposure_pct: float | None = None
    max_open_risk_pct: float | None = None
    daily_loss_limit_pct: float | None = None
    strict_market_data: bool = True
    # Direct low-level fixtures intentionally remain compatibility-default
    # false.  A validated shipped runtime config sets this true and therefore
    # cannot silently substitute a regular 16:00 close for an unknown broker
    # calendar day.
    require_exact_calendar: bool = False
    force_flat_minutes_before_close: int | None = None
    reject_new_entries_minutes_before_close: int | None = None
    # This flag is intentionally absent from runtime config.  It permits only
    # labelled historical-backfill records to use their provider ``as_of``
    # boundary for mechanics diagnostics; resulting rows are non-authorizing.
    allow_historical_backfill_diagnostics: bool = False
    # Runtime stressed-cost controls are optional on low-level replay
    # fixtures.  ``None``/``None`` deliberately disables this runtime-only
    # veto for historical callers; ``from_config`` carries the validated
    # production values into replay identity and account simulation.  They
    # are appended to preserve the positional constructor contract above.
    stressed_cost_scenario_bps: float | None = None
    max_stressed_cost_to_risk_ratio: float | None = None
    # Appended to preserve the positional constructor contract.  IEX is the
    # shipped equity authorization identity; legacy SIP envelopes select their
    # historical semantics explicitly during verification.
    equity_feed: str = "iex"
    # Empirical stress selection is an operator-activated overlay.  The
    # scalar scenario above remains the fail-closed fallback and shipped
    # default; an artifact can only narrow/widen it after held-out checks.
    stressed_cost_calibration_enabled: bool = False
    stressed_cost_calibration_path: str | None = None
    stressed_cost_calibration_artifact: Mapping[str, Any] | None = None
    equity_provider: str = "alpaca"

    def __post_init__(self) -> None:
        age = float(self.max_market_data_age_seconds)
        if not math.isfinite(age) or age < 0 or age > 30:
            raise CostError(
                "max_market_data_age_seconds must be finite and between 0 and 30 seconds")
        if int(self.options_min_dte) != self.options_min_dte or self.options_min_dte < 0:
            raise CostError("options_min_dte must be a non-negative integer")
        if int(self.options_max_dte) != self.options_max_dte or self.options_max_dte < self.options_min_dte:
            raise CostError("options_max_dte must be an integer >= options_min_dte")
        spread = float(self.options_max_spread_pct)
        if not math.isfinite(spread) or spread < 0:
            raise CostError("options_max_spread_pct must be finite and non-negative")
        for name in ("max_concurrent_positions",):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) != value or int(value) < 1):
                raise CostError(f"{name} must be a positive integer when supplied")
        for name in ("risk_per_trade_pct", "max_position_notional_pct", "max_gross_exposure_pct",
                     "max_open_risk_pct", "daily_loss_limit_pct"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise CostError(f"{name} must be finite and non-negative when supplied")
        scenario = self.stressed_cost_scenario_bps
        if scenario is not None:
            if (isinstance(scenario, bool) or
                    not math.isfinite(float(scenario)) or
                    float(scenario) not in COST_STRESS_SCENARIOS_BPS):
                raise CostError(
                    "stressed_cost_scenario_bps must be one of "
                    "9, 15, 25, or 50 when supplied")
        ratio = self.max_stressed_cost_to_risk_ratio
        if ratio is not None and (isinstance(ratio, bool) or
                                  not math.isfinite(float(ratio)) or
                                  float(ratio) < 0):
            raise CostError(
                "max_stressed_cost_to_risk_ratio must be finite and non-negative "
                "when supplied")
        feed = str(self.equity_feed or "").strip().lower().replace("-", "_")
        if feed == "delayed":
            feed = "delayed_sip"
        if feed not in {"iex", "sip", "delayed_sip"}:
            raise CostError(
                "equity_feed must be iex, sip, or delayed_sip")
        object.__setattr__(self, "equity_feed", feed)
        if not isinstance(self.stressed_cost_calibration_enabled, bool):
            raise CostError("stressed_cost_calibration_enabled must be true or false")
        if self.stressed_cost_calibration_path is not None and not isinstance(
                self.stressed_cost_calibration_path, str):
            raise CostError("stressed_cost_calibration_path must be a string")
        provider = str(self.equity_provider or "").strip().lower()
        if not provider:
            raise CostError("equity_provider must be non-empty")
        object.__setattr__(self, "equity_provider", provider)
        if not isinstance(self.strict_market_data, bool):
            raise CostError("strict_market_data must be true or false")
        if not isinstance(self.require_exact_calendar, bool):
            raise CostError("require_exact_calendar must be true or false")
        if not isinstance(self.allow_historical_backfill_diagnostics, bool):
            raise CostError(
                "allow_historical_backfill_diagnostics must be true or false")
        for name in ("force_flat_minutes_before_close",
                     "reject_new_entries_minutes_before_close"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or
                                      int(value) != value or int(value) < 0):
                raise CostError(f"{name} must be a non-negative integer when supplied")

    def as_dict(self) -> dict[str, Any]:
        return {
            "equity_feed": self.equity_feed,
            "max_market_data_age_seconds": float(self.max_market_data_age_seconds),
            "options_min_dte": int(self.options_min_dte),
            "options_max_dte": int(self.options_max_dte),
            "options_max_spread_pct": float(self.options_max_spread_pct),
            "risk_per_trade_pct": float(self.risk_per_trade_pct),
            "stressed_cost_scenario_bps": (
                None if self.stressed_cost_scenario_bps is None else
                float(self.stressed_cost_scenario_bps)),
            "max_stressed_cost_to_risk_ratio": (
                None if self.max_stressed_cost_to_risk_ratio is None else
                float(self.max_stressed_cost_to_risk_ratio)),
            "stressed_cost_calibration_enabled": bool(
                self.stressed_cost_calibration_enabled),
            "stressed_cost_calibration_path": self.stressed_cost_calibration_path,
            "stressed_cost_calibration_content_hash": (
                self.stressed_cost_calibration_artifact.get("content_hash")
                if isinstance(self.stressed_cost_calibration_artifact, Mapping)
                else None),
            "equity_provider": self.equity_provider,
            "latest_entry_time": (None if self.latest_entry_time is None else
                                   self.latest_entry_time.isoformat()),
            "force_flat_time": (None if self.force_flat_time is None else
                                 self.force_flat_time.isoformat()),
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_position_notional_pct": self.max_position_notional_pct,
            "max_gross_exposure_pct": self.max_gross_exposure_pct,
            "max_open_risk_pct": self.max_open_risk_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "strict_market_data": self.strict_market_data,
            "require_exact_calendar": self.require_exact_calendar,
            "force_flat_minutes_before_close": self.force_flat_minutes_before_close,
            "reject_new_entries_minutes_before_close": self.reject_new_entries_minutes_before_close,
            "allow_historical_backfill_diagnostics": (
                self.allow_historical_backfill_diagnostics),
        }

    @classmethod
    def from_config(cls, config: Mapping | None) -> "ReplayPolicy":
        """Read limits from the same validated runtime config blocks."""
        source = dict(config or {})
        execution = source.get("execution") or {}
        risk = source.get("risk") or {}
        strategy = source.get("strategy") or {}
        session = source.get("session") or {}
        broker = source.get("broker") or {}
        data = source.get("data") or {}
        if not all(isinstance(block, Mapping) for block in (
                execution, risk, strategy, session, broker, data)):
            raise CostError("runtime policy blocks must be mappings")
        calibration_enabled = risk.get("stressed_cost_calibration_enabled", False)
        if not isinstance(calibration_enabled, bool):
            raise CostError("risk.stressed_cost_calibration_enabled must be true or false")
        calibration_path = risk.get("stressed_cost_calibration_path")
        if calibration_path is not None and not isinstance(calibration_path, str):
            raise CostError("risk.stressed_cost_calibration_path must be a string")
        artifact = None
        if calibration_enabled:
            from .stressed_cost_calibration import load_stress_calibration_artifact
            artifact, _ = load_stress_calibration_artifact(calibration_path)
        provider = (broker.get("provider") if "provider" in broker else
                    data.get("provider") if "provider" in data else "alpaca")
        equity_feed = (broker.get("data_feed") if "data_feed" in broker else
                       data.get("feed") if "feed" in data else "iex")
        latest = strategy.get("latest_entry_time")
        if latest is not None and not isinstance(latest, time):
            try:
                latest = time.fromisoformat(str(latest))
            except ValueError as exc:
                raise CostError("strategy.latest_entry_time must be HH:MM") from exc
        force = strategy.get("force_flat_time")
        if force is None:
            minutes = session.get("force_flat_minutes_before_close")
            # The runtime session close is 16:00 ET; callers that provide only
            # the minute offset get the same force-flat wall clock.
            if minutes is not None:
                try:
                    force = (datetime.combine(date.today(), time(16, 0)) -
                             timedelta(minutes=int(minutes))).time()
                except (TypeError, ValueError):
                    raise CostError("session.force_flat_minutes_before_close must be an integer")
        elif not isinstance(force, time):
            try:
                force = time.fromisoformat(str(force))
            except ValueError as exc:
                raise CostError("force_flat_time must be HH:MM") from exc
        require_exact = session.get("require_exact_calendar", False)
        if not isinstance(require_exact, bool):
            raise CostError("session.require_exact_calendar must be true or false")
        flat_offset = session.get("force_flat_minutes_before_close", 10)
        reject_offset = session.get("reject_new_entries_minutes_before_close", 5)
        for name, value in (("session.force_flat_minutes_before_close", flat_offset),
                            ("session.reject_new_entries_minutes_before_close", reject_offset)):
            if value is not None and (isinstance(value, bool) or
                                      not isinstance(value, int) or value < 0):
                raise CostError(f"{name} must be a non-negative integer")
        return cls(
            equity_feed=str(equity_feed),
            max_market_data_age_seconds=float(execution.get("max_market_data_age_seconds", 30.0)),
            options_min_dte=int(risk.get("options_min_dte", 7)),
            options_max_dte=int(risk.get("options_max_dte", 60)),
            options_max_spread_pct=float(risk.get("options_max_spread_pct", 10.0)),
            risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 0.5)),
            stressed_cost_scenario_bps=(
                None if risk.get("stressed_cost_scenario_bps") is None else
                float(risk["stressed_cost_scenario_bps"])),
            max_stressed_cost_to_risk_ratio=(
                None if risk.get("max_stressed_cost_to_risk_ratio") is None else
                float(risk["max_stressed_cost_to_risk_ratio"])),
            stressed_cost_calibration_enabled=calibration_enabled,
            stressed_cost_calibration_path=calibration_path,
            stressed_cost_calibration_artifact=artifact,
            equity_provider=str(provider),
            latest_entry_time=latest,
            force_flat_time=force,
            max_concurrent_positions=(None if risk.get("max_concurrent_positions") is None else int(risk["max_concurrent_positions"])),
            max_position_notional_pct=(None if risk.get("max_position_notional_pct") is None else float(risk["max_position_notional_pct"])),
            max_gross_exposure_pct=(None if risk.get("max_gross_exposure_pct") is None else float(risk["max_gross_exposure_pct"])),
            max_open_risk_pct=(None if risk.get("max_total_open_risk_pct",
                                                risk.get("max_open_risk_pct")) is None else
                               float(risk.get("max_total_open_risk_pct",
                                              risk.get("max_open_risk_pct")))),
            daily_loss_limit_pct=(None if risk.get("daily_loss_limit_pct") is None else float(risk["daily_loss_limit_pct"])),
            # Every other policy field is read here; omitting this one pinned it
            # at the strict default with no way to change it from configuration.
            # A bars-only corpus (a backfill without ``--quotes``) then prices
            # nothing at all, which is a data-shape mismatch rather than a
            # research result -- see ``fill_source_summary``.
            strict_market_data=_strict_market_data(execution),
            require_exact_calendar=require_exact,
            force_flat_minutes_before_close=flat_offset,
            reject_new_entries_minutes_before_close=reject_offset,
        )

    def resolve_stress_scenario(self, symbol: str | None = None,
                                timestamp: Any = None,
                                *, bucket: str | None = None,
                                vehicle: str = "equity") -> tuple[float | None, str | None]:
        """Resolve an empirical stress cell with the configured scalar fallback."""
        if self.stressed_cost_scenario_bps is None and not self.stressed_cost_calibration_enabled:
            return None, "stressed_cost_scenario_missing"
        fallback = (25.0 if self.stressed_cost_scenario_bps is None else
                    float(self.stressed_cost_scenario_bps))
        normalized_vehicle = str(vehicle or "equity").strip().lower()
        if normalized_vehicle in {"option", "options",
                                  "defined_risk_options",
                                  "options_defined_risk"}:
            return (float(fallback),
                    "calibration_equity_only"
                    if self.stressed_cost_calibration_enabled
                    else "activation_disabled")
        observation_session = None
        if timestamp not in (None, ""):
            try:
                text = str(timestamp).replace("Z", "+00:00")
                stamp = datetime.fromisoformat(text)
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                from .quote_costs import bucket_label
                local = stamp.astimezone(ZoneInfo("America/New_York"))
                observation_session = local.date().isoformat()
                if bucket is None:
                    minutes = ((local.hour * 60 + local.minute + local.second / 60.0)
                               - 9 * 60 - 30)
                    bucket = bucket_label(minutes)
            except (TypeError, ValueError, OverflowError):
                bucket = None
                observation_session = "__invalid__"
        from .stressed_cost_calibration import resolve_stress_scenario
        return resolve_stress_scenario(
            self.stressed_cost_calibration_artifact,
            symbol=symbol, bucket=bucket, fallback_scenario_bps=fallback,
            operator_enabled=self.stressed_cost_calibration_enabled,
            expected_provider=self.equity_provider,
            expected_feed=self.equity_feed,
            observation_session=observation_session)


def replay_policy_for_session(
        policy: ReplayPolicy, *, session_open: datetime | None = None,
        session_close: datetime | None = None,
        session_date: date | None = None) -> ReplayPolicy:
    """Derive close-relative cutoffs for one broker calendar session.

    Exact metadata is mandatory only for policies created from the shipped
    production configuration.  Compatibility fixtures can omit it and retain
    their existing static cutoffs.  When metadata is present, both force-flat
    and latest-entry boundaries are derived from that close (with the checked
    strategy latest-entry cap still applied).
    """
    if not isinstance(policy, ReplayPolicy):
        raise CostError("policy must be a ReplayPolicy")
    if (session_open is None) != (session_close is None):
        raise CostError("exact_session_calendar_missing")
    if session_open is None:
        if policy.require_exact_calendar:
            raise CostError("exact_session_calendar_missing")
        return policy
    if (session_open.tzinfo is None or session_open.utcoffset() is None or
            session_close is None or session_close.tzinfo is None or
            session_close.utcoffset() is None):
        raise CostError("exact_session_calendar_malformed")
    zone = ZoneInfo("America/New_York")
    opened = session_open.astimezone(zone)
    closed = session_close.astimezone(zone)
    if opened.date() != closed.date() or opened >= closed:
        raise CostError("exact_session_calendar_conflict")
    if session_date is not None and opened.date() != session_date:
        raise CostError("exact_session_calendar_conflict")
    flat_minutes = (10 if policy.force_flat_minutes_before_close is None
                    else int(policy.force_flat_minutes_before_close))
    reject_minutes = (0 if policy.reject_new_entries_minutes_before_close is None
                      else int(policy.reject_new_entries_minutes_before_close))
    force = (closed - timedelta(minutes=flat_minutes)).time().replace(tzinfo=None)
    latest = (closed - timedelta(minutes=reject_minutes)).time().replace(tzinfo=None)
    if policy.latest_entry_time is not None:
        latest = min(latest, policy.latest_entry_time)
    return replace(policy, force_flat_time=force, latest_entry_time=latest)


def replay_policy_for_bars(policy: ReplayPolicy, bars: Sequence[Any], *,
                           session_date: date | None = None) -> ReplayPolicy:
    """Resolve one policy from bars carrying exact session metadata."""
    if not bars:
        return replay_policy_for_session(policy, session_date=session_date)
    metadata = {(getattr(bar, "session_open", None),
                 getattr(bar, "session_close", None)) for bar in bars}
    has_missing = any(opened is None or closed is None
                      for opened, closed in metadata)
    if has_missing and len(metadata) > 1:
        raise CostError("exact_session_calendar_conflict")
    if has_missing:
        return replay_policy_for_session(policy, session_date=session_date)
    if len(metadata) != 1:
        raise CostError("exact_session_calendar_conflict")
    opened, closed = next(iter(metadata))
    return replay_policy_for_session(policy, session_open=opened,
                                     session_close=closed,
                                     session_date=session_date)


# Descriptive alias used by callers that prefer the derivation terminology.
derive_session_replay_policy = replay_policy_for_session


def replay_policy_for_mode(policy: ReplayPolicy, mode: str, *,
                           backtest_bar_fallback: bool = False) -> ReplayPolicy:
    """Derive the policy allowed for one offline research lane.

    Backtests may explicitly replay historical bars without executable quotes;
    shadow replay is always point-in-time strict because it is the evidence
    that gates live-shadow authorization.  Direct replay callers retain their
    existing policy and do not pass through this helper.
    """
    if not isinstance(policy, ReplayPolicy):
        raise CostError("policy must be a ReplayPolicy")
    lane = str(mode).strip().lower()
    if lane not in {"backtest", "shadow"}:
        raise CostError("mode must be backtest or shadow")
    if not isinstance(backtest_bar_fallback, bool):
        raise CostError("backtest_bar_fallback must be true or false")
    strict = lane == "shadow" or not (lane == "backtest" and backtest_bar_fallback)
    return replace(
        policy, strict_market_data=strict,
        allow_historical_backfill_diagnostics=False,
    )


def diagnostic_backfill_policy(
        policy: ReplayPolicy | None = None) -> ReplayPolicy:
    """Return the explicit bars-capable, non-authorizing backfill policy."""
    base = ReplayPolicy() if policy is None else policy
    if not isinstance(base, ReplayPolicy):
        raise CostError("policy must be a ReplayPolicy")
    return replace(
        base, strict_market_data=False,
        allow_historical_backfill_diagnostics=True,
    )


def _strict_market_data(execution: Mapping) -> bool:
    """Resolve ``execution.strict_market_data``, defaulting to strict.

    Strict replay refuses to price a fill it has no recorded quote for, which
    is right when a quote *should* exist and did not.  A corpus that never
    carried quotes at all is a different situation: nothing can price, and the
    honest response is to say so rather than to report an edgeless run.
    """
    value = execution.get("strict_market_data", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise CostError("execution.strict_market_data must be true or false")


def _bps(value: Any, name: str) -> float:
    # Booleans are not numbers and a numeric string is not a measurement; both
    # are configuration mistakes that must surface rather than be coerced.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CostError(f"{name} must be finite and non-negative")
    return number


@dataclass(frozen=True)
class CostModel:
    """Expected per-fill cost, validated against the runtime's rejection caps."""

    spread_bps: float = DEFAULT_SPREAD_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    fee_bps: float = DEFAULT_FEE_BPS
    max_spread_bps: float = RUNTIME_MAX_SPREAD_BPS
    max_slippage_bps: float = RUNTIME_MAX_SLIPPAGE_BPS
    # Optional listed-option fee charged once per contract per side.  Kept
    # after the original fields so positional CostModel(...) callers remain
    # backward-compatible.
    option_fee_per_contract_side: float = DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE
    # Alias accepted for broker schedules that call the per-side amount simply
    # ``option_fee_per_contract``.
    option_fee_per_contract: float | None = None
    provenance: str = "default"

    def __post_init__(self) -> None:
        for name in ("spread_bps", "slippage_bps", "fee_bps",
                     "option_fee_per_contract_side",
                     "max_spread_bps", "max_slippage_bps"):
            object.__setattr__(self, name, _bps(getattr(self, name), name))
        if self.option_fee_per_contract is not None:
            object.__setattr__(self, "option_fee_per_contract",
                               _bps(self.option_fee_per_contract,
                                    "option_fee_per_contract"))
            object.__setattr__(self, "option_fee_per_contract_side",
                               self.option_fee_per_contract)
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise CostError("provenance must be a non-empty string")
        if self.spread_bps > self.max_spread_bps:
            raise CostError(
                f"expected spread {self.spread_bps} bps exceeds the runtime's "
                f"{self.max_spread_bps} bps rejection cap")
        # The runtime measures slippage against its own reference price and
        # refuses to submit past the cap.  A research model that expects more
        # than that is simulating fills the runtime would never take.
        if self.entry_cost_bps > self.max_slippage_bps:
            raise CostError(
                f"expected entry cost {self.entry_cost_bps} bps exceeds the "
                f"runtime's {self.max_slippage_bps} bps slippage cap")

    @property
    def entry_cost_bps(self) -> float:
        """Half the quoted spread plus adverse slippage, in basis points."""
        return self.spread_bps / 2.0 + self.slippage_bps

    def per_side_bps(self, *, executable_quote: bool = False) -> float:
        """Cost of one execution; an executable quote already includes spread."""
        return self.slippage_bps if executable_quote else self.entry_cost_bps

    def execution_price(self, reference: float, direction: str, *, entry: bool,
                        executable_quote: bool = False) -> float:
        """Move an execution reference adversely by one side's cost."""
        # Long buys at the ask and sells at the bid; short mirrors it.  An
        # option ``ask``/``bid`` or an equity quote is already executable, so
        # charging a modelled half-spread on top would bill the spread twice.
        sign = 1.0 if ((direction == "long") == entry) else -1.0
        rate = self.per_side_bps(executable_quote=executable_quote) / 10_000.0
        return float(reference) * (1.0 + sign * rate)

    def fees(self, entry_price: float, exit_price: float, quantity: float,
             multiplier: float = 1.0, *, vehicle: str = "equity") -> float:
        """Both-side notional fees plus optional per-contract option fees."""
        notional = (abs(float(entry_price)) + abs(float(exit_price))) * \
            float(quantity) * float(multiplier)
        total = notional * self.fee_bps / 10_000.0
        if vehicle == "option":
            total += float(quantity) * 2.0 * self.option_fee_per_contract_side
        return total

    def round_trip_cost(self, entry_price: float, exit_price: float,
                        quantity: float = 1.0, multiplier: float = 1.0,
                        *, vehicle: str = "equity",
                        executable_quotes: bool = False) -> float:
        """Return the configured expected cost for both execution legs.

        This is intentionally separate from :meth:`fees`: fees are only one
        component of the round trip and a risk-unit check must account for the
        same spread/slippage assumption that priced the replay.  A quoted
        execution already contains its spread, so callers that have an
        executable bid/ask may opt into the slippage-only leg cost.
        """
        entry = float(entry_price)
        exit = float(exit_price)
        qty = float(quantity)
        mult = float(multiplier)
        if not all(math.isfinite(value) for value in (entry, exit, qty, mult)):
            raise CostError("round-trip cost inputs must be finite")
        if qty < 0 or mult <= 0 or entry < 0 or exit < 0:
            raise CostError("round-trip cost inputs must be non-negative")
        bps = self.per_side_bps(executable_quote=bool(executable_quotes)) / 10_000.0
        notional = (abs(entry) + abs(exit)) * qty * mult
        return notional * bps + self.fees(entry, exit, qty, mult, vehicle=vehicle)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CostModel":
        """Rebuild a model persisted inside a risk-unit report."""
        if not isinstance(value, Mapping):
            raise CostError("cost model must be a mapping")
        return cls(
            spread_bps=value.get("spread_bps", DEFAULT_SPREAD_BPS),
            slippage_bps=value.get("slippage_bps", DEFAULT_SLIPPAGE_BPS),
            fee_bps=value.get("fee_bps", DEFAULT_FEE_BPS),
            option_fee_per_contract_side=value.get(
                "option_fee_per_contract_side",
                value.get("option_fee_per_contract", DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE)),
            max_spread_bps=value.get("max_spread_bps", RUNTIME_MAX_SPREAD_BPS),
            max_slippage_bps=value.get("max_slippage_bps", RUNTIME_MAX_SLIPPAGE_BPS),
            provenance=str(value.get("provenance", "report")),
        )

    def as_dict(self) -> dict:
        return {"spread_bps": self.spread_bps, "slippage_bps": self.slippage_bps,
                "fee_bps": self.fee_bps,
                "option_fee_per_contract_side": self.option_fee_per_contract_side,
                "option_fee_per_contract": self.option_fee_per_contract_side,
                "entry_cost_bps": self.entry_cost_bps,
                "max_spread_bps": self.max_spread_bps,
                "max_slippage_bps": self.max_slippage_bps,
                "provenance": self.provenance}

    @classmethod
    def from_config(cls, config: Mapping | None, *, vehicle: str | None = None) -> CostModel:
        """Build from the single ``costs`` block, capped by ``execution``.

        The caps are read from the same ``execution`` block the trader
        validates, so tightening the runtime's tolerance is immediately a
        research constraint rather than a number somebody remembers to copy.

        ``vehicle`` selects an optional nested schedule under
        ``costs.vehicles``.  A selected schedule is an override, not a second
        schema: omitted fields inherit the normalized flat schedule (including
        its provenance) and the same execution rejection caps always apply.
        Leaving ``vehicle`` unset retains the historical flat-config behavior.
        """
        if vehicle is not None and vehicle not in {"equity", "option"}:
            raise CostError("vehicle must be equity or option")
        source = dict(config or {})
        block = source.get(CONFIG_BLOCK) or {}
        if not isinstance(block, Mapping):
            raise CostError(f"{CONFIG_BLOCK} must be a mapping")
        cost_fields = {"spread_bps", "slippage_bps", "fee_bps",
                       "option_fee_per_contract_side",
                       "option_fee_per_contract", "provenance"}
        unknown = sorted(set(block) - cost_fields - {"vehicles"}, key=str)
        if unknown:
            raise CostError(f"{CONFIG_BLOCK} has unknown field(s): {', '.join(unknown)}")
        execution = source.get("execution") or {}
        if not isinstance(execution, Mapping):
            raise CostError("execution must be a mapping")
        selected = dict(block)
        vehicles = block.get("vehicles")
        if "vehicles" in block:
            if not isinstance(vehicles, Mapping):
                raise CostError(f"{CONFIG_BLOCK}.vehicles must be a mapping")
            unknown_vehicles = sorted(
                set(vehicles) - {"equity", "option"}, key=str)
            if unknown_vehicles:
                raise CostError(
                    f"{CONFIG_BLOCK}.vehicles has unknown vehicle(s): "
                    f"{', '.join(unknown_vehicles)}")
            for name, override in vehicles.items():
                if not isinstance(override, Mapping):
                    raise CostError(
                        f"{CONFIG_BLOCK}.vehicles.{name} must be a mapping")
                override_unknown = sorted(set(override) - cost_fields, key=str)
                if override_unknown:
                    raise CostError(
                        f"{CONFIG_BLOCK}.vehicles.{name} has unknown field(s): "
                        f"{', '.join(override_unknown)}")
                # Validate every declared schedule, including one not selected
                # by this call, so malformed configuration cannot hide behind
                # the flat/default resolver path.
                inherited = dict(block)
                inherited.pop("vehicles", None)
                inherited.update(dict(override))
                try:
                    cls.from_config({CONFIG_BLOCK: inherited,
                                     "execution": execution})
                except CostError as exc:
                    raise CostError(
                        f"{CONFIG_BLOCK}.vehicles.{name}: {exc}") from exc
            if vehicle is not None and vehicle in vehicles:
                selected.update(dict(vehicles[vehicle]))
        selected.pop("vehicles", None)
        return cls(
            spread_bps=selected.get("spread_bps", DEFAULT_SPREAD_BPS),
            slippage_bps=selected.get("slippage_bps", DEFAULT_SLIPPAGE_BPS),
            fee_bps=selected.get("fee_bps", DEFAULT_FEE_BPS),
            option_fee_per_contract_side=selected.get(
                "option_fee_per_contract_side", DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE),
            option_fee_per_contract=selected.get("option_fee_per_contract"),
            max_spread_bps=execution.get("max_spread_bps", RUNTIME_MAX_SPREAD_BPS),
            max_slippage_bps=execution.get("max_slippage_bps", RUNTIME_MAX_SLIPPAGE_BPS),
            provenance=str(selected.get(
                "provenance", "default" if not block else "config")),
        )


BAR = "bar"
QUOTE = "quote"
# Evidence labels are intentionally distinct: historical backfill identifies
# source provenance, while bar fallback identifies a forward source priced
# from bars under an explicitly non-strict policy.  Both are diagnostic-only,
# but callers must not conflate the two in telemetry or source validation.
DIAGNOSTIC_HISTORICAL_BACKFILL = "diagnostic_historical_backfill"
DIAGNOSTIC_BAR_FALLBACK = "diagnostic_bar_fallback"
DIAGNOSTIC_EVIDENCE_MODES = frozenset({
    DIAGNOSTIC_HISTORICAL_BACKFILL, DIAGNOSTIC_BAR_FALLBACK,
})
# A broker-resident stop/target can be observed as touched by a completed
# exact-feed bar, but it has no point-in-time executable quote at the trigger.
# Keep this source distinct from both a diagnostic bar fallback and a quote so
# gates can apply the ordinary adverse cost model and audit the claim.
RESTING_BRACKET = "resting_bracket"
RESTING_BRACKET_FILL_SCHEMA = "resting-bracket-fill.v1"
# Readable compatibility alias for callers that describe the field as a
# source schema rather than a fill schema.
RESTING_BRACKET_SCHEMA = RESTING_BRACKET_FILL_SCHEMA


@dataclass(frozen=True)
class QuoteFill:
    """The executable quote selected at a fill boundary.

    ``quote_fill`` intentionally retains its historical numeric return type;
    replay lanes that need to persist evidence use this richer record so feed
    and provider identity cannot disappear between normalization and gating.
    """

    price: float
    timestamp: datetime
    as_of: datetime
    provider: str
    feed: str
    # Historical rows are only visible through an explicit diagnostic policy.
    # Keep a default so older positional consumers remain source-compatible.
    source_mode: str = "forward_observed"


def resting_bracket_fill_claim(*, exit_reason: Any, exit_reference: Any,
                               stop_price: Any, target_price: Any,
                               bar_timestamp: Any = None,
                               bar_feed: Any = None,
                               bar_provider: Any = None,
                               tie_broken: Any = False) -> dict[str, Any]:
    """Build the canonical claim for a non-gap resting bracket leg.

    The constructor is deliberately strict: replay code must not emit an
    apparently auditable claim for a malformed level or provenance record.
    Authorization performs the same checks plus entry/evidence validation via
    :func:`validate_resting_bracket_fill`.
    """
    reason = str(exit_reason or "").strip().lower()
    if reason not in {"stop", "target"}:
        raise CostError("resting bracket exit_reason must be stop or target")
    if isinstance(tie_broken, bool) is False:
        raise CostError("resting bracket tie_broken must be boolean")

    def finite(value: Any, name: str, *, positive: bool = False) -> float:
        if isinstance(value, bool):
            raise CostError(f"resting bracket {name} must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CostError(f"resting bracket {name} must be numeric") from exc
        if not math.isfinite(number) or (positive and number <= 0):
            qualifier = "finite and positive" if positive else "finite"
            raise CostError(f"resting bracket {name} must be {qualifier}")
        return number

    reference = finite(exit_reference, "exit_reference", positive=True)
    stop = finite(stop_price, "stop_price", positive=True)
    target = finite(target_price, "target_price", positive=True)
    level = stop if reason == "stop" else target
    if not math.isclose(reference, level, rel_tol=0.0, abs_tol=1e-9):
        raise CostError("resting bracket exit_reference must equal planned level")
    feed = str(bar_feed or "").strip().lower().replace("-", "_")
    provider = str(bar_provider or "").strip()
    if not feed or not provider:
        raise CostError("resting bracket bar feed and provider are required")
    timestamp = None if bar_timestamp is None else str(bar_timestamp).strip()
    if not timestamp:
        raise CostError("resting bracket bar timestamp is required")
    return {
        "schema": RESTING_BRACKET_FILL_SCHEMA,
        "source": RESTING_BRACKET,
        "trigger": "intrabar",
        "exit_reason": reason,
        "planned_level": float(level),
        "exit_reference": float(reference),
        "stop_price": float(stop),
        "target_price": float(target),
        "gap": False,
        "tie_broken": bool(tie_broken),
        "bar_timestamp": timestamp,
        "bar_feed": feed,
        "bar_provider": provider,
    }


def validate_resting_bracket_fill(row: Mapping[str, Any], *,
                                  equity_feed: str = "iex") -> str | None:
    """Return a stable rejection reason for a resting-bracket row.

    This is the shared fail-closed predicate used by gates and replay/control
    callers.  It intentionally requires fresh quote-backed entry evidence,
    exact-feed bar provenance on signal/entry/exit bars, and a complete claim;
    callers must separately apply the cost model with ``executable_quotes``
    false for this source.
    """
    if not isinstance(row, Mapping):
        return "resting_bracket_malformed_claim"
    if str(row.get("exit_fill_source") or "").strip().lower() != RESTING_BRACKET:
        return "resting_bracket_source_mismatch"
    if str(row.get("exit_fill_schema") or "").strip() != RESTING_BRACKET_FILL_SCHEMA:
        return "resting_bracket_schema_mismatch"
    claim = row.get("exit_fill_claim")
    if not isinstance(claim, Mapping):
        return "resting_bracket_missing_claim"
    if str(claim.get("schema") or "").strip() != RESTING_BRACKET_FILL_SCHEMA:
        return "resting_bracket_schema_mismatch"
    if str(claim.get("source") or "").strip().lower() != RESTING_BRACKET:
        return "resting_bracket_claim_source_mismatch"
    if str(claim.get("trigger") or "").strip().lower() != "intrabar":
        return "resting_bracket_trigger_mismatch"
    reason = str(row.get("exit_reason") or "").strip().lower()
    if reason not in {"stop", "target"}:
        return "resting_bracket_non_level_exit"
    if str(claim.get("exit_reason") or "").strip().lower() != reason:
        return "resting_bracket_reason_mismatch"
    if row.get("gap_fill") is True or row.get("entry_gap_fill") is True or \
            row.get("exit_gap_fill") is True or claim.get("gap") is not False:
        return "resting_bracket_gap_claim"
    row_tie = row.get("tie_broken")
    claim_tie = claim.get("tie_broken")
    if not isinstance(row_tie, bool) or not isinstance(claim_tie, bool):
        return "resting_bracket_tie_malformed"
    if row_tie != claim_tie:
        return "resting_bracket_tie_mismatch"
    if reason == "target" and row_tie:
        return "resting_bracket_target_tie"

    def finite(value: Any, *, positive: bool = False) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return (number if math.isfinite(number) and
                (not positive or number > 0) else None)

    reference = finite(row.get("exit_reference"), positive=True)
    authored_stop = finite(row.get("stop_price"), positive=True)
    # Rule/factory replays may amend a broker-resident stop (breakeven or a
    # later trailing leg).  Prefer the persisted active leg for the claim while
    # retaining the authored stop for sizing/risk reports.
    stop = finite(row.get("active_stop_price",
                        row.get("exit_active_stop_price", row.get("stop_price"))),
                  positive=True)
    target = finite(row.get("target_price"), positive=True)
    level = stop if reason == "stop" else target
    planned = finite(claim.get("planned_level"), positive=True)
    claimed_reference = finite(claim.get("exit_reference"), positive=True)
    claimed_stop = finite(claim.get("stop_price"), positive=True)
    claimed_target = finite(claim.get("target_price"), positive=True)
    if (reference is None or authored_stop is None or stop is None or
            target is None or level is None or claimed_stop is None or
            claimed_target is None or
            planned is None or claimed_reference is None or
            not math.isclose(reference, level, rel_tol=0.0, abs_tol=1e-9) or
            not math.isclose(planned, level, rel_tol=0.0, abs_tol=1e-9) or
            not math.isclose(claimed_reference, reference,
                             rel_tol=0.0, abs_tol=1e-9) or
            not math.isclose(claimed_stop, stop, rel_tol=0.0, abs_tol=1e-9) or
            not math.isclose(claimed_target, target,
                             rel_tol=0.0, abs_tol=1e-9)):
        return "resting_bracket_level_mismatch"
    if str(row.get("entry_fill_source") or "").strip().lower() != QUOTE:
        return "resting_bracket_entry_not_quote"
    age = row.get("entry_quote_age_seconds")
    if (isinstance(age, bool) or not isinstance(age, (int, float)) or
            not math.isfinite(float(age)) or not 0.0 <= float(age) <= 30.0):
        return "resting_bracket_entry_quote_stale"
    feed = str(equity_feed or "").strip().lower().replace("-", "_")
    if feed == "delayed":
        feed = "delayed_sip"
    if feed not in {"iex", "sip"}:
        return "resting_bracket_non_authorizing_feed"
    entry_feed = str(row.get("entry_feed") or "").strip().lower().replace("-", "_")
    entry_provider = str(row.get("entry_provider") or "").strip()
    if entry_feed != feed or not entry_provider:
        return "resting_bracket_entry_provenance"
    evidence_mode = str(row.get("evidence_mode") or "forward_observed").strip().lower()
    if evidence_mode in DIAGNOSTIC_EVIDENCE_MODES:
        return evidence_mode
    # Every bar participating in the claim must carry explicit exact-feed
    # identity.  Missing metadata is not upgraded from the quote identity.
    for leg in ("signal_bar", "entry_bar", "exit_bar"):
        bar_feed = str(row.get(f"{leg}_feed") or "").strip().lower().replace("-", "_")
        bar_provider = str(row.get(f"{leg}_provider") or "").strip()
        if bar_feed != feed or not bar_provider:
            return "resting_bracket_bar_provenance"
    claim_feed = str(claim.get("bar_feed") or "").strip().lower().replace("-", "_")
    claim_provider = str(claim.get("bar_provider") or "").strip()
    if claim_feed != feed or not claim_provider:
        return "resting_bracket_claim_provenance"
    if claim_feed != str(row.get("exit_bar_feed") or "").strip().lower().replace("-", "_") or \
            claim_provider != str(row.get("exit_bar_provider") or "").strip():
        return "resting_bracket_claim_provenance"
    expected_bar_timestamp = row.get("exit_fill_bar_timestamp")
    claim_bar_timestamp = str(claim.get("bar_timestamp") or "").strip()
    if (not expected_bar_timestamp or not claim_bar_timestamp or
            claim_bar_timestamp != str(expected_bar_timestamp).strip()):
        return "resting_bracket_claim_timestamp"
    return None


def _quote_fill_from_record(quote: Any, *, side: str) -> QuoteFill | None:
    identity = getattr(quote, "identity", None)
    if identity is None:
        return None
    try:
        price = float(quote.ask if side == "buy" else quote.bid)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    try:
        timestamp = quote.timestamp
        as_of = identity.as_of
        provider = str(identity.provider).strip()
        feed = str(identity.feed).strip()
        source_mode = str(getattr(identity, "source_mode", "forward_observed") or
                          "forward_observed").strip().lower()
    except (AttributeError, TypeError, ValueError):
        return None
    if not provider or not feed:
        return None
    return QuoteFill(price, timestamp, as_of, provider, feed, source_mode)


def cost_model_for_vehicle(costs: Any, vehicle: str) -> CostModel:
    """Resolve a shared model or a vehicle-keyed model mapping."""
    if vehicle not in {"equity", "option"}:
        raise CostError("vehicle must be equity or option")
    if costs is None:
        return CostModel()
    if isinstance(costs, CostModel):
        return costs
    if isinstance(costs, Mapping):
        # A runtime config or a normalized costs block carries its schedule
        # under ``costs``.  Resolve it through the canonical parser so nested
        # overrides inherit the flat values and execution caps.
        if CONFIG_BLOCK in costs or "execution" in costs:
            return CostModel.from_config(costs, vehicle=vehicle)
        if "vehicles" in costs:
            return CostModel.from_config({CONFIG_BLOCK: costs}, vehicle=vehicle)
        selected = costs.get(vehicle, costs.get("default", costs))
        if isinstance(selected, CostModel):
            return selected
        if isinstance(selected, Mapping):
            return CostModel.from_dict(selected)
    raise CostError("costs must be a CostModel or vehicle-keyed mapping")


def stressed_cost_usd(planned_notional: float | None = None,
                      scenario_bps: float | None = None, *,
                      entry_notional: float | None = None,
                      vehicle: str, quantity: float = 1.0,
                      costs: Any = None, config: Mapping | None = None) -> float:
    """Return the deterministic all-in stressed entry cost for a plan.

    This deliberately matches :func:`research.gates.cost_stress_report`:
    entry notional is charged at the selected scenario in basis points, and
    listed options additionally pay two per-contract fees for the entry and
    exit sides.  ``planned_notional`` remains a compatibility name for the
    explicit ``entry_notional`` basis.  ``costs``/``config`` are resolved
    through the vehicle-aware :class:`CostModel` parser so nested runtime
    schedules cannot accidentally use the equity fee for an option plan.
    """
    if vehicle not in {"equity", "option"}:
        raise CostError("vehicle must be equity or option")
    if entry_notional is not None:
        if planned_notional is not None:
            raise CostError(
                "provide entry_notional or planned_notional, not both")
        planned_notional = entry_notional
    try:
        notional = float(planned_notional)
        scenario = float(scenario_bps)
        qty = float(quantity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CostError("stressed cost inputs must be numeric") from exc
    if (not math.isfinite(notional) or not math.isfinite(scenario) or
            not math.isfinite(qty)):
        raise CostError("stressed cost inputs must be finite")
    if notional < 0 or scenario < 0 or qty < 0:
        raise CostError("stressed cost inputs must be non-negative")
    model = cost_model_for_vehicle(
        costs if costs is not None else config, vehicle)
    stressed = abs(notional) * scenario / 10_000.0
    if vehicle == "option":
        stressed += abs(qty) * 2.0 * model.option_fee_per_contract_side
    if not math.isfinite(stressed):
        raise CostError("stressed cost is not finite")
    return float(stressed)


def stressed_cost_ratio_exceeds(ratio: float, limit: float) -> bool:
    """Compare the stress ratio without rejecting floating-point equality."""

    tolerance = max(1e-12, abs(float(limit)) * 1e-12)
    return float(ratio) - float(limit) > tolerance


def check_stressed_cost_plan(
        plan: Mapping[str, Any], *, scenario_bps: float | None,
        max_ratio: float | None, costs: Any = None,
        config: Mapping | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Apply the runtime stressed-cost veto to a sized plan.

    This is intentionally a pure, data-only seam so research can exercise the
    exact same arithmetic and failure reasons as ``RiskEngine`` without
    importing the runtime risk module (which would create a dependency cycle).
    A missing *either* control is malformed when the veto is requested; callers
    that retain legacy fixture behaviour should skip this helper only when both
    controls are ``None``.
    """
    vehicle = ("option" if isinstance(plan, Mapping) and
               str(plan.get("execution_profile", "shares")).lower()
               in {"option", "options", "defined_risk_options",
                   "options_defined_risk"} else "equity")
    activation_reason = "activation_disabled"
    if isinstance(config, Mapping):
        try:
            policy = ReplayPolicy.from_config(config)
            if policy.stressed_cost_calibration_enabled:
                scenario_bps, activation_reason = policy.resolve_stress_scenario(
                    plan.get("symbol") if isinstance(plan, Mapping) else None,
                    plan.get("entry_timestamp", plan.get("timestamp"))
                    if isinstance(plan, Mapping) else None,
                    vehicle=vehicle)
        except (CostError, TypeError, ValueError, OverflowError):
            return None, "stressed_cost_invalid"
    if scenario_bps is None or max_ratio is None:
        return None, "stressed_cost_invalid"
    if isinstance(scenario_bps, bool) or isinstance(max_ratio, bool):
        return None, "stressed_cost_invalid"
    try:
        scenario = float(scenario_bps)
        limit = float(max_ratio)
    except (TypeError, ValueError, OverflowError):
        return None, "stressed_cost_invalid"
    if (not math.isfinite(scenario) or scenario not in COST_STRESS_SCENARIOS_BPS or
            not math.isfinite(limit) or limit < 0):
        return None, "stressed_cost_invalid"
    if not isinstance(plan, Mapping):
        return None, "stressed_cost_invalid"
    if any(isinstance(plan.get(name), bool) for name in
           ("notional", "risk_usd",
            "contracts" if vehicle == "option" else "shares")):
        return None, "stressed_cost_invalid"
    try:
        notional = float(plan.get("notional"))
        risk_usd = float(plan.get("risk_usd"))
        quantity = float(plan.get("contracts" if vehicle == "option" else "shares"))
    except (TypeError, ValueError, OverflowError):
        return None, "stressed_cost_invalid"
    if (not math.isfinite(notional) or notional <= 0 or
            not math.isfinite(risk_usd) or risk_usd <= 0 or
            not math.isfinite(quantity) or quantity <= 0):
        return None, "stressed_cost_invalid"
    try:
        stressed = stressed_cost_usd(
            entry_notional=notional, scenario_bps=scenario,
            vehicle=vehicle, quantity=quantity, costs=costs, config=config)
        ratio = stressed / risk_usd
    except (CostError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None, "stressed_cost_invalid"
    if not math.isfinite(stressed) or not math.isfinite(ratio):
        return None, "stressed_cost_invalid"
    if stressed_cost_ratio_exceeds(ratio, limit):
        return None, "stressed_cost_risk_limit"
    enriched = dict(plan)
    enriched.update({
        "vehicle": vehicle,
        "stressed_cost_vehicle": vehicle,
        "stressed_cost_schema": STRESSED_COST_SCHEMA,
        "stressed_cost_basis": dict(STRESSED_COST_BASIS),
        "stressed_cost_entry_notional": float(notional),
        "stressed_cost_scenario_bps": float(scenario),
        "stressed_cost_usd": float(stressed),
        "stressed_cost_to_risk_ratio": float(ratio),
        "max_stressed_cost_to_risk_ratio": float(limit),
        "stressed_cost_activation_reason": activation_reason,
    })
    return enriched, None


# Concise aliases make the helper convenient for callers while retaining one
# implementation and one arithmetic contract.
stress_cost_usd = stressed_cost_usd
stressed_cost = stressed_cost_usd


# Kept as a private alias for callers that imported the pre-existing helper.
_cost_model_for_vehicle = cost_model_for_vehicle


def risk_unit_report(rows: Iterable[Mapping], *, vehicle: str,
                     costs: Any = None, config: Mapping | None = None,
                     min_cost_coverage: float = 1.0,
                     equity_feed: str = "iex") -> dict[str, Any]:
    """Recompute whether each executed row has a cost-covered risk unit.

    A risk unit is the monetary loss to the authored stop (``risk_usd`` when
    the replay recorded it, otherwise stop distance times quantity and
    multiplier).  The configured round-trip cost is charged independently for
    equity and options, including the option per-contract fee.  The complete
    row observations and model are persisted so a gate can recompute this
    report instead of trusting a caller-supplied boolean.
    """
    if not isinstance(vehicle, str) or vehicle not in {"equity", "option"}:
        raise CostError("vehicle must be equity or option")
    equity_feed = str(equity_feed or "").strip().lower().replace("-", "_")
    if equity_feed == "delayed":
        equity_feed = "delayed_sip"
    if equity_feed not in {"iex", "sip", "delayed_sip"}:
        raise CostError("equity_feed must be iex, sip, or delayed_sip")
    if config is not None and costs is None:
        costs = CostModel.from_config(config, vehicle=vehicle)
    model = cost_model_for_vehicle(costs, vehicle)
    coverage = float(min_cost_coverage)
    if not math.isfinite(coverage) or coverage < 0:
        raise CostError("min_cost_coverage must be finite and non-negative")
    local = [dict(row) for row in rows
             if isinstance(row, Mapping) and row.get("vehicle", vehicle) == vehicle]
    executed = [row for row in local if row.get("no_trade") is not True]
    observations: list[dict[str, Any]] = []
    failures: list[str] = []
    failure_reasons: dict[str, str] = {}
    total_risk = 0.0
    total_cost = 0.0
    for index, row in enumerate(executed):
        def number(*names: str, default: float | None = None) -> float | None:
            for name in names:
                raw = row.get(name)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(value):
                    return value
            return default

        qty = number("quantity", "contracts", default=1.0)
        mult = number("contract_multiplier", "multiplier", default=100.0 if vehicle == "option" else 1.0)
        entry = number("entry_price", "entry_reference", "plan_entry")
        exit = number("exit_price", "exit_reference", default=entry)
        stop_distance = number("stop_distance", "risk_per_unit")
        risk = number("risk_usd", "realized_risk_usd", "risk_unit_usd", "risk_unit")
        if risk is None and stop_distance is not None and qty is not None and mult is not None:
            risk = abs(stop_distance) * abs(qty) * abs(mult if vehicle == "option" and stop_distance else 1.0)
        cost = None
        if entry is not None and exit is not None and qty is not None and mult is not None:
            try:
                cost = model.round_trip_cost(
                    entry, exit, qty, mult, vehicle=vehicle,
                    executable_quotes=(row.get("entry_fill_source") == QUOTE and
                                       row.get("exit_fill_source") == QUOTE))
            except CostError:
                cost = None
        # Equity proof is bound to the report's explicit feed identity.  New
        # authorizing research passes IEX; SIP remains available only for
        # faithful verification of pre-binding historical envelopes.
        equity_provenance = True
        provenance_reason: str | None = None
        if vehicle == "equity":
            def _equity_leg(leg: str) -> bool:
                source = str(row.get(f"{leg}_fill_source") or "").strip().lower()
                feed = str(row.get(f"{leg}_feed") or "").strip().lower()
                provider = str(row.get(f"{leg}_provider") or "").strip()
                return source == QUOTE and feed == equity_feed and bool(provider)
            resting_exit = (str(row.get("exit_fill_source") or "").strip().lower()
                            == RESTING_BRACKET)
            if resting_exit:
                resting_reason = validate_resting_bracket_fill(
                    row, equity_feed=equity_feed)
                equity_provenance = resting_reason is None
                provenance_reason = resting_reason
            else:
                equity_provenance = _equity_leg("entry") and _equity_leg("exit")
            if not equity_provenance and provenance_reason is None:
                provenance_reason = (
                    f"equity legs require {equity_feed.upper()} quote provenance")
        elif vehicle == "option":
            # Option evidence authorizes only an executable OPRA quote on both
            # legs, observed no more than one recorder cycle (30 seconds) ago.
            # Missing, stale, indicative, or bar-derived evidence remains
            # useful diagnostically but cannot make the risk-unit report pass.
            for leg in ("entry", "exit"):
                source = str(row.get(f"{leg}_fill_source") or "").strip().lower()
                if source != QUOTE:
                    provenance_reason = (
                        f"option {leg} leg is not an executable quote fill")
                    break
                feed = str(row.get(
                    f"{leg}_feed", row.get(f"{leg}_option_feed")) or "").strip().lower()
                if feed != "opra":
                    provenance_reason = (
                        f"option {leg} leg requires OPRA quote provenance")
                    break
                provider = str(row.get(f"{leg}_provider") or "").strip()
                if not provider:
                    provenance_reason = (
                        f"option {leg} leg requires a non-empty quote provider")
                    break
                raw_age = row.get(
                    f"{leg}_quote_age_seconds",
                    row.get(f"{leg}_option_quote_age_seconds"))
                try:
                    age = float(raw_age)
                except (TypeError, ValueError, OverflowError):
                    age = float("nan")
                if not math.isfinite(age) or age < 0 or age > 30.0:
                    provenance_reason = (
                        f"option {leg} quote age must be finite and <= 30 seconds")
                    break
            equity_provenance = provenance_reason is None
        adequate = bool(
            risk is not None and cost is not None and risk > 0 and
            cost >= 0 and risk >= cost * coverage and equity_provenance)
        if not adequate:
            opportunity_id = str(row.get("opportunity_id", index))
            failures.append(opportunity_id)
            if provenance_reason is not None:
                failure_reasons[opportunity_id] = provenance_reason
            elif risk is None:
                failure_reasons[opportunity_id] = "missing or invalid risk unit"
            elif cost is None:
                failure_reasons[opportunity_id] = "missing or invalid round-trip cost"
            elif risk < cost * coverage:
                failure_reasons[opportunity_id] = "risk unit does not cover configured cost"
            else:
                failure_reasons[opportunity_id] = "risk unit is not positive"
        if risk is not None and math.isfinite(risk):
            total_risk += max(0.0, risk)
        if cost is not None and math.isfinite(cost):
            total_cost += max(0.0, cost)
        observations.append({
            "opportunity_id": str(row.get("opportunity_id", index)),
            "risk_usd": risk, "round_trip_cost": cost,
            "cost_coverage": (risk / cost if cost and risk is not None else None),
            "adequate": adequate,
            "failure_reason": (None if adequate else failure_reasons.get(
                str(row.get("opportunity_id", index)))),
        })
    # An empty set cannot authorize anything.  It remains a valid diagnostic
    # report so old evidence is readable and explicitly underpowered.
    adequate = bool(executed and observations and not failures)
    return {
        "schema": "risk-unit-report.v1",
        "equity_feed": equity_feed,
        "vehicle": vehicle,
        "minimum_cost_coverage": coverage,
        "cost_model": model.as_dict(),
        "rows": len(executed),
        "adequate_rows": sum(1 for item in observations if item["adequate"]),
        "total_risk_usd": total_risk,
        "total_round_trip_cost": total_cost,
        "mean_risk_usd": (total_risk / len(executed) if executed else None),
        "mean_round_trip_cost": (total_cost / len(executed) if executed else None),
        # Stable aliases make the economics legible to report consumers while
        # retaining the explicit aggregate names above.
        "risk_unit_usd": (total_risk / len(executed) if executed else None),
        "round_trip_cost_usd": (total_cost / len(executed) if executed else None),
        "cost_to_risk_ratio": (total_cost / total_risk if total_risk > 0 else None),
        "failed_opportunities": failures,
        "failure_reasons": failure_reasons,
        "adequacy_reason": (None if adequate else (
            "all executed rows satisfy cost coverage and provenance"
            if executed and not failures else
            (next(iter(failure_reasons.values()), "no executed rows")))),
        "observations": observations,
        "adequate": adequate,
    }


@dataclass(frozen=True)
class SQLiteQuoteIndexDescriptor:
    """Serializable, read-only handle for a finalized quote index.

    The descriptor intentionally carries the symbol-id map and summary
    metadata in addition to the SQLite path.  A worker can therefore resolve
    quotes without rebuilding an index or depending on mutable parent state.
    The owning :class:`SQLiteQuoteIndex` remains responsible for the temporary
    directory lifetime until all children have closed their handles.
    """

    path: str
    symbols: tuple[tuple[str, int], ...]
    count: int
    max_session_date: str | None
    # Version 1 indexes predate local observation metadata and version 3 adds
    # the source-mode label.  The reader inspects the SQLite schema so older
    # descriptors remain loadable without a destructive migration.
    schema_version: int = 3


class SQLiteQuoteIndex:
    """Disk-backed quote resolver for large recorded corpora.

    A research cycle only asks for the latest visible quote at a fill boundary;
    retaining millions of normalized quote objects in a Python list is both
    wasteful and the source of the backtest OOM.  This index stores the small
    set of fields needed by :func:`quote_fill` in a temporary SQLite database
    and keeps only the symbol-id map and a write batch in memory.
    """

    _BATCH_SIZE = 10_000
    _SCHEMA_VERSION = 3

    def __init__(self, directory: str | Path | None = None):
        import sqlite3

        self._temporary: tempfile.TemporaryDirectory | None = None
        if directory is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="alpaca-quotes-")
            root = Path(self._temporary.name)
        else:
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
        self.path = root / "quotes.sqlite3"
        self._db = sqlite3.connect(str(self.path), timeout=30)
        self._read_only = False
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute("PRAGMA temp_store=FILE")
        existing = {str(row[1]) for row in self._db.execute(
            "PRAGMA table_info(quotes)")}
        if existing:
            stored_rows = int(self._db.execute(
                "SELECT COUNT(*) FROM quotes").fetchone()[0])
            if stored_rows:
                # The symbol-id map is deliberately carried by the finalized
                # descriptor rather than duplicated in SQLite.  Reopening a
                # populated index for writing therefore cannot reconstruct
                # which symbol each integer names.  Starting from an empty
                # in-memory map would silently hide old rows and may reuse an
                # existing id for a different symbol, corrupting quote
                # resolution.  Existing indexes must be consumed through
                # ``open_read_only(descriptor)``; fail before migrating or
                # mutating their schema.
                self._db.close()
                raise CostError(
                    "a populated quote index cannot be reopened for writing; "
                    "use open_read_only with its descriptor")
            # A caller may reuse a finalized index directory.  Migrate only
            # an empty shell.  v1/v2 data-bearing indexes must be opened with
            # their descriptor so their symbol map remains authoritative.
            if "observed_at" not in existing:
                self._db.execute(
                    "ALTER TABLE quotes ADD COLUMN observed_at REAL NOT NULL DEFAULT 0")
                self._db.execute("UPDATE quotes SET observed_at=as_of")
                existing.add("observed_at")
            if "source_mode" not in existing:
                self._db.execute(
                    "ALTER TABLE quotes ADD COLUMN source_mode TEXT NOT NULL "
                    "DEFAULT 'forward_observed'")
                existing.add("source_mode")
            self._db.commit()
        else:
            self._db.execute("""
                CREATE TABLE quotes (
                    symbol_id INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    as_of REAL NOT NULL,
                    observed_at REAL NOT NULL,
                    bid REAL NOT NULL,
                    ask REAL NOT NULL,
                    provider TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    source_mode TEXT NOT NULL DEFAULT 'forward_observed',
                    session_day INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    PRIMARY KEY (symbol_id, timestamp, sequence)
                ) WITHOUT ROWID
            """)
        self._has_observed_at = True
        self._has_source_mode = True
        self._symbols: dict[str, int] = {}
        self._pending: list[tuple[int, float, float, float, float, float, str, str, str, int, int]] = []
        self._sequence = 0
        self._count = 0
        self._max_session_day: int | None = None
        self._closed = False

    @classmethod
    def open_read_only(cls, descriptor: SQLiteQuoteIndexDescriptor):
        """Open a finalized index from a serializable descriptor.

        The returned object owns only a read-only SQLite connection.  Closing
        it never removes the descriptor's file or its parent temporary
        directory.
        """
        import sqlite3

        if not isinstance(descriptor, SQLiteQuoteIndexDescriptor):
            raise TypeError("descriptor must be a SQLiteQuoteIndexDescriptor")
        path = Path(descriptor.path)
        if not path.is_file():
            raise FileNotFoundError(path)
        obj = cls.__new__(cls)
        obj._temporary = None
        obj.path = path
        obj._db = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        obj._read_only = True
        # Version 1 quote indexes have no local observation column.  Detect
        # the actual table shape rather than trusting the descriptor alone so
        # descriptors emitted before schema versioning remain auditable.
        columns = {str(row[1]) for row in obj._db.execute(
            "PRAGMA table_info(quotes)")}
        if not columns:
            obj._db.close()
            raise CostError("quote index is missing its quotes table")
        obj._has_observed_at = (
            "observed_at" in columns and
            int(getattr(descriptor, "schema_version", cls._SCHEMA_VERSION)) >= 2)
        obj._has_source_mode = (
            "source_mode" in columns and
            int(getattr(descriptor, "schema_version", cls._SCHEMA_VERSION)) >= 3)
        obj._symbols = dict(descriptor.symbols)
        obj._pending = []
        obj._sequence = 0
        obj._count = int(descriptor.count)
        max_session = descriptor.max_session_date
        obj._max_session_day = (None if max_session is None
                                else date.fromisoformat(max_session).toordinal())
        obj._closed = False
        return obj


    def descriptor(self) -> SQLiteQuoteIndexDescriptor:
        """Return a serializable descriptor for this finalized index."""
        if self._closed:
            raise RuntimeError("quote index is closed")
        self.finalize()
        return SQLiteQuoteIndexDescriptor(
            path=str(self.path),
            symbols=tuple(sorted(self._symbols.items())),
            count=self._count,
            max_session_date=(None if self.max_session_date is None
                              else self.max_session_date.isoformat()),
            schema_version=(self._SCHEMA_VERSION if self._has_source_mode else
                            (2 if self._has_observed_at else 1)),
        )

    def add(self, quote: Any) -> None:
        if self._closed:
            raise RuntimeError("quote index is closed")
        if self._read_only:
            raise RuntimeError("read-only quote index cannot write")
        symbol = str(quote.symbol).upper()
        symbol_id = self._symbols.get(symbol)
        if symbol_id is None:
            symbol_id = len(self._symbols) + 1
            self._symbols[symbol] = symbol_id
        timestamp = float(quote.timestamp.timestamp())
        as_of = float(quote.identity.as_of.timestamp())
        observed_at = float(quote.identity.observed_at.timestamp())
        provider = str(quote.identity.provider).strip()
        feed = str(quote.identity.feed).strip()
        source_mode = str(getattr(quote.identity, "source_mode", "forward_observed") or
                          "forward_observed").strip().lower()
        if not provider or not feed:
            raise CostError("quote provider and feed are required")
        if source_mode not in {"forward_observed", "historical_backfill"}:
            raise CostError("quote source_mode is unsupported")
        session_day = int(quote.session_date.toordinal())
        self._pending.append((
            symbol_id, timestamp, as_of, observed_at, float(quote.bid), float(quote.ask),
            provider, feed, source_mode, session_day, self._sequence,
        ))
        self._sequence += 1
        self._count += 1
        if self._max_session_day is None or session_day > self._max_session_day:
            self._max_session_day = session_day
        if len(self._pending) >= self._BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        if self._read_only:
            raise RuntimeError("read-only quote index cannot write")
        columns = ("symbol_id, timestamp, as_of, observed_at, bid, ask, "
                   "provider, feed, source_mode, session_day, sequence")
        self._db.executemany(
            f"INSERT INTO quotes ({columns}) VALUES "
            f"({','.join('?' for _ in range(11))})", self._pending)
        self._db.commit()
        self._pending.clear()

    def finalize(self) -> "SQLiteQuoteIndex":
        if not self._closed and not self._read_only:
            self._flush()
        return self

    @property
    def count(self) -> int:
        return self._count

    @property
    def max_session_date(self) -> date | None:
        return (date.fromordinal(self._max_session_day)
                if self._max_session_day is not None else None)

    def source_mode_counts(self) -> dict[str, int]:
        """Return provenance counts for every quote retained by the index."""
        if self._closed:
            raise RuntimeError("quote index is closed")
        if not self._read_only:
            self._flush()
        source_column = ("source_mode" if self._has_source_mode else
                         "'forward_observed'")
        rows = self._db.execute(
            f"SELECT {source_column} AS source_mode, COUNT(*) "
            "FROM quotes GROUP BY source_mode ORDER BY source_mode"
        )
        return {
            str(mode or "forward_observed").strip().lower(): int(count)
            for mode, count in rows
        }

    def quote_fill_record(self, *, symbol: str, at: datetime, side: str,
                   max_age_seconds: float | None = 30.0,
                   session_date: date | None = None,
                   allow_historical_backfill_diagnostics: bool = False) -> QuoteFill | None:
        """Resolve the latest-visible quote, retaining its provenance."""
        if self._closed or self._count == 0:
            return None
        if not self._read_only:
            self._flush()
        symbol_id = self._symbols.get(str(symbol).upper())
        if symbol_id is None:
            return None
        at_ts = float(at.timestamp())
        limit = 30.0 if max_age_seconds is None else float(max_age_seconds)
        session_day = (None if session_date is None else
                       int(session_date.toordinal()))
        observed_column = "observed_at" if self._has_observed_at else "as_of"
        source_column = "source_mode" if self._has_source_mode else "'forward_observed'"
        cursor = self._db.execute(
            f"""SELECT timestamp, as_of, {observed_column} AS observed_at,
                        bid, ask, provider, feed, {source_column} AS source_mode,
                        session_day
                 FROM quotes
                WHERE symbol_id=? AND timestamp<=?
                ORDER BY timestamp DESC, sequence DESC""",
            (symbol_id, at_ts),
        )
        for timestamp, as_of, observed_at, bid, ask, provider, feed, source_mode, row_session_day in cursor:
            age = at_ts - float(timestamp)
            if age > limit:
                # Rows are newest first, so all remaining rows are stale.
                break
            if session_day is not None and int(row_session_day) != session_day:
                continue
            mode = str(source_mode or "forward_observed").strip().lower()
            if mode == "historical_backfill" and allow_historical_backfill_diagnostics:
                visible_at = max(float(timestamp), float(as_of))
            else:
                visible_at = max(float(timestamp), float(as_of), float(observed_at))
            if visible_at > at_ts:
                continue
            price = float(ask if side == "buy" else bid)
            if not math.isfinite(price) or price <= 0:
                return None
            return QuoteFill(
                price=price,
                timestamp=datetime.fromtimestamp(float(timestamp), timezone.utc),
                as_of=datetime.fromtimestamp(float(as_of), timezone.utc),
                provider=str(provider), feed=str(feed), source_mode=mode,
            )
        return None

    def quote_fill(self, *, symbol: str, at: datetime, side: str,
                   max_age_seconds: float | None = 30.0,
                   session_date: date | None = None,
                   allow_historical_backfill_diagnostics: bool = False) -> float | None:
        record = self.quote_fill_record(symbol=symbol, at=at, side=side,
                                        max_age_seconds=max_age_seconds,
                                        session_date=session_date,
                                        allow_historical_backfill_diagnostics=(
                                            allow_historical_backfill_diagnostics))
        return None if record is None else record.price

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self._read_only:
                self._flush()
            self._db.close()
        finally:
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def __bool__(self) -> bool:
        return self._count > 0 and not self._closed

    def __del__(self):  # pragma: no cover - interpreter shutdown cleanup
        try:
            self.close()
        except Exception:
            pass


def index_quotes(quotes: Iterable[Any] | None) -> dict[str, list] | SQLiteQuoteIndex:
    """Group quote snapshots by symbol in chronological order."""
    if quotes is not None and callable(getattr(quotes, "quote_fill", None)):
        return quotes
    grouped: dict[str, list] = {}
    for quote in quotes or ():
        grouped.setdefault(str(quote.symbol).upper(), []).append(quote)
    for rows in grouped.values():
        rows.sort(key=lambda item: item.timestamp)
    return grouped


def quote_fill(indexed: Mapping[str, Sequence[Any]] | SQLiteQuoteIndex | None, *, symbol: str,
               at: datetime, side: str, max_age_seconds: float | None = 30.0,
               session_date: date | None = None,
               allow_historical_backfill_diagnostics: bool = False) -> float | None:
    """Return the executable side of the last quote visible at a fill instant.

    ``None`` means no quote was recorded for that instant; the caller must
    fall back to the bar and say so rather than inventing a price.
    """
    record = quote_fill_record(indexed, symbol=symbol, at=at, side=side,
                               max_age_seconds=max_age_seconds,
                               session_date=session_date,
                               allow_historical_backfill_diagnostics=(
                                   allow_historical_backfill_diagnostics))
    return None if record is None else record.price


def quote_fill_record(indexed: Mapping[str, Sequence[Any]] | SQLiteQuoteIndex | None,
                      *, symbol: str, at: datetime, side: str,
                      max_age_seconds: float | None = 30.0,
                      session_date: date | None = None,
                      allow_historical_backfill_diagnostics: bool = False) -> QuoteFill | None:
    """Return the latest visible quote and preserve feed/provider identity."""
    if indexed is None:
        return None
    resolver = getattr(indexed, "quote_fill_record", None)
    if callable(resolver):
        return resolver(symbol=symbol, at=at, side=side,
                        max_age_seconds=max_age_seconds,
                        session_date=session_date,
                        allow_historical_backfill_diagnostics=(
                            allow_historical_backfill_diagnostics))
    if not indexed:
        return None
    rows = indexed.get(str(symbol).upper())
    if not rows:
        return None
    best = None
    for quote in rows:
        if quote.timestamp > at:
            break
        identity = getattr(quote, "identity", None)
        if identity is None or not replay_record_is_available(
                quote, at,
                allow_historical_backfill_diagnostics=(
                    allow_historical_backfill_diagnostics)):
            continue
        if session_date is not None and getattr(quote, "session_date", None) != session_date:
            continue
        best = quote
    if best is None:
        return None
    age = (at - best.timestamp).total_seconds()
    limit = 30.0 if max_age_seconds is None else float(max_age_seconds)
    if age < 0 or age > limit:
        return None
    return _quote_fill_from_record(best, side=side)


__all__ = [
    "BAR", "CONFIG_BLOCK", "CostError", "CostModel",
    "ENTRY_SLIPPAGE_INVALID_REASON", "ENTRY_SLIPPAGE_REJECT_REASON",
    "check_entry_slippage", "DEFAULT_FEE_BPS",
    "DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE", "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_SPREAD_BPS", "QUOTE", "RESTING_BRACKET",
    "DIAGNOSTIC_HISTORICAL_BACKFILL", "DIAGNOSTIC_BAR_FALLBACK",
    "DIAGNOSTIC_EVIDENCE_MODES",
    "RESTING_BRACKET_FILL_SCHEMA", "RESTING_BRACKET_SCHEMA",
    "RUNTIME_MAX_SLIPPAGE_BPS", "RUNTIME_MAX_SPREAD_BPS",
    "COST_STRESS_SCENARIOS_BPS", "STRESSED_COST_SCHEMA", "STRESSED_COST_BASIS",
    "ReplayPolicy", "diagnostic_backfill_policy",
    "replay_policy_for_mode", "replay_policy_for_session",
    "replay_policy_for_bars", "derive_session_replay_policy",
    "cost_model_for_vehicle", "stressed_cost_usd", "stress_cost_usd",
    "stressed_cost_ratio_exceeds",
    "check_stressed_cost_plan",
    "stressed_cost", "risk_unit_report",
    "QuoteFill", "SQLiteQuoteIndex", "SQLiteQuoteIndexDescriptor", "index_quotes",
    "quote_fill", "quote_fill_record", "resting_bracket_fill_claim",
    "validate_resting_bracket_fill",
]
