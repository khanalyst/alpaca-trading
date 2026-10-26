"""Deterministic golden harness for every registered rule arm.

The harness replays a fixed synthetic market through the shared rule
evaluator and exit transition, one completed-bar prefix at a time, for every
arm that is already registered (the 24-arm diagnostic shadow cohort, each
mechanism cohort and the two sealed preregistered hypotheses).  Its output is
reduced to one digest per arm.  ``tests/research/test_rule_golden_equivalence``
pins the digests computed on the code that predates the signal-contract fixes,
so any change to what an existing arm emits or how it exits fails there.

Run as a module to print the fingerprint that the test pins::

    python -B -m tests.research.rule_golden_harness
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
import random
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RuleSpecError, causal_maturity_bars, completed_bar_exit_transition,
    evaluate_rule_signal, exit_deadline, feature_window_bars,
    initialize_exit_state, rule_semantic_signature, rule_spec_hash,
    rule_variant_id, thesis_exit_deadline, validate_rule_spec,
)


NY = ZoneInfo("America/New_York")
SESSION_DATES = (
    date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6),
    date(2026, 8, 7), date(2026, 8, 10), date(2026, 8, 11),
)
# The last session is evaluated; the five before it supply the prior
# same-clock volume samples that the v6 relative-volume mode requires.
EVALUATED_SESSIONS = SESSION_DATES[-1:]
PRIOR_CONTEXT_SESSIONS = 5
SESSION_BARS = 390
FORCE_FLAT = time(15, 55)
# symbol: (opening price, idiosyncratic one-minute sigma in bps, market beta,
# base one-minute volume)
SYMBOLS: dict[str, tuple[float, float, float, float]] = {
    "SPY": (520.0, 1.0, 1.0, 9_000.0),
    "QQQ": (440.0, 2.2, 1.2, 6_000.0),
    "XLU": (72.0, 2.6, 0.4, 2_500.0),
    "SMH": (250.0, 5.0, 1.6, 3_000.0),
}
# One XLU minute is withheld in the final session so the gap paths of every
# family (opening coverage, session prefixes, contiguous windows) are covered.
WITHHELD = {("XLU", SESSION_DATES[-1], 200)}


def _volume_shape(minute: int) -> float:
    """A U-shaped intraday participation profile."""
    centre = (minute - SESSION_BARS / 2) / (SESSION_BARS / 2)
    return 0.55 + 1.6 * centre * centre + (1.2 if minute < 10 else 0.0)


# Per-session opening drift in bps per minute over the first 25 minutes.
OPENING_DRIFT = (1.4, -0.6, 1.6, 1.2, -0.9, 1.8, 2.2)
JUMP_MINUTE = 140
# A compressed stretch immediately before the jump, so compression and
# breakout predicates have something to find.
QUIET_MINUTES = range(JUMP_MINUTE - 20, JUMP_MINUTE)


def _market_returns(day_index: int) -> list[float]:
    """Market-factor one-minute log returns in bps for one session."""
    rng = random.Random(10_007 * (day_index + 1))
    direction = 1.0 if day_index % 2 == 0 else -1.0
    returns = []
    for minute in range(SESSION_BARS):
        drift = 0.0
        sigma = 2.4
        if minute < 25:
            drift = direction * OPENING_DRIFT[day_index]
        elif minute < 60:
            drift = -direction * 0.5
        elif minute < JUMP_MINUTE:
            sigma = 0.8 if day_index % 3 != 1 else 2.0
            if minute in QUIET_MINUTES:
                sigma = 0.3
        elif minute == JUMP_MINUTE:
            drift = direction * (18.0 if day_index % 4 else -14.0)
        elif minute < 230:
            drift = direction * 0.6
        returns.append(drift + rng.gauss(0.0, sigma))
    return returns


def synthetic_market() -> dict[str, list[dict[str, Any]]]:
    """Return deterministic completed one-minute bars for every symbol."""
    market = [_market_returns(index) for index in range(len(SESSION_DATES))]
    result: dict[str, list[dict[str, Any]]] = {}
    for symbol_index, (symbol, (price, sigma, beta, base_volume)) in enumerate(
            SYMBOLS.items()):
        rng = random.Random(7_919 * (symbol_index + 3))
        close = price
        rows: list[dict[str, Any]] = []
        for day_index, day in enumerate(SESSION_DATES):
            opened_at = datetime.combine(day, time(9, 30), tzinfo=NY)
            close *= math.exp(rng.uniform(-35.0, 35.0) / 10_000.0)
            pending_reversal = 0.0
            anchor = close
            for minute in range(SESSION_BARS):
                opened = close
                quiet = 0.2 if minute in QUIET_MINUTES else 1.0
                shock = 0.0
                if rng.random() < 1.0 / 45.0 and quiet == 1.0:
                    shock = rng.choice((-1.0, 1.0)) * rng.uniform(12.0, 28.0)
                    pending_reversal -= shock * 0.6
                reversal = pending_reversal * 0.15
                pending_reversal -= reversal
                move = (beta * market[day_index][minute] +
                        rng.gauss(0.0, sigma * quiet) + shock + reversal)
                if minute == QUIET_MINUTES.start:
                    anchor = opened
                if minute in QUIET_MINUTES:
                    # Pin the close near the stretch's first open.
                    move = (-0.8 * math.log(opened / anchor) * 10_000.0 +
                            0.2 * move)
                close = opened * math.exp(move / 10_000.0)
                wick = max(sigma, 1.0) * 0.6 * quiet / 10_000.0
                high = max(opened, close) * (1.0 + abs(rng.gauss(0.0, wick)))
                low = min(opened, close) * (1.0 - abs(rng.gauss(0.0, wick)))
                if minute in QUIET_MINUTES:
                    high = max(opened, close) * 1.00006
                    low = min(opened, close) * 0.99994
                if rng.random() < 1.0 / 40.0 and quiet == 1.0:
                    # A rejected excursion: the wick extends, the close does not.
                    if rng.random() < 0.5:
                        high *= 1.0 + rng.uniform(10.0, 30.0) / 10_000.0
                    else:
                        low *= 1.0 - rng.uniform(10.0, 30.0) / 10_000.0
                volume = (base_volume * _volume_shape(minute) *
                          math.exp(rng.gauss(0.0, 0.35)) *
                          (3.0 if shock else 1.0) *
                          (3.0 if minute == JUMP_MINUTE else 1.0))
                if (symbol, day, minute) in WITHHELD:
                    continue
                stamp = (opened_at + timedelta(minutes=minute)).astimezone(
                    timezone.utc)
                rows.append({
                    "timestamp": stamp, "symbol": symbol, "feed": "iex",
                    "open": round(opened, 4), "high": round(high, 4),
                    "low": round(low, 4), "close": round(close, 4),
                    "volume": float(int(volume) + 1),
                })
                close = round(close, 4)
        result[symbol] = rows
    return result


def _session_rows(rows: Iterable[Mapping[str, Any]], day: date) -> list[Mapping[str, Any]]:
    return [row for row in rows if row["timestamp"].astimezone(NY).date() == day]


def registered_arms() -> list[tuple[str, dict[str, Any]]]:
    """Every rule arm registered before the signal-contract fixes.

    Variant ids repeat across cohorts (the preregistered subjects are also
    diagnostic-shadow arms), so the result is keyed and de-duplicated by id.
    """
    from research.diagnostic_shadow import _logical_arms
    from research.mechanism_cohort import mechanism_cohort as v1
    from research.mechanism_cohort_v2 import mechanism_cohort as v2
    from research.mechanism_cost_cohort import mechanism_cost_cohort
    from research.mechanism_deadline_cohort import mechanism_deadline_cohort
    from research.mechanism_refresh_cohort import mechanism_refresh_cohort
    from research.preregistered import (
        OPENING_RANGE_FADE_HYPOTHESIS, VWAP_REVERSION_HYPOTHESIS,
        preregistration,
    )

    specs: list[dict[str, Any]] = [arm["rule_spec"] for arm in _logical_arms()]
    for manifest in (v1(), v2(), mechanism_cost_cohort(),
                     mechanism_deadline_cohort(), mechanism_refresh_cohort()):
        specs.extend(arm["rule_spec"] for family in manifest["families"]
                     for arm in family["arms"])
    for hypothesis in (VWAP_REVERSION_HYPOTHESIS, OPENING_RANGE_FADE_HYPOTHESIS):
        manifest = preregistration(hypothesis)
        specs.append(manifest["subject"]["rule_spec"])
        specs.extend(item["rule_spec"]
                     for item in manifest.get("secondary_descriptive") or ()
                     if isinstance(item, Mapping) and "rule_spec" in item)
    arms: dict[str, dict[str, Any]] = {}
    for spec in specs:
        arms.setdefault(rule_variant_id(spec), dict(spec))
    return sorted(arms.items())


def _exit_record(rows: list[Mapping[str, Any]], index: int,
                 signal: Mapping[str, Any], spec: Mapping[str, Any]) -> Any:
    if index + 1 >= len(rows):
        return "no_entry_bar"
    entry_bar = rows[index + 1]
    entry_ts = entry_bar["timestamp"].timestamp()
    local = entry_bar["timestamp"].astimezone(NY)
    force_flat = datetime.combine(local.date(), FORCE_FLAT, tzinfo=NY).timestamp()
    try:
        deadline = exit_deadline(entry_ts, spec, force_flat_ts=force_flat)
        thesis = thesis_exit_deadline(entry_ts, spec)
        state = initialize_exit_state(
            signal["direction"], entry_bar["open"], signal["stop_price"],
            signal["target_price"], breakeven_r=spec.get("breakeven_r"),
            trailing_stop_r=spec.get("trailing_stop_r"),
            target_mode=spec.get("target_mode", "fixed_r"),
            target_lookback=spec.get("target_lookback"),
            exit_before_ts=thesis)
    except RuleSpecError as exc:
        return {"error": str(exc)}
    record: dict[str, Any] = {"deadline": deadline, "thesis": thesis,
                              "stop_changes": []}
    for bar in rows[index + 1:]:
        if (deadline is not None and
                bar["timestamp"].timestamp() + 60.0 > deadline["timestamp"]):
            record["closed_by"] = deadline["reason"]
            break
        try:
            transition = completed_bar_exit_transition(state, bar)
        except RuleSpecError as exc:
            record["error"] = str(exc)
            break
        state = transition["state"]
        if transition["stop_changed"]:
            record["stop_changes"].append(state["active_stop_price"])
        if transition["exit"] is not None:
            record["exit"] = transition["exit"]
            break
    record["final_state"] = state
    return record


def arm_fingerprint(spec: Mapping[str, Any],
                    market: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    """Replay one arm over every evaluated symbol-session prefix."""
    normalized = validate_rule_spec(spec)
    identity = {
        "variant_id": rule_variant_id(normalized),
        "rule_spec_hash": rule_spec_hash(normalized),
        "semantic_signature": rule_semantic_signature(normalized),
        "causal_maturity_bars": causal_maturity_bars(normalized),
        "feature_window_bars": feature_window_bars(normalized),
    }
    records: list[Any] = [identity]
    signals = exits = 0
    # Bounded families read only their trailing feature window, so they are
    # given exactly that window (as research replay does); session-anchored
    # families receive the whole session prefix.
    window = identity["feature_window_bars"]
    need = None if window is None else max(
        identity["causal_maturity_bars"], int(window),
        int(normalized["atr_period"]) + 1) + 2
    for symbol in SYMBOLS:
        history = market[symbol]
        for day in EVALUATED_SESSIONS:
            day_index = SESSION_DATES.index(day)
            context_days = set(SESSION_DATES[
                day_index - PRIOR_CONTEXT_SESSIONS:day_index + 1])
            context = {symbol: [row for row in history
                                if row["timestamp"].astimezone(NY).date()
                                in context_days]}
            if symbol != "SPY":
                context["SPY"] = _session_rows(market["SPY"], day)
            rows = _session_rows(history, day)
            for index in range(len(rows)):
                start = 0 if need is None else max(0, index + 1 - need)
                signal = evaluate_rule_signal(
                    rows[start:index + 1], normalized, bars_by_symbol=context,
                    symbol=symbol)
                if signal is None:
                    continue
                signals += 1
                outcome = _exit_record(rows, index, signal, normalized)
                if isinstance(outcome, Mapping) and "exit" in outcome:
                    exits += 1
                records.append([symbol, day.isoformat(), index, signal, outcome])
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"),
                         default=str)
    return {"signals": signals, "exits": exits,
            "digest": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


_MARKET: dict[str, list[dict[str, Any]]] | None = None


def _worker(item: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    global _MARKET
    if _MARKET is None:
        _MARKET = synthetic_market()
    return item[0], arm_fingerprint(item[1], _MARKET)


def fingerprint(processes: int | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{variant_id: {signals, exits, digest}}`` for every arm."""
    arms = registered_arms()
    workers = processes or max(1, min(3, os.cpu_count() or 1))
    if workers == 1:
        return dict(map(_worker, arms))
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(_worker, arms))


if __name__ == "__main__":
    print(json.dumps(fingerprint(), indent=1, sort_keys=True))
