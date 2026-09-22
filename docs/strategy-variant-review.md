# Strategy and variant review — 19 September 2026, amended 22 September 2026

**Amendment.** The measured audit in `docs/audit-2026-09-21/` changed three
rule roots, the shipped admission scenario, the IBR width band and the
replay/runtime IBR contract. The rows and paragraphs affected are updated
below; the historical evidence sections are unchanged and describe the
configuration they were measured under. Read `SIGNAL-VALUE.md` in that
directory for the current signal assessment, and
`docs/preregistered-hypotheses.md` for the one hypothesis now under a sealed
forward test.

## Scope and key gaps

This is a maintained source-and-evidence review of every registered equity arm:
24 rule arms (12 baseline/variant pairs), 12 frozen mechanism arms, and 7
registered IBR arms (43 distinct IDs). It records what each signal actually
computes, what its variant changes, and what remains unvalidated. It is not a
strategy catalogue, profitability result, promotion decision, or trading
authorization.

The inventory was resolved read-only with `build_inventory(load_config("config.yaml"),
code_hash="source-review-only")` in the [diagnostic suite](../research/diagnostic_suite.py);
the rule-arm construction is in the [diagnostic shadow cohort](../research/diagnostic_shadow.py);
the frozen mechanism structure was read from
`mechanism_cohort()` in the [mechanism cohort](../research/mechanism_cohort.py).

