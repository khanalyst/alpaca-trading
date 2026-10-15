"""Monte Carlo of the paper-trial verdict using the repository's own code."""
import random, sys, time
from math import comb
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.paper_trial import _confidence
from research.trial import _verdict

POLICY = {"min_sessions": 20, "min_trades": 20, "min_mean_r": 0.0, "min_total_r": 0.0}

def run(p_win, win_r, loss_r, cost_r, n=20, sims=1000, seed=7):
    rng = random.Random(seed)
    naive = passed = failed = 0
    for _ in range(sims):
        r = [(win_r if rng.random() < p_win else -loss_r) - cost_r for _ in range(n)]
        outcomes = [{"r_multiple": v, "session_date": f"2026-11-{i+1:02d}"} for i, v in enumerate(r)]
        perf = {"sessions": n, "outcomes": n, "total_r": sum(r), "mean_r": sum(r)/n,
                "session_cluster_confidence": _confidence(outcomes, POLICY)}
        v = _verdict(perf, POLICY)
        naive += (sum(r) > 0)
        passed += v["state"] == "passed"
        failed += v["state"] == "failed"
    return naive/sims, passed/sims, failed/sims

t = time.time()
for label, args in [
    ("zero-edge 1/3 win, +2R/-1R, no cost", (1/3, 2, 1, 0.0)),
    ("zero-edge 1/3 win, +2R/-1R, 0.1R cost", (1/3, 2, 1, 0.1)),
    ("brief case 50% win, +2R/-1R (EV +0.5R), no cost", (0.5, 2, 1, 0.0)),
    ("zero-edge 50% win, +1R/-1R, no cost", (0.5, 1, 1, 0.0)),
]:
    print(label, "naive_total_r>0=%.3f passed=%.3f failed=%.3f" % run(*args))
print("elapsed %.1fs" % (time.time()-t))
# exact binomial for the naive point-estimate rule, 1/3 win, +2/-1: total>0 iff 3W-20>0 iff W>=7
p=1/3
print("exact P(W>=7 | n=20,p=1/3) =", sum(comb(20,k)*p**k*(1-p)**(20-k) for k in range(7,21)))
# brief case p=.5: total>0 iff W>=7
print("exact P(W>=7 | n=20,p=1/2) =", sum(comb(20,k)*.5**20 for k in range(7,21)))
# 50% +1/-1: total>0 iff W>=11
print("exact P(W>=11 | n=20,p=1/2) =", sum(comb(20,k)*.5**20 for k in range(11,21)))
