"""Cross-check: my clock-matched signal-quality measure vs the repository instrument (via research.preregistered)
on the identical sealed windows and arms. Repository numbers come from out/prereg.json."""
import json, sys, importlib.util, collections
spec = importlib.util.spec_from_file_location("ar", "scripts/analyze_replay.py")
src = open("scripts/analyze_replay.py").read().split("res = []")[0]   # reuse helper definitions only
sys.argv = ["x", "results/replay", "data/bars_1m.json", "/dev/null"]
ns = {}; exec(compile(src, "analyze_helpers", "exec"), ns)
pre = json.load(open("results/prereg.json"))
pairs = [("vwap-reversion-control-adjusted.v1", "primary_60m", "rule.vwap-reversion.ab97bfe87ed566de", "2026-09-21", 60),
         ("vwap-reversion-control-adjusted.v1", "subject_120m", "rule.vwap-reversion.ab97bfe87ed566de", "2026-09-21", 120),
         ("vwap-reversion-control-adjusted.v1", "mirror_60m", "rule.vwap-trend.349de232c3d7a18c", "2026-09-21", 60),
         ("opening-range-fade-control-adjusted.v1", "primary_60m", "rule.opening-range-fade.d5785d9e70b56def", "2026-09-23", 60),
         ("opening-range-fade-control-adjusted.v1", "subject_120m", "rule.opening-range-fade.d5785d9e70b56def", "2026-09-23", 120),
         ("opening-range-fade-control-adjusted.v1", "mirror_60m", "rule.opening-range-breakout.0eb200d3136d80ee", "2026-09-23", 60)]
print(f"{'hypothesis/metric':55s} {'repo_n':>6s} {'repo_d':>7s} {'repo_t':>6s} | {'mine_n':>6s} {'mine_d':>7s} {'mine_t':>6s}")
for hid, key, vid, after, h in pairs:
    r = pre[hid]["interim"][key]
    a = json.load(open(f"results/replay/{vid}.json"))
    firsts = [f for f in a["firsts"] if f["open"]["sess"] > after]
    m = ns["signal_quality"](firsts, horizons=(h,))[h]
    print(f"{hid[:38]+'/'+key:55s} {r['candidate_count']:6d} {r['delta_bps']:7.2f} {r['cluster_t_stat']:6.2f} | {m.get('n',0):6d} {m.get('delta_bps',float('nan')):7.2f} {m.get('cluster_t') or float('nan'):6.2f}")