The latest retained diagnostic evidence is historical and execution-limited; see
the archived [edge-results JSON](https://github.com/khanalyst/alpaca-trading/blob/8068c73deb157d14fb84e49514c8f8eb8d0e9de5/docs/edge-results-2026-09-12.json),
which is historical evidence rather than current performance:
36 rule/mechanism arms were execution-blocked and 7 runtime IBR arms were
unavailable because the corpus had historical bars but no contemporaneous
quotes. The separate legacy seven-arm IBR comparison had six negative arms and
one small positive point estimate (`ibr.range.45`, +$1.055386 over 42 fixed
one-share trades). That is not holdout evidence or proof. The source corpus
covered 30 old sessions, 86,998 bars, 8 ETFs, and 0 contemporaneous quotes.
The 12-arm counterfactual summary had 8 positive gross references, 0 positive
after modeled execution drag before fees, and 12 negative net references. The
791 overlapping trades cannot be pooled into a causal claim. No new
profitability run was performed for this review.

No positive edge is proven, and no arm is live-eligible from these results.

## Released correctness fixes and deployment

The September 18 corrections are released in commit
`79c48c90fd6449fd46f5c762384f9f42264cbb43`, present on local, GitHub, and VM
`main`. Full GitHub CI succeeded across all four test shards and the container
in [run 35360972940](https://github.com/khanalyst/alpaca-trading/actions/runs/35360972940).
The VM is pinned to image `alpaca-agent-trading:signal-fix-79c48c9`, digest
`sha256:da939d12584c9a193fb92001697d14af100c4efc28749fba7375089fa96b4067`;
services restarted **2026-09-18 18:32 UTC** and the six-service
postverification was healthy with zero restarts. A later documentation-only
commit does not require changing this image or proof identity. All 43 frozen
strategy IDs/specs, risk limits, and cost assumptions remain unchanged.

Research-bar normalization now rejects boolean/nonpositive prices, invalid
volume, row/override duration conflicts, and `bar_1m` intervals other than 60
seconds while preserving explicit duration metadata. Direct rule evaluation
validates OHLCV and explicit 60-second intervals; volume confirmation/breakout
requires positive current and prior-mean volume, while VWAP requires explicit
valid volume and a positive total but permits zero current volume. Now-aware
runtime generation requires completed bars, aware available timestamps, and
symbol consistency, with only the required SPY context for cross-sectional
evaluation.

Benchmark fit, signal quality, and replay now use the same completed,
point-in-time context at the actual subject decision time; future-observed data
cannot generate diagnostics. Historical backfill remains an explicit diagnostic
opt-in. Live SDK bar validation is tightened; the recorder rejects explicit
symbol/key mismatches and detects internal gaps in its first window. IBR
replay/live `close_confirmed` now shares the close-relative buffer predicate;
the separately hashed legacy wick mode is retained and not retuned. Direct IBR
checks reject malformed bars and range summaries in both signal and setup
paths, validate explicit minute intervals, and bound observations and ATR
history at decision time. A supplied `max_ibr_width_pct` mapping value must
be finite and nonnegative, preserving explicit-zero versus omission behavior.
Replay requires the `America/New_York` timezone configuration; aware UTC
market timestamps remain valid.

The earlier local verification passed all **2,173 discovered tests** across
disjoint shards: edge 61, factory 6, research 1,147, and runtime 959, with zero
failures, errors, or skips. Compilation and diff checks passed. Independent
synthetic parity checks across all 24 registered rule arms found no output or
ID changes on the tested valid prefixes; they do not establish market
profitability. No new profitability run, orders, resume, cancellation, or paper
activation occurred for this review. The frozen paper trial remains paused
under its old identity with zero accepted sessions/outcomes; deployment is not
paper activation.

## Pending post-release evidence

The release and paused rollout are complete. No fresh accepted count was
observed in postverification (the last pre-release state was 0/30), and there
were no quoteable opens, replay fills, or actual fills. Recompute benchmark-fit,
signal-quality, and replay diagnostics only on untouched forward data for the
same 43 frozen arms, using completed point-in-time context at each decision
time. Do not reuse prior evidence or activate from a diagnostic recompute;
historical backfill remains diagnostic-only. Fresh forward quotes/costs,
market-open latency, after-cost validation, and the paper/research evidence
gates remain open requirements. No positive edge is proven.

The main evidence gap is executable forward quotes and cost measurement. Under
the former 25 bps admission scenario the 0.30 cost/risk ceiling implied an
83.33 bps minimum stop, which no measured one-minute ATR on this universe
reached (maximum 44.8 bps), so every rule arm was vetoed. The shipped admission
scenario is now 9 bps, whose implied stop is exactly the 30 bps grammar floor;
25 bps remains the proof-time stress in `research/gates.py`. Quantity does not
change the ratio. The static bar round trip is 17 bps (4 bps spread + 12 bps
slippage + 1 bps fees). Quote-based execution uses bid/ask prices, with spread
already embedded, plus 13 bps of modeled slippage/fees. No risk-limit
or calibration change is authorized here. The next useful evidence is
untouched accepted forward quotes and costs, not five to ten more strategies on
the examined data.

Historical `c7f996a` software-contract repairs are separate from the released
September 18 fixes and from signal evidence. The
24/31 readiness contract, current-epoch census binding, recorder-error
payload/schema preservation, future-observed bar/quote exclusion, and
explicit-zero setup handling are already implemented in `c7f996a`. Historical
software verification records 2,099 full-suite tests passed in the archived
[release verification](https://github.com/khanalyst/alpaca-trading/blob/8068c73deb157d14fb84e49514c8f8eb8d0e9de5/docs/release-verification-2026-09-14.json),
and the September 15 activation receipt records `verification_passed: true`.
These software checks do not establish strategy performance or accepted trial
sessions.

## Parameter realism

A causal signal and a correctly implemented parameter do not establish an
economic edge. None of the retained settings has been validated as a profitable
operating value by the evidence reviewed here. The immediate numerical conflict
is the cost/stop geometry above, not proof that every entry threshold is wrong.

Before changing values, measure signal reach and rejection reasons alongside
ATR, range width, spread, volume ratio and time of day for each arm and symbol.
Fixed-bps thresholds are not volatility-normalized, and increasing a parameter
does not always make a filter stricter. Keep zero-trade or unavailable arms
distinct from measured losing trades. A revised anchor, unit, exit or horizon
needs a new predeclared comparison; it must not overwrite a frozen arm or be
selected because it makes the examined historical sample positive.

## Common rule contract

The 24 rule arms below are the existing `rule-strategy.v4` diagnostic shadow
cohort. They share ATR period 14, a 1 ATR stop with a 30 bps stop floor, both
sides, entry after 0 and before 390 minutes, maximum hold 90 bars, and no
breakeven or trailing stop. `opening_range_fade`, `mean_reversion`, and
`vwap_reversion` use a fixed 1.5R target; the other nine families use fixed 2R.
The paired variant changes one authored coordinate; it does not authorize a
semantic rewrite of the baseline.

## Rule baseline/variant review (24 arms)

| Family and classification | Baseline ID → active variant ID | Actual authored change and review |
| --- | --- | --- |
| `opening_range_breakout` — sound proxy, unvalidated | `rule.opening-range-breakout.0eb200d3136d80ee` → `rule.opening-range-breakout.4b10682772f81284` | Completed 15-minute opening-range close break; threshold 5 → 8 bps, with volume confirmation. This is a plausible causal proxy for continuation, not a measured auction or institutional-flow signal. Validate cost, time of day, and hold decay. |
| `opening_range_fade` — sound but unvalidated | `rule.opening-range-fade.d5785d9e70b56def` → `rule.opening-range-fade.be61630d346f0923` | 20-minute range; threshold 8 → 12 bps; no confirmation; 1.5R target. The existing predicate already requires a wick outside and close back inside the range: it is not a blind fade. Eligibility is broader than an opening overshoot; lookback 15 is inactive here. Compare predeclared entry-window and value-exit hypotheses. |
| `momentum_continuation` — sound proxy, unvalidated | `rule.momentum-continuation.fe74f73cfc7d082a` → `rule.momentum-continuation.984cbcedd4a2846a` | Lookback 12 bars; threshold 18 → 24 bps; volume confirmation (prior 12-bar volume × 1.25). The signal compares the current close with `closes[-lookback-1]` and checks direction against the previous close. Require cost-compatible decay and time-of-day evidence; institutional flow is unobserved. |
| `mean_reversion` — causal entry, unvalidated exit thesis | `rule.mean-reversion.b4e231565af56f03` → `rule.mean-reversion.75a110f082d304d1` (formerly `e8a26abe2aef631e` → `9fb941032e03c02b`) | 20-bar close z-score 1.50 → 1.75 with volatility-compression confirmation, now with an explicit 12 bps `compression_bps`: the confirmation measures the prior-window range width, and the former 45 bps default admitted every bar; 1.5R fixed target. The current bar is included in the 20-close z-score causally. There is no reversal confirmation, and the exit is fixed-R rather than a mean/value exit. Predeclare reclaim, value-exit, or shorter-horizon hypotheses; do not search z-scores on old data. |
| `trend_pullback` — active proximity axis; redundant legacy confirmation | `rule.trend-pullback.eaf3e32977d88904` → `rule.trend-pullback.2ffd1a88404dd020` (formerly `a2b51fa10dd145fd` → `946cfe961a9f767d`) | Lookback 10, slow lookback 35; the proximity coordinate is now 3 → 5 bps (formerly 15 → 20, which admitted 97.1% and 98.6% of bars against a measured 2.4 bps median deviation, and a hard 5 bps floor overrode any smaller authored value). The legacy predicate is fast-SMA polarity + near-fast + green candle for long / red candle for short, not a sequenced retracement/reclaim. Legacy trend confirmation redundantly repeats the same fast/slow test. The v5 reclaim mechanism is a separate experiment; do not silently change v4 identity. |
| `volatility_breakout` — active isolated variant, unvalidated | `rule.volatility-breakout.cc1b851a285825c5` → `rule.volatility-breakout.ba43d3a496ff37d8` (formerly `651520055477ae38` → `30aa361de5e11ca7`) | Lookback 12; prior total 12-bar range compression now 8 → 12 bps (formerly 55 → 65, which admitted 98.3% and 98.9% of bars, so the family did not select compression); 5 bps break threshold with volume confirmation. The compression field is a range bound, not ATR. Increasing the bound loosens compression; measure the resulting selectivity and cost compatibility. |
| `volume_breakout` — feed-limited volume axis, unvalidated | `rule.volume-breakout.30fece6e3f9b9fd6` → `rule.volume-breakout.a68b65c3c8e96ff4` | Prior 15-bar mean-volume multiplier 1.50 → 1.75; 5 bps price break and trend confirmation. IEX volume is feed-limited and is not same-clock historical RVOL or consolidated participation. A feed/volume thesis needs its own evidence identity. |
| `vwap_reversion` — causal signal, distinct value experiment | `rule.vwap-reversion.ab97bfe87ed566de` → `rule.vwap-reversion.cf8b38b12c9bf952` | Lookback 20; session-cumulative VWAP distance threshold 25 → 35 bps; no confirmation; 1.5R fixed target. Cumulative VWAP deviation is causal, but the target is fixed-R, not return-to-VWAP. The fair-value mechanism uses a frozen session-VWAP target and is a separate experiment. |
| `vwap_trend` — unit/definition needs care, unvalidated | `rule.vwap-trend.349de232c3d7a18c` → `rule.vwap-trend.588bc47ed64d051e` | Lookback 15; threshold 8 → 12 bps with volume confirmation. The predicate combines current price side with cumulative-VWAP shift versus the prior session prefix (`session[:-lookback]`); it is not a price-return bps threshold or slope-per-minute. Time-of-day sensitivity is therefore a separate validation question. |
| `range_expansion` — misnamed stored axis | `rule.range-expansion.a9871756f22463a5` → `rule.range-expansion.f7fd1b25f2f95ba6` | Lookback 20; current-bar range divided by the prior 20-bar mean range is 2.0 → 2.5, with a 5 bps threshold and no confirmation. The stored field is named `volume_multiplier` but means RANGE; document its unit and do not reinterpret or rename the frozen field. A wide wick with a small body can pass; body/location follow-through would be a new hypothesis. |
| `opening_drive` — implementation semantics need explicit identity | `rule.opening-drive.393e7229056fc663` → `rule.opening-drive.560520e6c8c85b1e` | 30-minute opening window; drive threshold 30 → 40 bps with volume confirmation. The implementation moves from first open to opening-window last close, then checks only a green/red current candle; it does not require holding above/below the opening endpoint. A synthetic example (open 100, endpoint 106, pullback 103, current 103.05 green) can pass. An explicit anchor/reclaim rule would be a new identity, not an arbitrary fix. |
| `cross_sectional_residual` — mischaracterized hedge thesis | `rule.cross-sectional-residual.374375e771a64864` → `rule.cross-sectional-residual.2969f7f47855f157` | Lookback 15; single-leg return less SPY with fixed beta 1; threshold 12 → 16 bps; no confirmation; fixed 2R target. This is not fitted beta-neutral residual trading or a hedge. The 19-ETF allowlist excludes SPY, GLD, TLT and other possible factor instruments. A hedged/factor interpretation needs a separate execution contract. |

The [resolved pairwise deltas](strategy-variant-deltas.json)
confirm one changed coordinate for each of these 12 pairs. The rule rows are
signal descriptions, not positive-edge classifications. “Sound
proxy” means the entry has a coherent, causal observable; it does not mean it
has passed execution costs, forward validation, or live eligibility. The
trend-pullback proximity axis, range-expansion `volume_multiplier`, and
cross-sectional residual label are documentation/identity risks rather than
evidence for changing frozen parameters.

## Frozen mechanism cohort (12 arms)

The mechanism manifest is `COHORT_ID = intraday-mechanisms.v1` with immutable
manifest hash
`13c2b58b22cd5f669d26d9f7611c4067304f6390b9e1666bf83821aa0393abe6`.
The data and manifest are frozen; changing an arm requires a new cohort
version. All arms use rule v5, ATR14, 1 ATR stop, entry before 300 minutes,
both sides, and no confirmation. “Bundle” below is intentional: the listed
fields changed together and must not be reported as one-factor evidence.

| Family / arm | Exact ID | Explicit comparator and actual change | Classification |
| --- | --- | --- | --- |
| Opening continuation 0 | `rule.opening-range-breakout.3578223ed48d29e5` | Root baseline: 15-minute range, 5 bps threshold, 60-bar hold, fixed 2R. | Baseline hypothesis |
| Opening continuation 1 | `rule.opening-range-breakout.511ac0622f1dc0bc` | vs arm 0: add completed 5-minute trend regime, lookback 3, efficiency threshold 0.4. | Intentional multi-field bundle, not one factor |
| Opening continuation 2 | `rule.opening-range-breakout.4d812cf5660e0c25` | vs arm 1: completed regime timeframe 5 → 15 minutes. | Intentional timeframe comparison |
| Opening continuation 3 | `rule.opening-range-breakout.026e91aa282aba27` | vs arm 1 (not arm 2): max hold 60 → 30 bars, with the arm-1 entry predicate. | Intentional horizon comparison |
| Trend pullback 0 | `rule.trend-pullback.54a4944ddb2cc60e` | Legacy parent: lookback 10, slow 35, threshold 15 bps, 60-bar hold, fixed 2R. | Legacy comparator baseline |
| Trend pullback 1 | `rule.trend-pullback.f7f19e02b4d866be` | vs arm 0: `entry_trigger=reclaim`, requiring the structural completed retracement/reclaim behavior. | Intentional new mechanism |
| Trend pullback 2 | `rule.trend-pullback.1afa4bc3e16ea5dd` | vs arm 1: add completed 5-minute trend regime, lookback 3, efficiency 0.4. | Intentional multi-field bundle |
| Trend pullback 3 | `rule.trend-pullback.8627ac15537e638f` | vs arm 2: add completed-close trailing stop at 1.5R. | Intentional exit comparison |
| Fair-value reversion 0 | `rule.vwap-reversion.918ecc939194518f` | Root: threshold 20 bps, lookback 20, 30-bar hold, `target_mode=session_vwap`. | Baseline hypothesis |
| Fair-value reversion 1 | `rule.vwap-reversion.36845f38771a5266` | vs arm 0: `entry_trigger=reclaim`. | Intentional reversal-confirmation comparison |
| Fair-value reversion 2 | `rule.vwap-reversion.348e64e69600cac7` | vs arm 1: add range regime plus completed 5-minute lookback 3 and efficiency 0.4. | Intentional multi-field bundle |
| Fair-value reversion 3 | `rule.vwap-reversion.e982e7a55f7d8ed1` | vs arm 2: max hold 30 → 15 bars. | Intentional horizon comparison |

For all four fair-value arms, the authored `target_r=2` field is inactive because
the actual target is the frozen session VWAP. None is a fixed-2R reclaim test.
The 12-arm historical result is therefore a mechanism diagnostic, not a winner
selection: all 12 were execution-blocked in the retained run.

## Registered IBR equity cohort (7 arms)

Each IBR variant changes one coordinate from `ibr.baseline`:

| ID | Change from baseline | Assessment |
| --- | --- | --- |
| `ibr.baseline` | No override; registered paired baseline. | Comparator only; runtime shadow was unavailable with historical/no-quote input. |
| `ibr.range.30` | `strategy.range_minutes`: 15 → 30 minutes. | Plausible selectivity/time-window comparison; its opening-range width/ATR filter selectivity can differ. Measure forward signal reach and costs. |
| `ibr.range.45` | `strategy.range_minutes`: 15 → 45 minutes. | Same duration axis with potentially different width/ATR filter selectivity; the retained legacy point estimate (+$1.055386/42 fixed one-share trades) is too small and historical to establish an edge. |
| `ibr.target.1_5r` | `strategy.target_r`: 2.0R → 1.5R. | Target-axis comparison; order/fill and after-cost behavior remain unmeasured. |
| `ibr.target.3r` | `strategy.target_r`: 2.0R → 3.0R. | Target-axis comparison; same limitation. |
| `ibr.buffer.0bps` | `strategy.breakout_buffer_bps`: 5 → 0 bps. | Setup/buffer comparison; explicit zero must remain a real zero for long and short. |
| `ibr.buffer.10bps` | `strategy.breakout_buffer_bps`: 5 → 10 bps. | Setup/buffer comparison; stricter close confirmation must be measured, not assumed. |

The mounted active strategy in `config.yaml` is `rule`; the seven IBR arms
inherit the following IBR settings through the registry/runtime adapter for
diagnostic comparison, not as an independent active IBR deployment. The
registry is the [frozen IBR variant file](../research/variants.yaml). It
contains minimum relative volume 1.0,
opening-range width between 0.25 and 3.0 horizon-scaled ATR (range width
divided by one-minute ATR times the square root of `range_minutes`; the
unscaled quotient admitted 0 of 348 measured symbol-sessions), ATR period 14,
maximum entry
extension 1R, stale limit 0.5 minutes (30 seconds) after candidate-bar
completion,
maximum spread 25 bps, latest entry 15:00, and force-flat 10 minutes before the
close. Its relative-volume candidate is candidate-bar volume divided by the
opening-range mean minute volume from the feed; it is not historical same-minute
RVOL or consolidated participation.
The setup-coercion implementation keeps an explicit 0 bps buffer as zero for both long
and short paths; that software contract is not a profitability result.

ATR includes the completed breakout bar and does not create future lookahead.
A pre-breakout ATR definition would be a new comparison, not a correction to
the current identity. Availability must use the timestamp and bar-interval
completion, `as_of`, and `observed_at` checks so every input is causal at the
evaluation time; future-observed bars and quotes must be excluded.

The diagnostic shadow models synthetic orders and fills under its replay
assumptions, but does not observe actual broker orders, actual broker fills,
broker buying power, shortability, borrow, or broker P&L. No IBR runtime arm is
live-eligible based on this review or on the historical one-share comparison.

## Ordered next research steps

1. Obtain untouched accepted forward equity quotes for the exact registered
   arms, preserving provider/feed, timestamps, spread, slippage, fee, and fill
   provenance. Keep the 24 rule, 12 mechanism, and 7 IBR identities separate.
2. Measure realized cost and signal reach by arm, side, symbol, and time of day.
   Apply the existing cost/risk gate without widening stops or changing risk
   limits to rescue a failing arm.
3. Evaluate the predeclared mechanism comparators with correlated-session and
   multiple-testing controls; do not pool overlapping trades into a causal
   winner. Treat bundle comparisons as bundles and keep each comparator exact.
4. For IBR, measure the seven one-coordinate axes with broker constraints and
   actual-fill observability. For feed-sensitive volume signals, establish a
   separate feed identity before interpreting volume as participation.
5. Keep the nonauthorizing diagnostic shadow separate from proof: it can precede
   profitability proof and is not a promotion. Broker-connected shadow,
   promotion, or live activation may be considered only after untouched forward
   evidence clears the applicable execution, risk, and evidence gates. This
   review recommends no additional strategy families.

## Verification and limitations

Coverage is 43/43 unique registered equity arms: 24 rule diagnostic-shadow
arms, 12 `intraday-mechanisms.v1` arms, and 7 IBR registry arms. The September 14
inventory review was read-only: inventory resolution from the source
builder, read-only mechanism manifest inspection, exact-ID/spec cross-check,
and consistency review against the retained 12 September edge results and the
stated cost geometry. No profitability backtest, parameter search, or live order
was performed in that review; its deployment status was not the subject of that
inventory. The archived software verification records
2,099 full-suite tests passed; that historical software result and the
activation receipt’s `verification_passed` status do not establish executable
edge or accepted trial sessions. Historical bars without contemporaneous
quotes cannot establish executable edge. The released September 18 fixes passed
all 2,173 full-suite tests and the focused synthetic parity checks described
above. This review does not replace the implementation-validation status in
the current findings.
