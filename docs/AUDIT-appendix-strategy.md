# Strategy / hypothesis-space audit — branch claude/trading-strategy-audit-zveg57
Domain: family distinctness, reachable parameter space, search accounting.
Status legend: CONFIRMED = read in code and/or reproduced by running it. SUSPECTED = inferred.

---
## Empirical setup (reproducible)
`/tmp/.../scratchpad/gen.py` + `overlap.py`. Synthetic 1-min NY-session bars (390/session,
px0=450, 1-min sigma 6bps = realistic SPY), 5 regimes (trend / meanrev / noise / quiet /
volatile) x 12 sessions = 23,400 bar-evaluations per family. Every family evaluated at
`DEFAULT_RULE_SPEC` (rule.py:57-72) with only `family` swapped, on IDENTICAL bars.
Signal key = (regime, session, bar_index, direction).

Signal counts:
```
opening_range_breakout      12397     vwap_reversion              15280
opening_range_fade             92     vwap_trend                   5768
momentum_continuation        9149     range_expansion              2227
mean_reversion              10796     opening_drive                8291
trend_pullback               5532     volume_breakout               180
volatility_breakout            22
```

---

## CRITICAL-1 — Three families are strict SUBSETS of other families (containment 100%)
CONFIRMED, measured.

| A | B | \|A∩B\| / min(\|A\|,\|B\|) |
|---|---|---|
| volume_breakout ⊂ momentum_continuation | 180/180 | **100.0%** |
| volatility_breakout ⊂ range_expansion | 22/22 | **100.0%** |
| volatility_breakout ⊂ momentum_continuation | 22/22 | **100.0%** |
| volume_breakout ⊂ range_expansion | 157/180 | 87.2% |
| vwap_trend ⊂ opening_range_breakout | 5615/5768 | **97.3%** |

Not one signal of `volatility_breakout` or `volume_breakout` is an event
`momentum_continuation` did not already emit, same bar and same side. Structurally this is
forced by the predicates (rule.py:679-694 vs 671-675): all three are "close exceeded the
trailing extreme by `threshold_bps`". `volume_breakout` = momentum + a volume gate;
`volatility_breakout` = momentum + a prior-range-width gate. They are momentum_continuation
with an extra AND-clause, i.e. **filters, not hypotheses**. Under any multiple-testing
correction that treats the 11 families as independent draws, these three consume alpha
budget for evidence that is a literal subset of another family's evidence.

## CRITICAL-2 — Families come in exact anti-correlated PAIRS: same bar, opposite side
CONFIRMED, measured (same bar, flipped direction):
```
vwap_reversion  vs vwap_trend            99.4% opposite   (0.0% same-direction)
mean_reversion  vs volume_breakout      100.0% opposite   (0.0% same-direction)
mean_reversion  vs volatility_breakout  100.0% opposite   (0.0% same-direction)
opening_range_breakout vs vwap_reversion 88.4% opposite
momentum_continuation  vs mean_reversion 74.5% opposite   (0.03% same-direction)
mean_reversion  vs vwap_trend            70.0% opposite
```
`vwap_reversion` and `vwap_trend` read the SAME statistic (`close/vwap - 1`, rule.py:697-717)
and act on opposite signs of it. `momentum_continuation` and `mean_reversion` likewise.
Testing both members of such a pair is not two hypotheses — it is one hypothesis and its
negation. If the mean-reverting side loses, the trend side wins by construction, so a search
over 11 families is effectively a search over ~5 directions plus their mirrors. Any FDR /
family-wise correction that assumes even weak positive dependence is mis-specified here:
the dependence is strongly NEGATIVE for these pairs, which makes a "discovery" on one side
mechanically likely whenever the other side fails.

## HIGH-1 — Two families are effectively dead at their own defaults and templates
CONFIRMED. Over 23,400 evaluations at `DEFAULT_RULE_SPEC`:
- `volatility_breakout`: 22 signals (0.09%)
- `volume_breakout`: 180 signals (0.77%)
- `opening_range_fade`: 92 signals (0.39%)
vs 12,397 for `opening_range_breakout` on the identical bars. `volatility_breakout`
(rule.py:679-687) requires the prior `lookback`-bar range width <= `compression_bps` AND the
current close to exceed that range's high by `threshold_bps`. At the default 15/45bps that
means the single breakout bar must travel >1/4 of the entire compressed 15-minute range in
one minute — a near-self-contradictory conjunction. The two conditions pull in opposite
directions by construction.

## CRITICAL-3 — Every family is a STATE predicate, not an EVENT predicate; the simulator
## takes only the first firing per session, so most families collapse to "time of day"
CONFIRMED (code + measured).

`_simulate_trade` (research/factory_core.py:275) scans bars in order and **`return`s the
trade dict on the first bar whose signal is non-None** (factory_core.py:561), falling
through to `return None` (factory_core.py:605). One trade per spec per session, maximum.

