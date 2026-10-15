"""Intraday variance ratios VR(q) = Var(q-bar return) / (q * Var(1-bar return)), within-session only.
VR < 1: mean reversion at that horizon; VR > 1: momentum. Pooled over symbols; session-block
bootstrap (resampling whole sessions) for a 95% interval. Diagnostic only."""
import json, sys, collections, numpy as np
D = sys.argv[1]
def load(p):
    raw = json.load(open(p))["bars"]; out = {}
    for s, rows in raw.items():
        by = collections.defaultdict(list)
        for r in rows: by[r["session"]].append(r)
        out[s] = {d: np.array([r["close"] for r in sorted(v, key=lambda r: r["minute"])]) for d, v in by.items()}
    return out
def vr_by_session(B, q, skip_first=0):
    per = collections.defaultdict(lambda: [0.0, 0, 0.0, 0])   # session -> [sum r1^2, n1, sum rq^2, nq]
    for s, S in B.items():
        for d, c in S.items():
            c = c[skip_first:]
            if len(c) < q + 2: continue
            lr = np.diff(np.log(c)); lq = np.log(c[q:]) - np.log(c[:-q])
            lq = lq[::q]   # non-overlapping
            p = per[d]; p[0] += float((lr ** 2).sum()); p[1] += len(lr); p[2] += float((lq ** 2).sum()); p[3] += len(lq)
    return per
def vr(per, keys):
    a = sum(per[k][0] for k in keys); n1 = sum(per[k][1] for k in keys); b = sum(per[k][2] for k in keys); nq = sum(per[k][3] for k in keys)
    return (b / nq) / (q_ * a / n1)
rng = np.random.default_rng(1)
for label, path, qs in (("1m (22 sessions)", f"{D}/bars_1m.json", (2, 5, 15, 30, 60)), ("5m (42 sessions)", f"{D}/bars_5m.json", (2, 3, 6, 12)), ("60m (487 sessions)", f"{D}/bars_60m.json", (2, 3))):
    B = load(path)
    for skip in ((0, 15) if "1m" in label else (0, 3) if "5m" in label else (0,)):
        for q in qs:
            q_ = q
            per = vr_by_session(B, q, skip)
            keys = list(per)
            point = vr(per, keys)
            boots = [vr(per, list(rng.choice(keys, len(keys)))) for _ in range(500)]
            lo, hi = np.percentile(boots, [2.5, 97.5])
            print(f"{label:20s} skip_first_bars={skip:2d} q={q:3d}  VR={point:.3f}  95% session-bootstrap [{lo:.3f}, {hi:.3f}]")
