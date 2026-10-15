import sys
sys.path.insert(0, "/home/user/alpaca-trading")
from research.factory_ledger import _lord_plus_plus_allocation
from statistics import NormalDist
N = NormalDist()
alpha = 0.05
def ref(decisions, alpha, w0frac):
    # independent LORD++ (Ramdas et al. 2017, eq. for alpha_t)
    t = len(decisions) + 1; W0 = alpha * w0frac
    g = lambda k: 1.0/(k*(k+1)) if k >= 1 else 0.0
    taus = [i for i, d in enumerate(decisions, 1) if d]
    a = W0*g(t)
    if taus: a += (alpha - W0)*g(t - taus[0])
    for tj in taus[1:]: a += alpha*g(t - tj)
    return min(alpha, a)
worst = 0
import random
r = random.Random(3)
for _ in range(500):
    d = [r.random() < 0.2 for _ in range(r.randint(0, 40))]
    code = _lord_plus_plus_allocation([{"decision": x} for x in d], alpha, 0.5)[1]
    worst = max(worst, abs(code - ref(d, alpha, 0.5)))
print("max |code - reference LORD++| =", worst)
print("test i : alpha_i with no prior discovery (W0=alpha/2), z needed, and power for z_true=2.0 / 3.0")
for i in (1, 2, 3, 5, 10, 20, 50):
    a = _lord_plus_plus_allocation([{"decision": False}]*(i-1), alpha, 0.5)[1]
    z = N.inv_cdf(1 - a)
    print(f"{i:3d}  {a:.3e}  z>={z:.2f}  power(z=2)={1-N.cdf(z-2):.3f}  power(z=3)={1-N.cdf(z-3):.3f}")
print("sum of alpha over first 1000 tests with no discovery:",
      sum(_lord_plus_plus_allocation([{"decision": False}]*(i-1), alpha, 0.5)[1] for i in range(1, 1001)))