Measured repeat factor = signals / distinct consecutive same-direction runs:
```
family                      signals  runs  repeat  % of eligible bars firing
vwap_trend                     5768    79   73.0x       26.0%
opening_range_breakout        12397   424   29.2x       55.8%
vwap_reversion                15280  1016   15.0x       68.8%
mean_reversion                10796  2897    3.7x       48.6%
opening_drive                  8291  3144    2.6x       37.3%
momentum_continuation          9149  3843    2.4x       41.2%
```
None of the predicates in `evaluate_rule_signal` (rule.py:600-757) has any first-cross /
de-bounce / state-change condition. `opening_range_breakout` is true on 55.8% of all
post-opening bars — "price is above the morning high" is a *condition*, not a breakout.
`vwap_trend` re-asserts for 73 consecutive bars on average.

Trading consequences:
1. The only bar that is ever traded or measured is the **earliest bar of the day** where
   the state holds. So the family's edge estimate is an estimate of one fixed entry per day.
2. Tuning `threshold_bps` / `lookback` therefore mostly moves the **clock time of that one
   entry**, not the selectivity of the signal. A parameter sweep across these families is
   largely a time-of-day sweep in disguise, which is exactly the axis that overfits fastest
   on intraday ETF data.
3. Max achievable sample per spec = number of sessions in the corpus. Every per-spec
   statistic in the gate is built on <= N_sessions observations.
4. The three families that are genuinely event-like (`opening_range_fade` 1.0x,
   `volatility_breakout` 1.0x, `volume_breakout` 1.0x) are exactly the three that almost
   never fire (HIGH-1), so the search has no family that is both selective and alive.

## RESOLVED — is `opening_range_fade` the inverse of `opening_range_breakout`?
CONFIRMED: **no, but worse — they are the two outcomes of one conditioning event.**
Measured same-bar same-direction overlap 0/12397; same-bar opposite-direction < 30%.
Reading rule.py:658-668: ORB-long needs `close > high*(1+t)`; ORF-short needs
`current.high > high*(1+t)` AND `close < high`. The two are mutually exclusive by
construction on a single bar (one requires the close outside, the other inside), which is
why the overlap is exactly zero — that zero is a tautology, not evidence of independence.
Both condition on the identical event "price traded through the opening-range extreme"; ORB
bets it continues, ORF bets it fails. Testing both is one directional coin flip evaluated
from both sides. But the asymmetry is severe: ORB re-fires as a state (12,397) while ORF is
event-like (92), so the pair is not even a fair coin — ORB gets ~135x the exposure.

## HIGH-2 — The one traded bar per session is pinned to the front of the session
CONFIRMED (measured). Because only the first firing is traded, the realized entry is the
EARLIEST bar at which the state predicate is true. Median first-firing minute after 09:30
(scan started at minute 20; earliest evaluable bar at default lookback=15/atr_period=14 is
minute 15, so "20" means "as early as it possibly could"):
```
family                    median  p10   p90     sessions with any trade (of 60)
opening_drive                 20   20    21      38
vwap_reversion                20   20    53      60
vwap_trend                    20   20    26      34
mean_reversion                20   20    26      60
momentum_continuation         21   20    35      60
opening_range_breakout        26   20   106      57
range_expansion               31   20    55      48
trend_pullback                41   39   107      60
```
Seven of eleven families have p10 at the earliest evaluable bar. In practice the factory is
measuring **one fixed early-session entry per day per family**, and the family parameters
mostly shift that clock time. That is the single most overfittable axis on intraday data,
and it is being explored implicitly with no acknowledgement in the hypothesis text
(`_thesis`, factory_core.py:74).

## HIGH-3 — min_trades=100 is arithmetically out of reach for most corpora
CONFIRMED (code). `PROTOCOL_BACKTEST_MIN_TRADES = 100` (research/gates.py:72) and the
held-out leg is floored with the SAME `min_trades` (strategy_factory.py:1422-1424, after a
70/30 chronological split at :1405). With one trade per (symbol, session)
(factory_core.py:646, 654-657, 561), total trades = symbols x sessions. Therefore a spec
needs >= 100 held-out (symbol, session) pairs, i.e. >= ~333 (symbol x session) pairs total.
On a single-symbol corpus that is ~16 months of continuous data before any spec can clear
the structural floor once. Any family that fires on only a subset of sessions (measured
above: `volatility_breakout` 20/60, `vwap_trend` 34/60, `opening_range_fade` 27/60) needs
proportionally more. This is a power problem, not a bug, but it means the families with the
most selective (i.e. most plausible) predicates are the ones structurally guaranteed to be
retired as "underpowered" rather than tested.

## CRITICAL-4 — v2 `confirmation` + `confirmations` is a 4x-redundant encoding; the dedup
## key does not collapse it, and the SAME mutation batch ships behavioural duplicates
CONFIRMED (measured, `scratchpad/v2collide.py`, `scratchpad/confalias.py`).

