"""A deterministic market, in the layout `download_okx_history.py` writes.

`research/edge_lab.py` is the engine behind every committed tier verdict, and
until now nothing in the suite exercised it: `tests/test_gates.py` says the
gates are "integration-tested by running the tournament against real data", and
that data lives under gitignored `runtime/`. So the guarded path was the one
with no corpus yet, and the path that had already issued three `T0_REJECTED`
verdicts had neither a lookahead test nor a reproduction test.

This builds a few thousand bars from a seeded generator rather than committing
CSVs, because a committed fixture drifts from the loader that reads it and this
cannot. It is not realistic market data and is not trying to be: what it has to
be is *identical every run*, so that a change in a feature, a shifted index or
a new lookahead shows up as a diff instead of as noise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


BAR_MS = 15 * 60 * 1000
START_MS = 1_700_000_000_000 - (1_700_000_000_000 % BAR_MS)

# Quote volume per bar. The live universe filter needs a 24h turnover above
# $50m to consider a symbol at all, so anything thinner produces an empty
# universe and a test that passes by measuring nothing.
QUOTE_VOLUME_PER_BAR = 4_000_000.0


def _series(bars: int, seed: int, start_price: float,
            drift: float, vol: float) -> pd.DataFrame:
    """A random walk with a fixed seed, as OHLCV on a 15m grid."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, size=bars)
    close = start_price * np.exp(np.cumsum(steps))
    # Open is the previous close, so the series is continuous the way a real
    # candle series is; the wick is a bounded fraction of the bar's own move.
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(close - open_) + close * vol * 0.5
    high = np.maximum(open_, close) + spread * rng.uniform(0.0, 0.5, size=bars)
    low = np.minimum(open_, close) - spread * rng.uniform(0.0, 0.5, size=bars)
    ts = START_MS + np.arange(bars, dtype=np.int64) * BAR_MS
    volume = QUOTE_VOLUME_PER_BAR / close
    return pd.DataFrame({
        "timestamp_ms": ts,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume,
        "quote_volume": np.full(bars, QUOTE_VOLUME_PER_BAR),
    })


SYMBOLS = (
    # symbol, seed, start price, per-bar drift, per-bar vol
    ("BTC/USDT:USDT", 11, 40_000.0, 0.00002, 0.0035),
    ("ETH/USDT:USDT", 12, 2_500.0, -0.00001, 0.0050),
    ("SOL/USDT:USDT", 13, 100.0, 0.00004, 0.0080),
)


def write_dataset(root, bars: int = 4000, symbols=SYMBOLS) -> None:
    """Write swap/funding/oi CSVs for every symbol under ``root``."""
    from pathlib import Path

    root = Path(root)
    for folder in ("swap", "funding", "oi"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    for symbol, seed, price, drift, vol in symbols:
        stem = symbol.replace("/", "_").replace(":", "_")
        swap = _series(bars, seed, price, drift, vol)
        swap.to_csv(root / "swap" / f"{stem}.csv", index=False)

        # Funding settles every 8h; the rate oscillates so both crowding
        # directions occur and the percentile features have a distribution.
        rng = np.random.default_rng(seed + 500)
        settlements = swap["timestamp_ms"].to_numpy()[::32]
        phase = np.linspace(0.0, 12.0, len(settlements))
        rate = (0.0001 * np.sin(phase)
                + rng.normal(0.0, 0.00002, size=len(settlements)))
        pd.DataFrame({"timestamp_ms": settlements,
                      "funding_rate": rate}).to_csv(
            root / "funding" / f"{stem}.csv", index=False)

        # Open interest, hourly, with drawdowns so flush-fade's falling-OI
        # condition is satisfied somewhere in the sample.
        hourly = swap["timestamp_ms"].to_numpy()[::4]
        wobble = np.sin(np.linspace(0.0, 40.0, len(hourly)))
        noise = rng.normal(0.0, 0.01, size=len(hourly))
        pd.DataFrame({
            "timestamp_ms": hourly,
            "oi_usd": 5e8 * (1.0 + 0.15 * wobble + noise),
        }).to_csv(root / "oi" / f"{stem}.csv", index=False)


def truncated(root, keep_bars: int, out):
    """Write the same dataset with every bar after ``keep_bars`` removed.

    The point-in-time test: a feature at bar *i* may not change when the bars
    after *i* stop existing. Truncation is the honest way to ask that, because
    it removes the future entirely rather than trusting a mask.
    """
    from pathlib import Path

    root, out = Path(root), Path(out)
    swap_frames = {}
    for path in sorted((root / "swap").glob("*.csv")):
        swap_frames[path.name] = pd.read_csv(path)
    cutoff = min(frame["timestamp_ms"].to_numpy()[keep_bars - 1]
                 for frame in swap_frames.values())

    for folder in ("swap", "funding", "oi"):
        (out / folder).mkdir(parents=True, exist_ok=True)
        for path in sorted((root / folder).glob("*.csv")):
            frame = pd.read_csv(path)
            frame[frame["timestamp_ms"] <= cutoff].to_csv(
                out / folder / path.name, index=False)
    return cutoff
