# Strategy tournament

Generated 2026-07-28 11:30:01Z from `runtime/research/data`.

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
| `momentum/phase1-v2` | T2_CANDIDATE | 906 | -0.0553 | -0.0741 | 79 | HOLD - promising but not clear of the placebo/cost gates |
| `flush-fade/v1` | T0_REJECTED | 337 | -0.2876 | -0.1572 | 1 | REJECT - archive with the finding; do not retest without a new mechanism |
| `funding-carry` | T1_HYPOTHESIS (registered) | - | - | - | - | no pre-registration at research/hypotheses/funding-carry.yaml; write the mechanism, parameters and hypothesis count before running the test |
| `ls-ratio-fade` | T1_HYPOTHESIS (registered) | - | - | - | - | no pre-registration at research/hypotheses/ls-ratio-fade.yaml; write the mechanism, parameters and hypothesis count before running the test |
| `scalp-maker` | T1_HYPOTHESIS (registered) | - | - | - | - | no pre-registration at research/hypotheses/scalp-maker.yaml; write the mechanism, parameters and hypothesis count before running the test |
| `trend-multiday` | T1_HYPOTHESIS (registered) | - | - | - | - | no pre-registration at research/hypotheses/trend-multiday.yaml; write the mechanism, parameters and hypothesis count before running the test |

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
| `is_detectable` | FAIL | effect -0.0741 R is not positive; nothing to size for |

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
| `is_detectable` | FAIL | effect -0.1572 R is not positive; nothing to size for |

*Forward evidence: no forward evidence recorded yet.*

## How to read this

No gate here tests a t-statistic. On this data a placebo reached t = 2.60 on deliberately destroyed information, so `t > 2` is not evidence - the placebo ratio is.

A tier is a claim about evidence, not about promise. `T3_VALIDATED` means every offline gate passed; `T4_CONFIRMED` additionally requires forward trades at the sample size the detectability gate computed, agreeing in sign with the backtest.

