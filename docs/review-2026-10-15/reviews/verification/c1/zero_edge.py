"""Zero-edge pass probability through the real paper-trial verdict path."""
import math, random, sys, time
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.paper_trial import _performance, _policy
from research.trial import _verdict

cfg = {"research": {"trial": {"min_sessions": 20, "min_trades": 20,
                              "min_mean_r": 0.0, "min_total_r": 0.0}}}
policy = _policy(cfg)

def verdict(rs, sessions):
    outcomes = [{"r_multiple": r, "net_pnl": r, "session_date": d}
                for r, d in rs]
    st = {"outcomes": outcomes, "accepted_sessions": [{}] * sessions}
    return _verdict(_performance(st, policy), policy)["state"]

def draw(kind, rng):
    if kind == "bracket_1.5R":           # zero edge: P(target)=1/(1+1.5)
        return 1.5 if rng.random() < 0.4 else -1.0
    if kind == "bracket_2R":
        return 2.0 if rng.random() < 1/3 else -1.0
    return rng.gauss(0.0, 1.0)

def day(i):
    return f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"

# exact binomial point-estimate gate (mean>0 and total>0), n = 20
def binom_tail(n, p, kmin):
    return sum(math.comb(n, k) * p**k * (1-p)**(n-k) for k in range(kmin, n+1))
print("point gate only, n=20: bracket 1.5R (K>=9 of Bin(20,0.4)) =",
      round(binom_tail(20, 0.4, 9), 4),
      "| bracket 2R (K>=7 of Bin(20,1/3)) =", round(binom_tail(20, 1/3, 7), 4))

rng = random.Random(20261015)
t0 = time.time()
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
for kind in ("bracket_1.5R", "bracket_2R", "normal"):
    counts = {}
    point = 0
    for _ in range(N):
        rs = [(draw(kind, rng), day(i)) for i in range(20)]
        point += sum(r for r, _ in rs) > 0
        v = verdict(rs, 20)
        counts[v] = counts.get(v, 0) + 1
    print(f"single look n=20 one trade/session {kind}: point-gate pass "
          f"{point/N:.3f}; verdict {dict(sorted(counts.items()))} "
          f"-> passed {counts.get('passed',0)/N:.3f}")
print("elapsed", round(time.time() - t0, 1), "s")

# sequential looks: one trade per accepted session, verdict at each session 20..60
M = int(sys.argv[2]) if len(sys.argv) > 2 else 300
ever_pass = ever_fail = 0
for _ in range(M):
    rs = [(draw("bracket_1.5R", rng), day(i)) for i in range(60)]
    passed = failed = False
    for n in range(20, 61):
        v = verdict(rs[:n], n)
        if v == "passed":
            passed = True
        if v == "failed":
            failed = True
            break                      # failed is terminal in refresh_state
    ever_pass += passed
    ever_fail += failed
print(f"sequential looks 20..60 sessions, bracket_1.5R zero edge, {M} paths: "
      f"ever 'passed' {ever_pass/M:.3f}, terminal 'failed' {ever_fail/M:.3f}")
print("elapsed", round(time.time() - t0, 1), "s")
