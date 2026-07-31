# Strategies, hypotheses, and variants

This is the canonical, human-readable definition of every strategy currently
registered by the system. It explains what each strategy does, why it might
work, what would disprove it, how paper outcomes are measured, and which
settings are eligible for testing.

The executable sources remain authoritative when prose and code disagree:

- `agent/registry.py` defines strategy identity, mechanism, falsification,
  tier, timeframe, and evidence;
- `agent/contracts/` defines the exact signal conditions;
- `agent/forward_models.py` defines entry, stop, target, holding, and cost
  assumptions used by paper simulation;
- `research/hypotheses/*.yaml` defines pre-registered research settings;
- `research/variants.yaml` defines hand-authored momentum variants;
- `agent/hypotheses.py` defines bounded numeric hypotheses exposed to the
  active momentum analyst.

## Current inventory

| Layer | Current shipped count/use |
| --- | --- |
| Registered strategies | 7 mechanism/falsification claims |
| Historical/tournament setting rows | 22 across 7 YAML files |
| Hand-authored momentum variants | 16 immutable identities including baseline |
| Materialized static identities | 53 including all 7 baselines |
| Bounded LLM selector candidates | 33 eligible single-axis candidates |
| Active real-time arms | 14 maximum: baseline plus at most one candidate per strategy |
| Historical tournament coverage | 5 strategies and 16 applicable setting results; 2 remain `NOT SCORED` |
| Adaptive exact-value variants | Dynamic; every attempted value is permanently recorded in schema 14 |

All seven strategies receive the same market snapshot and timestamp. Each has
independent paper cash, positions, risk state, decisions, and trades. Only
`momentum/phase1-v3` is connected to the configured demo order path.

## Strategy definitions

### Momentum (`momentum`, version `phase1-v3`)

**Plain definition.** Follow a completed short-term impulse when trend,
breakout, or funding-reversal evidence supports it. This is the configured demo
strategy, but it is retained primarily as the benchmark null that new ideas
must beat.

**Exact current signal families.**

- `trend_continuation`: 1h and 4h trends agree, the 15m trend does not oppose
  them, and 1h momentum points in the same direction.
- `range_breakout`: price is in the outer 15% of its 24h range, relative volume
  is at least 1.0, momentum points through the range edge, and the higher-timeframe
  trend does not oppose the trade. Optional discriminator variants separate a
  breakout from an already-established continuation.
- `funding_squeeze`: at least 10 funding samples exist, 8h-normalized funding is
  at least 0.01% in magnitude, funding is in its 25th/75th percentile tail,
  basis and open interest are available, and the trade fades the crowded side.

**Paper outcome contract.** Enter at the first observed post-decision price;
use a structure stop with at least 1 ATR, a fixed 3R target, observed taker
costs/funding, and a maximum 48-hour hold.

**Rationale and current evidence.** No durable economic mechanism has been
established. The strategy is useful as an operational rehearsal and negative
benchmark because its failure has been measured: 115,929 signals, directional
hit rate 45.6–47.3%, negative expectancy after costs, and 0 of 79 walk-forward
variants positive out of sample.

**Falsifier.** Directional hit rate at or below 50% across horizons, negative
out-of-sample expectancy, or failure to beat random timing and inverted-signal
nulls. Those conditions have already been met, so the registered tier is
`T0_REJECTED` and it is not live eligible.

**Pre-registered tournament settings.**

- `registered`: shipped configuration;
- `target_2r`: fixed reward/risk changes from 3R to 2R;
- `wider_stop`: minimum stop changes from 1 ATR to 1.5 ATR.

### Liquidation flush fade (`flush-fade`, version `v1`)

**Plain definition.** Fade a violent price move only when open interest falls,
indicating existing leveraged positions are being closed rather than new
positions being opened.

**Exact current trigger.** Open interest must fall at least 1% over 4h,
relative 1h volume must be at least 1.2, and the 1h move must be at least
1.5 ATR. A downward flush creates a long probe; an upward flush creates a
short probe.

**Paper outcome contract.** Enter at the first post-signal price; use a
structure stop with at least 1.5 ATR, a 2R target, taker costs/funding, and a
maximum 24-hour hold.

