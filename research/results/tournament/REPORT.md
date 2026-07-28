# Strategy tournament

Generated 2026-07-28 12:12:46Z from `runtime/research/data`.

- instruments: 8
- bars per instrument (min): 19200
- window: 2026-01-09 to 2026-07-28
- cost scenario: `base`, exit policy: `fixed_rr`

## Harness check

**PASS** - benchmark reproduces its measured failure

Benchmark `momentum` measured -0.0741 R against an expected -0.0960 R (drift 0.0219, tolerance 0.20).

## Leaderboard

| Strategy | Tier | Trades | Expectancy % | Expectancy R | Hypotheses | Recommendation |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `trend-multiday/v1` | T2_CANDIDATE | 628 | +0.0876 | -0.0251 | 1 | HOLD - promising but not clear of the placebo/cost gates |
| `momentum/phase1-v2` | T2_CANDIDATE | 906 | -0.0553 | -0.0741 | 79 | HOLD - promising but not clear of the placebo/cost gates |
| `funding-unwind/v1` | T1_HYPOTHESIS | 116 | +2.0079 | +0.4268 | 2 | HOLD - keep collecting data; nothing testable has passed yet |
| `funding-carry/v1` | T0_REJECTED | 116 | +2.0079 | +0.4268 | 1 | REJECT - archive with the finding; do not retest without a new mechanism |
| `flush-fade/v1` | T0_REJECTED | 337 | -0.2876 | -0.1572 | 1 | REJECT - archive with the finding; do not retest without a new mechanism |
| `ls-ratio-fade` | T1_HYPOTHESIS (registered) | - | - | - | - | registered but no research contract implements it yet; see the register's notes for what blocks it |
| `scalp-maker` | T1_HYPOTHESIS (registered) | - | - | - | - | registered but no research contract implements it yet; see the register's notes for what blocks it |

## trend-multiday/v1

**Measured tier: T2_CANDIDATE** - net +0.0876% at base costs, breakeven cost 0.3408% vs 0.20% charged

> Measured T2_CANDIDATE is ABOVE the registered T1_HYPOTHESIS. Do not promote on this alone: check that this run's window and instrument count are not thinner than the evidence behind the registered tier. A promotion needs more data than a demotion, not less.

**Mechanism.** Slow adoption flows and reflexive positioning make crypto trend persist at multi-week horizons. Cost falls from roughly 15% of a typical intraday move to about 1% of a multi-day one. The payer is the mean-reversion seller who is early.

**Falsified by.** Extending the existing features to 4-14 day horizons leaves expectancy negative net of costs, or positive only in the in-sample half.

| Gate | Result | Detail |
| --- | --- | --- |
| `has_mechanism` | pass | mechanism and falsification criterion are both stated |
| `beat_nulls` | pass | signal +0.0876% beats every null |
| `survive_oos` | pass | in-sample -0.3091%, out-of-sample +0.6053% |
| `survive_costs` | FAIL | net +0.0876% at base costs, breakeven cost 0.3408% vs 0.20% charged |
| `survive_placebo` | pass | placebo -0.0426% is -49% of candidate +0.0876% |
| `mechanism_is_the_source` | pass | no isolable return source declared; not checked |
| `is_detectable` | FAIL | effect -0.0251 R is not positive; nothing to size for |

*Forward evidence: no forward evidence recorded yet.*

## momentum/phase1-v2

**Measured tier: T2_CANDIDATE** - net -0.0553% at base costs, breakeven cost 0.2140% vs 0.20% charged

> Measured T2_CANDIDATE is ABOVE the registered T0_REJECTED. Do not promote on this alone: check that this run's window and instrument count are not thinner than the evidence behind the registered tier. A promotion needs more data than a demotion, not less.

**Mechanism.** None established. Retained as the benchmark null that any new strategy must beat, and as the only strategy here whose true expectancy has actually been measured.

**Falsified by.** Already falsified: directional hit rate 45.6-47.3% at every horizon from 15m to 24h, -0.096 R at ordinary costs (t=-4.60), 0 of 79 walk-forward variants positive out-of-sample, and at zero cost both random entry timing and the inverted signal beat it.

| Gate | Result | Detail |
| --- | --- | --- |
| `has_mechanism` | pass | mechanism and falsification criterion are both stated |
| `beat_nulls` | pass | signal -0.0553% beats every null |
| `survive_oos` | pass | in-sample -0.2455%, out-of-sample +0.1937% |
| `survive_costs` | FAIL | net -0.0553% at base costs, breakeven cost 0.2140% vs 0.20% charged |
| `survive_placebo` | FAIL | candidate expectancy is not positive; placebo not decisive |
| `mechanism_is_the_source` | pass | no isolable return source declared; not checked |
| `is_detectable` | FAIL | effect -0.0741 R is not positive; nothing to size for |

*Forward evidence: no forward evidence recorded yet.*

## funding-unwind/v1

