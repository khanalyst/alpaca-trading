"""Re-price a strategy's legs at the official auction prints.

Why this exists.  A market-on-open or market-on-close order fills at the
auction price of the fund's primary listing exchange.  The daily bars the
research uses report the *first valid trade* as the open, and that can differ
from the auction (Alpaca staff give examples 17 to 22 cents apart).  Alpaca's
paper trading cannot settle the question either: it simulates an opening
order as a market order at the opening quote.

The decisive check is therefore historical: Alpaca's ``/v2/stocks/auctions``
endpoint (consolidated SIP feed, from 2016) returns every exchange's opening
and closing auction prints.  For each session the primary auction is the
print with the largest size.  The candidate's decisions stay on the bars
(what the trader sees); only its legs are re-priced at the auctions (what it
would get).

    python -m edgefinder.auctions --spec edgefinder/results/strategy.json

needs ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY`` (the free plan serves SIP
history that ends at least 15 minutes ago).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from edgefinder import backtest, data, stats
from edgefinder.catalogue import Candidate
from edgefinder.data import Panel

AUCTION_START = dt.date(2016, 1, 1)
SURVIVES_T = 1.96


def primary_print(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The auction print with the largest size; without sizes, the first one."""
    usable = [e for e in entries or () if e.get("p") and float(e["p"]) > 0]
    if not usable:
        return None
    best = max(usable, key=lambda e: float(e.get("s") or 0))
    return {"price": float(best["p"]), "size": float(best.get("s") or 0),
            "exchange": best.get("x"), "condition": best.get("c")}