**Rationale.** Liquidation flow is price-insensitive and mechanically finite.
The identifiable payer is the over-leveraged trader whose position is forcibly
closed. Falling open interest distinguishes this from genuine new positioning.

**Falsifier and status.** If falling-OI flushes do not revert more than
rising-OI moves, or a random-timing placebo is comparable, the mechanism does
not pay. The first offline test was negative before costs, so this is
`T0_REJECTED`. It remains in paper research as a negative control.

**Pre-registered settings.**

- `registered`: 1.5 ATR move, 1% OI drop, 1.2 relative volume;
- `move_2_atr`: single-axis 2 ATR move threshold;
- `violent_only`: 2.5 ATR move and 2% OI drop; tournament multi-axis setting;
- `permissive`: 1 ATR move, 0.5% OI drop, and 1.0 relative volume; tournament
  multi-axis setting designed to buy sample.

### Funding carry (`funding-carry`, version `v1`)

**Plain definition.** Hold the side that receives funding when funding is
extreme relative to that instrument’s own recent history.

**Exact current trigger.** At least 20 funding samples and basis data must
exist. Positive 8h-normalized funding at or above 0.01% and the 80th percentile
creates a short; the mirrored negative tail creates a long. The contract
refuses to fight a fully aligned 1h/4h trend.

**Paper outcome contract.** Enter after the 1h signal; use a structure stop
with at least 3 ATR, a 2R price target, observed taker costs and actual funding
settlements, and a maximum ten-day hold.

**Rationale.** The crowd pays continuously to maintain leveraged positioning,
so the funding-receiving side should earn carry without requiring a directional
forecast.

**Falsifier and status.** Funding must be the material source of returns. The
measured result was positive, but funding supplied only about 2% while price
movement supplied about 98%. The stated carry mechanism was therefore
falsified and the strategy is `T0_REJECTED`.

**Pre-registered settings.**

- `registered`: 80th-percentile tail with 20 samples;
- `extreme_crowding`: 95th percentile with 30 samples; tournament multi-axis
  test of whether funding attribution rises with crowding;
- `mild_crowding`: 60th percentile; a result that persists here is likely
  directional rather than carry.

### Funding unwind (`funding-unwind`, version `v1`)

**Plain definition.** Take the same direction as the carry entry, but test the
opposite economic claim: extreme funding identifies unstable crowded
positioning, and the expected return comes from price moving against that
crowd rather than from funding income.

**Exact current trigger.** At least 20 funding samples must exist. Positive
8h-normalized funding at or above 0.01% and the 80th percentile creates a
short; the mirrored negative tail creates a long. A still-aligned trend in the
crowd’s favor blocks the entry.

**Paper outcome contract.** Enter after the 1h crowding signal; use a structure
stop with at least 3 ATR, a 2R unwind target, taker costs and actual funding,
and a maximum ten-day hold.

**Rationale.** Crowded leveraged traders need continuation to finance their
positions. When continuation fails, forced exits can create a directional
unwind. Funding is treated as a positioning indicator, not the return source.

**Falsifier and status.** Reject if funding supplies at least half the result,
the effect fails outside the May–July 2026 discovery window, a placebo reaches
25% of the candidate, or it fails the random nulls. This hypothesis was derived
from the carry decomposition on the same data, so it remains
`T1_HYPOTHESIS`/in-sample-only until genuinely new forward evidence exists.

**Pre-registered settings.**

- `registered`: 80th-percentile tail with 20 samples;
- `extreme_crowding`: 95th percentile with 30 samples; tournament multi-axis
  monotonicity check;
- `mild_crowding`: 60th percentile; expectancy should weaken as crowding is
  relaxed.

### Multi-day trend (`trend-multiday`, version `v1`)

**Plain definition.** Follow an established 1h/4h trend for days, avoiding
short-term impulse timing and entering only when volatility is not already
spiking.

**Exact current trigger.** The 1h and 4h trends must agree, 1h momentum must
point in that direction, the 24h range position must be at least 55% for longs
or at most 45% for shorts, and the fast/slow ATR ratio must be no more than 2.0.

**Paper outcome contract.** Enter after a completed 4h bar; use a structure
stop with at least 2 ATR, a 3R target, taker costs and all funding settlements,
and a maximum 14-day hold.

