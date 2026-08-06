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

## What every candidate actually measures

Before choosing, each contract was replayed over the recorded corpus (13,637
symbol observations, 2026-07-29..08-05, 15 instruments) and scored as
R-multiples under one common outcome rule: structure stop at 1 ATR + 0.15
buffer, 3R target, 48h timeout, 0.10% round-trip taker cost. Fires were
collapsed to **independent episodes** first — one entry per symbol and
direction per non-overlapping 48h window — because the corpus re-observes
every symbol every five minutes and 5,867 raw fires is not 5,867 tests. The
baseline is the same rule applied at unselected times with the same direction
mix, so the week's own drift is charged to both alike.

| Candidate | Episodes | Mean R | Win % | t | vs baseline |
| --- | --- | --- | --- | --- | --- |
| `oi-buildup-fade` (staged) | 13 | +0.629 | 46.2 | 1.19 | +0.734 |
| `basis-crowd-fade` (staged) | 6 | +0.250 | 66.7 | 0.64 | +0.369 |
| `funding-crowd-unwind` (staged) | 16 | -0.164 | 37.5 | -0.55 | -0.055 |
| `funding-unwind` | 45 | -0.191 | 35.6 | -1.04 | -0.070 |
| `ls-ratio-fade` | 34 | -0.235 | 32.4 | -1.22 | -0.126 |
| `momentum` | 43 | -0.428 | 23.3 | -2.45 | -0.329 |

Nothing here is significant except `momentum`, which is significantly
**negative** — consistent with its -8.97% live result, and a useful check that
the harness measures something real. The two positive point estimates sit on
13 and 6 episodes and were calibrated on this same corpus, so they are not
evidence of an edge; they are the reason those contracts are worth measuring
forward in the staged lane, which is exactly where they are.

## The state now

`strategy.id: ls-ratio-fade`, `execution_mode: deterministic`. It replaces
`momentum` on the demo order path and places real demo orders.

This is a choice among unproven mechanisms, not a promotion. What ruled the
others out:

- **`momentum`** is the one option the data rejects rather than merely fails
  to support.
- **`funding-unwind`** is the least-refuted registered contract, but its model
  horizon is 240h against `risk.max_hold_hours: 48`. The account would force
  every position closed at a fifth of the contract's holding assumption, so it
  would trade something other than the thing being measured. It is also
  `realtime_eligible=False` for exactly this reason.
- **`trend-multiday`** has the same problem more severely (336h) and is also
  offline-only.
- **The staged contracts** are not registered strategies, so they cannot be
  named in `strategy.id` at all. Promoting a contract whose thresholds were
  fit on 13 episodes of this corpus straight onto the account is the failure
  mode this platform exists to prevent; they stay in the staged lane and
  earn the seat through the criterion below or not at all.

That leaves `ls-ratio-fade`: complete contract so it can trade
deterministically, 48h horizon matching `risk.max_hold_hours`,
realtime-eligible, adequate throughput (34 episodes/week at 15 symbols, so
roughly 57 at the configured 25), and a derivation independent of this corpus
(~210 observations from the edge-discovery study). Its measured -0.235R at
t=-1.22 is unproven rather than rejected.

### The shipped thresholds

`ls_high_percentile: 70`, `ls_low_percentile: 30`,
`hard_max_entry_extension_atr: 1.5` — the argmax of a 7x4 grid over the same
episode-level measure, at -0.153R.

Two findings from that grid matter more than the argmax itself:

- **Widening helped, tightening hurt.** 70/30 beat 80/20 at every extension
  cap tested, and 85/15 and 90/10 were the worst cells everywhere. A tail
  carrying less signal than the body is what a percentile with no
  tail-concentrated information looks like.
- **`long_short_percentile_30` is barely a discriminator.** Its median over
  13,637 observations is 97.1 and its 75th percentile is 100, so "above the
  80th percentile" describes most of the corpus rather than an elevated
  reading. This is the same measurement artifact that makes
  `funding_percentile_30 >= 80` fire on 56.3% of observations.

The extension cap is a plateau rather than an optimum: 1.5 beat 2.0 and 3.0 at
every percentile pair, and 0.8/1.0/1.2 were flat below it.

### Where the LLM takes over

`research/hypotheses/ls-ratio-fade.yaml` gained four settings so the selector
searches the axes that matter rather than only the five that move the tails
tighter — the direction the corpus says is worst. `wider_tails` (70/30) and
`widest_tails` (60/40) open the axis downward; `no_chase_tight` (1.0) and
`no_chase_loose` (3.0) make the extension cap a first-class axis with the
registered value kept as the comparison point. The nightly reviewer nominates
from this catalog and may propose exact intermediate values through the
adaptive-variant path, so tuning from here is the loop's job rather than an
edit.

## What earns the seat on evidence

`ls-ratio-fade` holds the order path by elimination, which is a weaker claim
than earning it. The criterion below is what a successor has to show to take
the seat on evidence instead — decided by the shortlist, not by a reading of
it. All four conditions, on one candidate, at the same time:

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

- Being the best of a bad set. That is how the current occupant got there, and
  it is why it holds the seat provisionally rather than being promoted:
  `SUPPORTED` is an absolute bar, not a ranking position. If the shortlist's
  top entry is `PRELIMINARY`, nothing changes hands.
- A promising partial window. Point 1 exists because 30 trades of positive
  mean is the sample size at which noise looks like an edge — and it is
  roughly the sample every candidate above sits on.
- A positive point estimate on a corpus its own thresholds were fitted to.
  `oi-buildup-fade` at +0.629R over 13 episodes is the clearest example: worth
  measuring forward, not worth trading.
- Urgency. Demo P&L is not the deliverable; the evidence is. A seat change
  costs a `code_fingerprint` fork and restarts the collection window, so it
  needs to buy more than a better-looking week.

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
