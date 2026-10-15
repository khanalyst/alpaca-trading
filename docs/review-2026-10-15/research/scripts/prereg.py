"""research.preregistered.evaluate on fresh public bars, explicitly NOT decision-eligible (diagnostic_only)."""
import sys, os, json
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA
sys.path.insert(0, REPO_ROOT)
from research.market_data import normalize_underlying_bar
from research.preregistered import evaluate, VWAP_REVERSION_HYPOTHESIS
RAW = json.load(open(DATA))["bars"]
bars = [normalize_underlying_bar({**r, "provider": "public_chart", "feed": "delayed_sip", "source_mode": "historical_backfill"})
        for s in sorted(RAW) for r in RAW[s]]
out = {}
for hid in (VWAP_REVERSION_HYPOTHESIS, "opening-range-fade-control-adjusted.v1"):
    out[hid] = evaluate(bars, hypothesis_id=hid, decision_eligible=False,
                        eligibility_reasons=["public consolidated-tape bars, not Alpaca IEX forward_observed"],
                        provenance={"source": "public chart endpoint", "feed": "delayed_sip", "fetched": "2026-10-14"})
    print(hid, json.dumps({k: out[hid][k] for k in ("decision", "rule_outcome_if_eligible", "interim", "looks")}, default=str)[:1500], flush=True)
json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)
