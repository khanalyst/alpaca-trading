"""Receipt-time bar revisions, separate from the first-observation CSV corpus.

Each changed OHLCV observation is appended; a later correction never replaces
what an earlier decision could see. Quotes remain in the recorder CSV. This
compact bar store supports bounded chart/context reads without scanning quotes.
It is evidence storage, not a trading or promotion boundary.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Mapping

SCHEMA = "market-observations.v1"
BAR_FIELDS = ("open", "high", "low", "close", "volume")


def epoch(value):
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("an aware observation timestamp is required")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("observation timestamp must have a timezone")
        result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError("observation timestamp must be finite")
    return result


def _bar(row: Mapping, maximum_lag: float):
    if row.get("event_type") not in {"bar", "bar_1m"}:
        return None
    stamp, available, received = (epoch(row.get(key))
                                  for key in ("timestamp", "as_of", "observed_at"))
    if available < stamp + 60 or received < available:
        raise ValueError("bar receipt precedes its information boundary")
    values = {key: float(row[key]) for key in BAR_FIELDS}
    if any(not math.isfinite(v) or (v < 0 if k == "volume" else v <= 0)
           for k, v in values.items()):
        raise ValueError("invalid OHLCV observation")
    if not (values["low"] <= min(values["open"], values["close"])
            <= max(values["open"], values["close"]) <= values["high"]):
        raise ValueError("inconsistent OHLC observation")
    identity = {key: str(row.get(key) or "").strip()
                for key in ("provider", "feed", "symbol")}
    if not all(identity.values()):
        raise ValueError("bar provider, feed and symbol are required")
    payload = {**identity, "timestamp": stamp, "as_of": available, **values}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    mode = ("historical_backfill" if row.get("source_mode") == "historical_backfill"
            or received - available > maximum_lag else "forward_observed")
    return payload, received, mode, encoded, hashlib.sha256(encoded.encode()).hexdigest()


def append_observations(path: Path, rows, *, maximum_lag_seconds=900.0) -> dict:
    bars = [value for row in rows if (value := _bar(row, maximum_lag_seconds))]
    if not bars:
        return {"schema": SCHEMA, "observations": 0, "revisions": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    added = revised = 0
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS bar_observations (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL, feed TEXT NOT NULL,
            symbol TEXT NOT NULL, market_ts REAL NOT NULL, available_ts REAL NOT NULL,
            received_ts REAL NOT NULL, source_mode TEXT NOT NULL,
            revision INTEGER NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
            UNIQUE(provider,feed,symbol,market_ts,revision))""")
        db.execute("CREATE INDEX IF NOT EXISTS bar_observations_lookup ON bar_observations(symbol,feed,market_ts,received_ts)")
        with db:
            for bar, received, mode, encoded, digest in bars:
                key = (bar["provider"], bar["feed"], bar["symbol"], bar["timestamp"])
                latest = db.execute("""SELECT revision,content_hash,received_ts FROM bar_observations
                    WHERE provider=? AND feed=? AND symbol=? AND market_ts=?
                    ORDER BY revision DESC LIMIT 1""", key).fetchone()
                if latest and latest[1] == digest:
                    continue
                if latest and received < latest[2]:
                    raise ValueError("revision receipt moved backwards")
                revision = latest[0] + 1 if latest else 0
                db.execute("""INSERT INTO bar_observations
                    (provider,feed,symbol,market_ts,available_ts,received_ts,source_mode,
                     revision,content_hash,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (*key, bar["as_of"], received, mode, revision, digest, encoded))
                added += 1
                revised += int(revision > 0)
    return {"schema": SCHEMA, "observations": added, "revisions": revised}


def read_bars(path: Path, *, symbol: str, feed: str, start: float, end: float,
              as_of: float, limit: int = 10000) -> list[dict]:
    """Return the latest revision actually received by an explicit cutoff."""
    if not 0 < limit <= 100000 or end <= start or end - start > 40 * 86400:
        raise ValueError("bar reads require a bounded window and row limit")
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        # Select the received revision first; LIMIT applies to unique bars,
        # not raw versions, so a heavily revised minute cannot hide the rest.
        rows = db.execute("""SELECT payload_json, received_ts, source_mode, revision
            FROM (SELECT *, ROW_NUMBER() OVER (
                PARTITION BY provider,feed,symbol,market_ts ORDER BY revision DESC) AS rank
                FROM bar_observations WHERE symbol=? AND feed=? AND market_ts>=?
                  AND market_ts<? AND available_ts<=? AND received_ts<=?)
            WHERE rank=1 ORDER BY market_ts LIMIT ?""",
            (symbol, feed, start, end, as_of, as_of, limit)).fetchall()
    return [{**json.loads(payload), "observed_at": received, "source_mode": mode,
             "revision": revision} for payload, received, mode, revision in rows]