`_confirmations_pass` (rule.py:527-535) applies the scalar `confirmation` and every entry of
the v2 `confirmations` list identically — the effective rule is the SET
`{confirmation} \ {"none"} | set(confirmations)`. Enumerating the grammar:
```
32 nominal (confirmation x confirmations) states
-> 32 distinct rule_semantic_signature() values      (rule.py:216-224)
->  8 distinct behaviours
```
e.g. behaviour `{trend, volatility, volume}` has **7 different encodings**, each with its own
`rule_variant_id` and its own semantic signature.

Concretely: `{schema:v1, confirmation:"trend"}` vs
`{schema:v2, confirmation:"none", confirmations:["trend"]}` are identical on all 1850 bars
tested (0 divergences) yet produce
`rule.momentum-continuation.d52b1399b5d9dd4d` vs `rule.momentum-continuation.c4a84f6c0689e4a3`
and `rule_semantic_distance = 0.182` (rule.py:227-256) — the "distance of zero means
semantic equivalence" contract in the docstring is false for this pair.

**This is live in the search loop.** `coordinate_mutation_pool` (factory_core.py:978-1017)
sweeps `confirmation` and `confirmations` as two separate coordinate axes
(`_COORDINATE_FIELDS`, factory_core.py:915-920; `_coordinate_values`, factory_core.py:955-965).
Run on a v2 root: **pool of 28 variants contains 25 distinct behaviours — 3 exact duplicate
pairs in one batch**:
```
filters=['trend']       -> rule...4085da5ad112dc75 AND rule...c4a84f6c0689e4a3
filters=['volume']      -> rule...0f5aa9816ab7df9b AND rule...1d61d81ef2163d1e
filters=['volatility']  -> rule...a2536b0dd8fa4adf AND rule...bcac669fc9217a9d
```
The `rule_semantic_signature(candidate) == root_signature` guard at factory_core.py:1010
only catches identity with the ROOT, never sibling-vs-sibling. Both members of each pair are
replayed, both get a `p_raw`, and both are fed to `benjamini_hochberg`
(strategy_factory.py:2259-2277). Consequences at a trading level:
- m is inflated by ~11% in this batch, so genuine candidates are held to a stricter BH cutoff.
- The duplicated rule gets **two independent draws at the same significance threshold** while
  its p-values are perfectly correlated (identical trade rows), which is precisely the
  dependence structure BH is not valid under.
- `collapse_behavior_aliases` exists (strategy_factory.py:2188) but the code explicitly
  chooses `task["excluded_behavior_aliases"] = []` (strategy_factory.py:2199) and keeps
  every alias in replay AND in BH ("Measurement-first", :2196-2198). So the alias detector
  is a report, not a control.

## ANSWER — Q5, v1/v2 signature collision
CONFIRMED both directions:
- v1 and v2-at-all-defaults collapse to the same `rule_semantic_signature` (intended,
  rule.py:216-224) but to **different `rule_variant_id`s** (`rule_spec_hash` includes
  `schema` and the extension fields, rule.py:443-447). So the storage id and the dedup id
  disagree: the same rule can be stored twice under two ids and only the signature-based
  dedup catches it — and `_variant_keys`/`_variant_seen` (strategy_factory.py:440-450) keys
  on variant ids.
- A v2 spec with a NON-default extension cannot collide with a v1 signature via the
  extension fields, but CAN collide behaviourally through the `confirmation`/`confirmations`
  aliasing above, which the signature does not detect.

## Q4 — session anchoring (vwap_*), verified: no defect there, but the guarantee is
## in the callers, not in the evaluator
CONFIRMED by test (`scratchpad/session_leak.py`): prepending a full prior session (prices
shifted +5%) to day 2 changes ZERO signals for `vwap_reversion` and `vwap_trend`
(`_session_prefix`, rule.py:565-585). `feature_window_bars` correctly returns `None` for both
(rule.py:161-188).

However the same test shows **`trend_pullback` DOES change across the day boundary — 8 of
370 bars differ** (day2-only fires 140 times, day1+day2 fires 148). Cause: the trailing-window
families read `bars` raw (rule.py:611, 671-694) with no session filter, so at the start of a
session the prepended prior-day closes enter the 40-bar SMA across the overnight gap. This is
currently unreachable because both callers pre-slice to one session
(`factory_core.py:646,654`; `_rule_runtime_bars`, agent/engine_cycle.py:96-98,118), so it is
LOW severity — but the invariant is asserted in a comment (rule.py:37-40) about the vwap
families only, and nothing in `evaluate_rule_signal` enforces it for the other nine.

## Q3 — `slow_lookback > lookback`: ENFORCED
CONFIRMED. rule.py:376 `if spec["slow_lookback"] <= spec["lookback"]: raise`. Applied to every
family, not just `trend_pullback`. Side effect worth noting: it is enforced even for the eight
families that never read `slow_lookback`, so a `lookback` mutation on e.g. `mean_reversion`
can be rejected for a reason that has no bearing on that family's signal
(`_safe_variant`, factory_core.py:857 swallows it as a skipped coordinate).

