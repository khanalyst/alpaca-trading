"""Broker-free recorder key-store benchmark on disposable synthetic databases.

No production database, corpus, configuration or credentials are opened. Both
control and alternatives retain SQLite FULL durability. Closing/checkpoint time
is included so a faster commit cannot hide work at connection shutdown.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import tempfile
import time


START = datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc)


def entries(start: int, count: int, rate: float):
    for index in range(start, start + count):
        yield (hashlib.sha256(f"recorder-profile-{index}".encode()).hexdigest(),
               (START + timedelta(seconds=index / rate)).isoformat())


def configure(db, mode: str, cache_kib: int):
    assert db.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode.lower()
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA temp_store=FILE")
    db.execute(f"PRAGMA cache_size=-{cache_kib}")
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=650_000)
    parser.add_argument("--peak-rows", type=int, default=1_300_000)
    parser.add_argument("--new-rows", type=int, default=55_000)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=("DELETE", "WAL", "PERSIST"),
                        default=["DELETE", "WAL"])
    parser.add_argument("--cache-kib", type=int, default=65_536)
    args = parser.parse_args(argv)
    if not (0 < args.new_rows <= args.rows <= args.peak_rows <= 5_000_000 and
            1 <= args.cycles <= 20 and 1 <= args.cache_kib <= 262_144):
        parser.error("use bounded positive row counts/cycles/cache")
    rate = args.rows / 900.0
    with tempfile.TemporaryDirectory(prefix="alpaca-recorder-profile-") as tmp:
        root = Path(tmp)
        seed = root / "seed.sqlite3"
        with closing(sqlite3.connect(seed)) as db:
            configure(db, "DELETE", args.cache_kib)
            db.execute("CREATE TABLE recent_keys (event_key TEXT PRIMARY KEY NOT NULL, "
                       "event_ts TEXT NOT NULL)")
            db.execute("CREATE INDEX recent_keys_event_ts ON recent_keys(event_ts)")
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID")
            with db:
                for start in range(0, args.peak_rows, 10_000):
                    db.executemany("INSERT INTO recent_keys VALUES (?,?)",
                                   entries(start, min(10_000, args.peak_rows-start), rate))
            floor = next(entries(args.peak_rows-args.rows, 1, rate))[1]
            with db:
                db.execute("DELETE FROM recent_keys WHERE event_ts < ?", (floor,))
        print(json.dumps({"scope": "synthetic_disposable_only", "platform": platform.platform(),
                          "sqlite": sqlite3.sqlite_version, "settings": vars(args),
                          "seed_bytes": seed.stat().st_size}), flush=True)
        fingerprints = []
        for mode in args.modes:
            path = root / f"{mode}.sqlite3"
            with closing(sqlite3.connect(seed.as_uri()+"?mode=ro", uri=True)) as source, \
                    closing(sqlite3.connect(path)) as target:
                source.backup(target)
            for cycle in range(args.cycles):
                phases = {}
                started = step = time.perf_counter()
                db = sqlite3.connect(path)
                configure(db, mode, args.cache_kib)
                phases["open_configure_seconds"] = time.perf_counter()-step
                values = list(entries(args.peak_rows+cycle*args.new_rows, args.new_rows, rate))
                floor = next(entries(args.peak_rows+(cycle+1)*args.new_rows-args.rows, 1, rate))[1]
                try:
                    db.execute("BEGIN IMMEDIATE")
                    step = time.perf_counter()
                    db.executemany("INSERT INTO recent_keys VALUES (?,?)", values)
                    phases["insert_seconds"] = time.perf_counter()-step
                    step = time.perf_counter()
                    removed = db.execute("DELETE FROM recent_keys WHERE event_ts < ?", (floor,)).rowcount
                    phases["expiry_seconds"] = time.perf_counter()-step
                    step = time.perf_counter()
                    count = db.execute("SELECT COUNT(*) FROM recent_keys").fetchone()[0]
                    phases["count_seconds"] = time.perf_counter()-step
                    db.executemany("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) "
                                   "DO UPDATE SET value=excluded.value",
                                   [("count", str(count)), ("corpus_signature", f"synthetic-{cycle}")])
                    step = time.perf_counter()
                    db.commit()
                    phases["commit_seconds"] = time.perf_counter()-step
                    assert count == args.rows and removed == args.new_rows
                    step = time.perf_counter()
                finally:
                    db.close()
                phases["close_checkpoint_seconds"] = time.perf_counter()-step
                phases["total_seconds"] = time.perf_counter()-started
                print(json.dumps({"mode": mode, "cycle": cycle, "remaining": count,
                                  "removed": removed, "database_bytes": path.stat().st_size,
                                  "phases": phases}, sort_keys=True), flush=True)
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
                assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                digest = hashlib.sha256()
                for row in db.execute("SELECT event_key,event_ts FROM recent_keys ORDER BY event_key"):
                    digest.update(json.dumps(row, separators=(",", ":")).encode())
                fingerprints.append(digest.hexdigest())
        assert len(set(fingerprints)) == 1, "alternatives changed exact key membership"
        print(json.dumps({"exact_membership_equal": True, "fingerprint": fingerprints[0],
                          "full_durability": True}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
