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

    nohup python research/record_flow.py --config config.yaml &

Storage is roughly 20 MB per month at the defaults.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

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
DEFAULT_COLLECTOR_WORKERS = 4
MAX_COLLECTOR_WORKERS = 32
DEFAULT_COLLECTOR_CONFIG = {
    "enabled": True,
    "out": Path("runtime/research/recorded"),
    "top_n": 12,
    "min_volume_usd": 30e6,
    "book_interval_seconds": 300,
    "hourly_interval_seconds": 900,
    "refresh_minutes": 60,
    "workers": DEFAULT_COLLECTOR_WORKERS,
}

BOOK_FIELDS = [
    "ts", "inst_id", "mid", "bid", "ask", "spread_bps",
    "bid_sz_top", "ask_sz_top", "book_levels", "book_span_bps",
    *[f"bid_depth_{b}bps" for b in DEPTH_BANDS_BPS],
    *[f"ask_depth_{b}bps" for b in DEPTH_BANDS_BPS],
    "imbalance_top", "imbalance_5bps",
    "observed_at", "available_at", "source", "feed", "schema",
    "revision_id",
]
# The discovery replay consumes only closed one-minute execution bars.  OKX's
# market/candles response is [ts, open, high, low, close, volume, ...,
# confirm]; ``confirm=1`` is persisted as ``closed=true`` so an in-progress
# candle can never become an outcome bar through an as-of join.
EXECUTION_BAR_FIELDS = [
    "ts", "inst_id", "open", "high", "low", "close", "volume", "closed",
    "observed_at", "available_at", "source", "feed", "schema", "revision_id",
]
HOURLY_FIELDS = {
    "long_short_ratio": ["ts", "ccy", "long_short_ratio",
                          "observed_at", "available_at", "source", "feed",
                          "schema", "revision_id"],
    "taker_volume": ["ts", "ccy", "sell_vol", "buy_vol",
                      "observed_at", "available_at", "source", "feed",
                      "schema", "revision_id"],
    "open_interest": ["ts", "inst_id", "oi_contracts", "oi_ccy", "oi_usd",
                       "observed_at", "available_at", "source", "feed",
                       "schema", "revision_id"],
    # Actual filled liquidations: the DIRECT observation of the forced-flow
    # mechanism, rather than the open-interest proxy that flush-fade v1 used
    # and that was falsified. OKX serves only a short recent window, so this
    # exists only if it is recorded.
    "liquidations": ["ts", "inst_id", "side", "pos_side", "bk_px", "sz",
                     "observed_at", "available_at", "source", "feed",
                     "schema", "revision_id"],
}

