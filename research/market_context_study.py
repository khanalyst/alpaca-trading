"""Causal, diagnostic market context and synchronized factor measurements.

No output from this module changes a strategy, risk cap or proof. The universe
participation statistic describes only the supplied fixed symbol universe.
Prices are unadjusted observations; corporate actions and catalysts are not
inferred from an overnight gap.
"""
from collections import defaultdict
from datetime import datetime
import math
from statistics import median
from zoneinfo import ZoneInfo

from deploy.market_observations import epoch

NY = ZoneInfo("America/New_York")


def _complete(rows):
    if not rows:
        return False
    opened, closed = epoch(rows[0]["session_open"]), epoch(rows[0]["session_close"])
    return [r["timestamp"] for r in rows] == [opened + i * 60 for i in range(int((closed-opened)/60))]


def context_study(bars, *, as_of, symbol, universe, feed, allow_backfill=False):
    """Measure a prefix with a fixed universe and an explicit information cutoff."""
    cutoff = epoch(as_of)
    day = datetime.fromtimestamp(cutoff, NY).date().isoformat()
    configured = sorted(set(universe))
    if symbol not in configured or len(configured) > 64:
        raise ValueError("symbol must belong to a bounded fixed universe")
    groups = defaultdict(list)
    rejected = 0
    providers = set()
    seen = set()
    for raw in bars:
        if raw.get("symbol") not in configured or raw.get("feed") != feed:
            continue
        try:
            ts = epoch(raw["timestamp"])
            available = max(ts + 60, epoch(raw.get("as_of") or ts + 60))
            received = epoch(raw["observed_at"])
            historical = raw.get("source_mode") == "historical_backfill"
            if available > cutoff or (received > cutoff and not (allow_backfill and historical)):
                continue
            if historical and not allow_backfill:
                continue
            values = {key: float(raw[key]) for key in ("open", "high", "low", "close", "volume")}
            if any(not math.isfinite(v) or v < 0 for v in values.values()) or min(values[k] for k in ("open", "high", "low", "close")) <= 0:
                raise ValueError
            opened, closed = epoch(raw["session_open"]), epoch(raw["session_close"])
            if not opened <= ts < closed:
                continue
            provider = str(raw["provider"])
            identity = (raw["symbol"], provider, ts)
            if identity in seen:
                raise ValueError("duplicate bar revision; provide a resolved receipt-time view")
            seen.add(identity)
            providers.add(provider)
            session = datetime.fromtimestamp(ts, NY).date().isoformat()
            groups[(raw["symbol"], session)].append({**raw, **values, "timestamp": ts})
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected += 1
    if len(providers) > 1:
        raise ValueError("a context study cannot pool market-data providers")
    for rows in groups.values():
        rows.sort(key=lambda r: r["timestamp"])
    current = groups.get((symbol, day), [])
    prior_dates = sorted(d for s, d in groups if s == symbol and d < day)
    prior_rows = groups.get((symbol, prior_dates[-1]), []) if prior_dates else []
    prior = None
    if _complete(prior_rows):
        prior = {"session_date": prior_dates[-1], "high": max(r["high"] for r in prior_rows),
                 "low": min(r["low"] for r in prior_rows), "close": prior_rows[-1]["close"]}
    opening = next((r for r in current if r["timestamp"] == epoch(r["session_open"])), None)
    gap = (10000 * (opening["open"] / prior["close"] - 1) if opening and prior else None)
    # Same-clock volume uses only earlier, complete sessions. It does not
    # compare today's opening volume with today's quieter midday minutes.
    matched_volumes = []
    current_volume = None
    if current and opening:
        elapsed = int((current[-1]["timestamp"] - opening["timestamp"]) / 60) + 1
        if [r["timestamp"] for r in current] == [opening["timestamp"] + i * 60 for i in range(elapsed)]:
            current_volume = sum(r["volume"] for r in current)
            for earlier in prior_dates[-20:]:
                rows = groups[(symbol, earlier)]
                if _complete(rows) and len(rows) >= elapsed:
                    matched_volumes.append(sum(r["volume"] for r in rows[:elapsed]))
    baseline = median(matched_volumes) if len(matched_volumes) >= 10 else None
    participants = []
    latest = max((r[-1]["timestamp"] for (s, d), r in groups.items() if d == day), default=None)
    for s in configured:
        rows = groups.get((s, day), [])
        if rows and rows[0]["timestamp"] == epoch(rows[0]["session_open"]) and rows[-1]["timestamp"] == latest:
            participants.append({"symbol": s, "return_bps": (rows[-1]["close"] / rows[0]["open"] - 1) * 10000})
    return {"schema": "market-context-study.v1", "authorizing": False,
            "diagnostic_only": True, "as_of": cutoff, "symbol": symbol, "feed": feed,
            "historical_diagnostics_enabled": allow_backfill, "rejected_rows": rejected,
            "prior_session": prior, "overnight_gap_bps": gap,
            "same_clock_relative_volume": current_volume / baseline if current_volume is not None and baseline else None,
            "volume_reference_sessions": len(matched_volumes),
            "universe_participation": {"scope": "configured universe only", "configured_symbols": configured,
                "synchronized_symbols": len(participants), "missing_symbols": sorted(set(configured)-{r["symbol"] for r in participants}),
                "advance_fraction": sum(r["return_bps"] > 0 for r in participants) / len(participants) if participants else None},
            "catalyst_calendar": {"available": False, "reason": "no authenticated point-in-time catalyst data supplied"},
            "price_basis": "unadjusted recorded OHLCV"}


def factor_beta(subject, benchmark, *, min_pairs=200, min_sessions=5):
    """OLS beta of synchronized one-minute returns, with no gap bridging."""
    def returns(rows):
        ordered = sorted(rows, key=lambda r: epoch(r["timestamp"]))
        values = {}
        for a, b in zip(ordered, ordered[1:]):
            ta, tb = epoch(a["timestamp"]), epoch(b["timestamp"])
            day = datetime.fromtimestamp(tb, NY).date().isoformat()
            if tb-ta != 60 or datetime.fromtimestamp(ta, NY).date().isoformat() != day:
                continue
            if a.get("feed") != b.get("feed") or a.get("provider") != b.get("provider"):
                continue
            values[(tb, b.get("provider"), b.get("feed"))] = (float(b["close"]) / float(a["close"])-1, day)
        return values
    left, right = returns(subject), returns(benchmark)
    paired = sorted(set(left) & set(right))
    sessions = {left[k][1] for k in paired}
    result = {"schema": "synchronized-beta.v1", "authorizing": False,
              "pairs": len(paired), "sessions": len(sessions), "beta": None}
    if len(paired) < min_pairs or len(sessions) < min_sessions:
        return {**result, "reason": "insufficient synchronized observations"}
    x, y = [right[k][0] for k in paired], [left[k][0] for k in paired]
    mx, my = sum(x)/len(x), sum(y)/len(y)
    variance = sum((a-mx)**2 for a in x)
    if variance <= 0 or not all(math.isfinite(v) for v in x+y):
        return {**result, "reason": "degenerate benchmark"}
    beta = sum((a-mx)*(b-my) for a, b in zip(x, y)) / variance
    return {**result, "beta": beta, "reason": "diagnostic estimate; freeze before a portfolio decision"}