## HIGH-4 — Most of the declared parameter box is either signal-dead or behaviourally
## degenerate; the declared bounds advertise a search volume that does not exist
CONFIRMED (measured, `scratchpad/reach.py`). Empirical distribution of the statistic each
bound gates, on 1-min ETF-realistic bars (sigma 6bps/bar, incl. a 3x-sigma "volatile"
regime — already more extreme than SPY):
```
statistic                p50    p90    p99   p99.9    max
|L-bar return| bps     16.06  88.12 134.78  177.72  178.03   <- threshold_bps gates this
15-bar range bps       39.23  99.70 154.96  199.05  199.07   <- compression_bps
|zscore|                1.36   2.42   3.45    4.98    6.28   <- zscore
volume / mean(volume)   0.89   1.76   3.19    4.97    7.18   <- volume_multiplier
ATR14 / close bps      10.86  19.20  23.78   26.03   26.35   <- min/max_atr_bps
current/avg range       0.95   1.69   2.43    3.21    3.22   <- range_expansion multiplier
```
Resulting dead / degenerate axis fractions (rule.py:73-87, 101-106):
```
threshold_bps    [0,500]     >~178 emits ZERO signals            64.5% of axis dead
volume_multiplier[0.25,10]   >~5   emits ZERO signals            51.5% of axis dead
min_atr_bps      [0,2000]    >~26  emits ZERO signals            98.7% of axis dead
max_atr_bps      [1,5000]    >~26  is a NO-OP (never binds)      99.5% degenerate
compression_bps  [1,2000]    >~200 is a NO-OP (never binds)      90.0% degenerate
zscore           [0.25,5]    genuinely spans the distribution     0.5% dead
```
`threshold_bps=500` is a 5% one-bar move on a liquid ETF; `min_atr_bps=2000` is a 20% ATR;
`max_atr_bps=5000` is a 50% ATR. None of these are reachable states of the instrument. The
grammar's own JSON schema (rule.py:270-297) publishes these to the LLM as the legitimate
search range, so the model is being told the space is ~100x larger than it is.

## HIGH-5 — The search cannot traverse its own declared bounds
CONFIRMED (code). Every mutation step is capped at ~20% of the CURRENT value —
deterministic (`_coordinate_values`, factory_core.py:1055-1060) and LLM
(`_tuning_reason_check`, llm_strategy.py:284-299) alike — and a step is only kept if that
variant beats the gate. From the `momentum_continuation` template (threshold_bps=18):
```
field              bound        template   reachable in 10 accepted steps   % of declared axis
threshold_bps      [0,500]      18         [1.93, 111.45]                    21.9%
compression_bps    [1,2000]     45         [4.83, 278.63]                    13.7%
lookback           [3,120]      12         [3, 74]                           60.9%
stop_atr           [0.2,10]     1.0        [0.20, 6.19]                      61.1%
```
Crossing the full declared range needs 19 consecutive accepted up-steps on `threshold_bps`
and 21 on `compression_bps`, against `max_generations=5` (strategy_factory.py:1630). The
declared box is therefore not the search space; the search space is a small ball around 11
hand-written templates (factory_core.py:91-114). Any statement of the form "we searched the
audited parameter space" is false as written.

## CRITICAL-5 — Zero-trade candidates ARE counted as tested hypotheses, and they
## mechanically destroy the discovery power of the real ones
CONFIRMED (code + reproduced).

Chain:
1. A zero-trade variant gets `matched=0` from `matched_cluster_test` (research/gates.py:649)
   and therefore `p_value = 1.0` -> `gate["p_raw"] = 1.0` (strategy_factory.py:1519).
   Reproduced: `matched_cluster_test([], [], vehicle="equity")` -> `{'p_value': 1.0,
   'matched': 0, 'available': False}`.
2. It is NOT filtered out. `fit_probe` deliberately keeps them: "every intended member
   remains scheduled, **including zero-signal aliases**" (strategy_factory.py:2153-2156),
   and `collapse_behavior_aliases` refuses to group anything with
   `signal_count == 0` (research/fit_diagnostics.py:535).
3. It enters BOTH `benjamini_hochberg` families (strategy_factory.py:2259-2277), so it
   inflates `m` in the family correction and the cycle-global correction.
4. Promotion requires `multiple_tests["significant"] AND global_multiple_tests["significant"]`
   (strategy_factory.py:2318-2323).

