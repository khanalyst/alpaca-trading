import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.governance import record_config_version, config_history
d = Path(tempfile.mkdtemp())
j = d / "journal.db"
A = {"mode": "paper", "risk": {"risk_per_trade_pct": 0.5}}
B = {"mode": "paper", "risk": {"risk_per_trade_pct": 2.0}}
for label, cfg in (("A", A), ("B", B), ("A again", A)):
    r = record_config_version(j, cfg); time.sleep(0.01)
    print(label, r["config_version_id"], "recorded=", r["recorded"])
h = config_history(j)
print("newest history entry:", h[0]["config_version_id"], h[0]["changed_paths"], "(config actually in force is A)")
