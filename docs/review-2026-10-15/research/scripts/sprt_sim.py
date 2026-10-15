"""Simulate the edge-ledger paper-drift SPRT and the advisory rolling-R warning.

Mirrors research/edge_ledger.py: LLR_n = sum scale*(mu0/2 - x_i), scale = mu0/sd^2,
degraded when LLR >= 4.0 after at least 20 outcomes; the advisory warning fires when
the last 20 R values sum to <= -2.0.  Per-trade R is drawn from a bracket-like
mixture: stop at -1R, target at +1.5R, otherwise a time exit ~ N(m, 0.5).
"""
import random, statistics as st

def trade(edge, rng):
    # choose probabilities so the mean is `edge` R; time exits 30% of trades
    # mean = p_t*1.5 - p_s*1 + 0.3*edge ; with p_t + p_s = 0.7
    # solve p_t*1.5 - (0.7-p_t) = 0.7*edge -> p_t = (0.7*edge + 0.7)/2.5
    p_t = (0.7 * edge + 0.7) / 2.5
    u = rng.random()
    if u < p_t:
        return 1.5
    if u < 0.7:
        return -1.0
    return rng.gauss(edge, 0.5)

def run(true_edge, mu0, sd, n_max, rng, pre_good=0):
    llr, scale = 0.0, mu0 / (sd * sd)
    xs = []
    for i in range(pre_good + n_max):
        e = mu0 if i < pre_good else true_edge
        x = trade(e, rng)
        xs.append(x)
        llr += scale * (mu0 / 2.0 - x)
        if len(xs) >= 20 and llr >= 4.0:
            return i + 1 - pre_good
    return None

def warn_ever(true_edge, n, rng):
    xs = [trade(true_edge, rng) for _ in range(n)]
    return any(sum(xs[i-20:i]) <= -2.0 for i in range(20, n + 1))

rng = random.Random(7)
sample = [trade(0.0, rng) for _ in range(200000)]
print("per-trade R sd at zero edge: %.3f" % st.pstdev(sample))
mu0, sd = 0.25, 1.1
for edge in (0.25, 0.10, 0.0, -0.10):
    hits = [run(edge, mu0, sd, 1000, rng) for _ in range(2000)]
    det = [h for h in hits if h is not None]
    print(f"true edge {edge:+.2f}R: retired within 1000 trades {len(det)/len(hits):.1%}; "
          f"median trades to retire {st.median(det) if det else None}")
for pre in (0, 100, 300):
    hits = [run(0.0, mu0, sd, 2000, rng, pre_good=pre) for _ in range(2000)]
    det = [h for h in hits if h is not None]
    print(f"edge dies after {pre} good trades: median trades after death to retire {st.median(det)}")
for edge in (0.25, 0.10, 0.0):
    print(f"advisory rolling warning fires at least once in 250 trades, true edge {edge:+.2f}R: "
          f"{sum(warn_ever(edge, 250, rng) for _ in range(2000))/2000:.1%}")
