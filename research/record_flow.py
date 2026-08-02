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
    # Actual filled liquidations: the DIRECT observation of the forced-flow
    # mechanism, rather than the open-interest proxy that flush-fade v1 used
    # and that was falsified. OKX serves only a short recent window, so this
    # exists only if it is recorded.
    "liquidations": ["ts", "inst_id", "side", "pos_side", "bk_px", "sz"],
}

# Keep these four columns first and unchanged: existing exports and readers
# use them.  The additional columns distinguish a forecast observed before a
# settlement from the rate OKX later says was actually settled.
LEGACY_FUNDING_FIELDS = [
    "ts", "inst_id", "funding_rate", "next_funding_time",
]
FUNDING_FIELDS = [
    *LEGACY_FUNDING_FIELDS,
    "observed_at", "settlement_time", "source", "status",
    "forecast_rate", "realized_rate",
]
FUNDING_FORECAST_SOURCE = "/api/v5/public/funding-rate"
FUNDING_REALIZED_SOURCE = "/api/v5/public/funding-rate-history"
HOURLY_FIELDS["funding"] = FUNDING_FIELDS

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

    @staticmethod
    def _row_key(series: str, row: dict) -> tuple[str, ...]:
        instrument = str(row.get("inst_id") or row.get("ccy"))
        if series != "funding":
            return str(row.get("ts")), instrument

        # A forecast changes as its settlement approaches.  Its settlement
        # timestamp therefore cannot be its identity: doing that retained the
        # first estimate and silently discarded every later observation.
        status = str(row.get("status") or "legacy")
        source = str(row.get("source") or "legacy")
        settlement = str(row.get("settlement_time") or row.get("ts"))
        if status == "forecast":
            return (source, status, str(row.get("observed_at")), settlement,
                    instrument)
        # A realized history row is one immutable result per settlement.
        return source, status, settlement, instrument

    @staticmethod
    def _upgrade_legacy_funding_header(path: Path, fields: list[str]) -> None:
        """Add provenance columns without discarding an existing day file."""
        if not path.exists():
            return
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            existing = reader.fieldnames or []
            if existing == fields:
                return
            if existing != LEGACY_FUNDING_FIELDS:
                raise ValueError(
                    f"unsupported funding CSV header in {path}: {existing}")
            rows = list(reader)

        temporary = path.with_suffix(path.suffix + ".schema.tmp")
        try:
            with temporary.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

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
        if series == "funding":
            self._upgrade_legacy_funding_header(path, fields)
        # History returns the latest settlements on every poll.  Funding
        # identity must therefore span day partitions or every new UTC day
        # would write the same realized rows again.
        seen_name = series if series == "funding" else f"{series}:{day}"
        key_set = self.seen.setdefault(seen_name, set())
        if not key_set:
            existing_paths = (
                sorted((self.out / series).glob("*.csv"))
                if series == "funding" else [path]
            )
            for existing_path in existing_paths:
                if not existing_path.exists():
                    continue
                try:
                    with existing_path.open() as handle:
                        for row in csv.DictReader(handle):
                            key_set.add(self._row_key(series, row))
                except Exception:
                    pass
        fresh = []
        for row in rows:
            key = self._row_key(series, row)
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
            data = self.get(FUNDING_FORECAST_SOURCE,
                            {"instId": inst_id}) or []
            # Record when this process actually received the snapshot.  The
            # endpoint's `ts` is an upstream update time, not our observation.
            observed_at = int(time.time() * 1000)
            for item in data:
                try:
                    settlement_time = int(item["fundingTime"])
                except (KeyError, TypeError, ValueError):
                    continue
                forecast_rate = item.get("fundingRate")
                if forecast_rate in (None, ""):
                    continue
                rows.append({
                    # `ts` and `funding_rate` remain compatibility aliases.
                    "ts": settlement_time,
                    "inst_id": inst_id,
                    "funding_rate": forecast_rate,
                    "next_funding_time": item.get("nextFundingTime"),
                    "observed_at": observed_at,
                    "settlement_time": settlement_time,
                    "source": FUNDING_FORECAST_SOURCE,
                    "status": "forecast",
                    "forecast_rate": forecast_rate,
                    "realized_rate": "",
                })

            # The current-rate endpoint is a moving forecast.  Poll history
            # separately so settled rates are not inferred from that forecast.
            history = self.get(
                FUNDING_REALIZED_SOURCE,
                {"instId": inst_id, "limit": "100"}) or []
            observed_at = int(time.time() * 1000)
            for item in history:
                try:
                    settlement_time = int(item["fundingTime"])
                except (KeyError, TypeError, ValueError):
                    continue
                realized_rate = item.get("realizedRate")
                if realized_rate in (None, ""):
                    # Never infer a settled value from another response field.
                    continue
                rows.append({
                    "ts": settlement_time,
                    "inst_id": item.get("instId") or inst_id,
                    "funding_rate": realized_rate,
                    "next_funding_time": "",
                    "observed_at": observed_at,
                    "settlement_time": settlement_time,
                    "source": FUNDING_REALIZED_SOURCE,
                    "status": "realized",
                    # A history row is not the forecast snapshot observed
                    # before settlement. Keep that evidence in forecast rows.
                    "forecast_rate": "",
                    "realized_rate": realized_rate,
                })
        written["funding"] = self.append(
            "funding", HOURLY_FIELDS["funding"], rows)

        # Filled liquidation orders. flush-fade v1 inferred forced flow from
        # open interest falling and was falsified; this is the event itself,
        # with size, side and price. Deduplication in append() makes the
        # short poll interval safe.
        rows = []
        for inst_id in instruments:
            underlying = "-".join(inst_id.split("-")[:2])
            data = self.get("/api/v5/public/liquidation-orders",
                            {"instType": "SWAP", "state": "filled",
                             "uly": underlying}) or []
            for item in data:
                for detail in item.get("details") or []:
                    rows.append({
                        "ts": int(detail.get("ts")
                                  or detail.get("time") or 0),
                        "inst_id": item.get("instId") or inst_id,
                        "side": detail.get("side"),
                        "pos_side": detail.get("posSide"),
                        "bk_px": detail.get("bkPx"),
                        "sz": detail.get("sz"),
                    })
        written["liquidations"] = self.append(
            "liquidations", HOURLY_FIELDS["liquidations"], rows)
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
        "funding/          forecasts plus separately polled realized rates; "
        "~97 day retention upstream\n"
        "\nFiles are day-partitioned CSVs. Forecast identity includes "
        "observed_at and settlement_time; realized rows are deduplicated by "
        "settlement and instrument.\n")

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
