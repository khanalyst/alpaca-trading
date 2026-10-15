import math, sys, random, itertools
sys.path.insert(0, "/home/user/alpaca-trading")
import numpy as np
from research.stats import (cluster_robust_mean_inference, paired_cluster_sign_flip,
    sign_flip_null_statistics, benjamini_yekutieli, benjamini_hochberg, _sign_flip)

rng = np.random.default_rng(1)
# --- CR1: independent sandwich computation for intercept-only OLS
G = 12
sizes = rng.integers(1, 7, size=G)
vals, cl = [], []
for g, n in enumerate(sizes):
    shock = rng.normal(0, 2)
    for _ in range(n):
        vals.append(float(shock + rng.normal(0.3, 1))); cl.append(f"s{g}")
x = np.array(vals); N = len(x); X = np.ones((N, 1))
beta = x.mean(); e = x - beta
meat = sum((np.sum(e[[i for i,c in enumerate(cl) if c==k]]))**2 for k in set(cl))
V = (G/(G-1)) * ((N-1)/(N-1)) * meat / N**2   # Stata CR1 with K=1
code = cluster_robust_mean_inference(vals, cl)
print("CR1 code se", code["stderr"], "independent", math.sqrt(V), "df", code["df"])
# singleton clusters reduce to s/sqrt(n)
y = list(rng.normal(size=25))
c1 = cluster_robust_mean_inference(y, [str(i) for i in range(25)])
print("singleton CR1 se", c1["stderr"], "s/sqrt(n)", np.std(y, ddof=1)/math.sqrt(25))

# --- exact sign flip vs brute force (one-sided, >=)
contrib = [1.2, -0.4, 0.9, 0.3, -0.1, 2.0, 0.7]
obs = sum(contrib)
brute = sum(1 for s in itertools.product([1,-1], repeat=len(contrib))
            if sum(a*b for a,b in zip(s, contrib)) >= obs - 1e-12) / 2**len(contrib)
print("exact flip code", _sign_flip(contrib, obs, exact_max_clusters=16, iterations=1, seed=1)["p_value"], "brute", brute)
# paired_cluster_sign_flip with day clusters: 86400 s buckets
pairs = [(86400*d + 50000 + i, v, 0.0) for d, v in enumerate(contrib) for i in range(1)]
print("paired flip p", paired_cluster_sign_flip(pairs, cluster_seconds=86400)["p_value"])
# default cluster_seconds=21600: a 13:30-20:00 UTC session splits into two buckets
sess = [(1_760_000_000 - (1_760_000_000 % 86400) + h*3600, 1.0, 0.0) for h in (14, 15, 17, 19)]
print("clusters at default 21600:", paired_cluster_sign_flip(sess)["clusters"], "at 86400:", paired_cluster_sign_flip(sess, cluster_seconds=86400)["clusters"])

# --- BY vs independent implementation
def by_ref(p):
    m = len(p); c = sum(1/i for i in range(1, m+1))
    order = sorted(range(m), key=lambda i: p[i]); adj = [0]*m; run = 1.0
    for r in range(m-1, -1, -1):
        i = order[r]; run = min(run, p[i]*m*c/(r+1)); adj[i] = min(1.0, run)
    return adj
worst = 0
for trial in range(2000):
    m = int(rng.integers(1, 40)); p = list(rng.uniform(0, 1, m) ** 3)
    code = benjamini_yekutieli({str(i): v for i, v in enumerate(p)})
    ref = by_ref(p)
    worst = max(worst, max(abs(code[str(i)]["p_adjusted"] - ref[i]) for i in range(m)))
print("max |BY code - reference| over 2000 random families:", worst)
# counterexample to protocol.md:531-532 (global BY q never less than family q)
fam = {"a": 0.01, "b": 0.04}
glob = {"a": 0.01, "b": 0.04, "z": 0.0001}
print("family q(a)", benjamini_yekutieli(fam)["a"]["p_adjusted"], "global q(a)", benjamini_yekutieli(glob)["a"]["p_adjusted"])