**Rationale.** Slow adoption and reflexive positioning can persist over days.
The identifiable payer is the mean-reversion trader who fades a trend too
early. Costs consume a much smaller share of a multi-day move than an intraday
move.

**Falsifier and status.** Reject if net expectancy is negative out of sample,
no better than random timing, positive only in sample, or materially reproduced
by placebo. It is `T1_HYPOTHESIS`; the available history is short relative to
the holding period.

**Pre-registered settings.**

- `registered`: 14-day hold and 55% range-position floor;
- `half_horizon`: seven-day hold; the mechanism predicts less expectancy;
- `stronger_trend`: 70% range floor and 1.5 ATR-ratio ceiling; tournament
  multi-axis quality filter.

### Relative long/short positioning (`ls-ratio-fade`, version `v1`)

**Plain definition.** Trade an instrument’s long/short account ratio relative
to its own recent history. Despite the legacy strategy name, this is not a
naive “fade retail” rule: an unusually high ratio creates a long, and an
unusually low ratio creates a short.

**Exact current trigger.** A ratio and 30-sample percentile must exist. The
80th percentile or higher creates a long unless the 1h trend is down; the 20th
percentile or lower creates a short unless the 1h trend is up.

**Paper outcome contract.** Enter after the 1h ratio signal; use a structure
stop with at least 2 ATR, a 2R target, taker costs/funding, and a maximum
48-hour hold.

**Rationale.** The raw cross-sectional ratio has a fixed-effect problem, while
within-instrument deviations showed stronger association with future return.
The hypothesis is about relative positioning, not the absolute ratio level.

**Falsifier and status.** On more than 30 days of data, reject if a
within-timestamp shuffled placebo performs similarly. Current evidence is too
short and placebo-prone, so this remains `T1_HYPOTHESIS` and real-time-forward
only.

**Pre-registered settings.**

- `registered`: 80th/20th percentile tails;
- `higher_long_tail`: long threshold rises to the 90th percentile;
- `lower_short_tail`: short threshold falls to the 10th percentile.

### Maker spread capture (`scalp-maker`, version `v1`)

**Plain definition.** Simulate joining the best bid or ask on a tight,
two-sided book and use depth imbalance to choose which side supplies
liquidity. Missing book data always produces an explicit veto, never a
synthetic fill.

**Exact current trigger.** A valid observed book must have spread no greater
than 0.02%, at least 10,000 USDT depth on each side, and absolute depth
imbalance of at least 0.15. Bid-heavy books create longs; ask-heavy books
create shorts.

**Paper outcome contract.** Join the observed best bid for a long or best ask
for a short; assume a 0.02% maker entry fee, taker exit, no crossed spread or
entry slippage, a 0.5 ATR stop, 1R target, and four-hour timeout.

**Rationale.** The impatient taker pays the spread. Passive entry may avoid
the taker fee and crossed spread, making this the only short-horizon design in
the registry whose economics are not immediately dominated by round-trip
taker costs.

**Falsifier and status.** Reject if real book observations show fills require
more than roughly 10–15 bps penetration, because adverse selection would then
exceed the fee/spread saving. It remains `T1_HYPOTHESIS`: the forward simulator
is implemented, but historical queue position and real fill probability cannot
be reconstructed from candles.

**Pre-registered settings.**

- `registered`: 0.02% spread, 0.15 imbalance, 10,000 USDT per-side depth;
- `stronger_imbalance`: imbalance threshold rises to 0.25;
- `tighter_spread`: spread ceiling falls to 0.01%.

## Why funding carry and funding unwind are separate strategies

They intentionally share nearly the same signal and direction. The difference
is the claimed source of return:

- `funding-carry` says funding payments themselves are the edge;
- `funding-unwind` says funding only identifies crowding and price movement is
  the edge.

The outcome evaluator records funding and price attribution separately. A
positive headline return cannot make both mechanisms true.

## Bounded momentum analyst hypotheses

The active momentum analyst may propose one exact numeric value for one of
these registered hypotheses. Bounds, reasoning, exact identity, and every
acceptance/rejection event are immutable.

