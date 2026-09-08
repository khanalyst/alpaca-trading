#!/usr/bin/env python3
"""Read-only, paired IEX/SIP historical coverage diagnostic; no feed fallback."""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import argparse
import json
import math
import os
from pathlib import Path
from statistics import median
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.instruments import validate_equity_symbol
from deploy.market_observations import epoch


def compare(iex, sip, *, opened, closed):
    start, end = epoch(opened), epoch(closed)
    if end <= start or end-start > 8*3600 or (end-start) % 60:
        raise ValueError("a recorded regular-session calendar is required")
    expected = {start + i*60 for i in range(int((end-start)/60))}

    def indexed(rows, feed):
        result = {}
        for row in rows:
            if row.get("feed") != feed:
                raise ValueError("paired study feed mismatch")
            ts = epoch(row["timestamp"])
            if ts not in expected:
                continue
            close, volume = float(row["close"]), float(row["volume"])
            if not math.isfinite(close) or close <= 0 or not math.isfinite(volume) or volume < 0:
                raise ValueError("invalid paired OHLCV")
            if ts in result:
                raise ValueError("duplicate paired minute")
            result[ts] = (close, volume)
        return result

    left, right = indexed(iex, "iex"), indexed(sip, "sip")
    paired = sorted(set(left) & set(right))
    deviations = sorted(abs(left[t][0]/right[t][0]-1)*10000 for t in paired)
    right_volume = sum(right[t][1] for t in paired)
    return {"expected_minutes": len(expected), "iex_minutes": len(left),
            "sip_minutes": len(right), "paired_minutes": len(paired),
            "iex_missing_minutes": len(expected-set(left)),
            "sip_missing_minutes": len(expected-set(right)),
            "close_absolute_difference_bps_median": median(deviations) if deviations else None,
            "close_absolute_difference_bps_p95": deviations[max(0, math.ceil(.95*len(deviations))-1)] if deviations else None,
            "paired_iex_to_sip_volume": sum(left[t][1] for t in paired)/right_volume if right_volume else None,
            "basis": "same symbol and calendar minute; unadjusted historical bars; no missing-minute imputation"}


def run(provider, *, symbols, start, end, output):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if first > last or (last-first).days > 6 or last >= today:
        raise ValueError("choose at most seven completed calendar days")
    symbols = sorted({validate_equity_symbol(s) for s in symbols})
    if not symbols or len(symbols) > 16:
        raise ValueError("choose one to sixteen symbols")
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "paired-feed-study.v1", "diagnostic_only": True,
              "authorizing": False, "source_mode": "historical_backfill",
              "symbols": symbols, "start": start, "end": end, "sessions": []}
    for session in provider.calendar(start=first, end=last):
        if not first <= session.date <= last:
            raise ValueError("provider calendar escaped requested dates")
        data, errors, receipts = {}, {}, {}
        for feed in ("iex", "sip"):
            try:
                result = provider.bars(symbols, timeframe="1m", start=session.open,
                                       end=session.close, feed=feed)
                data[feed] = {s: [asdict(bar) for bar in result.get(s, [])] for s in symbols}
            except Exception as exc:
                # Never substitute another feed or write credential-bearing
                # exception strings to a diagnostic artifact.
                errors[feed] = {"error_type": type(exc).__name__, "available": False}
            receipts[feed] = datetime.now(timezone.utc).isoformat()
        raw = {"session": asdict(session), "data": data, "errors": errors,
               "received_at": receipts, "source_mode": "historical_backfill"}
        _write(output / f"bars-{session.date}.json", raw)
        report["sessions"].append({"date": session.date.isoformat(), "received_at": receipts,
            "errors": errors, "symbols": {s: compare(data["iex"][s], data["sip"][s],
                opened=session.open, closed=session.close) for s in symbols} if not errors else {}})
    report["complete"] = bool(report["sessions"]) and not any(s["errors"] for s in report["sessions"])
    _write(output / "summary.json", report)
    return report


def _write(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, default=str, allow_nan=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    args = parser.parse_args()
    from dotenv import load_dotenv
    from agent.alpaca_provider import AlpacaProvider
    from agent.config import load_config
    load_dotenv(os.getenv("ALPACA_AGENT_SECRETS_FILE") or ROOT / ".env", override=False)
    report = run(AlpacaProvider(load_config(args.config)), symbols=args.symbols,
                 start=args.start, end=args.end, output=args.output)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
