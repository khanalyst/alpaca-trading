"""Deterministic fixtures and a fingerprint for the registered IBR lanes.

This module deliberately uses only interfaces that existed before the
2026-10-15 review fixes (A-06, A-07, A-10, A-16, A-17), so the same code can
compute the golden fingerprint on the original revision and on the fixed one.
The fingerprint records, for every registered equity IBR arm resolved from
the repository ``config.yaml`` and for the runtime contract with that config:

* the runtime contract signals (one full scan per session and an incremental
  minute-by-minute scan with ``now`` set just after each bar close);
* the research replay, through the mapper the live and diagnostic lanes use
  (``_effective_ibr_config``), under the strict quote-backed policy and under
  the bar-fallback policy, with every trade field and every refusal;
* the effective mapped configuration and the arm identities.

Every float is stored with ``repr`` so the comparison is bit-exact.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"
FIXTURE_PATH = Path(__file__).with_name("fixtures") / "ibr_golden_signals.json"
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc

SYMBOLS = ("SPY", "XLB")
BASE_PRICE = {"SPY": 560.0, "XLB": 88.0}
HALF_SPREAD_BPS = {"SPY": 1.0, "XLB": 4.0}
# Two weeks either side of the 2026-03-08 daylight-saving change, so the
# fixtures cover both UTC offsets of the 09:30 New York open.
DATES = (
    date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5),
    date(2026, 3, 6), date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11),
)
SCENARIOS = (
    "up", "down", "flat", "gap_hold", "range_gap", "wide_spread",
    "early_end", "late_up",
)
# Sessions whose hold is designed to be cut short before the force-flat
# instant: an internal gap ("gap_hold") or the observed data ending early
# ("early_end").  Only their compatibility ``force_flat`` exits may change
# canonical label under the A-07 correction.
TRUNCATED_HOLD_SCENARIOS = ("gap_hold", "early_end")
# The incremental runtime scan stops here; the full scan covers the rest of
# the session.  Every designed breakout fires before this clock.
LATEST_RUNTIME_SCAN = time(12, 0)


def scenario_for(symbol: str, day: date) -> str:
    index = DATES.index(day) + (3 if symbol == "XLB" else 0)
    return SCENARIOS[index % len(SCENARIOS)]


def _volume_profile(minute: int, rng: random.Random, base: float) -> float:
    """A U-shaped intraday profile with multiplicative noise."""
    shape = (1.0 + 2.5 * math.exp(-minute / 18.0) +
             1.5 * math.exp(-(389 - minute) / 25.0))
    return float(round(base * shape * rng.uniform(0.7, 1.3)))


def _quarter(value: float) -> float:
    """Snap a price to a quarter dollar.

    Quarter dollars are exact in binary floating point and lie on the
    one-cent equity tick, so a range-anchored stop distance between two such
    prices carries no representation noise into the stressed-cost geometry.
    """
    return round(value * 4.0) / 4.0


def _full_session(symbol: str, day: date, *,
                  exact_calendar: bool | None = None) -> list[dict[str, Any]]:
    """Every minute 09:30-15:59 before any scenario removes a bar."""
    scenario = scenario_for(symbol, day)
    rng = random.Random(f"ibr-golden:{symbol}:{day.isoformat()}")
    base = _quarter(BASE_PRICE[symbol] * (1.0 + rng.uniform(-0.01, 0.01)))
    band = 0.0035  # opening band half-width as a fraction of price
    band_high = _quarter(base * (1.0 + band))
    band_low = _quarter(base * (1.0 - band))
    volume_base = 4_000.0 if symbol == "SPY" else 600.0
    opened = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK)
    rows: list[dict[str, Any]] = []
    price = base
    breakout_minute = 50 if scenario != "late_up" else 140
    direction = -1.0 if scenario == "down" else 1.0
    breakout_close = _quarter(
        (band_high if direction > 0 else band_low) *
        (1.0 + direction * 0.0025))
    # The shipped config requires the exact broker calendar, so most
    # sessions carry it; two dates per symbol exercise its absence.
    use_calendar = (exact_calendar if exact_calendar is not None
                    else DATES.index(day) not in {0, 4})
    for minute in range(390):
        previous = price
        if minute < 45 or (
                scenario in {"flat", "range_gap"} or minute < breakout_minute):
            # Inside the opening band.  The first two minutes touch both
            # band edges, so the 15, 30 and 45 minute ranges share one high
            # and one low; later minutes stay strictly inside it.
            spread = 0.6 if minute < 45 else 0.8
            price = base * (1.0 + rng.uniform(-spread, spread) * band)
            high = max(previous, price) * (1.0 + rng.uniform(0.0, 0.0003))
            low = min(previous, price) * (1.0 - rng.uniform(0.0, 0.0003))
            ceiling = band_high if minute < 45 else band_high - 0.05
            floor = band_low if minute < 45 else band_low + 0.05
            if minute == 0:
                high = band_high
            if minute == 1:
                low = band_low
            high = min(round(high, 2), ceiling)
            low = max(round(low, 2), floor)
            price = min(max(round(price, 2), low), high)
        elif minute == breakout_minute:
            price = breakout_close
            high = max(previous, price) * (1.0 + 0.0002)
            low = min(previous, price) * (1.0 - 0.0002)
        elif scenario == "gap_hold" and minute < breakout_minute + 6:
            # Keep the position quiet near entry until the internal gap.
            price = breakout_close * (1.0 + rng.uniform(-0.0004, 0.0004))
            high = max(previous, price) * (1.0 + 0.0002)
            low = min(previous, price) * (1.0 - 0.0002)
        else:
            price = previous * (1.0 + direction * 0.00012 +
                                rng.gauss(0.0, 0.0006))
            high = max(previous, price) * (1.0 + rng.uniform(0.0, 0.0004))
            low = min(previous, price) * (1.0 - rng.uniform(0.0, 0.0004))
        price = round(price, 2) if minute != breakout_minute else price
        open_price = previous if minute else base
        volume = _volume_profile(minute, rng, volume_base)
        if minute == breakout_minute and scenario not in {"flat", "range_gap"}:
            volume *= 6.0
        if scenario == "wide_spread" and minute == breakout_minute + 2:
            volume *= 6.0
        stamp = opened + timedelta(minutes=minute)
        ended = stamp + timedelta(minutes=1)
        row = {
            "symbol": symbol,
            "timestamp": stamp.astimezone(UTC).isoformat(),
            "open": round(open_price, 2), "high": round(high, 2),
            "low": round(low, 2), "close": price,
            "volume": volume,
            "provider": "alpaca", "feed": "iex",
            "source_mode": "forward_observed",
            "as_of": ended.astimezone(UTC).isoformat(),
            "observed_at": ended.astimezone(UTC).isoformat(),
            "session_date": day.isoformat(),
        }
        # Rounding can only move a bound inside the bar; restore the OHLC
        # envelope explicitly so every fixture row is valid.
        row["high"] = max(row["high"], row["open"], row["close"])
        row["low"] = min(row["low"], row["open"], row["close"])
        if use_calendar:
            row["session_open"] = opened.astimezone(UTC).isoformat()
            row["session_close"] = (datetime.combine(
                day, time(16, 0), tzinfo=NEW_YORK).astimezone(UTC).isoformat())
        rows.append(row)
    if scenario == "early_end":
        rows = rows[:210]  # the recorder stops at 13:00 New York
    return rows


def session_rows(symbol: str, day: date, *, exact_calendar: bool | None = None
                 ) -> list[dict[str, Any]]:
    """One deterministic IEX minute session, with the scenario's gaps."""
    rows = _full_session(symbol, day, exact_calendar=exact_calendar)
    scenario = scenario_for(symbol, day)
    if scenario == "range_gap":
        del rows[7]
    if scenario == "gap_hold":
        # IEX prints no bar for a minute without IEX trades: two missing
        # minutes a few bars after entry, and another one later.
        for minute in (140, 57, 56):
            del rows[minute]
    return rows


