"""Market data: fetch, cache and align bars into a leg-aware panel.

Every trading day ``t`` is split into two legs, because the decision points of
a daily strategy are the opening and the closing auction:

* overnight ``on[t]``: close of ``t-1`` to open of ``t``, dividend included
  (a dividend goes ex at the open, so its holder is the overnight holder);
* intraday ``id[t]``: open of ``t`` to close of ``t``.

``(1 + on) * (1 + id)`` equals the close-to-close total return up to the
dividend's reinvestment price.  Signals are computed on a total-return index
built from the same close-to-close returns, so every signal is invariant to
the price scale and to how a provider back-adjusts history.

Sources:

* ``yahoo``: the public chart endpoint.  Consolidated prices, daily since each
  fund's inception and hourly for the last 730 days.  Diagnostic quality: an
  unofficial endpoint with no service guarantee.
* ``alpaca``: Alpaca historical bars (consolidated SIP feed, dividend-adjusted)
  when ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY`` are set.  Daily history
  starts in 2016.  Keys are only ever sent to the HTTPS Alpaca data host.

The risk-free rate is the 13-week Treasury bill yield (``^IRX``), applied to
each leg pro rata to its calendar time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

NY = ZoneInfo("America/New_York")
UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI", "XLP", "XLY",
    "XLU", "XLB", "XLRE", "VTI", "VO", "VB", "EFA", "EEM", "TLT", "HYG", "GLD",
    "SLV", "SMH",
)
RF_SYMBOL = "^IRX"
CALENDAR_SYMBOL = "SPY"
YAHOO_HOST = "query1.finance.yahoo.com"
ALPACA_DATA_HOST = "data.alpaca.markets"
# Regular-session hours as a share of a calendar day, for splitting the bill
# rate between the legs.
INTRADAY_DAY_FRACTION = 6.5 / 24.0
DEFAULT_CACHE = Path("runtime/edgefinder/cache")


class DataError(RuntimeError):
    """Raised when data cannot be fetched or fails validation."""


# --------------------------------------------------------------------------
# fetching and caching


def _http_json(url: str, *, headers: Mapping[str, str] | None = None,
               attempts: int = 4, timeout: float = 60.0) -> Any:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise DataError(f"refusing a non-HTTPS URL: {url}")
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           **(headers or {})})
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as error:  # network errors are retried, then raised
            last = error
            time.sleep(2.0 * (attempt + 1))
    raise DataError(f"fetch failed after {attempts} attempts: {url}: {last}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it could carry credential headers elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


def _ny_date(timestamp: int) -> dt.date:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone(NY).date()


def _ny_datetime(timestamp: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone(NY)


def parse_yahoo_daily(payload: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    """Normalise a chart payload into ``rows`` and ``dividends`` keyed by ISO date."""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise DataError(f"{symbol}: malformed chart payload") from error
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    rows = []
    for i, stamp in enumerate(timestamps):
        values = [quote[k][i] for k in ("open", "high", "low", "close")]
        if any(v is None or not math.isfinite(v) or v <= 0 for v in values):
            continue
        rows.append([_ny_date(stamp).isoformat(), *map(float, values),
                     float(adj[i]) if adj and adj[i] is not None else None,
                     float(quote["volume"][i] or 0.0)])
    dividends: dict[str, float] = {}
    for event in (result.get("events") or {}).get("dividends", {}).values():
        day = _ny_date(int(event["date"])).isoformat()
        dividends[day] = dividends.get(day, 0.0) + float(event["amount"])
    splits = {_ny_date(int(e["date"])).isoformat(): f"{e.get('numerator')}:{e.get('denominator')}"
              for e in (result.get("events") or {}).get("splits", {}).values()}
    return {"symbol": symbol, "rows": rows, "dividends": dividends, "splits": splits}


def drop_incomplete_today(rows: list[list[Any]], now: dt.datetime | None = None,
                          *, settle_minutes: int = 20) -> list[list[Any]]:
    """Remove today's bar while the session may still be open or settling."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
    if not rows:
        return rows
    cutoff = now.replace(hour=16, minute=settle_minutes, second=0, microsecond=0)
    if rows[-1][0] == now.date().isoformat() and now < cutoff:
        return rows[:-1]
    return rows