**Measured tier: T1_HYPOTHESIS** - gates return T3_VALIDATED, but this hypothesis was generated from the data it is scored on, so the result is in-sample by construction. Needs data that did not suggest it: Out-of-sample data. Specifically: funding history from a period outside 2026-05..2026-07, or enough forward shadow evidence to reach the trade count the detectability gate computes. Until one of those

**Mechanism.** Extreme funding is a POSITIONING indicator, not a yield opportunity. When funding sits at the top of its own recent range, leveraged longs are crowded and paying to stay in. Crowded leveraged positions are unstable: they are held by traders who need the move to continue in order to keep affording the position, and who are forced out when it does not. The payer is the crowded leveraged trader squeezed out of a position they could not finance. This is the opposite economics to funding-carry, whose carry claim was falsified at 2% attribution; here the carry is incidental and the return is the unwind.

**Falsified by.** Funding contributes 50% or more of the result, which would make it the carry trade already rejected; or the effect fails outside 2026-05..2026-07, the window that generated it; or the placebo reaches 25% of the candidate; or it fails to beat the random-timing and random-direction nulls.

| Gate | Result | Detail |
| --- | --- | --- |
| `has_mechanism` | pass | mechanism and falsification criterion are both stated |
| `beat_nulls` | pass | signal +2.0079% beats every null |
| `survive_oos` | pass | in-sample +1.4668%, out-of-sample +3.7247% |
| `survive_costs` | pass | net +2.0079% at base costs, breakeven cost 2.3186% vs 0.20% charged |
| `survive_placebo` | pass | placebo -0.0767% is -4% of candidate +2.0079% |
| `mechanism_is_the_source` | pass | declared source 'price' accounts for 98% of +2.0079% (funding +0.0391%, price +1.9689%) |
| `is_detectable` | pass | +0.4268 R needs 80 trades (~0.9 months at 3/day) |

*Forward evidence: no forward evidence recorded yet.*

## funding-carry/v1

**Measured tier: T0_REJECTED** - declared source 'funding' accounts for 2% of +2.0079% (funding +0.0391%, price +1.9689%) - the stated mechanism is not the source of the result

**Mechanism.** Funding is the price of leverage. When positioning is crowded the crowd pays continuously to hold, and the payer is the leveraged long in a persistently positive-funding regime. The return source is the carry itself rather than a directional forecast.

**Falsified by.** Holding the funding-receiving side through settlements does not produce positive net expectancy once price risk over the same window is charged against it.

| Gate | Result | Detail |
| --- | --- | --- |
| `has_mechanism` | pass | mechanism and falsification criterion are both stated |
| `beat_nulls` | pass | signal +2.0079% beats every null |
| `survive_oos` | pass | in-sample +1.4668%, out-of-sample +3.7247% |
| `survive_costs` | pass | net +2.0079% at base costs, breakeven cost 2.3186% vs 0.20% charged |
| `survive_placebo` | pass | placebo -0.0767% is -4% of candidate +2.0079% |
| `mechanism_is_the_source` | FAIL | declared source 'funding' accounts for 2% of +2.0079% (funding +0.0391%, price +1.9689%) - the stated mechanism is not the source of the result |
| `is_detectable` | pass | +0.4268 R needs 80 trades (~0.9 months at 3/day) |

*Forward evidence: no forward evidence recorded yet.*

## flush-fade/v1

**Measured tier: T0_REJECTED** - signal -0.2876% does not beat: null_random_timing

**Mechanism.** Liquidation engines sell at market regardless of price. That flow is price-insensitive, mechanically finite and overshoots, and whoever absorbs it is compensated. The payer is the over-leveraged trader whose margin ran out and who has no discretion about exiting. Open interest falling during an adverse move distinguishes forced deleveraging from new positioning, which should not revert.

**Falsified by.** Among the largest adverse moves, bars with open interest falling show no more 4-24h reversion than bars with open interest rising. If both subsets behave alike, OI adds nothing and the move is noise.

| Gate | Result | Detail |
| --- | --- | --- |
| `has_mechanism` | pass | mechanism and falsification criterion are both stated |
| `beat_nulls` | FAIL | signal -0.2876% does not beat: null_random_timing |
| `survive_oos` | FAIL | in-sample -0.1954%, out-of-sample -0.6100% |
| `survive_costs` | FAIL | net -0.2876% at base costs, breakeven cost -0.0431% vs 0.20% charged |
| `survive_placebo` | FAIL | candidate expectancy is not positive; placebo not decisive |
| `mechanism_is_the_source` | pass | no isolable return source declared; not checked |
| `is_detectable` | FAIL | effect -0.1572 R is not positive; nothing to size for |

*Forward evidence: no forward evidence recorded yet.*

## How to read this

No gate here tests a t-statistic. On this data a placebo reached t = 2.60 on deliberately destroyed information, so `t > 2` is not evidence - the placebo ratio is.

A tier is a claim about evidence, not about promise. `T3_VALIDATED` means every offline gate passed; `T4_CONFIRMED` additionally requires forward trades at the sample size the detectability gate computed, agreeing in sign with the backtest.