Reproduced effect (research/stats.benjamini_hochberg, alpha=0.05), 3 genuine candidates at
p = 0.004 / 0.02 / 0.03 padded with zero-trade nulls at p=1.0:
```
pad=  0  m= 3  significant: ['a','b','c']
pad= 10  m=13  significant: []      <- even p=0.004 fails: 1*0.05/13 = 0.00385
pad= 30  m=33  significant: []
pad= 60  m=63  significant: []
```
**Ten dead candidates in a cycle are enough to make a p=0.004 real edge unpromotable.**
Given HIGH-4 (50-99% of several axes emit zero signals) and HIGH-1 (three families barely
fire at their own templates), dead candidates are the normal case, not the exception. And
they are deterministically identifiable before any replay — the fit probe already computes
`signal_count` (fit_diagnostics.py:532) and then throws the information away.

This is a power corruption, not a false-discovery corruption. It is nonetheless the more
damaging of the two here: the loop is structured so that it will report "nothing passed"
regardless of whether an edge exists.

## CRITICAL-6 — the LLM tuning lane cannot propose anything the deterministic table
## did not already enumerate
CONFIRMED (code). In `_tuned_variants`, every model-proposed variant is discarded unless
its `rule_variant_id` is already a member of the deterministic pool:
```python
allowed_pool_ids = {rule_variant_id(spec) for spec, _reason in pool}   # :863
...
if variant_id not in allowed_pool_ids:                                 # :886
    continue
```
(research/strategy_factory.py:863, 886). The pool is `coordinate_mutation_pool` /
`interaction_mutation_pool` (factory_core.py:978, 1018). So the LLM tuning lane is a
**re-ranking of a fixed finite list**, not a proposal lane. Everything downstream that
frames it as learning — `TUNING_SYSTEM_PROMPT` (llm_strategy.py:90-129), the `builds_on`
lesson-citation enforcement (llm_strategy.py:377-392), the reason grading in
`mutation_reason` ("the feedback loop can then show that a tuned reason outperformed ... the
fixed mutation table", factory_core.py:879-888) — is comparing two orderings of the same
candidate set, not two hypothesis generators. Any claim that the model "discovered" a
parameter setting is false: the deterministic table enumerated it first.

### Q6 sub-answers
- **Is the one-field/two-field refinement contract enforced in CODE?** YES.
  `_tuning_reason_check` (llm_strategy.py:265-311): `expected = 2 if phase == "interaction"
  else 1`, raises on mismatch; the <=20% local-step cap is enforced at :284-299; the
  reason must literally name every changed field (:301-311) and cite a supplied lesson
  (:317-330); `builds_on` must match a supplied lesson id (llm_strategy.py:377-392).
- **Can it propose degenerate or duplicate specs?** For TUNING, no new ones — but it can
  select the behavioural duplicates the deterministic pool itself contains (CRITICAL-4),
  and `_variant_seen` (strategy_factory.py:447) keys on `rule_variant_id` +
  `rule_semantic_signature`, neither of which collapses the confirmation aliasing. For
  DISCOVERY / REPLACEMENT the model does author new specs, guarded only by
  `_semantic_duplicate` (strategy_factory.py:463-483) which uses `rule_semantic_distance`
  and **returns early on a family mismatch** (:479-480) — so it can never detect that a
  proposed `volume_breakout` is a strict subset of an existing `momentum_continuation`
  (CRITICAL-1). Cross-family redundancy is structurally invisible to the dedup layer.

## Q7 — dead code in strategy_factory.py
No dead module-level definitions: all 47 `def`/`class` in research/strategy_factory.py are
referenced (AST + repo-wide reference scan). The dead logic is not unused functions, it is
**deliberately inert results**: `collapse_behavior_aliases` computes `proposed_exclusions`
(research/fit_diagnostics.py:541, 552-558) and the caller hard-codes
`task["excluded_behavior_aliases"] = []` (strategy_factory.py:2200, 2211). The alias
detector runs, finds the duplicates, writes them to a report, and changes nothing about
what gets replayed or corrected.

## CRITICAL-7 — 53% of the uniformly-sampled parameter space produces ZERO trades
CONFIRMED (measured, `scratchpad/space_small.py`). 40 uniform draws per family from
`_BOUNDS` (rule.py:73-87), `side="both"`, `confirmation="none"`, corpus = 3 sessions
(trend / meanrev / noise); "trade" = first-signal-per-session, exactly matching
`_simulate_trade`:
```
family                     specs  zero-trade   %dead
opening_range_fade            40          40  100.0%
volume_breakout               40          40  100.0%
range_expansion               40          39   97.5%
volatility_breakout           40          39   97.5%
opening_drive                 40          27   67.5%
vwap_trend                    40          21   52.5%
mean_reversion                40          18   45.0%
momentum_continuation         40           8   20.0%
opening_range_breakout        40           0    0.0%
trend_pullback                40           0    0.0%
vwap_reversion                40           0    0.0%
ALL                          440         232   52.7%
```
Combine with CRITICAL-5: every one of those 232 draws, if it reached replay, would carry
`p_raw = 1.0` into both Benjamini-Hochberg families and raise the bar for the ones that did
trade. Four families are >97% dead over their own declared box.
(A 200-draw / 5-session run was still executing at report time; the 40-draw result is the
confirmed figure.)

## CRITICAL-8 — `compression_bps` has two OPPOSITE meanings, making one
## (family, confirmation) combination mathematically unsatisfiable
CONFIRMED (measured + proof).
- `volatility_breakout` requires the trailing `lookback`-bar range width, in bps, to be
  **<= compression_bps** (rule.py:683-684).
- the `"volatility"` confirmation requires `ATR(atr_period)` in bps to be
  **>= compression_bps** (rule.py:556-558).

Both read the same field. A spec with `family="volatility_breakout"` and a `volatility`
confirmation therefore needs `range15_bps <= C <= ATR14_bps`, i.e. a 15-bar range no wider
than a 1-bar ATR. Measured: **0 of 1850 bars satisfy that** — it is impossible for any
lookback >= 2. Reproduced:
```
volatility_breakout conf=volatility  compression_bps=45/200/1000/2000 ->  0 / 0 / 0 / 0 signals
volatility_breakout conf=none        compression_bps=45/200/1000/2000 ->  2 /53 /  53 /  53 signals
```
`_coordinate_values` (factory_core.py:945-948) sweeps `confirmation` over all four values on
every cycle, so this provably-empty variant is generated, replayed, and given a BH slot
every single time a `volatility_breakout` slot is tuned. The same run also shows
compression_bps saturating at ~200 (53 signals at 200, 1000 and 2000 alike) — the top 90%
of that axis is one point.

## HIGH-6 — `shared_learning` pools parameter lessons across families that use the
## same field name for physically different quantities
CONFIRMED (code). `shared_learning` keys its aggregate on `(parameter_name, direction)` with
**no family key** (research/strategy_factory.py:594-602) and the digest is fed into the LLM
tuning request (strategy_factory.py:842-844). But the same field name means different
things per family:
```
threshold_bps      ORB/ORF     : distance beyond the opening-range extreme (rule.py:661-668)
                   momentum    : L-bar return                              (rule.py:671-675)
                   trend_pullback: |close - fastSMA|/fastSMA, floored 5bps (rule.py:677)
                   vwap_reversion: |close/vwap - 1|                        (rule.py:706-710)
                   vwap_trend  : vwap advance rate over lookback           (rule.py:715-719)
                   range_expansion: |close/open - 1| of the CURRENT bar    (rule.py:730-733)
                   opening_drive: net displacement of the opening window   (rule.py:751-755)
volume_multiplier  volume_breakout: volume / mean(prior volume)            (rule.py:691-693)
                   range_expansion: BAR RANGE / mean(prior bar ranges)     (rule.py:725-729)
compression_bps    volatility_breakout: range-width CEILING                (rule.py:683)
                   "volatility" confirmation: ATR FLOOR                    (rule.py:556-558)
```
Their empirical scales differ by an order of magnitude (measured p50: 15-bar return 16bps
vs current-bar |close/open| ~4bps vs |close/vwap-1| 25bps). A pooled statement like
"threshold_bps raised: 9 attempts, 6 passed" is therefore not a fact about any parameter.
`compression_bps` is the worst case: "raised helped" means loosen in one use and tighten in
the other. This is the aggregate the model is explicitly asked to reason from.

## HIGH-7 — `opening_drive` has no bar-dependent signal after the opening window closes
CONFIRMED (code + measured). rule.py:751-757: `drive = last/first - 1` is computed entirely
from the opening `range_minutes` bars and is **constant for the rest of the session**. The
only current-bar term is `close > opened` (a ~50% condition). With first-signal-only
execution, the trade is therefore taken at the first bar after the opening window whose
close exceeds its open — measured p10/median/p90 first-firing minute = **20 / 20 / 21**.
`opening_drive` is not a signal; it is "at 09:45+1, trade the direction of the first 15
minutes". `range_minutes` is its only real degree of freedom, and it selects the clock time.
That is a legitimate thing to test, but the grammar presents it as a comparable peer of the
ten bar-conditional families, and it is one of the three families whose predicate is
`opening_range_breakout`'s conditioning event under another name (70.5% containment measured).

## Resolution — ORB / opening_drive / range_expansion / volatility_breakout
CONFIRMED. They are **not** four independent hypotheses.
- `volatility_breakout` (rule.py:679-687) = `momentum_continuation`'s break-the-trailing-
  extreme predicate AND-ed with a prior-range-width ceiling. 100% contained in
  `momentum_continuation` and 100% contained in `range_expansion` (measured).
- `range_expansion` (rule.py:721-733) = "current bar's range >= k x average range AND the
  bar closed away from its open by `threshold_bps`". It is a one-bar version of the same
  breakout idea; 66.4% of its signals are also `momentum_continuation` signals, 50.0%
  also `opening_range_breakout`.
- `opening_drive` (rule.py:735-757) = opening-window displacement, 70.5% contained in
  `opening_range_breakout`.
- `opening_range_breakout` (rule.py:657-663) is the only one of the four with a
  distinct predicate (a clock-anchored level rather than a trailing statistic), and even it
  contains 97.3% of `vwap_trend`'s signals.

They are reparameterizations of one hypothesis — "price is extended in the direction it just
moved" — measured on four different windows. Treating them as four families in the
`family_corrections` BH scope (strategy_factory.py:2266-2277) gives that single idea four
independent multiplicity budgets, which is the opposite of what a family-wise correction is
for: the same idea gets four chances at a per-family cutoff instead of one.

### CRITICAL-7 update — the 200-draw / 5-session run finished and confirms the figure
```
family                     specs  zero-trade   %dead        family                  %dead
volume_breakout              200         198   99.0%        opening_drive           60.5%
volatility_breakout          200         190   95.0%        mean_reversion          31.5%
range_expansion              200         187   93.5%        momentum_continuation   24.5%
opening_range_fade           200         182   91.0%        opening_range_breakout   0.0%
vwap_trend                   200         129   64.5%        trend_pullback           0.0%
                                                            vwap_reversion           0.0%
ALL                         2200        1119   50.9%
```

## CRITICAL-9 — two of the thirteen discovery payoff shapes have NEGATIVE expectancy at
## any win rate, and they were added deliberately
CONFIRMED (arithmetic on `_DISCOVERY_SHAPES`, research/factory_core.py:1105-1115).
Using the audited 30 bps stop floor (`MIN_STOP_DISTANCE_BPS`, rule.py:55) and the 17 bps
round-trip cost, with the measured median ATR14 = 11 bps of price:
```
side  target_r stop_atr hold   stop_bps  target_gross  breakeven win rate
both   0.25     0.2       1      30.0        7.5        IMPOSSIBLE   <- gross win < cost
both   0.5      0.5      10      30.0       15.0        IMPOSSIBLE   <- gross win < cost
both   1.0      0.75     30      30.0       30.0            78.3%
both   1.25     0.75     45      30.0       37.5            69.6%
both   2.0      1.0      90      30.0       60.0            52.2%
both   3.0      1.5     180      30.0       90.0            39.2%
both   5.0      2.0     240      30.0      150.0            26.1%
both  10.0      4.0     390      44.0      440.0            12.6%
both  10.0     10.0     390     110.0     1100.0            10.5%
long   2.0      1.0      60      30.0       60.0            52.2%
short  2.0      1.0      60      30.0       60.0            52.2%
long  10.0     10.0     390     110.0     1100.0            10.5%
short 10.0     10.0     390     110.0     1100.0            10.5%
```
Shapes 1 and 2 lose money on **every trade, winners included**: a 0.25R target off a
30 bps floored stop is 7.5 bps gross against 17 bps of cost. Shape 1 also holds for 1 bar.
These are not accidents — the code comment at factory_core.py:1106-1107 says they were added
to "cover the complete audited payoff span, including low-risk-unit roots whose default
stop/target pair otherwise leaves no economic signal". Covering the span of a grammar is not
a reason to test a shape whose expectancy is negative before any data exists. 2/13 = 15% of
every discovery slot's payoff axis is guaranteed dead, and each such candidate still consumes
a BH slot (CRITICAL-5). Shapes 3 and 4 need 78% and 70% win rates on 1-minute ETF entries.

## MEDIUM-1 — the discovery ladder's "complete Cartesian traversal" reaches 9.1% of itself
CONFIRMED (measured). `MAX_DISCOVERY_ATTEMPTS = 5 x 5 x 4 x 13 = 1300`
(factory_core.py:1119-1121) and the code comment at :1116-1118 calls one traversal "the
bounded search contract". But `discovery_hypothesis` couples the ladder index to the family
rotation: `family = RULE_FAMILIES[(start + index) % 11]` while the ladder cell is
`(index%5, (index//5)%5, (index//25)%4, (index//100)%13)` (factory_core.py:1183-1185,
1137-1141). Since `lcm(11, 1300) = 14300`, each family reaches exactly **118 of the 1300
cells (9.1%)** in a full traversal, and which 118 is fixed by the family's index parity —
the other 90.9% of the conditional grammar is unreachable for that family, forever, not just
unvisited. E.g. a family can be permanently barred from ever being tested with the
`(25,120)` ATR band at the `(30,210)` entry window.

## MEDIUM-2 — `_DISCOVERY_BANDS` volatility bands are mostly outside the instrument's range
CONFIRMED. `_DISCOVERY_BANDS = ((0,5000), (0,60), (25,120), (60,5000))`
(factory_core.py:1101-1102). Measured ATR14 in bps: p50=10.9, p90=19.2, p99.9=26.0,
max=26.4 on bars that already include a 3x-sigma regime.
- `(0, 5000)` = no filter.
- `(0, 60)` = no filter (never binds; max observed 26).
- `(25, 120)` = admits only the top ~0.5% of bars -> near-zero trades.
- `(60, 5000)` = admits **nothing**; a 60 bps 1-minute ATR on a liquid ETF is a ~1500-point
  SPY day.
So 2 of the 4 bands are no-ops, 1 is empty, and 1 is a 0.5% tail. The "conditional edge"
axis the discovery prompt is built around (llm_strategy.py:71-77) has one usable setting.

---

## Repo state
`git status --short` was EMPTY at the start of this audit. I wrote nothing into the repo —
all scripts and this report live in the scratchpad. At the end of the audit `git status`
shows files changing under a concurrent process (first `M agent/risk.py`, seconds later
`M research/gates.py`, 1 insertion / 1 deletion); those edits are not mine and I have not
reverted them.

## Ranked summary
CRITICAL
1. Three families are strict subsets of others at default params (volume_breakout and
   volatility_breakout 100% contained in momentum_continuation; vwap_trend 97.3% in
   opening_range_breakout). rule.py:679-694 vs 671-675.
2. Families come in exact anti-correlated pairs (vwap_reversion/vwap_trend 99.4% opposite
   side on the same bar; mean_reversion/volume_breakout 100%). One hypothesis and its
   negation counted as two. rule.py:697-717, 677-694.
3. Every predicate is a state, not an event (opening_range_breakout true on 55.8% of bars,
   29x repeat), and `_simulate_trade` takes only the first firing per session
   (factory_core.py:275, 561). The measured object is one fixed early-session entry per day.
4. `confirmation` + `confirmations` is a 4x-redundant encoding the dedup key does not
   collapse; one `coordinate_mutation_pool` batch ships 3 exact behavioural duplicate pairs
   into BH. rule.py:527-535, 216-224; factory_core.py:915-965, 1010; strategy_factory.py:2200.
5. Zero-trade candidates get p_raw=1.0 and are deliberately kept in both BH families; 10 of
   them make a genuine p=0.004 unpromotable. gates.py:649, strategy_factory.py:1519,
   2153-2156, 2259-2277, 2318-2323; fit_diagnostics.py:535.
6. The LLM tuning lane can only re-rank the deterministic pool (`allowed_pool_ids`,
   strategy_factory.py:863, 886) — it is not a proposal lane.
7. 50.9% of uniformly sampled parameter space produces zero trades (2200 draws); four
   families >91% dead.
8. `compression_bps` is a range CEILING for volatility_breakout (rule.py:683) and an ATR
   FLOOR for the volatility confirmation (rule.py:556-558) -> that (family, confirmation)
   pair is mathematically unsatisfiable, 0/1850 bars, and is generated every cycle.
9. `_DISCOVERY_SHAPES[0]` and `[1]` (target_r 0.25 / 0.5) are negative expectancy at any
   win rate against the 30 bps stop floor and 17 bps cost. factory_core.py:1105-1115.

HIGH
1. opening_range_fade / volatility_breakout / volume_breakout are effectively dead at their
   own templates (92 / 22 / 180 signals vs 12,397 for ORB on identical bars).
2. The single traded bar is pinned to the earliest evaluable minute for 7 of 11 families.
3. min_trades=100 applied to the held-out leg needs ~333 (symbol x session) pairs per spec.
   gates.py:72, strategy_factory.py:1422-1424.
4. 64.5% of threshold_bps, 51.5% of volume_multiplier and 98.7% of min_atr_bps emit zero
   signals; 90% of compression_bps and 99.5% of max_atr_bps are no-ops.
5. The +/-20% step cap means the search covers ~14-22% of the declared axes in 10 accepted
   steps against max_generations=5. factory_core.py:1055-1060; llm_strategy.py:284-299.
6. `shared_learning` pools parameter lessons across families where the same field name is a
   different physical quantity (and, for compression_bps, an opposite inequality).
   strategy_factory.py:594-602.
7. `opening_drive` has no bar-dependent term after the opening window; it is a fixed-time
   directional bet. rule.py:751-757.

MEDIUM
1. A "complete" discovery traversal reaches 9.1% of its own Cartesian product per family.
   factory_core.py:1119-1121, 1183-1185.
2. 2 of 4 `_DISCOVERY_BANDS` are no-ops, 1 is empty, 1 is a 0.5% tail.

LOW
1. Trailing-window families read raw `bars` with no session filter (rule.py:611, 671-694);
   safe only because both callers pre-slice. Measured: trend_pullback changes on 8/370 bars
   when a prior session is prepended. vwap_* are correctly anchored (0/370).
2. `slow_lookback > lookback` is enforced globally (rule.py:376) including for the 8
   families that never read it, silently dropping otherwise-valid coordinate mutations.
3. v1 and v2-at-defaults share a semantic signature but not a `rule_variant_id`
   (rule.py:443-447), so storage id and dedup id disagree.
