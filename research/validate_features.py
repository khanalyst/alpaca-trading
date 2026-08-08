"""Prove the research feature engine reproduces the LIVE agent's snapshot.

A backtest is only evidence if it computes the same numbers the running
agent computes. This module drives `agent.market.symbol_snapshot` - the real
production code path - through a fake exchange that replays historical CSVs,
freezes the clock at a chosen bar boundary, and compares every field against
`research.edge_lab.SymbolFrame`.

Any field that disagrees is reported. Exit code is non-zero on mismatch.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import (  # noqa: E402
    BAR_MS, HOUR_MS, Contract, SymbolFrame, aggregate_bars, derive_levels,
    evidence_masks, load_dataset,
)

TF_MS = {"15m": BAR_MS, "1h": HOUR_MS, "4h": 4 * HOUR_MS}


class FakeCcxt:
    """Minimal ccxt surface serving historical bars up to a frozen `now`."""

    def __init__(self, frame: SymbolFrame, spot: pd.DataFrame | None,
                 oi: pd.DataFrame | None):
        self.frame = frame
        self.spot = spot
        self.oi = oi
        self.now_ms = 0
        self._frames = {
            "15m": frame.fast,
            "1h": aggregate_bars(frame.fast, HOUR_MS),
            "4h": aggregate_bars(frame.fast, 4 * HOUR_MS),
        }

    @staticmethod
    def parse_timeframe(timeframe: str) -> int:
        return TF_MS[timeframe] // 1000

    def market(self, symbol: str) -> dict:
        return {"contractSize": 1.0, "swap": True, "settle": "USDT"}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        frame = self._frames[timeframe]
        ts = frame["timestamp_ms"].to_numpy(np.int64)
        # Include a bar as soon as it has started; agent.market._closed_ohlcv
        # is responsible for dropping the one that has not closed yet.
        usable = frame[ts <= self.now_ms]
        rows = usable.tail(limit)
        return [
            [int(r.timestamp_ms), float(r.open), float(r.high),
             float(r.low), float(r.close), float(r.volume)]
            for r in rows.itertuples(index=False)
        ]

    def fetch_ticker(self, symbol):
        fast = self.frame.fast
        ts = fast["timestamp_ms"].to_numpy(np.int64)
        # `now_ms` is the instant the signal bar closed, so the newest
        # information available is that bar's close - NOT the bar that is
        # only just starting. Reading the latter would be look-ahead.
        pos = int(np.searchsorted(ts, self.now_ms - BAR_MS, side="right") - 1)
        window = fast.iloc[max(0, pos - 95):pos + 1]
        last = float(fast.iloc[pos]["close"])
        return {
            "last": last,
            "close": last,
            "bid": last * (1 - 0.00025),
            "ask": last * (1 + 0.00025),
            "high": float(window["high"].max()),
            "low": float(window["low"].min()),
            "percentage": 0.0,
            "quoteVolume": float(
                window.get("quote_volume", window["volume"]).sum()),
            "info": {},
        }

    def fetch_funding_rate(self, symbol):
        ts, rates = self.frame.funding_ts, self.frame.funding_rates
        pos = int(np.searchsorted(ts, self.now_ms, side="right") - 1)
        if pos < 0:
            raise ValueError("no funding data")
        nxt = ts[pos + 1] if pos + 1 < len(ts) else ts[pos] + 8 * HOUR_MS
        # Mark and index must both come from the bar that has already
        # closed, for the same no-look-ahead reason as the ticker.
        closed = self.now_ms - BAR_MS
        spot_price = None
        if self.spot is not None and not self.spot.empty:
            match = self.spot[self.spot["timestamp_ms"] <= closed]
            if not match.empty:
                spot_price = float(match.iloc[-1]["close"])
        mark = float(self.frame.fast[
            self.frame.fast["timestamp_ms"] <= closed].iloc[-1]["close"])
        return {
            "fundingRate": float(rates[pos]),
            "fundingTimestamp": int(ts[pos]),
            "nextFundingTimestamp": int(nxt),
            "markPrice": mark,
            "indexPrice": spot_price,
            "info": {},
        }

    def fetch_funding_rate_history(self, symbol, since=None, limit=30):
        ts, rates = self.frame.funding_ts, self.frame.funding_rates
        pos = int(np.searchsorted(ts, self.now_ms, side="right") - 1)
        start = max(0, pos - limit + 1)
        return [{"fundingRate": float(r)} for r in rates[start:pos + 1]]

    def fetch_open_interest(self, symbol):
        if self.oi is None or self.oi.empty:
            raise ValueError("no oi")
        match = self.oi[self.oi["timestamp_ms"] <= self.now_ms]
        if match.empty:
            raise ValueError("no oi")
        return {"openInterestValue": float(match.iloc[-1]["oi_usd"]),
                "info": {}}


class FakeExchange:
    def __init__(self, ccxt_like: FakeCcxt):
        self.x = ccxt_like

    @staticmethod
    def retry(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def taker_fee_pct(self, symbol: str) -> float:
        return 0.05


NUMERIC_FIELDS = [
    "atr_1h_pct", "atr_1h_ratio", "mom_1h_pct", "mom_15m_pct",
    "signal_candle_return_pct", "relative_volume_1h", "swing_low_pct",
    "swing_high_pct", "ema20_1h_dist_pct", "range_pos_pct",
    "funding_rate_pct", "funding_percentile_30", "perp_index_basis_pct",
    "open_interest_musd", "beta_btc_1h_72",
]
EXACT_FIELDS = [
    "trend_15m", "trend_1h", "trend_4h", "regime", "signal_ts",
    "signal_1h_ts", "fresh_breakout_long", "fresh_breakout_short",
    "price_stabilized_long", "price_stabilized_short",
]
# agent.market computes all of these inside one try block around the funding
# fetch, so any funding failure nulls the whole group together.
FUNDING_DEPENDENT_FIELDS = {
    "funding_rate_pct", "funding_percentile_30", "perp_index_basis_pct",
}


def compare(symbol: str, frames: dict, data_root: Path, samples: int,
            seed: int) -> dict:
    import agent.market as market_module
    from agent.config import validate_config
    import yaml

    repo = Path(__file__).resolve().parents[1]
    cfg = validate_config(yaml.safe_load((repo / "config.yaml").read_text()))

    frame = frames[symbol]
    stem = symbol.replace("/", "_").replace(":", "_")

    def read(folder):
        path = data_root / folder / f"{stem}.csv"
        if not path.exists():
            return None
        out = pd.read_csv(path)
        return out if not out.empty else None

    fake = FakeCcxt(frame, read("spot"), read("oi"))
    exchange = FakeExchange(fake)

    btc = frames.get("BTC/USDT:USDT")
    rng = np.random.default_rng(seed)
    n = len(frame.df)
    low = 400          # past every warmup window
    high = n - 2
    if high <= low:
        return {"symbol": symbol, "checked": 0, "skipped": "insufficient bars"}
    picks = rng.choice(np.arange(low, high), size=min(samples, high - low),
                       replace=False)

    original_time = market_module.time.time
    mismatches = []
    checked = 0
    warmup_skipped = 0
    no_funding_skipped = 0
    covered_from = getattr(frame, "funding_covered_from", None)
    for index in sorted(picks):
        # OKX serves only ~97 days of funding history, so bars older than the
        # fixture's coverage make the fake exchange raise on fetch_funding_rate.
        # agent.market then nulls the whole funding block - including
        # perp_index_basis_pct, which is computed inside the same try. That is
        # correct fail-closed behaviour against a live exchange (the squeeze
        # contract needs funding anyway), but against this fixture it would
        # measure missing test data. Compare every other field on those bars
        # instead of discarding 87% of the history.
        funded = covered_from is not None and int(frame.ts[index]) >= covered_from
        if not funded:
            no_funding_skipped += 1
        # The live agent computes EMAs from min(available, 120) completed
        # bars once a timeframe has >= min_history_candles; the research
        # engine requires a full 120-bar window and reports None below it.
        # Those bars are excluded from every analysis, so comparing them
        # would measure a deliberate exclusion, not a disagreement.
        trend_4h = frame.df["trend_4h"].to_numpy(object)[index]
        if trend_4h not in ("up", "down", "flat"):
            warmup_skipped += 1
            continue
        bar_ts = int(frame.ts[index])
        now_ms = bar_ts + BAR_MS          # the instant the signal bar closes
        fake.now_ms = now_ms
        market_module.time.time = lambda: now_ms / 1000.0
        try:
            # agent.market.market_snapshot always supplies BTC's own 1h
            # returns as the benchmark, including for BTC itself (where the
            # measured beta is 1.0 by construction). Omitting it here would
            # compare against a code path the agent never takes.
            benchmark_returns = None
            if btc is not None:
                btc_1h = aggregate_bars(btc.fast, HOUR_MS)
                usable = btc_1h[btc_1h["timestamp_ms"] + HOUR_MS <= now_ms]
                benchmark_returns = (
                    usable.tail(cfg["cycle"]["candles"])
                    .set_index("timestamp_ms")["close"].pct_change().dropna())
            live = market_module.symbol_snapshot(
                exchange, symbol, cfg, benchmark_returns)
        except Exception as exc:
            mismatches.append({"index": int(index), "error": str(exc)[:200]})
            continue
        finally:
            market_module.time.time = original_time

        checked += 1
        mine = frame.df.iloc[index]

        def missing(value) -> bool:
            if value is None:
                return True
            if isinstance(value, (float, np.floating)):
                return math.isnan(float(value))
            return False

        row_issues = {}
        skip = set() if funded else FUNDING_DEPENDENT_FIELDS
        for field in EXACT_FIELDS:
            if field in skip or field not in live or field not in mine.index:
                continue
            a, b = live[field], mine[field]
            if missing(a) and missing(b):
                continue
            if missing(a) or missing(b):
                row_issues[field] = [a, b]
                continue
            if field in ("signal_ts", "signal_1h_ts"):
                if int(a) != int(b):
                    row_issues[field] = [int(a), int(b)]
                continue
            if isinstance(a, (bool, np.bool_)) or isinstance(b, (bool, np.bool_)):
                if bool(a) != bool(b):
                    row_issues[field] = [bool(a), bool(b)]
                continue
            if a != b:
                row_issues[field] = [a, b]
        for field in NUMERIC_FIELDS:
            if field in skip or field not in live or field not in mine.index:
                continue
            a, b = live[field], mine[field]
            if missing(a) and missing(b):
                continue
            if missing(a) or missing(b):
                row_issues[field] = [a, None if missing(b) else float(b)]
                continue
            a, b = float(a), float(b)
            tol = max(2e-2, abs(a) * 3e-3)
            if abs(a - b) > tol:
                row_issues[field] = [a, b]
        if row_issues:
            mismatches.append({"index": int(index), "ts": bar_ts,
                               "fields": row_issues})

    return {"symbol": symbol, "checked": checked,
            "warmup_bars_skipped": warmup_skipped,
            "outside_funding_coverage_skipped": no_funding_skipped,
            "mismatched_bars": len(mismatches),
            "examples": mismatches[:5]}


def contract_agreement(frames: dict, samples: int, seed: int) -> dict:
    """Confirm vectorized evidence masks equal agent.strategy.setup_evidence."""
    from agent.config import validate_config
    from agent.strategy import build_setup_plan, setup_evidence
    import yaml

    from agent.registry import spec_for

    repo = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((repo / "config.yaml").read_text())
    # The vectorized masks this checks are momentum's three setups, so the
    # comparison is pinned to momentum rather than to whatever occupies the
    # order path. Every other configured parameter is kept, which is the part
    # that has to agree: a threshold change in config.yaml must not silently
    # make this test compare two differently parameterised contracts. Reading
    # strategy.id from the file was only coherent while momentum was the one
    # strategy that could be configured there.
    raw["strategy"] = dict(raw.get("strategy") or {})
    raw["strategy"]["id"] = "momentum"
    raw["strategy"]["version"] = spec_for("momentum").version
    raw["strategy"]["signal_timeframe"] = "15m"
    raw["strategy"]["execution_mode"] = "analyst"
    # The shipped config names the coupled ls-ratio-fade tuning.  This
    # validation lane is intentionally pinned to momentum, so carrying that
    # explicit variant identity would make config validation reject the
    # otherwise useful cross-contract feature comparison.
    raw["strategy"].pop("variant_id", None)
    cfg = validate_config(raw)
    contract = Contract.from_config(cfg)
    rng = np.random.default_rng(seed)
    issues = []
    checked = 0
    fields = [
        "atr_1h_pct", "ema20_1h_dist_pct", "trend_15m", "trend_1h", "trend_4h",
        "range_pos_pct", "relative_volume_1h", "mom_1h_pct", "mom_15m_pct",
        "funding_rate_pct", "funding_percentile_30", "funding_samples_30",
        "perp_index_basis_pct", "open_interest_musd", "fresh_breakout_long",
        "fresh_breakout_short", "price_stabilized_long",
        "price_stabilized_short", "swing_low_pct", "swing_high_pct",
        "signal_ts",
    ]
    for symbol, frame in frames.items():
        masks = evidence_masks(frame.df, contract)
        n = len(frame.df)
        picks = rng.choice(np.arange(400, n - 2),
                           size=min(samples, max(0, n - 402)), replace=False)
        for index in picks:
            row = frame.df.iloc[index]
            snap = {}
            for field in fields:
                value = row.get(field)
                if isinstance(value, (np.floating, float)):
                    value = None if math.isnan(float(value)) else float(value)
                elif isinstance(value, (np.integer,)):
                    value = int(value)
                elif isinstance(value, (np.bool_,)):
                    value = bool(value)
                snap[field] = value
            reference = setup_evidence(snap, cfg)
            checked += 1
            for setup in ("trend_continuation", "range_breakout",
                          "funding_squeeze"):
                for direction in ("long", "short"):
                    want = bool(reference[setup][direction])
                    got = bool(masks[f"{setup}_{direction}"][index])
                    if want != got:
                        issues.append({
                            "symbol": symbol, "index": int(index),
                            "setup": f"{setup}_{direction}",
                            "authoritative": want, "vectorized": got,
                        })
            for direction in ("long", "short"):
                want = reference["extension_atr"][direction]
                got = masks[f"extension_{direction}"][index]
                if want is None:
                    continue
                if abs(float(want) - float(got)) > 0.02:
                    issues.append({
                        "symbol": symbol, "index": int(index),
                        "field": f"extension_{direction}",
                        "authoritative": want, "vectorized": float(got)})
            # Stop/target derivation must match build_setup_plan exactly.
            for direction in ("long", "short"):
                if not reference["trend_continuation"][direction] \
                        and not reference["range_breakout"][direction]:
                    continue
                setup = ("trend_continuation"
                         if reference["trend_continuation"][direction]
                         else "range_breakout")
                for policy in ("fixed_rr", "extended_rr", "structure_target"):
                    plan, why = build_setup_plan({
                        "symbol": symbol, "direction": direction,
                        "setup_type": setup, "invalidation_anchor": "structure",
                        "exit_policy": policy, "execution_choice": "normal",
                        "confidence": 1.0,
                    }, snap, cfg)
                    if plan is None:
                        continue
                    stop, take = derive_levels(
                        frame.df.iloc[[index]], contract, direction, policy)
                    if abs(float(plan["stop_loss_pct"]) - float(stop[0])) > 1e-6 \
                            or abs(float(plan["take_profit_pct"])
                                   - float(take[0])) > 1e-6:
                        issues.append({
                            "symbol": symbol, "index": int(index),
                            "policy": policy, "direction": direction,
                            "authoritative": [plan["stop_loss_pct"],
                                              plan["take_profit_pct"]],
                            "vectorized": [float(stop[0]), float(take[0])]})
    return {"checked_bars": checked, "disagreements": len(issues),
            "examples": issues[:10]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()

    frames = load_dataset(args.data)
    if not frames:
        print("no usable symbols in dataset")
        return 2
    wanted = ([s.strip() for s in args.symbols.split(",") if s.strip()]
              or list(frames)[:4])

    print("=== A. vectorized evidence vs agent.strategy (authoritative) ===")
    agreement = contract_agreement(
        {s: frames[s] for s in wanted if s in frames},
        args.samples, args.seed)
    print(json.dumps(agreement, indent=2, default=str))

    print("\n=== B. research features vs agent.market.symbol_snapshot (live) ===")
    results = []
    for symbol in wanted:
        if symbol not in frames:
            continue
        result = compare(symbol, frames, args.data, args.samples, args.seed)
        results.append(result)
        print(json.dumps(result, indent=2, default=str))

    bad = agreement["disagreements"] > 0 or any(
        r.get("mismatched_bars", 0) > 0 for r in results)
    print("\nRESULT:", "MISMATCHES FOUND" if bad else "ALL CHECKS AGREE")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