def parse_auctions(payload: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, days in (payload.get("auctions") or {}).items():
        for day in days or ():
            row = {"open": primary_print(day.get("o")), "close": primary_print(day.get("c"))}
            out.setdefault(symbol, {})[day["d"]] = row
    return out


def fetch_auctions(symbols: Sequence[str], start: dt.date, end: dt.date | None = None, *,
                   key: str | None = None, secret: str | None = None,
                   http: Callable[..., Any] = data._http_json) -> dict[str, dict[str, Any]]:
    key = key or os.getenv("ALPACA_API_KEY")
    secret = secret or os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise data.DataError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for auctions")
    end = end or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)).date()
    merged: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        token = None
        while True:
            params = {"symbols": symbol, "start": start.isoformat(), "end": end.isoformat(),
                      "limit": 10000, "feed": "sip", "sort": "asc"}
            if token:
                params["page_token"] = token
            payload = http(f"https://{data.ALPACA_DATA_HOST}/v2/stocks/auctions?"
                           f"{urllib.parse.urlencode(params)}",
                           headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
            for sym, days in parse_auctions(payload).items():
                merged.setdefault(sym, {}).update(days)
            token = payload.get("next_page_token")
            if not token:
                break
    return merged


def reprice(panel: Panel, auctions: Mapping[str, Mapping[str, Mapping[str, Any]]]
            ) -> tuple[Panel, np.ndarray]:
    """A copy of ``panel`` with opens and closes replaced by auction prints.

    Returns the panel and a ``(T, S)`` mask of sessions where both the open and
    the close came from an auction.  Auction prints are as traded, while the
    bars are split-adjusted, so a session whose auction price lies more than 5%
    from the bar price (before a split) is left out rather than mis-scaled; the
    result reports how many sessions each fund keeps.
    """
    open_, close = panel.open.copy(), panel.close.copy()
    covered = np.zeros(open_.shape, dtype=bool)
    position = {d.isoformat(): i for i, d in enumerate(panel.dates)}
    for j, symbol in enumerate(panel.symbols):
        for day, row in (auctions.get(symbol) or {}).items():
            i = position.get(day)
            if i is None or not row.get("open") or not row.get("close"):
                continue
            o, c = row["open"]["price"], row["close"]["price"]
            if not (math.isfinite(open_[i, j]) and math.isfinite(close[i, j])):
                continue
            if abs(o / open_[i, j] - 1) > 0.05 or abs(c / close[i, j] - 1) > 0.05:
                continue                      # a different price basis (e.g. a split)
            open_[i, j], close[i, j] = o, c
            covered[i, j] = True
    repriced = Panel(list(panel.dates), list(panel.symbols), open_, panel.high, panel.low, close,
                     panel.volume, panel.dividend, panel.rf_rate, dict(panel.sources))
    return repriced, covered


def check(candidate: Candidate, panel: Panel, auctions: Mapping[str, Any]) -> dict[str, Any]:
    """Bar-priced against auction-priced results on the sessions both cover."""
    repriced, covered = reprice(panel, auctions)
    cols = [panel.index[s] for s in candidate.symbols]
    # a session counts when every traded fund has auction prints that day and the day before
    both = covered[:, cols].all(axis=1)
    window = both & np.concatenate([[False], both[:-1]])
    bar = backtest.run(panel, candidate)
    auc = backtest.run(repriced, candidate, decision_panel=panel)
    m = window & bar.ready
    if m.sum() < 60:
        return {"verdict": "insufficient", "sessions": int(m.sum())}
    gaps = {}
    for s, j in zip(candidate.symbols, cols):
        ok = covered[:, j]
        gaps[s] = {
            "sessions": int(ok.sum()),
            "open_auction_minus_bar_bps": 1e4 * float(np.mean(repriced.open[ok, j] / panel.open[ok, j] - 1)),
            "close_auction_minus_bar_bps": 1e4 * float(np.mean(repriced.close[ok, j] / panel.close[ok, j] - 1)),
        }
    held = m & ((bar.exposure_on > 0) | (bar.exposure_id > 0))

    def summary(r: backtest.Result) -> dict[str, Any]:
        x, b = r.excess[m], r.benchmark[m]
        fit = stats.hac_alpha(x, b)
        sd = x.std(ddof=1)
        return {"ann_excess": float(x.mean()) * stats.TRADING_DAYS,
                "sharpe": float(x.mean() / sd * math.sqrt(stats.TRADING_DAYS)) if sd > 0 else 0.0,
                "alpha": fit["alpha"] * stats.TRADING_DAYS, "alpha_t": fit["alpha_t"],
                "net_bps_per_held_session": 1e4 * float(r.excess[held].mean()) if held.any() else None}

    bar_s, auc_s = summary(bar), summary(auc)
    if auc_s["alpha"] > 0 and auc_s["alpha_t"] >= SURVIVES_T:
        verdict = "survives"
    elif auc_s["alpha"] > 0:
        verdict = "weakened"
    else:
        verdict = "fails"
    dates = [d for d, ok in zip(panel.dates, m) if ok]
    return {"candidate": candidate.id, "verdict": verdict, "sessions": int(m.sum()),
            "first": dates[0].isoformat(), "last": dates[-1].isoformat(),
            "bar_priced": bar_s, "auction_priced": auc_s, "print_gaps": gaps,
            "rule": f"survives if the auction-priced alpha is positive with t >= {SURVIVES_T}"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-price the frozen strategy at auction prints.")
    parser.add_argument("--spec", type=Path, default=Path("edgefinder/results/strategy.json"))
    parser.add_argument("--cache", type=Path, default=data.DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=None, help="write the result as JSON here")
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    if not spec.get("candidate"):
        print("the spec has no candidate", file=sys.stderr)
        return 2
    candidate = Candidate.from_json(spec["candidate"])
    symbols = list(dict.fromkeys(list(candidate.symbols) + [data.CALENDAR_SYMBOL]))
    try:
        raw = data.fetch_daily(symbols, args.cache)
        panel = data.build_panel(raw, list(candidate.symbols))
        result = check(candidate, panel, fetch_auctions(candidate.symbols, AUCTION_START))
    except data.DataError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
