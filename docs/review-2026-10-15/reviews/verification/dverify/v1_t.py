import math, sys
sys.path.insert(0, "/home/user/alpaca-trading")
import numpy as np
from research.stats import student_t_sf

def t_sf_numeric(t, df):
    # independent: integrate the Student-t density from t to +inf using u = atan substitution
    c = math.exp(math.lgamma((df+1)/2) - math.lgamma(df/2)) / math.sqrt(df*math.pi)
    a = math.atan(t)
    u = np.linspace(a, math.pi/2, 2_000_001)
    x = np.tan(u[:-1]); x = np.append(x, 1e300)
    dens = c * (1 + x*x/df) ** (-(df+1)/2) / np.cos(u)**2
    dens[-1] = 0.0
    return float(np.trapezoid(dens, u))

closed = {
 1: lambda t: 0.5 - math.atan(t)/math.pi,
 2: lambda t: 0.5 - t/(2*math.sqrt(2+t*t)),
 3: lambda t: 0.5 - (math.atan(t/math.sqrt(3)) + (t/math.sqrt(3))/(1+t*t/3))/math.pi,
}
worst = 0
for df, f in closed.items():
    for t in (-3, -1, 0, 0.5, 1, 2, 3, 10):
        a, b = student_t_sf(t, df), f(t)
        worst = max(worst, abs(a-b))
print("max |code - closed form| df=1..3:", worst)
for df in (1, 2, 4, 9, 17, 29, 59):
    for t in (0.5, 1.7, 2.05, 3.5):
        a, b = student_t_sf(t, df), t_sf_numeric(t, df)
        worst = max(worst, abs(a-b))
print("max |code - numeric integral| df in {1,2,4,9,17,29,59}:", worst)
# tabulated two-sided 5% critical values -> sf should be 0.025
for df, crit in ((1, 12.7062), (5, 2.5706), (17, 2.1098), (29, 2.0452), (59, 2.0010)):
    print(f"df={df} t={crit} sf={student_t_sf(crit, df):.5f}")
# ORF manifest: clustered t +2.95 df 17
print("ORF in-sample t=2.95 df=17 one-sided p:", student_t_sf(2.95, 17))
