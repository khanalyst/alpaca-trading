"""Record the market data OKX does not keep, so it exists to test later.

The candle study failed for a reason that no amount of re-analysis can fix:
price history is the most-mined dataset in this market, and the information
that might still carry an edge is either never published historically or
deleted within weeks.

    order book depth       not served historically at all
    rubik long/short       ~30 days
    rubik taker volume     ~30 days
    open interest          ~60 days
    funding rate           ~97 days

Every hour this is not running is an hour of evidence that cannot be
recovered at any price. The script is deliberately boring: poll, append,
deduplicate, survive restarts, never block on a failure.

Run it alongside the agent:

    nohup python research/record_flow.py --out runtime/research/recorded &

Storage is roughly 20 MB per month at the defaults.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://www.okx.com"
log = logging.getLogger("recorder")

# Depth bands, in basis points from mid, at which resting size is summed.
#
# These are deliberately tight. Measured on OKX, the top 50 levels of
# BTC-USDT-SWAP span under 1 bp and the top 400 span about 7 bps, so bands of
# 25-50 bps would every time return "the entire book" and be indistinguishable
# from each other. Wider bands are still useful on thin alts, which is why 25
# is kept - but `book_span_bps` records where the returned book actually ends
# so a saturated band can be identified as a lower bound rather than a
# measurement.
DEPTH_BANDS_BPS = (1, 2, 5, 10, 25)
BOOK_LEVELS = "400"

BOOK_FIELDS = [
    "ts", "inst_id", "mid", "bid", "ask", "spread_bps",
    "bid_sz_top", "ask_sz_top", "book_levels", "book_span_bps",
    *[f"bid_depth_{b}bps" for b in DEPTH_BANDS_BPS],
    *[f"ask_depth_{b}bps" for b in DEPTH_BANDS_BPS],
    "imbalance_top", "imbalance_5bps",
]
HOURLY_FIELDS = {
    "long_short_ratio": ["ts", "ccy", "long_short_ratio"],
    "taker_volume": ["ts", "ccy", "sell_vol", "buy_vol"],
    "open_interest": ["ts", "inst_id", "oi_contracts", "oi_ccy", "oi_usd"],
    "funding": ["ts", "inst_id", "funding_rate", "next_funding_time"],
}

_running = True


def _stop(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    log.info("stop requested; finishing current cycle")


class Recorder:
    def __init__(self, out: Path):
        self.out = out
        self.session = requests.Session()
        self.seen: dict[str, set] = {}
        # Order-book sizes are quoted in CONTRACTS, not base units, and the
        # contract multiplier differs per instrument (0.01 BTC, 1000 DOGE...).
        # Without it, depth is not comparable across instruments - which is
        # the entire reason for recording it.
        self.contract_size: dict[str, float] = {}
        out.mkdir(parents=True, exist_ok=True)

    def load_contract_sizes(self) -> None:
        data = self.get("/api/v5/public/instruments",
                        {"instType": "SWAP"}) or []
        for row in data:
            try:
                value = float(row.get("ctVal") or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                self.contract_size[row["instId"]] = value

    # ------------------------------------------------------------- plumbing

    def get(self, path: str, params: dict) -> list | dict | None:
        for attempt in range(3):
            try:
                response = self.session.get(
                    f"{BASE}{path}", params=params, timeout=15)
                if response.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) != "0":
                    return None
                return payload.get("data")
            except Exception as exc:
                log.debug("%s failed (%s/3): %s", path, attempt + 1, exc)
                time.sleep(0.5 * (attempt + 1))
        return None

    def append(self, series: str, fields: list[str], rows: list[dict]) -> int:
        """Append rows to a per-day CSV, skipping ones already written.

        Day-partitioned files keep any single file small and make a partial
        write cost at most one day, not the whole archive.
        """
        if not rows:
            return 0
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.out / series / f"{day}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        key_set = self.seen.setdefault(f"{series}:{day}", set())
        if not key_set and path.exists():
            try:
                with path.open() as handle:
                    for row in csv.DictReader(handle):
                        key_set.add((row.get("ts"),
                                     row.get("inst_id") or row.get("ccy")))
            except Exception:
                pass
        fresh = []
        for row in rows:
            key = (str(row.get("ts")),
                   str(row.get("inst_id") or row.get("ccy")))
            if key in key_set:
                continue
            key_set.add(key)
            fresh.append(row)
        if not fresh:
            return 0
        write_header = not path.exists()
        with path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields,
                                    extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerows(fresh)
        return len(fresh)

    # -------------------------------------------------------------- capture

    def order_book(self, inst_id: str) -> dict | None:
        data = self.get("/api/v5/market/books",
                        {"instId": inst_id, "sz": BOOK_LEVELS})
        if not data:
            return None
        book = data[0]
        bids = [(float(p), float(s)) for p, s, *_ in book.get("bids", [])]
        asks = [(float(p), float(s)) for p, s, *_ in book.get("asks", [])]
        if not bids or not asks:
            return None
        bid, ask = bids[0][0], asks[0][0]
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        # Convert contracts to base units before valuing the book.
        multiplier = self.contract_size.get(inst_id)
        if multiplier is None:
            self.load_contract_sizes()
            multiplier = self.contract_size.get(inst_id, 1.0)
        bids = [(price, size * multiplier) for price, size in bids]
        asks = [(price, size * multiplier) for price, size in asks]
        row = {
            "ts": int(book.get("ts") or time.time() * 1000),
            "inst_id": inst_id,
            "mid": mid, "bid": bid, "ask": ask,
            "spread_bps": (ask - bid) / mid * 10_000,
            "bid_sz_top": bids[0][1], "ask_sz_top": asks[0][1],
            "book_levels": min(len(bids), len(asks)),
            # How far the returned book reaches. A depth band at or beyond
            # this value summed the whole book and is a lower bound.
            "book_span_bps": min((bids[0][0] - bids[-1][0]) / mid * 10_000,
                                 (asks[-1][0] - asks[0][0]) / mid * 10_000),
        }
        for band in DEPTH_BANDS_BPS:
            floor = mid * (1 - band / 10_000)
            ceiling = mid * (1 + band / 10_000)
            row[f"bid_depth_{band}bps"] = sum(
                price * size for price, size in bids if price >= floor)
            row[f"ask_depth_{band}bps"] = sum(
                price * size for price, size in asks if price <= ceiling)
        top = bids[0][1] + asks[0][1]
        row["imbalance_top"] = (
            (bids[0][1] - asks[0][1]) / top if top > 0 else 0.0)
        # 5bps sits inside the measured span for majors, so this imbalance is
        # a real reading rather than a saturated one.
        deep = row["bid_depth_5bps"] + row["ask_depth_5bps"]
        row["imbalance_5bps"] = (
            (row["bid_depth_5bps"] - row["ask_depth_5bps"]) / deep
            if deep > 0 else 0.0)
        return row

    def capture_books(self, instruments: list[str]) -> int:
        rows = [row for row in (self.order_book(i) for i in instruments) if row]
        return self.append("order_book", BOOK_FIELDS, rows)

    def capture_hourly(self, instruments: list[str],
                       currencies: list[str]) -> dict[str, int]:
        written: dict[str, int] = {}

        rows = []
        for ccy in currencies:
            data = self.get(
                "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                {"ccy": ccy, "period": "1H", "limit": "6"}) or []
            rows += [{"ts": int(r[0]), "ccy": ccy,
                      "long_short_ratio": float(r[1])} for r in data]
        written["long_short_ratio"] = self.append(
            "long_short_ratio", HOURLY_FIELDS["long_short_ratio"], rows)

        rows = []
        for ccy in currencies:
            data = self.get("/api/v5/rubik/stat/taker-volume",
                            {"ccy": ccy, "instType": "CONTRACTS",
                             "period": "1H", "limit": "6"}) or []
            rows += [{"ts": int(r[0]), "ccy": ccy,
                      "sell_vol": float(r[1]), "buy_vol": float(r[2])}
                     for r in data]
        written["taker_volume"] = self.append(
            "taker_volume", HOURLY_FIELDS["taker_volume"], rows)

        rows = []
        for inst_id in instruments:
            data = self.get("/api/v5/public/open-interest",
                            {"instType": "SWAP", "instId": inst_id}) or []
            for item in data:
                rows.append({
                    "ts": int(item.get("ts") or time.time() * 1000),
                    "inst_id": inst_id,
                    "oi_contracts": item.get("oi"),
                    "oi_ccy": item.get("oiCcy"),
                    "oi_usd": item.get("oiUsd"),
                })
        written["open_interest"] = self.append(
            "open_interest", HOURLY_FIELDS["open_interest"], rows)

        rows = []
        for inst_id in instruments:
            data = self.get("/api/v5/public/funding-rate",
                            {"instId": inst_id}) or []
            for item in data:
                rows.append({
                    "ts": int(item.get("fundingTime") or time.time() * 1000),
                    "inst_id": inst_id,
                    "funding_rate": item.get("fundingRate"),
                    "next_funding_time": item.get("nextFundingTime"),
                })
        written["funding"] = self.append(
            "funding", HOURLY_FIELDS["funding"], rows)
        return written


def discover(recorder: Recorder, top_n: int, min_volume: float) -> list[str]:
    """Mirror the live universe filter: linear USDT swaps, crypto category."""
    instruments = {
        row["instId"]: row for row in
        (recorder.get("/api/v5/public/instruments", {"instType": "SWAP"}) or [])
    }
    tickers = recorder.get("/api/v5/market/tickers",
                           {"instType": "SWAP"}) or []
    ranked = []
    for ticker in tickers:
        market = instruments.get(ticker["instId"])
        if (not market or market.get("settleCcy") != "USDT"
                or market.get("ctType") != "linear"
                or market.get("state") != "live"
                or str(market.get("instCategory")) != "1"):
            continue
        try:
            volume = float(ticker["volCcy24h"]) * float(ticker["last"])
        except (TypeError, ValueError):
            continue
        if volume >= min_volume:
            ranked.append((volume, ticker["instId"]))
    ranked.sort(reverse=True)
    return [inst_id for _, inst_id in ranked[:top_n]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/recorded"))
    parser.add_argument("--book-interval", type=int, default=300,
                        help="seconds between order-book snapshots")
    parser.add_argument("--top-n", type=int, default=12)
    parser.add_argument("--min-volume-usd", type=float, default=30e6)
    parser.add_argument("--refresh-minutes", type=int, default=60)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    recorder = Recorder(args.out)
    instruments: list[str] = []
    universe_at = 0.0
    last_hourly = 0.0
    (args.out / "README.txt").write_text(
        "OKX market data recorded because the exchange deletes it.\n"
        "order_book/       depth snapshots; never served historically\n"
        "long_short_ratio/ retail positioning; ~30 day retention upstream\n"
        "taker_volume/     aggressor flow;    ~30 day retention upstream\n"
        "open_interest/    ~60 day retention upstream\n"
        "funding/          ~97 day retention upstream\n"
        "\nFiles are day-partitioned CSVs, deduplicated on (ts, instrument).\n")

    log.info("recording to %s", args.out)
    while _running:
        cycle_start = time.time()
        try:
            if cycle_start - universe_at > args.refresh_minutes * 60:
                found = discover(recorder, args.top_n, args.min_volume_usd)
                if found:
                    instruments = found
                    universe_at = cycle_start
                    log.info("universe (%d): %s", len(instruments),
                             ", ".join(instruments))
            if not instruments:
                time.sleep(30)
                continue

            written = recorder.capture_books(instruments)
            if cycle_start - last_hourly > 900:
                currencies = sorted({i.split("-")[0] for i in instruments})
                hourly = recorder.capture_hourly(instruments, currencies)
                last_hourly = cycle_start
                log.info("books=%d hourly=%s", written,
                         json.dumps(hourly, separators=(",", ":")))
            else:
                log.info("books=%d", written)
        except Exception as exc:
            # A recorder that dies on a bad response loses the data it exists
            # to protect. Log and keep going.
            log.warning("cycle failed: %s", exc)

        elapsed = time.time() - cycle_start
        for _ in range(int(max(1.0, args.book_interval - elapsed))):
            if not _running:
                break
            time.sleep(1)
    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