def fetch_yahoo_daily(symbol: str, cache_dir: Path = DEFAULT_CACHE, *, refresh: bool = False,
                      max_age_hours: float = 12.0) -> dict[str, Any]:
    """Daily bars since inception, cached as JSON under ``cache_dir``."""
    path = Path(cache_dir) / "yahoo-1d" / f"{_safe(symbol)}.json"
    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(cached.get("fetched_epoch", 0))
        if age < max_age_hours * 3600:
            return cached
    query = urllib.parse.urlencode({"interval": "1d", "period1": 0, "period2": int(time.time()),
                                    "events": "div,splits", "includeAdjustedClose": "true"})
    payload = _http_json(f"https://{YAHOO_HOST}/v8/finance/chart/"
                         f"{urllib.parse.quote(symbol)}?{query}")
    parsed = parse_yahoo_daily(payload, symbol)
    parsed["rows"] = drop_incomplete_today(parsed["rows"])
    parsed.update(source="yahoo-chart-1d", fetched_epoch=time.time(),
                  fetched_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    _write_json(path, parsed)
    return parsed


def fetch_yahoo_hourly(symbol: str, cache_dir: Path = DEFAULT_CACHE, *, refresh: bool = False,
                       max_age_hours: float = 12.0, days: int = 729) -> dict[str, Any]:
    """Regular-session 60-minute bars (09:30 ... 15:30 starts) for the last ``days``."""
    path = Path(cache_dir) / "yahoo-60m" / f"{_safe(symbol)}.json"
    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(cached.get("fetched_epoch", 0)) < max_age_hours * 3600:
            return cached
    now = int(time.time())
    query = urllib.parse.urlencode({"interval": "60m", "period1": now - days * 86400,
                                    "period2": now, "includePrePost": "false"})
    payload = _http_json(f"https://{YAHOO_HOST}/v8/finance/chart/"
                         f"{urllib.parse.quote(symbol)}?{query}")
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    bars = []
    for i, stamp in enumerate(result.get("timestamp") or []):
        values = [quote[k][i] for k in ("open", "high", "low", "close")]
        if any(v is None or v <= 0 for v in values):
            continue
        when = _ny_datetime(stamp)
        minute = when.hour * 60 + when.minute - 570
        if not 0 <= minute < 390:
            continue
        bars.append([when.date().isoformat(), minute, *map(float, values),
                     float(quote["volume"][i] or 0.0)])
    data = {"symbol": symbol, "bars": bars, "source": "yahoo-chart-60m",
            "fetched_epoch": time.time(),
            "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    _write_json(path, data)
    return data


def fetch_alpaca_daily(symbol: str, start: dt.date, end: dt.date | None = None, *,
                       feed: str = "sip") -> dict[str, Any]:
    """Dividend- and split-adjusted daily bars from Alpaca (needs API keys).

    The free plan serves SIP history that ends at least 15 minutes ago, which
    is all a daily decision needs.  Adjusted prices carry dividends inside the
    price series, so ``dividends`` is empty and legs are price ratios.
    """
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise DataError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for alpaca data")
    end = end or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)).date()
    rows: list[list[Any]] = []
    token = None
    while True:
        params = {"symbols": symbol, "timeframe": "1Day", "adjustment": "all", "feed": feed,
                  "start": start.isoformat(), "end": end.isoformat(), "limit": 10000}
        if token:
            params["page_token"] = token
        payload = _http_json(f"https://{ALPACA_DATA_HOST}/v2/stocks/bars?"
                             f"{urllib.parse.urlencode(params)}",
                             headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        for bar in (payload.get("bars") or {}).get(symbol, []):
            stamp = dt.datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
            rows.append([stamp.astimezone(NY).date().isoformat(), float(bar["o"]), float(bar["h"]),
                         float(bar["l"]), float(bar["c"]), float(bar["c"]), float(bar["v"])])
        token = payload.get("next_page_token")
        if not token:
            break
    return {"symbol": symbol, "rows": drop_incomplete_today(rows), "dividends": {}, "splits": {},
            "source": f"alpaca-{feed}-1d-adjusted", "fetched_epoch": time.time(),
            "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat()}


def fetch_daily(symbols: Iterable[str], cache_dir: Path = DEFAULT_CACHE, *,
                refresh: bool = False, provider: str = "yahoo",
                alpaca_start: dt.date = dt.date(2016, 1, 1)) -> dict[str, dict[str, Any]]:
    """Fetch every symbol plus the bill rate, from cache where fresh."""
    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        if provider == "alpaca" and symbol != RF_SYMBOL:
            out[symbol] = fetch_alpaca_daily(symbol, alpaca_start)
        else:
            out[symbol] = fetch_yahoo_daily(symbol, cache_dir, refresh=refresh)
    if RF_SYMBOL not in out:
        out[RF_SYMBOL] = fetch_yahoo_daily(RF_SYMBOL, cache_dir, refresh=refresh)
    return out


def _safe(symbol: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in symbol)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# the panel


@dataclass
class Panel:
    """Aligned daily arrays, shape ``(T, S)``; ``NaN`` where a fund has no bar."""

    dates: list[dt.date]
    symbols: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    dividend: np.ndarray
    rf_rate: np.ndarray            # annual bill yield (decimal) known at the close of t-1
    sources: dict[str, str] = field(default_factory=dict)
    # Trading dates known beyond the last bar (the live calendar); used only by
    # calendar signals that must know the next session in advance.
    future_dates: list[dt.date] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._derive()

    def _derive(self) -> None:
        prev_close = np.vstack([np.full((1, len(self.symbols)), np.nan), self.close[:-1]])
        with np.errstate(invalid="ignore", divide="ignore"):
            self.ret_on = (self.open + self.dividend) / prev_close - 1.0
            self.ret_id = self.close / self.open - 1.0
            self.ret_cc = (self.close + self.dividend) / prev_close - 1.0
        days = np.array([0] + [(b - a).days for a, b in zip(self.dates[:-1], self.dates[1:])],
                        dtype=float)
        self.rf_cc = self.rf_rate * days / 365.0
        self.rf_id = np.where(days > 0, self.rf_rate * INTRADAY_DAY_FRACTION / 365.0, 0.0)
        self.rf_on = self.rf_cc - self.rf_id
        growth = np.where(np.isfinite(self.ret_cc), 1.0 + self.ret_cc, 1.0)
        tri = np.cumprod(growth, axis=0)
        self.tri = np.where(np.isfinite(self.close), tri, np.nan)

    @property
    def index(self) -> dict[str, int]:
        return {s: i for i, s in enumerate(self.symbols)}

    def slice_dates(self, start: dt.date | None, end: dt.date | None) -> "Panel":
        keep = [i for i, d in enumerate(self.dates)
                if (start is None or d >= start) and (end is None or d <= end)]
        if not keep:
            raise DataError("empty date window")
        lo, hi = keep[0], keep[-1] + 1
        return Panel(self.dates[lo:hi], list(self.symbols), self.open[lo:hi], self.high[lo:hi],
                     self.low[lo:hi], self.close[lo:hi], self.volume[lo:hi],
                     self.dividend[lo:hi], self.rf_rate[lo:hi], dict(self.sources),
                     list(self.future_dates))

    def digest(self) -> str:
        """sha256 over dates, symbols and every price array (reproducibility)."""
        h = hashlib.sha256()
        h.update(json.dumps([d.isoformat() for d in self.dates]).encode())
        h.update(json.dumps(self.symbols).encode())
        for array in (self.open, self.high, self.low, self.close, self.dividend, self.rf_rate):
            h.update(np.nan_to_num(np.round(array, 8), nan=-1.0).tobytes())
        return h.hexdigest()


def build_panel(raw: Mapping[str, Mapping[str, Any]], symbols: Sequence[str] | None = None, *,
                start: dt.date | None = None, end: dt.date | None = None,
                calendar_symbol: str = CALENDAR_SYMBOL) -> Panel:
    """Align raw per-symbol data on the calendar symbol's trading dates."""
    symbols = [s for s in (symbols or [k for k in raw if k != RF_SYMBOL])]
    if calendar_symbol not in raw:
        raise DataError(f"calendar symbol {calendar_symbol} missing")
    calendar = sorted({row[0] for row in raw[calendar_symbol]["rows"]})
    dates = [dt.date.fromisoformat(d) for d in calendar]
    position = {d: i for i, d in enumerate(calendar)}
    shape = (len(dates), len(symbols))
    arrays = {k: np.full(shape, np.nan) for k in ("open", "high", "low", "close", "volume")}
    dividend = np.zeros(shape)
    sources = {}
    for j, symbol in enumerate(symbols):
        data = raw[symbol]
        sources[symbol] = str(data.get("source", "unknown"))
        for row in data["rows"]:
            i = position.get(row[0])
            if i is None:
                continue
            arrays["open"][i, j], arrays["high"][i, j], arrays["low"][i, j] = row[1], row[2], row[3]
            arrays["close"][i, j], arrays["volume"][i, j] = row[4], row[6]
        for day, amount in data.get("dividends", {}).items():
            i = position.get(day)
            if i is not None and math.isfinite(arrays["close"][i, j]):
                dividend[i, j] += amount
    rf = _rf_series(raw.get(RF_SYMBOL), calendar)
    panel = Panel(dates, symbols, arrays["open"], arrays["high"], arrays["low"], arrays["close"],
                  arrays["volume"], dividend, rf, sources)
    return panel.slice_dates(start, end) if (start or end) else panel


def _rf_series(raw: Mapping[str, Any] | None, calendar: Sequence[str]) -> np.ndarray:
    """Bill yield known at the previous close, forward-filled; zero if unavailable."""
    if not raw:
        return np.zeros(len(calendar))
    by_day = {row[0]: row[4] for row in raw["rows"]}
    out = np.zeros(len(calendar))
    last = 0.0
    previous = 0.0
    for i, day in enumerate(calendar):
        out[i] = previous / 100.0           # the rate known at the close of t-1
        if day in by_day and math.isfinite(by_day[day]):
            last = by_day[day]
        previous = last
    return out


def quality_report(raw: Mapping[str, Mapping[str, Any]], panel: Panel) -> dict[str, Any]:
    """Data checks: coverage, stale opens, extreme legs, and agreement with adjclose."""
    report: dict[str, Any] = {}
    for j, symbol in enumerate(panel.symbols):
        close, open_ = panel.close[:, j], panel.open[:, j]
        valid = np.isfinite(close)
        if not valid.any():
            report[symbol] = {"bars": 0}
            continue
        first = int(np.argmax(valid))
        prev = np.concatenate([[np.nan], close[:-1]])
        both = np.isfinite(open_) & np.isfinite(prev)
        stale_open = float(np.mean(np.abs(open_[both] - prev[both]) < 1e-9)) if both.any() else None
        adj_dev = None
        rows = raw[symbol]["rows"]
        adj = {r[0]: r[5] for r in rows if r[5]}
        series = [(d.isoformat(), panel.ret_cc[i, j]) for i, d in enumerate(panel.dates)
                  if math.isfinite(panel.ret_cc[i, j])]
        deviations = []
        for (d0, _), (d1, r) in zip(series[:-1], series[1:]):
            if d0 in adj and d1 in adj and adj[d0] > 0:
                deviations.append(abs((adj[d1] / adj[d0] - 1.0) - r))
        if deviations:
            adj_dev = {"median_bps": float(np.median(deviations) * 1e4),
                       "p99_bps": float(np.percentile(deviations, 99) * 1e4),
                       "max_bps": float(np.max(deviations) * 1e4)}
        legs = np.concatenate([panel.ret_on[:, j][np.isfinite(panel.ret_on[:, j])],
                               panel.ret_id[:, j][np.isfinite(panel.ret_id[:, j])]])
        report[symbol] = {
            "first_date": panel.dates[first].isoformat(),
            "last_date": panel.dates[int(len(close) - 1 - np.argmax(valid[::-1]))].isoformat(),
            "bars": int(valid.sum()),
            "missing_after_inception": int((~valid[first:]).sum()),
            "stale_open_share": stale_open,
            "legs_beyond_15pct": int(np.sum(np.abs(legs) > 0.15)),
            "dividends": int(np.sum(panel.dividend[:, j] > 0)),
            "splits": raw[symbol].get("splits", {}),
            "adjclose_deviation": adj_dev,
            "source": panel.sources.get(symbol),
        }
    return report


# --------------------------------------------------------------------------
# hourly panel (last-half-hour studies)


@dataclass
class HourlyPanel:
    """Per session and symbol: prior close, first-hour close, 15:30 open, close."""

    dates: list[dt.date]
    symbols: list[str]
    prev_close: np.ndarray
    open: np.ndarray
    first_hour_close: np.ndarray
    last_bar_open: np.ndarray
    close: np.ndarray


def build_hourly_panel(raw: Mapping[str, Mapping[str, Any]], symbols: Sequence[str]) -> HourlyPanel:
    """Keep only sessions whose 09:30 and 15:30 bars both exist (full-length days)."""
    per: dict[str, dict[str, dict[int, list[float]]]] = {}
    for symbol in symbols:
        sessions: dict[str, dict[int, list[float]]] = {}
        for day, minute, o, h, l, c, v in raw[symbol]["bars"]:
            sessions.setdefault(day, {})[int(minute)] = [o, h, l, c, v]
        per[symbol] = sessions
    calendar = sorted(set().union(*[set(s) for s in per.values()]))
    shape = (len(calendar), len(symbols))
    arrays = {k: np.full(shape, np.nan) for k in ("open", "first", "last_open", "close")}
    for j, symbol in enumerate(symbols):
        for i, day in enumerate(calendar):
            bars = per[symbol].get(day)
            if not bars or 0 not in bars or 360 not in bars:
                continue
            arrays["open"][i, j] = bars[0][0]
            arrays["first"][i, j] = bars[0][3]
            arrays["last_open"][i, j] = bars[360][0]
            arrays["close"][i, j] = bars[360][3]
    prev = np.vstack([np.full((1, len(symbols)), np.nan), arrays["close"][:-1]])
    return HourlyPanel([dt.date.fromisoformat(d) for d in calendar], list(symbols), prev,
                       arrays["open"], arrays["first"], arrays["last_open"], arrays["close"])
