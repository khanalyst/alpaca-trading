"""Simulate paper-trial verdicts for zero/negative-edge outcome models using the repo's own code."""
import random, sys, time, math
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.paper_trial import _confidence, _performance
from research.trial import _verdict

POLICY = {"min_sessions": 20, "min_trades": 20, "min_mean_r": 0.0, "min_total_r": 0.0}

def draw(model, rng):
    u = rng.random()
    if model == "S1_literal":          # 50% stop at -1R, else time exit ~ N(0, 0.25)
        return -1.0 if u < 0.5 else rng.gauss(0.0, 0.25)
    if model == "S2_zero_mean_orf":    # 50% stop -1R, 1/3 target +1.5R, 1/6 time exit ~ N(0,0.25): mean 0
        if u < 0.5: return -1.0
        if u < 0.5 + 1/3: return 1.5
        return rng.gauss(0.0, 0.25)
    if model == "S3_coin":             # -1R / +1R coin: mean 0
        return -1.0 if u < 0.5 else 1.0
    if model == "S2_minus_cost":       # S2 minus 0.1R round-trip cost
        return draw("S2_zero_mean_orf", rng) - 0.1
    raise ValueError(model)

def verdict_for(values, sessions):
    outcomes = [{"r_multiple": v, "net_pnl": v, "session_date": s} for v, s in zip(values, sessions)]
    state = {"outcomes": outcomes, "accepted_sessions": [{"date": d} for d in sorted(set(sessions))]}
    perf = _performance(state, POLICY)
    return _verdict(perf, POLICY)

def single_look(model, sims, seed=1):
    rng = random.Random(seed)
    point = full = failed = 0
    for _ in range(sims):
        vals = [draw(model, rng) for _ in range(20)]
        sess = [f"2026-01-{i+1:02d}" if i < 31 else f"d{i}" for i in range(20)]
        if sum(vals) > 0: point += 1
        v = verdict_for(vals, sess)
        full += v["state"] == "passed"
        failed += v["state"] == "failed"
    return point / sims, full / sims, failed / sims

def sequential(model, sims, max_sessions=60, seed=2):
    """One trade per accepted session; re-judged after every session from 20 to max_sessions."""
    rng = random.Random(seed)
    ever_pass = ever_fail = 0
    for _ in range(sims):
        vals = [draw(model, rng) for _ in range(max_sessions)]
        sess = [f"s{i:03d}" for i in range(max_sessions)]
        passed = failed = False
        for n in range(20, max_sessions + 1):
            st = verdict_for(vals[:n], sess[:n])["state"]
            if st == "passed": passed = True
            if st == "failed":
                failed = True
                break  # failed is terminal in agent/paper_trial.py refresh_state
        ever_pass += passed
        ever_fail += failed
    return ever_pass / sims, ever_fail / sims

if __name__ == "__main__":
    mode = sys.argv[1]
    model = sys.argv[2]
    sims = int(sys.argv[3])
    t = time.time()
    if mode == "single":
        p, f, fl = single_look(model, sims)
        print(f"{model} single n=20 sims={sims}: P(total_r>0 & mean_r>0)={p:.4f}  P(verdict=passed)={f:.4f}  P(verdict=failed)={fl:.4f}  [{time.time()-t:.0f}s]")
    else:
        p, fl = sequential(model, sims)
        print(f"{model} sequential looks n=20..60 sims={sims}: P(ever passed)={p:.4f}  P(failed, absorbing)={fl:.4f}  [{time.time()-t:.0f}s]")
