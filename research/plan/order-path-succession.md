# Who trades the account, and what it takes to earn that

Written before the evidence exists, so the decision cannot be made by
whoever is looking at the numbers when they arrive.

## The state this replaces

`momentum` occupied the order path because it was the only strategy with
`analyst_ready=True`, not because it had earned it. Over 2026-07-29..08-05 it
returned −8.97% on the demo account across 35 closes: 11.4% wins, mean
−0.974% per trade. It is `T0_REJECTED` in the registry. Every other
registered strategy is falsified (`flush-fade`, `funding-carry`,
`scalp-maker` at t ≈ −6 on forward evidence) or has never been measured
(`funding-unwind`, `ls-ratio-fade`, `trend-multiday`).

Leaving a falsified mechanism on the account is not a neutral holding
pattern. `risk.max_drawdown_pct` is 15; a mechanism losing ~9% a week
reaches it, and reaching it flattens the book and self-kills the process.
That would end the shadow and staged collection every other lane depends on.
The order path is not the measuring instrument — the research lanes are — so
the mechanism that would kill the instrument does not get to keep the seat.

## The state now

`strategy.execution_mode: shadow_only`. Nothing opens a position. Research
lanes are untouched: every registered contract and every staged contract is
still evaluated on every snapshot, on the same fixed 24h harness, and still
accumulates forward evidence. Open positions still exit through exchange
stops and targets, the `max_hold_hours` force-close, and every risk
reduction path. Only discretionary opens and closes have no source.

`strategy.id` stays `momentum`. It is the registry anchor for the shadow
lanes and does not participate in the evidence scope key, so changing it
would fork nothing and clarify nothing.

## What earns the seat

The successor is decided by the shortlist, not by a reading of it. All four
conditions, on one candidate, at the same time:

1. `research.py shortlist` labels it **`SUPPORTED`**. That requires at least
   100 closed forward trades (`MIN_FOR_SUPPORTED`), a positive mean, a
   bootstrap p-value that survives Benjamini-Hochberg correction at
   α = 0.05 across every candidate measured in the same family, and
   agreement between the full window and the last 30% of it by time
   (`CONFIRMATION_FRACTION`) — a candidate that died out of sample is
   `INCONCLUSIVE`, not supported.
2. It is not `RETIRING` in `research.py review-staged`.
3. Its evidence was collected at a single `forward_feed_version`, under one
   `code_fingerprint`, with G2 replay fidelity passing over the same window.
   Evidence pooled across a feed fork describes two different pipelines.
4. It has a complete forward contract, so `execution_mode: deterministic`
   can trade the exact thing that was measured. A staged contract satisfies
   this by construction: the compiled DSL callable is what the lane ran.

When all four hold, the change is mechanical and is a config edit only:

```yaml
strategy:
  id: <the successor>
  version: <its registry version>
  execution_mode: deterministic
```

A staged contract that has not been promoted into `agent/registry.py` cannot
be named in `strategy.id`. Promoting it there is the same reviewed step every
registered strategy went through, and it does not itself authorise capital:
`mode: live` still requires `T3_VALIDATED` and a signed content-addressed
packet.

## What does not earn it

- Being the best of a bad set. `SUPPORTED` is an absolute bar, not a ranking
  position. If the shortlist's top entry is `PRELIMINARY`, the seat stays
  empty.
- A promising partial window. Point 1 exists because 30 trades of positive
  mean is the sample size at which noise looks like an edge.
- Being untested. `funding-unwind`, `ls-ratio-fade` and `trend-multiday` have
  no negative evidence because they have no evidence. Swapping a known-bad
  mechanism for an unmeasured one is the exact substitution this platform was
  built to stop.
- Urgency. An empty order path costs nothing that the research lanes were
  producing. It costs demo P&L that was negative.

## The three staged replacements

Registered by `research.py stage-seed` from
`research/staged/pre-registered.yaml`, at `T1_HYPOTHESIS`, measured on
`staged.fixed_rr.15m.v1` — 24h horizon, 1 ATR structure stop, 2R target,
observed taker costs — the same harness every staged mechanism uses, so
differences between them are attributable to the mechanism rather than to
the exits.

| contract | claim | measured firing rate |
|---|---|---|
| `funding-crowd-unwind` | the price move persistent extreme funding predicts, not the carry | 2.47% of direction-evaluations (674 over the corpus) |
| `oi-buildup-fade` | new leverage arriving at the edge of a 4h range | 1.13% (309) |
| `basis-crowd-fade` | perpetual premium/discount to its own index converging | 0.25% (69) |

Firing rates are from replaying the compiled contracts over the 13,637
recorded symbol observations of 2026-07-29..08-05. At 25 symbols on a 15m
bar the first two clear the 30-trade screening floor within days and the
100-trade measurement floor within a week or two. The third will not:
`perp_index_basis_pct` was unavailable on 25,990 of 27,274 direction
evaluations, so it should be expected to verdict `STARVED_OF_DATA` and be
retired as untestable rather than as wrong. That is the correct outcome for
it and is written into its falsifier.

A fourth, `liq-absorption-direct`, is in the seed file as `deferred` and is
deliberately not registered: `liq_notional_1h_usd` appears in none of the
13,637 observations, so its threshold cannot be calibrated and the field is
not confirmed to populate. Register it after one deployment shows the field
present, with the threshold set from that distribution.
