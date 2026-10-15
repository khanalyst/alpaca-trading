import sqlite3, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "/home/user/alpaca-trading")
from agent import state
tmp = Path(tempfile.mkdtemp(prefix="c1-journal-", dir="."))
state.configure_runtime("test", base=tmp)
state.ensure_ready()
real_connect = sqlite3.connect
log = {"connect": 0, "stmts": []}
def counting_connect(*a, **k):
    log["connect"] += 1
    db = real_connect(*a, **k)
    db.set_trace_callback(log["stmts"].append)
    return db
sqlite3.connect = counting_connect
state.log_event("probe", "{}")
writes = [s.split()[0:3] for s in log["stmts"]
          if s.split()[0].upper() in {"INSERT", "COMMIT", "BEGIN", "ALTER", "CREATE"}]
print("one state.log_event -> sqlite connections opened:", log["connect"])
print("write-type statements:", [" ".join(w) for w in writes])
print("COMMITs:", sum(1 for s in log["stmts"] if s.strip().upper().startswith("COMMIT")))
sqlite3.connect = real_connect
t0 = time.perf_counter()
for _ in range(50):
    state.log_event("probe", "{}")
print(f"mean wall time per log_event on this disk: {(time.perf_counter()-t0)/50*1000:.1f} ms")