| Hypothesis | Definition and rationale | Registered point | Declared alternatives |
| --- | --- | --- | --- |
| `volume-thrust` | A directional impulse backed by unusually high participation should persist more reliably than a thin-book drift. | `min_relative_volume=1.5` | `1.2`, `2.0` |
| `oi-divergence` | A move accompanied by falling OI is position closing rather than new initiation and should exhaust. | `max_oi_change_4h_pct=-1.0` | `-0.5`, `-2.0` |
| `basis-stretch` | Extreme perp/index basis and an extreme funding percentile identify a crowded side worth fading. | `basis_threshold_pct=0.05`, funding tails `80/20`, 10 samples | basis `0.03`, `0.10` |

## Hand-authored momentum variants

These identities are immutable. A changed claim or setting requires a new
variant ID; status is the only field that may advance.

| Variant | Exact change | Question being tested |
| --- | --- | --- |
| `momentum.baseline` | None | Comparison floor and replay identity |
| `momentum.rr.fixed_1_5` | `fixed_reward_risk=1.5` | Does higher hit rate outweigh the smaller win? |
| `momentum.rr.fixed_2_0` | `fixed_reward_risk=2.0` | Does 2R capture more valid excursions than shipped 3R? |
| `momentum.rr.fixed_2_5` | `fixed_reward_risk=2.5` | Is an intermediate target better than the registered baseline? |
| `momentum.rr.fixed_3_0` | `fixed_reward_risk=3.0` | Superseded because it duplicates the current baseline |
| `momentum.stop.atr_1_25` | stop floor `1.25 ATR` | Does a modestly wider stop remove ordinary-noise exits? |
| `momentum.stop.atr_1_5` | stop floor `1.5 ATR` | Does a materially wider stop rescue early-but-correct trades? |
| `momentum.stop.atr_2_0` | stop floor `2.0 ATR` | Is the stop-width effect monotonic? |
| `momentum.net_direction.60` | net-direction cap `60%` | Are refused same-side entries mostly correlated duplicates? |
| `momentum.net_direction.80` | net-direction cap `80%` | Is there a better concentration/sample trade-off? |
| `momentum.net_direction.120` | net-direction cap `120%` | Does the shipped 100% ceiling suppress independent signals? |
| `momentum.conf.floor_0_50` | confidence floor `0.50` | Does the shipped floor discard profitable lower-confidence trades? |
| `momentum.conf.floor_0_55` | confidence floor `0.55` | Does a middle floor improve sample without admitting the weakest proposals? |
| `momentum.conf.floor_0_60` | confidence floor `0.60` | Does a small relaxation improve evidence without erasing expectancy? |
| `momentum.discriminator.trend_alignment` | breakout discriminator `trend_alignment` | Is a breakout specifically a transition out of chop? |
| `momentum.discriminator.volatility_regime` | breakout discriminator `volatility_regime` | Is compression versus expansion the true distinction? |

## Which settings can rotate in real time

Real-time rotation accepts only one exact setting axis at a time. Settings that
change multiple parameters remain valid pre-registered tournament questions,
but they are not silently treated as one runtime axis. Each strategy therefore
keeps:

- one stable baseline;
- zero or one active single-axis candidate;
- a durable queue of remaining candidates and accepted LLM selections.

The default assignment closes only after both three elapsed days and 100
comparable paired observations. Restart restores the active assignment.

## LLM selection, verdicts, and edge authority

The live analyst may select one registered strategy or exact eligible variant.
Invalid selections and their reasons persist as `REJECTED`; accepted selections
queue without preempting active work and progress through `ACCEPTED`,
`ASSIGNED`, and `TESTED`.

Every terminal assignment receives exactly one deterministic verdict:

- `WORKED`: adequate evidence passed every conservative performance gate;
- `FAILED`: adequate evidence disproved the setting or a disqualifier applies;
- `INCONCLUSIVE`: evidence is inadequate or does not establish the difference.

A separate research-only LLM may explain the immutable verdict and nominate
one next registered selection. It cannot revise the verdict or authorize an
order. `WORKED` creates only `RESEARCH_ONLY` edge evidence with
`promotion_allowed=false`; nothing here automatically edits `config.yaml`,
raises a tier, changes the main strategy, or places an order.