def quote_rows(symbol: str, day: date) -> list[dict[str, Any]]:
    """One quote at each minute open, including minutes without a bar.

    A minute without IEX trades still has a quote, so quotes continue across
    the bar gaps.  The wide-spread scenario quotes 50 bps at two minutes.
    """
    out: list[dict[str, Any]] = []
    for minute, row in enumerate(_full_session(symbol, day)):
        half = HALF_SPREAD_BPS[symbol] / 10_000.0
        if scenario_for(symbol, day) == "wide_spread" and minute in {51, 52}:
            half = 25.0 / 10_000.0
        mid = float(row["open"])
        out.append({
            "symbol": symbol, "timestamp": row["timestamp"],
            "bid": round(mid * (1.0 - half), 2),
            "ask": round(mid * (1.0 + half), 2),
            "provider": "alpaca", "feed": "iex",
            "source_mode": "forward_observed",
            "as_of": row["timestamp"], "observed_at": row["timestamp"],
        })
    return out


def all_sessions() -> list[tuple[str, date, list[dict[str, Any]]]]:
    return [(symbol, day, session_rows(symbol, day))
            for symbol in SYMBOLS for day in DATES]


def canon(value: Any) -> Any:
    """A JSON projection that keeps every value bit-exact and typed."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return "f:" + repr(value)
    if isinstance(value, Decimal):
        return "dec:" + str(value)
    if isinstance(value, datetime):
        return "dt:" + value.isoformat()
    if isinstance(value, date):
        return "d:" + value.isoformat()
    if isinstance(value, time):
        return "t:" + value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: canon(getattr(value, item.name))
                for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): canon(item) for key, item in sorted(
            value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canon(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return items
    return "repr:" + repr(value)


def shipped_config() -> dict[str, Any]:
    from agent.config import load_config
    return load_config(CONFIG_PATH)


def raw_shipped_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def registered_arms() -> list[dict[str, Any]]:
    from research.diagnostic_shadow import _ibr_logical_arms
    return _ibr_logical_arms(shipped_config())


def _runtime_signals(config: Mapping[str, Any],
                     sessions: list[tuple[str, date, list[dict[str, Any]]]]
                     ) -> list[dict[str, Any]]:
    from agent.contracts.ibr import generate_ibr_signal
    records: list[dict[str, Any]] = []
    for symbol, day, rows in sessions:
        full = generate_ibr_signal(symbol, [dict(row) for row in rows],
                                   config=config)
        incremental = None
        state: dict[str, Any] = {"signals": set()}
        for index, row in enumerate(rows):
            stamp = datetime.fromisoformat(str(row["timestamp"]))
            if stamp.astimezone(NEW_YORK).time() > LATEST_RUNTIME_SCAN:
                break
            now = stamp + timedelta(minutes=1, seconds=2)
            signal = generate_ibr_signal(
                symbol, [dict(item) for item in rows[:index + 1]],
                config=config, now=now, session_state=state)
            if signal is not None:
                incremental = {"index": index, "now": now.isoformat(),
                               "signal": signal}
                break
        records.append({
            "symbol": symbol, "session": day.isoformat(),
            "scenario": scenario_for(symbol, day),
            "full_scan": canon(full), "incremental": canon(incremental),
        })
    return records


def _replay_records(result) -> dict[str, Any]:
    return {
        "trades": [canon(trade) for trade in result.trades],
        "refusals": [canon(refusal.as_dict()) for refusal in result.refusals],
        "authorizing": bool(result.authorizing),
        "diagnostic_only": bool(result.diagnostic_only),
    }


def _replay_inputs(sessions):
    from research.market_data import normalize_quote, normalize_underlying_bar
    bars = []
    quotes = []
    for symbol, day, rows in sessions:
        bars.extend(normalize_underlying_bar(row) for row in rows)
        quotes.extend(normalize_quote(row) for row in quote_rows(symbol, day))
    bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
    return bars, quotes


def _config_projection(cfg) -> dict[str, Any]:
    return canon(cfg)


def compute_fingerprint() -> dict[str, Any]:
    """Compute every golden record from the code currently importable."""
    from research.costs import ReplayPolicy
    from research.diagnostic_shadow import content_identity
    from research.edge_discovery_core import _effective_ibr_config
    from research.ibr import IBRConfig, replay_ibr

    sessions = all_sessions()
    bars, quotes = _replay_inputs(sessions)
    out: dict[str, Any] = {
        "schema": "ibr-golden-signals.v1",
        "sessions": [{"symbol": symbol, "session": day.isoformat(),
                      "scenario": scenario_for(symbol, day),
                      "bars": len(rows)}
                     for symbol, day, rows in sessions],
        "arms": [],
    }
    for arm in registered_arms():
        config = arm["config"]
        strict_cfg, effective = _effective_ibr_config(
            config, {}, vehicle="equity", close_confirmed=True)
        fallback_cfg, _ = _effective_ibr_config(
            config, {}, vehicle="equity", close_confirmed=True,
            policy=replace(ReplayPolicy.from_config(config),
                           strict_market_data=False))
        out["arms"].append({
            "variant_id": arm["variant_id"],
            "spec_identity": arm["spec_identity"],
            "config_identity": content_identity(config),
            "effective_config": canon(effective),
            "strict_config": _config_projection(strict_cfg),
            "fallback_config": _config_projection(fallback_cfg),
            "runtime": _runtime_signals(config, sessions),
            "replay_strict": _replay_records(
                replay_ibr(bars, config=strict_cfg, quotes=quotes)),
            "replay_bar_fallback": _replay_records(
                replay_ibr(bars, config=fallback_cfg, quotes=quotes)),
        })
    out["shipped_runtime"] = {
        "validated": _runtime_signals(shipped_config(), sessions),
        "raw": _runtime_signals(raw_shipped_config(), sessions),
    }
    # The research replay's own defaults are a separate, legacy strategy.
    # Pin them too, so keeping them available is checked rather than assumed.
    legacy_fallback = IBRConfig(policy=ReplayPolicy(strict_market_data=False))
    out["legacy_research_defaults"] = {
        "config": _config_projection(IBRConfig()),
        "replay_default": _replay_records(replay_ibr(bars, quotes=quotes)),
        "replay_bar_fallback": _replay_records(
            replay_ibr(bars, config=legacy_fallback, quotes=quotes)),
    }
    return out


_MISSING = "<missing>"
# Trade fields kept in clear so the A-07 label correction can be checked
# precisely; every other field is folded into the record digest.
TRADE_CLEAR_FIELDS = ("symbol", "session_date", "exit_reason",
                      "canonical_exit_reason")


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iter_signals(full: dict[str, Any]):
    groups = [arm["runtime"] for arm in full["arms"]]
    groups.extend(full["shipped_runtime"].values())
    for records in groups:
        for record in records:
            if record["full_scan"] is not None:
                yield record["full_scan"]
            if record["incremental"] is not None:
                yield record["incremental"]["signal"]


def _iter_trades(full: dict[str, Any]):
    replays = [arm[name] for arm in full["arms"]
               for name in ("replay_strict", "replay_bar_fallback")]
    legacy = full["legacy_research_defaults"]
    replays.extend((legacy["replay_default"], legacy["replay_bar_fallback"]))
    for replay in replays:
        yield from replay["trades"]


def record_keys(full: dict[str, Any]) -> dict[str, list[str]]:
    """The key set of each record kind, which must be uniform per kind."""
    def uniform(records, kind):
        sets = {tuple(sorted(record)) for record in records}
        if len(sets) != 1:
            raise AssertionError(f"{kind} records do not share one key set")
        return list(next(iter(sets)))
    arm_configs = [arm[name] for arm in full["arms"]
                   for name in ("strict_config", "fallback_config")]
    arm_configs.append(full["legacy_research_defaults"]["config"])
    return {
        "signal": uniform(list(_iter_signals(full)), "signal"),
        "trade": uniform(list(_iter_trades(full)), "trade"),
        "replay_config": uniform(arm_configs, "replay config"),
        "effective_config": uniform(
            [arm["effective_config"] for arm in full["arms"]],
            "effective config"),
    }


def _compact_mapping(record: Mapping[str, Any] | None, keys: list[str], *,
                     clear: tuple[str, ...] = ()) -> Any:
    """Digest a canonical record over the original revision's keys.

    Keys that a fixed revision adds are outside the projection and are
    checked separately by the tests; a key the original carried that is now
    missing changes the digest.
    """
    if record is None:
        return None
    body = {key: record.get(key, _MISSING) for key in keys
            if key not in clear}
    if not clear:
        return _digest(body)
    out: dict[str, Any] = {"sha256": _digest(body)}
    for key in clear:
        out[key] = record.get(key, _MISSING)
    return out


def _compact_runtime(records: list[dict[str, Any]], keys: list[str]
                     ) -> list[dict[str, Any]]:
    out = []
    for record in records:
        incremental = record["incremental"]
        out.append({
            "symbol": record["symbol"], "session": record["session"],
            "scenario": record["scenario"],
            "full_scan": _compact_mapping(record["full_scan"], keys),
            "incremental": None if incremental is None else {
                "index": incremental["index"], "now": incremental["now"],
                "signal": _compact_mapping(incremental["signal"], keys),
            },
        })
    return out


def _compact_replay(record: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {
        "trades": [_compact_mapping(trade, keys, clear=TRADE_CLEAR_FIELDS)
                   for trade in record["trades"]],
        "refusals": record["refusals"],
        "authorizing": record["authorizing"],
        "diagnostic_only": record["diagnostic_only"],
    }


def compact_fingerprint(full: dict[str, Any],
                        keys: dict[str, list[str]] | None = None
                        ) -> dict[str, Any]:
    """Fold full records into digests over the original key sets.

    ``keys`` is the fixture's ``record_keys``; when omitted (fixture
    generation) the key sets are read from ``full`` itself.
    """
    keys = dict(keys or record_keys(full))
    arms = []
    for arm in full["arms"]:
        arms.append({
            "variant_id": arm["variant_id"],
            "spec_identity": arm["spec_identity"],
            "config_identity": arm["config_identity"],
            "effective_config": _compact_mapping(
                arm["effective_config"], keys["effective_config"]),
            "strict_config": _compact_mapping(
                arm["strict_config"], keys["replay_config"]),
            "fallback_config": _compact_mapping(
                arm["fallback_config"], keys["replay_config"]),
            "runtime": _compact_runtime(arm["runtime"], keys["signal"]),
            "replay_strict": _compact_replay(
                arm["replay_strict"], keys["trade"]),
            "replay_bar_fallback": _compact_replay(
                arm["replay_bar_fallback"], keys["trade"]),
        })
    legacy = full["legacy_research_defaults"]
    return {
        "schema": full["schema"],
        "record_keys": keys,
        "sessions": full["sessions"],
        "arms": arms,
        "shipped_runtime": {
            name: _compact_runtime(full["shipped_runtime"][name],
                                   keys["signal"])
            for name in ("validated", "raw")
        },
        "legacy_research_defaults": {
            "config": _compact_mapping(legacy["config"],
                                       keys["replay_config"]),
            "replay_default": _compact_replay(
                legacy["replay_default"], keys["trade"]),
            "replay_bar_fallback": _compact_replay(
                legacy["replay_bar_fallback"], keys["trade"]),
        },
    }


def write_fixture(path: Path = FIXTURE_PATH) -> Path:
    payload = compact_fingerprint(compute_fingerprint())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=0) + "\n",
                    encoding="utf-8")
    return path


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover - fixture generation entry point
    print(write_fixture())
