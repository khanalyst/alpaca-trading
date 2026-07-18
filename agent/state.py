"""Agent state file, PID management and SQLite journal.

The state file (runtime/state.json) is the control channel between the CLI
and the running loop. The CLI writes it; the loop reads it every cycle.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
STATE_FILE = RUNTIME / "state.json"
PID_FILE = RUNTIME / "agent.pid"
DB_FILE = RUNTIME / "journal.db"

RUNNING = "RUNNING"          # normal operation: manage positions, open new ones
PAUSED = "PAUSED"            # housekeeping only: no LLM calls, no new entries
DAY_STOPPED = "DAY_STOPPED"  # daily loss limit hit: model may close, cannot open
KILLED = "KILLED"            # terminal: flatten everything and exit

DEFAULT = {
    "state": PAUSED,
    "high_water_mark": None,
    "day": None,
    "day_start_equity": None,
    "last_ledger_ts": 0,
    "cooldowns": {},   # symbol -> unix ts until which no new entries are allowed
    "opened_at": {},   # symbol -> unix ts the position was opened
    "kill_reason": None,
    "operator_pause": False,  # True = pause was an explicit CLI command and
                              # must survive crashes and restarts
}


def _ensure() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(DEFAULT, indent=2))


def load_state() -> dict:
    _ensure()
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        data = {}
    return {**DEFAULT, **data}


def save_state(st: dict) -> None:
    _ensure()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, STATE_FILE)


def set_state(name: str, reason: str | None = None, **extra) -> dict:
    st = load_state()
    st["state"] = name
    if reason is not None:
        st["kill_reason"] = reason
    st.update(extra)
    save_state(st)
    return st


# Keys the trading loop owns. commit() persists these without clobbering a
# state change (pause/kill) the CLI may have written while a cycle was running.
LOOP_KEYS = ("high_water_mark", "day", "day_start_equity", "last_ledger_ts",
             "cooldowns", "opened_at")


def commit(st: dict, transition: tuple[str, str] | None = None,
           kill: str | None = None) -> dict:
    """Merge the loop's bookkeeping into the state file.

    The file is reloaded first because the CLI may have set PAUSED or KILLED
    mid-cycle; a plain save_state(st) with the loop's stale copy would erase
    that command. A state change is applied only as a compare-and-set
    `transition=(from, to)` or an unconditional `kill=reason`. `st` is
    updated in place to the merged result so the caller sees CLI changes.
    """
    cur = load_state()
    for k in LOOP_KEYS:
        if k in st:
            cur[k] = st[k]
    if kill is not None:
        cur["state"] = KILLED
        cur["kill_reason"] = kill
    elif transition and cur["state"] == transition[0]:
        cur["state"] = transition[1]
    save_state(cur)
    st.clear()
    st.update(cur)
    return st


# ---------------------------------------------------------------- PID file

def write_pid() -> None:
    _ensure()
    PID_FILE.write_text(str(os.getpid()))


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- journal

def _db() -> sqlite3.Connection:
    _ensure()
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, payload TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trades ("
        "ts REAL, symbol TEXT, side TEXT, action TEXT, qty REAL, price REAL, "
        "notional REAL, leverage REAL, reason TEXT, confidence REAL, "
        "pnl_pct REAL)"
    )
    # Migrate journals created before confidence/pnl_pct existed.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    for col in ("confidence", "pnl_pct"):
        if col not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS equity (ts REAL, equity REAL, state TEXT)"
    )
    return conn


def log_event(kind: str, payload: str) -> None:
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO events VALUES (?,?,?)", (time.time(), kind, payload)
            )
    except Exception:
        pass


def log_trade(symbol, side, action, qty, price, notional, leverage, reason,
              confidence=None, pnl_pct=None) -> None:
    """Journal one trade. Opens carry the model's confidence; closes carry
    the realized PnL percent — together they make calibration measurable
    (does 0.8-confidence actually outperform 0.7?). See report.py."""
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO trades (ts, symbol, side, action, qty, price, "
                "notional, leverage, reason, confidence, pnl_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), symbol, side, action, qty, price, notional,
                 leverage, reason, confidence, pnl_pct),
            )
    except Exception:
        pass


def log_equity(equity: float, st: str) -> None:
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO equity VALUES (?,?,?)", (time.time(), equity, st)
            )
    except Exception:
        pass
