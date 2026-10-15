import numpy as np
from statistics import NormalDist
N = NormalDist(); z05 = N.inv_cdf(0.95)
rng = np.random.default_rng(7)

def chain(mu, sd=40.0, tpd=5, S=30, ctrl_sd=0.0, reps=200_000, m_global=1, stress_extra=12.0,
          lord_alpha=0.0125, alpha=0.05):
    """Monte Carlo of the gate chain with session-level aggregation (trades iid).
    Returns probabilities of passing successive cumulative stages."""
    n = S * tpd
    def arm(mean):  # per-session sums of candidate pnl and of delta vs a control
        c = rng.normal(mean, sd, size=(reps, S, tpd))
        b = rng.normal(0.0, ctrl_sd, size=(reps, S, tpd)) if ctrl_sd else 0.0
        return c, c - b
    out = {}
    # fit window
    cf, df_ = arm(mu)
    fit_delta_pos = df_.mean(axis=(1, 2)) > 0
    # held-out window: actual-control delta and null-control delta share candidate pnl
    ch, dh = arm(mu)
    nul = ch - (rng.normal(0.0, ctrl_sd, size=(reps, S, tpd)) if ctrl_sd else 0.0)
    def zstat(d):  # session-clustered z (CR-style) on per-session sums
        s = d.sum(axis=2); mean = s.sum(axis=1) / n
        se = np.sqrt(S / (S - 1) * ((s - s.mean(axis=1, keepdims=True)) ** 2).sum(axis=1)) / n
        return mean / se
    zh = zstat(dh); zn = zstat(nul)
    p_ok = zh > z05                     # heldout_p_significant / falsification / LCB>0 (same test, alpha .05)
    null_ok = zn > z05                  # null_control_delta_positive (p <= alpha)
    net_ok = ch.mean(axis=(1, 2)) > 0   # heldout_net_pnl_positive == expectancy_positive
    # walk-forward: 3 contiguous folds of the held-out sessions after 1 fit session
    folds = np.array_split(np.arange(1, S), 3)
    pos = sum(((dh[:, f, :].mean(axis=(1, 2)) > 0) & (ch[:, f, :].mean(axis=(1, 2)) > 0)).astype(int) for f in folds)
    wf_ok = pos >= 2
    # BY: candidate p vs (m_global-1) uniform nulls; candidate is significant iff BY-adjusted q <= alpha
    if m_global > 1:
        p = 1 - np.vectorize(N.cdf)(zh[:20000])
        c = sum(1 / i for i in range(1, m_global + 1))
        others = rng.uniform(size=(20000, m_global - 1))
        rank = 1 + (others < p[:, None]).sum(axis=1)
        # step-up: q_cand = min over ranks >= own rank; approximate with own rank (upper bound on power)
        by_ok_rate = float(np.mean(p * m_global * c / rank <= alpha))
    else:
        by_ok_rate = 1.0
    # cost stress: fit+heldout net after extra incremental cost must be > 0
    stress_ok = (np.concatenate([cf, ch], axis=1).mean(axis=(1, 2)) - stress_extra) > 0
    # sealed qualification window (independent): net>0, delta>0, LCB95>0
    cq, dq = arm(mu)
    qual_ok = (cq.mean(axis=(1, 2)) > 0) & (zstat(dq) > z05)
    # live-shadow confirmatory window (independent): raw p <= LORD allocation (first test)
    cs, ds = arm(mu)
    lord_ok = zstat(ds) > N.inv_cdf(1 - lord_alpha)
    stages = [("fit_delta_positive", fit_delta_pos), ("+heldout p<=.05 (also LCB, falsification)", p_ok),
              ("+null-control p<=.05", null_ok), ("+heldout net>0", net_ok), ("+walk-forward majority", wf_ok),
              ("+sealed qualification", qual_ok)]
    acc = np.ones(reps, bool)
    for name, ok in stages:
        acc &= ok; out[name] = acc.mean()
    out[f"+BY q<=.05 with m={m_global} (factor)"] = out["+sealed qualification"] * by_ok_rate
    out["+live-shadow confirm p<=LORD a_1=.0125"] = (acc & lord_ok).mean() * by_ok_rate
    out["+cost stress 25 bps (extra 12 bps)"] = (acc & lord_ok & stress_ok).mean() * by_ok_rate
    out["cost stress alone"] = stress_ok.mean()
    return out

for label, kw in (("mu=+2, sd=40, controls noise-free", dict(mu=2.0)),
                  ("mu=+2, sd=40, control arms sd=40", dict(mu=2.0, ctrl_sd=40.0)),
                  ("mu=+2, m_global=50", dict(mu=2.0, m_global=50)),
                  ("mu=+10, controls noise-free", dict(mu=10.0)),
                  ("mu=+15, controls noise-free", dict(mu=15.0))):
    r = chain(**kw, reps=100_000)
    print("==", label)
    for k, v in r.items():
        print(f"   {k:45s} {v:.3g}")
# analytic single-test power at 30 sessions x 5 trades, sd 40
se = 40/np.sqrt(150)
print("analytic one-sided .05 power at mu=2, se=%.2f: %.3f" % (se, 1-N.cdf(z05 - 2/se)))
print("mu needed for 80%% power on one .05 test: %.1f bps" % ((z05+N.inv_cdf(.8))*se))
print("P(mean over 300 trades > 12 bps | mu=2): %.2e" % (1-N.cdf((12-2)/(40/np.sqrt(300)))))
