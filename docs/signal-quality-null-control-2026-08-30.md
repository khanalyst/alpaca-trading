# Signal-quality null control — 2026-08-30

The conditional forward-return screen added on 2026-08-29 reported a large
edge on a corpus containing no predictive structure. This change set corrects
the control it compares against, and gives every number it reports an error
term. Nothing here touches costs, stress limits, the exposure contract, gates,
FDR, proofs, or authorization state; the screen remains fit-only and
non-authorizing.

## The defect

`_control_indices` drew its null from every bar in the candidate's session.
Candidate signals are not uniform in time — opening-anchored families cannot
fire until their range completes, the VWAP families need a session prefix, and
every discovery variant sets an explicit `entry_after_minutes` /
`entry_before_minutes`. Intraday returns carry strong time-of-day structure, so
the difference between a concentrated candidate and a uniform null measured the
gap between two clocks and attributed it to the predicate.

Measured on a corpus of deterministic white noise plus a time-of-day drift
confined to the entry window — where any long entry in the window earns the
drift and the signal adds nothing:

| horizon | candidate | control | candidate − control |
|---|---|---|---|
| 15m | +56.28 bps | +9.66 bps | **+46.62 bps** |
| 30m | +57.69 bps | +14.27 bps | **+43.42 bps** |
| 60m | +55.66 bps | +9.99 bps | **+45.67 bps** |

Against a 17 bps bar-reference hurdle that reads as a 2.7× edge. Drawing the
null at the candidate's own clock instead shows the signal is in fact slightly
worse than an arbitrary entry at a comparable time.

## Changes

### 1. The null is matched on the clock

The control is now the same instrument at the same session minute on every
*other* session in the corpus, restricted to bars the rule was admissible to
enter on: a mature prefix, a contiguous feature window, and a timestamp inside
the spec's own entry window. Admissibility reads the executable predicate's
bounds through the new `entry_window_bounds` and `session_minutes` helpers in
`agent/contracts/rule.py` rather than a copy that can drift from it.

Restricting to the entry window alone is not sufficient and was measured not to
be: a rule that fires as early as its window allows still sits systematically
ahead of a same-session draw spread across that window (a 5.2-minute residual
gap on the corpus above, worth ~30 bps of the drift). Same-session tiers are
retained only as a fallback for corpora too small to supply a cross-session
match, ordered tightest-first, and the tier actually used is reported in
`control_matching_counts`.

`candidate_mean_session_minute` and `control_mean_session_minute` are reported
so any residual clock gap stays visible instead of being absorbed into the
result.

### 2. Every mean carries its error

`mean_forward_return_bps` and `candidate_minus_control_bps` now report sample
standard deviation, standard error, and a t-statistic; `after_hurdle_t_stat`
centres the candidate distribution on the cost hurdle. The text and markdown
renderers print the t beside each mean. A point estimate with no error term is
what allowed a 47-trade replay to read as a finding, and the screen was
reproducing that failure one level up.

### 3. The null averages its pool

The control previously selected one pool member by hash. A single draw carries
the same variance as the candidate itself and inflated the paired standard
error by roughly √2 — measured at 1.6×–1.8×, which is about three times the
observations for the same detection power. The pool is now averaged, which is
also strictly more deterministic than hash selection.

## Verification

`tests/research/test_signal_quality_null.py` pins both directions on corpora
whose answer is known by construction:

- a drift-only corpus with no predictive structure, where the matched null must
  leave under 20% of the raw conditional return and |t| < 2;
- a session-varying conditional edge, where the matched null must still be
  cleared at t > 3, so the fix cannot be satisfied by a control that simply
  zeroes everything.

It also pins that the null is drawn at the candidate's own session minute
inside the entry window, that the pool is larger than one draw, that each
reported t equals its mean over its standard error, and that an empty screen
reports absent error terms rather than zeroes.

The screen's schema is `signal-quality.v2`: the control's meaning changed, so a
consumer must not read the new shape as the old one.

Runtime is unchanged (14.03s → 14.05s on a 48,000-bar corpus across six
horizons). The eligible null pool is now built once per session per horizon
rather than once per candidate per horizon.

## Not addressed

These remain open and are not touched here:

- The screen still measures the first actionable signal per symbol-session, so
  its sample is bounded by the exposure contract rather than by the data. The
  power problem is unchanged.
- The return basis still spans the signal close to the horizon close, which
  includes the close-to-next-open transition a fill cannot capture.
- IEX remains the authorizing feed, and the 2026-08-29 whole-session continuity
  requirement for opening-anchored families reduces the sessions available to
  those families on gap-prone symbols.