# Keep these four columns first and unchanged: existing exports and readers
# use them.  The additional columns distinguish a forecast observed before a
# settlement from the rate OKX later says was actually settled.
LEGACY_FUNDING_FIELDS = [
    "ts", "inst_id", "funding_rate", "next_funding_time",
]
FUNDING_FIELDS = [
    *LEGACY_FUNDING_FIELDS,
    "observed_at", "available_at", "settlement_time", "source", "feed",
    "schema", "revision_id", "status", "forecast_rate", "realized_rate",
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
    def __init__(self, out: Path, *, workers: int = DEFAULT_COLLECTOR_WORKERS):
        self.out = out
        self.session = requests.Session()
        self._thread_local = threading.local()
        self._append_lock = threading.Lock()
        self._contract_size_lock = threading.Lock()
        try:
            requested_workers = int(workers)
        except (TypeError, ValueError):
            requested_workers = DEFAULT_COLLECTOR_WORKERS
        self.workers = max(1, min(MAX_COLLECTOR_WORKERS, requested_workers))
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

    def _session(self) -> requests.Session:
        """Return one HTTP session per request worker.

        ``requests.Session`` is not documented as thread-safe. The collector
        can parallelize public reads, so each worker gets its own connection
        pool while the durable CSV append remains serialized below.
        """
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return session

    # Plumbing

    def get(self, path: str, params: dict) -> list | dict | None:
        for attempt in range(3):
            try:
                response = self._session().get(
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
    def _payload_hash(row: dict) -> str:
        """Hash the observed payload, excluding local provenance columns.

        Upstream timestamps are often unchanged while values are revised. A
        payload hash lets append() retain such revisions while still
        suppressing an identical poll on restart.
        """
        excluded = {
            "observed_at", "available_at", "revision_id", "quality",
        }
        payload = {str(k): row.get(k) for k in sorted(row) if k not in excluded}
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _row_key(series: str, row: dict) -> tuple[str, ...]:
        instrument = str(row.get("inst_id") or row.get("ccy"))
        source = str(row.get("source") or "legacy")
        feed = str(row.get("feed") or "legacy")
        schema = str(row.get("schema") or "legacy_unknown")
        payload_hash = Recorder._payload_hash(row)
        if series != "funding":
            return (source, feed, schema, str(row.get("ts")), instrument,
                    payload_hash)

        # A forecast changes as settlement approaches. Using settlement time
        # as identity would retain only the first estimate.
        status = str(row.get("status") or "legacy")
        settlement = str(row.get("settlement_time") or row.get("ts"))
        if status == "forecast":
            return (source, feed, schema, status, str(row.get("observed_at")),
                    settlement, instrument, payload_hash)
        # A realized history row is one immutable result per settlement.
        return source, feed, schema, status, settlement, instrument, payload_hash

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
        with self._append_lock:
            if not rows:
                return 0
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = self.out / series / f"{day}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            if series == "funding":
                self._upgrade_legacy_funding_header(path, fields)
            # History returns the latest settlements on every poll. Funding
            # identity must therefore span day partitions or every new UTC
            # day would write the same realized rows again.
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
                # Normalize programmatic rows to the declared CSV schema so
                # a legacy row with newly added blank columns deduplicates
                # exactly like a row read back through DictReader.
                row = {field: row.get(field, "") for field in fields}
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

    # Capture

    @staticmethod
    def _annotate(row: dict, *, observed_at: int, source: str,
                  feed: str = "okx-public",
                  schema: str = "record_flow.v1") -> dict:
        """Attach local receipt/provenance without changing legacy values.

        ``available_at`` is deliberately local receipt time.  We never use a
        file mtime or an upstream event timestamp as a claim of availability.
        """
        result = dict(row)
        result.update({
            "observed_at": int(observed_at),
            "available_at": int(observed_at),
            "source": source,
            "feed": feed,
            "schema": schema,
        })
        result["revision_id"] = Recorder._payload_hash(result)
        return result

    def order_book(self, inst_id: str) -> dict | None:
        data = self.get("/api/v5/market/books",
                        {"instId": inst_id, "sz": BOOK_LEVELS})
        if not data:
            return None
        # Receipt time is taken after the response is available.  Taking it
        # before the request would make a slow request appear available in
        # the past and weaken strict as-of joins.
        observed_at = int(time.time() * 1000)
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
            with self._contract_size_lock:
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
        return self._annotate(
            row, observed_at=observed_at, source="/api/v5/market/books")

    def capture_books(self, instruments: list[str]) -> int:
        if self.workers <= 1 or len(instruments) <= 1:
            fetched = [self.order_book(i) for i in instruments]
        else:
            # Only public reads run in parallel. ``append`` is called once,
            # below, so a wider collector cannot interleave durable writes.
            with ThreadPoolExecutor(
                    max_workers=min(self.workers, len(instruments)),
                    thread_name_prefix="research-book") as pool:
                fetched = list(pool.map(self.order_book, instruments))
        rows = [row for row in fetched if row]
        return self.append("order_book", BOOK_FIELDS, rows)

    def execution_bars(self, inst_id: str) -> list[dict]:
        """Read and retain only closed 1m candles for offline replay."""
        data = self.get("/api/v5/market/candles",
                        {"instId": inst_id, "bar": "1m", "limit": "100"}) or []
        observed_at = int(time.time() * 1000)
        rows: list[dict] = []
        for item in data:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            # The last field is OKX's confirmation marker.  Do not infer a
            # closed candle when the exchange omits it.
            if len(item) < 9 or str(item[8]).strip() not in {"1", "true", "True"}:
                continue
            try:
                ts = int(item[0])
                values = [float(item[index]) for index in range(1, 6)]
            except (TypeError, ValueError):
                continue
            open_price, high, low, close, volume = values
            if min(open_price, high, low, close) <= 0 or high < low:
                continue
            rows.append(self._annotate({
                "ts": ts, "inst_id": inst_id, "open": open_price,
                "high": high, "low": low, "close": close,
                "volume": volume, "closed": True,
            }, observed_at=observed_at,
                source="/api/v5/market/candles"))
        return rows

    def capture_execution_bars(self, instruments: list[str]) -> int:
        """Append closed execution bars without exposing network state to discovery."""
        rows: list[dict] = []
        for inst_id in instruments:
            rows.extend(self.execution_bars(inst_id))
        return self.append("execution_bar_1m", EXECUTION_BAR_FIELDS, rows)

    def capture_hourly(self, instruments: list[str],
                       currencies: list[str]) -> dict[str, int]:
        written: dict[str, int] = {}

        rows = []
        for ccy in currencies:
            data = self.get(
                "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                {"ccy": ccy, "period": "1H", "limit": "6"}) or []
            observed_at = int(time.time() * 1000)
            rows += [self._annotate(
                {"ts": int(r[0]), "ccy": ccy,
                 "long_short_ratio": float(r[1])},
                observed_at=observed_at,
                source="/api/v5/rubik/stat/contracts/long-short-account-ratio")
                     for r in data]
        written["long_short_ratio"] = self.append(
            "long_short_ratio", HOURLY_FIELDS["long_short_ratio"], rows)

        rows = []
        for ccy in currencies:
            data = self.get("/api/v5/rubik/stat/taker-volume",
                            {"ccy": ccy, "instType": "CONTRACTS",
                             "period": "1H", "limit": "6"}) or []
            observed_at = int(time.time() * 1000)
            rows += [self._annotate(
                {"ts": int(r[0]), "ccy": ccy,
                 "sell_vol": float(r[1]), "buy_vol": float(r[2])},
                observed_at=observed_at,
                source="/api/v5/rubik/stat/taker-volume") for r in data]
        written["taker_volume"] = self.append(
            "taker_volume", HOURLY_FIELDS["taker_volume"], rows)

        rows = []
        for inst_id in instruments:
            data = self.get("/api/v5/public/open-interest",
                            {"instType": "SWAP", "instId": inst_id}) or []
            for item in data:
                rows.append(self._annotate({
                    "ts": int(item.get("ts") or time.time() * 1000),
                    "inst_id": inst_id,
                    "oi_contracts": item.get("oi"),
                    "oi_ccy": item.get("oiCcy"),
                    "oi_usd": item.get("oiUsd"),
                }, observed_at=int(time.time() * 1000),
                    source="/api/v5/public/open-interest"))
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
                rows.append(self._annotate({
                    # `ts` and `funding_rate` remain compatibility aliases.
                    "ts": settlement_time,
                    "inst_id": inst_id,
                    "funding_rate": forecast_rate,
                    "next_funding_time": item.get("nextFundingTime"),
                    "observed_at": observed_at,
                    "available_at": observed_at,
                    "settlement_time": settlement_time,
                    "source": FUNDING_FORECAST_SOURCE,
                    "feed": "okx-public",
                    "schema": "record_flow.v1",
                    "status": "forecast",
                    "forecast_rate": forecast_rate,
                    "realized_rate": "",
                }, observed_at=observed_at, source=FUNDING_FORECAST_SOURCE))

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
                rows.append(self._annotate({
                    "ts": settlement_time,
                    "inst_id": item.get("instId") or inst_id,
                    "funding_rate": realized_rate,
                    "next_funding_time": "",
                    "observed_at": observed_at,
                    "available_at": observed_at,
                    "settlement_time": settlement_time,
                    "source": FUNDING_REALIZED_SOURCE,
                    "feed": "okx-public",
                    "schema": "record_flow.v1",
                    "status": "realized",
                    # A history row is not the forecast snapshot observed
                    # before settlement. Keep that evidence in forecast rows.
                    "forecast_rate": "",
                    "realized_rate": realized_rate,
                }, observed_at=observed_at, source=FUNDING_REALIZED_SOURCE))
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
                    rows.append(self._annotate({
                        "ts": int(detail.get("ts")
                                  or detail.get("time") or 0),
                        "inst_id": item.get("instId") or inst_id,
                        "side": detail.get("side"),
                        "pos_side": detail.get("posSide"),
                        "bk_px": detail.get("bkPx"),
                        "sz": detail.get("sz"),
                    }, observed_at=int(time.time() * 1000),
                        source="/api/v5/public/liquidation-orders"))
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


def _collector_number(value, *, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"collector.{name} must be a number")
    value = float(value)
    if not lo <= value <= hi:
        raise ValueError(
            f"collector.{name} must be between {lo:g} and {hi:g}")
    return value


def _collector_integer(value, *, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"collector.{name} must be an integer")
    if not lo <= value <= hi:
        raise ValueError(f"collector.{name} must be between {lo} and {hi}")
    return value


def load_collector_config_from_mapping(raw: dict) -> dict:
    """Validate one merged research-only collector settings mapping."""
    allowed = set(DEFAULT_COLLECTOR_CONFIG)
    unknown = sorted(set(raw or {}) - allowed)
    if unknown:
        raise ValueError(
            "collector has unknown field(s): " + ", ".join(unknown))
    settings = dict(DEFAULT_COLLECTOR_CONFIG)
    settings.update(raw or {})

    enabled = settings.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("collector.enabled must be true or false")
    out = settings.get("out")
    if not isinstance(out, (str, Path)) or not str(out).strip():
        raise ValueError("collector.out must be a path string")
    settings["out"] = Path(out)
    settings["top_n"] = _collector_integer(
        settings.get("top_n"), name="top_n", lo=1, hi=500)
    settings["min_volume_usd"] = _collector_number(
        settings.get("min_volume_usd"), name="min_volume_usd", lo=0,
        hi=1_000_000_000_000)
    settings["book_interval_seconds"] = _collector_integer(
        settings.get("book_interval_seconds"), name="book_interval_seconds",
        lo=1, hi=86_400)
    settings["hourly_interval_seconds"] = _collector_integer(
        settings.get("hourly_interval_seconds"),
        name="hourly_interval_seconds", lo=60, hi=7 * 86_400)
    settings["refresh_minutes"] = _collector_integer(
        settings.get("refresh_minutes"), name="refresh_minutes", lo=1,
        hi=7 * 24 * 60)
    settings["workers"] = _collector_integer(
        settings.get("workers"), name="workers", lo=1,
        hi=MAX_COLLECTOR_WORKERS)
    return settings


def load_collector_config(path: Path | None = None) -> dict:
    """Load research-only collector settings without touching trading config.

    The collector reads public market endpoints and writes its own CSV tree;
    it never constructs ``Engine``/``RiskEngine`` or receives exchange order
    credentials. Keeping these settings under ``research.collector`` lets the
    recorder widen its universe/cadence without changing ``universe`` or any
    active account risk policy.
    """
    raw_settings = {}
    if path is not None and Path(path).is_file():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        research = raw.get("research") or {}
        if not isinstance(research, dict):
            raise ValueError("config.research must be a mapping")
        collector = research.get("collector") or {}
        if not isinstance(collector, dict):
            raise ValueError("config.research.collector must be a mapping")
        raw_settings = collector
    return load_collector_config_from_mapping(raw_settings)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"),
                        help="config containing research.collector settings")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--book-interval", type=int, default=None,
                        help="seconds between order-book snapshots")
    parser.add_argument("--hourly-interval", type=int, default=None,
                        help="seconds between hourly-series polls")
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--min-volume-usd", type=float, default=None)
    parser.add_argument("--refresh-minutes", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None,
                        help=f"bounded public-read workers (1-{MAX_COLLECTOR_WORKERS})")
    args = parser.parse_args()

    settings = load_collector_config(args.config)
    overrides = {
        "out": args.out,
        "book_interval_seconds": args.book_interval,
        "hourly_interval_seconds": args.hourly_interval,
        "top_n": args.top_n,
        "min_volume_usd": args.min_volume_usd,
        "refresh_minutes": args.refresh_minutes,
        "workers": args.workers,
    }
    settings.update({key: value for key, value in overrides.items()
                     if value is not None})
    # Revalidate CLI overrides using the same bounded collector contract.
    settings = load_collector_config_from_mapping(settings)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if not settings["enabled"]:
        log.info("research collector disabled by config")
        return 0

    recorder = Recorder(settings["out"], workers=settings["workers"])
    instruments: list[str] = []
    universe_at = 0.0
    last_hourly = 0.0
    (settings["out"] / "README.txt").write_text(
        "OKX market data recorded because the exchange deletes it.\n"
        "order_book/       depth snapshots; never served historically\n"
        "execution_bar_1m/ closed 1m OHLC bars for outcome replay\n"
        "long_short_ratio/ retail positioning; ~30 day retention upstream\n"
        "taker_volume/     aggressor flow;    ~30 day retention upstream\n"
        "open_interest/    ~60 day retention upstream\n"
        "funding/          forecasts plus separately polled realized rates; "
        "~97 day retention upstream\n"
        "\nFiles are day-partitioned CSVs. Forecast identity includes "
        "observed_at and settlement_time; realized rows are deduplicated by "
        "settlement and instrument.\n")

    log.info("recording to %s", settings["out"])
    while _running:
        cycle_start = time.time()
        try:
            if cycle_start - universe_at > settings["refresh_minutes"] * 60:
                found = discover(recorder, settings["top_n"],
                                 settings["min_volume_usd"])
                if found:
                    instruments = found
                    universe_at = cycle_start
                    log.info("universe (%d): %s", len(instruments),
                             ", ".join(instruments))
            if not instruments:
                time.sleep(30)
                continue

            written = recorder.capture_books(instruments)
            bars_written = recorder.capture_execution_bars(instruments)
            if (cycle_start - last_hourly
                    > settings["hourly_interval_seconds"]):
                currencies = sorted({i.split("-")[0] for i in instruments})
                hourly = recorder.capture_hourly(instruments, currencies)
                last_hourly = cycle_start
                log.info("books=%d execution_bars=%d hourly=%s", written,
                         bars_written,
                         json.dumps(hourly, separators=(",", ":")))
            else:
                log.info("books=%d execution_bars=%d", written, bars_written)
        except Exception as exc:
            # A recorder that dies on a bad response loses the data it exists
            # to protect. Log and keep going.
            log.warning("cycle failed: %s", exc)

        elapsed = time.time() - cycle_start
        for _ in range(int(max(
                1.0, settings["book_interval_seconds"] - elapsed))):
            if not _running:
                break
            time.sleep(1)
    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
