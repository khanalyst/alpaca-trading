# Measured execution-cost model — 2026-08-30

> **Supersession note (2026-08-29 remediation):** This file name is a
> pre-existing `2026-08-30` historical artifact; its measurements are retained
> unchanged. Current remediation requires per-symbol/session 9/15/25/50-bps
> stress calibration to remain disabled by default, operator-activated, and
> bound to exact feed/provenance, disjoint held-out sessions, content hashes,
> and an artifact-wide effective-after boundary. See [the remediation
> record](trading-edge-remediation-2026-08-29.md). No calibration result here
> authorizes a candidate.

The shipped cost model applies two constants to every symbol at every minute of
the session: a 4 bps quoted spread and a 6 bps adverse-slippage charge, giving
a 17 bps round trip on bar references and 13 bps on quote references. They are
an assumption, and the completed 11 × 4 diagnostic showed them dominating every
executable result — execution drag came out at 0.162–0.179R across nine
variants spanning five families with different lookbacks, thresholds, holds and
symbols. A range that tight across that much variety is a constant divided by a
constant, not a property of the strategies.

This change set replaces the assumption with a measurement fitted to the
recorded quote corpus, and adds a runner that replays a frozen cohort under
both models so the difference is attributable per strategy and per variant.

Neither module can promote, authorize, tune, or propose. Both are fit-only.

## `research/quote_costs.py` — the fitted schedule

`measure_quote_costs` streams the corpus quotes and fits, per symbol and per
half-hour of the session:

- quoted spread in basis points of the mid — count, mean, min, max, p25,
  median, p75, p90, p95;
- displayed size at the touch, taken from the thinner of the two sides, since
  an entry and its exit cross in opposite directions over a position's life;
- session and quote coverage, rejected-row counts, feeds and providers.

Percentiles come from bounded fixed-width histograms rather than retained
observations, so an 18.9M-row corpus fits in memory. Quotes that are one-sided,
crossed, or non-positive are counted as rejected rather than allowed to shape
the fit.

`cost_model_from_schedule` builds an ordinary `CostModel` from the result:

- `spread_bps` is the measured spread at a named percentile — default `p75`, so
  the model sits above a typical quote without chasing the tail;
- `slippage_bps` is the size term: an order larger than the displayed depth
  walks the book, and with top-of-book quotes the tightest defensible charge
  for the excess is a further half spread per depth multiple, bounded by
  `max_impact_half_spreads`;
- `provenance` records the schedule hash, the cell used, and the percentile, so
  any number in a replay can be traced back to the measurement behind it.

**This is not a way to make costs smaller.** The schedule reports what the
corpus contains; a wide measured spread produces a wide model, and
`test_a_measured_model_still_obeys_the_runtime_caps` pins that a schedule
cannot license a fill the runtime would refuse. Conservatism is an explicit,
auditable percentile choice instead of a number nobody can trace.

## `research/cost_rerun.py` — the comparison

Replays each spec twice over the identical corpus, policy, and sizing, changing
only the cost schedule, and reports per variant: trades, gate refusals, net
P&L, expectancy, win rate, profit factor, and the reference / drag / net R
decomposition the diagnostic report uses.

```
python -m research.cost_rerun \
  --corpus runtime/research/recorded \
  --config config.yaml \
  --specs runtime/research/diagnostics/factory-2d5523f-stream-v2-equity-latest.json \
  --out runtime/research/diagnostics/cost-rerun.json \
  --schedule-out runtime/research/diagnostics/cost-schedule.json
```

`--specs` takes the frozen cohort from an existing factory report, so the exact
44 variants already measured are replayed rather than regenerated. Omitting it
falls back to the catalog roots and their leading coordinate variants, which
reproduces the shape of a cycle but not a past run's tuned parameters.

## What the runner already establishes

Run against the repository's synthetic fixture (a corpus quoting a known 2 bps
spread), the measured model produces a 3 bps round trip against the shipped
17 bps, and execution drag falls from 0.136R to 0.018R — a 7.5× reduction, and
the effect the diagnostic report's constant drag column predicted.

Nothing crossed zero in that fixture, correctly: its reference R is −0.597 by
construction. **A cost fix only rescues a variant whose reference R is already
positive.** On the real cohort that is range expansion (+0.085), trend pullback
(+0.097, +0.146 at a 90-bar hold) and ORB (+0.024, +0.033). VWAP trend and VWAP
reversion are negative before any cost is charged and no cost model repairs
them.

## The gate the cost fit does not move

Replaying the catalog roots at their authored `stop_atr` refuses **every**
opportunity with `stressed_cost_risk_limit`. The stressed-cost control is an
admission gate, not an expected cost:

```
stop_distance / price  ≥  stressed_cost_scenario_bps / max_cost_to_risk_ratio
                       =  25 / 0.30  =  83.3 bps
```

A better expected-cost fit does not touch it. Any variant whose stop is tighter
than 83.3 bps of price is refused in both arms, while a sufficiently wide-stop
variant can pass the same geometry gate; the gate does not widen stops for a
candidate. This is why the factory's own bounded tuning had to reach
`stop_atr = 7` before anything could execute — and why the resulting bracket
never triggers, leaving 71–84% time-expiry exits.

The runner reports the implied minimum stop and counts gate refusals per
variant so a zero-trade row stays attributable to the control that caused it
rather than reading as "the strategy found nothing".

Deciding what the stress scenario should be is a risk decision and is
deliberately not made here. The shipped 25 bps scenario remains the
default/fallback until an operator activates a valid artifact; setting one
flag is not calibration. The schedule now supplies the evidence for it: the
measured p95 and p99 spread per symbol and per half-hour is what a 25 bps
stress assumption should be checked against.

## Not addressed

- The replay takes one `CostModel` for the whole account, so the comparison arm
  uses the universe-level fit. Per-symbol costs are measured and reported in the
  schedule but applying them per fill needs a replay-side change.
- Entry, target and stop legs are charged symmetrically. A triggered stop in a
  moving market genuinely pays more than a marketable entry; separating them
  needs the same replay-side change.
- IEX remains the feed. A spread measured from IEX top-of-book is not the NBBO
  and is generally wider; the fitted numbers should be re-derived if a SIP
  entitlement lands.
