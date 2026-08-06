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
| Pre-registered YAML setting rows | 38 across 7 strategy files |
| Hand-authored momentum variants | 24 immutable identities including baseline |
| Materialized static identities | 77 including all 7 baselines |
| Bounded LLM selector candidates | 33 eligible single-axis candidates |
| Realtime comparison arms | Up to 20 deterministic arms at shipped config: one shared baseline plus 4 pre-registered candidates in each of 4 realtime lanes (hard cap 36) |
| Adaptive exact-value variants | Dynamic; every attempted value is permanently recorded in schema 16 |

The superseded `14 maximum` wording described seven realtime strategies; it is
not a current arm count.

The active analyst's separate `:llm` scope can hold its own baseline and
candidate, adding up to two non-comparable arms. At shipped configuration the
combined maximum is therefore 22 arms (20 deterministic plus 2 `:llm` arms);
at the hard cap it is 38 (36 deterministic plus 2 `:llm` arms).

The active realtime simulator identity is `forward_feed_version: 8`. Feed v8
uses deterministic contract proposals in four realtime lanes: `momentum`,
`flush-fade`, `ls-ratio-fade`, and `scalp-maker`. Each lane receives the same
market snapshot and timestamp and owns independent paper cash, positions, risk
state, decisions, and trades. `funding-carry`, `funding-unwind`, and
`trend-multiday` remain registered offline-only models. **`ls-ratio-fade/v1`
occupies the configured demo order path** under `execution_mode:
deterministic`, replacing `momentum/phase1-v3`, which is `T0_REJECTED` and is
the only strategy the recorded corpus says something significant about
(-0.428R over 43 independent 48h episodes, t=-2.45). The replacement is a
choice among unproven mechanisms, not a promotion:
`research/plan/order-path-succession.md` holds the comparison and the
pre-committed criterion for what would earn the seat on evidence.

Feeds v1-v7 remain immutable historical rows and must not be pooled with v8
outcomes. Feed v4 is the market-data plumbing repair feed; feed v5 is the
immutable experiment-provenance fork. The analyst's actual decisions continue
in the sibling `:llm` scope for planner history; that lane is not comparable
with deterministic research lanes.

**The analyst's own decisions are still recorded, in the sibling scope
`...:llm`.** That lane sees the identical snapshot and timestamp and holds
what the LLM actually chose. It is what `research_history_context()` is built
from, so it is the record the planner learns an edge from; dropping it would
have left the analyst with no evidence of its own past decisions. The two
lanes are deliberately not comparable and live in separate scopes so no
verdict can pool them.

`funding-carry` exits on `carry_until_normalised`, not on a price target. Its
own mechanism says the return source is the carry rather than a directional
forecast, but its contract closed on a 2R price move, so it was scored on the
one thing it claims not to be forecasting - and a position could be closed at a
profit while funding was still paying, or held after the carry had gone. It now
closes when the 30-day funding percentile falls back to its median (50). The
stop is unchanged, because its falsification requires price risk over the
holding window to be charged against the carry, and the ten-day timeout remains
the outer bound. `funding-unwind` keeps `fixed_rr`: it is a directional bet on
the crowd being forced out, so a price exit is the right one for it.

The four realtime lanes use the shared deterministic snapshot. The three
offline-only models retain their historical tournament contracts because their
holding horizons make a 100-pair realtime assignment impractical. The floor is
a measurement constraint, not a claim that any model has already cleared it.

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
  multi-axis setting designed to buy sample;
- `permissive_move`: single-axis 1 ATR move threshold.

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
  directional rather than carry;
- `extreme_percentile`: single-axis 95th-percentile tail.

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
  relaxed;
- `extreme_percentile`: single-axis 95th-percentile tail.

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
  multi-axis quality filter;
- `three_quarter_horizon`: single-axis 10.5-day hold.

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
- `lower_short_tail`: short threshold falls to the 10th percentile;
- `extreme_long_tail`: long threshold rises to the 95th percentile;
- `extreme_short_tail`: short threshold falls to the 5th percentile;
- `wider_tails`: 70th/30th, the shipped order-path setting;
- `widest_tails`: 60th/40th, the end of the widening axis;
- `no_chase_tight`: entry extension capped at 1.0 ATR;
- `no_chase_loose`: the registered 3.0 ATR cap, kept as the comparison point.

**Shipped order-path setting.** This strategy occupies the demo order path
under `execution_mode: deterministic`, with `ls_high_percentile: 70`,
`ls_low_percentile: 30` and `hard_max_entry_extension_atr: 1.5`. Those come
from replaying the contract over the recorded corpus across a 7x4 grid, scored
as R-multiples on independent 48-hour episodes against a direction-matched
random baseline. Two results are worth stating because they shape what the
selector should search:

- widening the tails helped and tightening hurt. 70/30 beat 80/20 at every
  extension cap, and 85/15 and 90/10 were the worst cells everywhere. A tail
  that carries less signal than the body is what a percentile with no
  tail-concentrated information looks like;
- `long_short_percentile_30` has a median of 97.1 and a 75th percentile of 100
  over 13,637 observations, so "above the 80th percentile" describes most of
  the corpus rather than an elevated reading.

The setting is the argmax of that grid at -0.153R, not a positive result. The
contract is unproven on this evidence rather than supported, and the reason it
holds the order path is that every alternative is unproven too while momentum
is measurably worse.

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
- `tighter_spread`: spread ceiling falls to 0.01%;
- `very_strong_imbalance`: imbalance threshold rises to 0.35;
- `very_tight_spread`: spread ceiling falls to 0.005%;
- `penetration_5bps`: paper maker fills require 5 bps of later trade-through;
- `penetration_15bps`: paper maker fills require 15 bps of later trade-through.

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
| `momentum.rr.fixed_1_5` | `fixed_reward_risk=1.5` | Retired: the order path closed 35 live trades at 11.4% wins. |
| `momentum.rr.fixed_2_0` | `fixed_reward_risk=2.0` | Retired: sizing an already-negative directional edge. |
| `momentum.rr.fixed_2_5` | `fixed_reward_risk=2.5` | Retired: sizing an already-negative directional edge. |
| `momentum.rr.fixed_3_0` | `fixed_reward_risk=3.0` | Superseded because it duplicates the current baseline |
| `momentum.stop.atr_1_25` | stop floor `1.25 ATR` | Retired: sizing an already-negative directional edge. |
| `momentum.stop.atr_1_5` | stop floor `1.5 ATR` | Retired: sizing an already-negative directional edge. |
| `momentum.stop.atr_2_0` | stop floor `2.0 ATR` | Retired: sizing an already-negative directional edge. |
| `momentum.net_direction.60` | net-direction cap `60%` | Retired: sizing an already-negative directional edge. |
| `momentum.net_direction.80` | net-direction cap `80%` | Retired: sizing an already-negative directional edge. |
| `momentum.net_direction.120` | net-direction cap `120%` | Retired: sizing an already-negative directional edge. |
| `momentum.conf.floor_0_50` | confidence floor `0.50` | Superseded: proposals below the shipped 0.65 prompt floor are unobservable. |
| `momentum.conf.floor_0_55` | confidence floor `0.55` | Superseded: proposals below the shipped 0.65 prompt floor are unobservable. |
| `momentum.conf.floor_0_60` | confidence floor `0.60` | Superseded: proposals below the shipped 0.65 prompt floor are unobservable. |
| `momentum.conf.floor_0_70` | confidence floor `0.70` | Retired: the 0.70-0.80 confidence bucket went 0 for 13 live. |
| `momentum.conf.floor_0_75` | confidence floor `0.75` | Retired: confidence is anti-informative in the observable band. |
| `momentum.conf.floor_0_80` | confidence floor `0.80` | Retired: one live trade ever exceeded 0.80 confidence. |
| `momentum.discriminator.trend_alignment` | breakout discriminator `trend_alignment` | Retired as unrunnable: 2 breakout trades in 24 live round trips. |
| `momentum.discriminator.volatility_regime` | breakout discriminator `volatility_regime` | Retired as unrunnable: no breakout population to partition. |
| `momentum.cond.vol_regime` | None; conditioning axis | Does expectancy change sign between volatility compression and expansion? |
| `momentum.cond.session` | None; conditioning axis | Are thin-liquidity-hour entries disproportionately stop-runs? |
| `momentum.universe.top_5` | `universe.top_n=5` | Does the edge live in the liquid majors? Registered, not scheduled. |
| `momentum.universe.top_25` | `universe.top_n=25` | Superseded: 25 is now the shipped universe. |
| `momentum.universe.top_10` | `universe.top_n=10` | Does the previous shipped breadth beat 25? Registered, not scheduled. |

The two `momentum.cond.*` rows carry no override on purpose. A conditioning
axis partitions trades that already exist instead of dividing the sample, so it
consumes no rotation arm: `declared_research_setting()` returns `None` for a
variant with no single declared setting, and both the shadow rotation and the
selector catalog skip it. They are scored by `sweep.partition()` against the
buckets pre-registered in `research/sweeps/regime_conditioning.yaml` and
`research/sweeps/session_conditioning.yaml`.

The two `momentum.universe.*` rows are registered claims, not scheduled work.
`apply()` accepts `universe.*` unchanged, but neither `research/replay.py` nor
`agent/shadow.py` re-selects the universe — both consume the symbol set
`select_universe` already chose live — so the axis cannot be measured until the
recorder records a wider universe than the agent trades. They are therefore not
`candidate`: that status is what schedules, and it would also place both
variant IDs in the live system prompt through
`research_selection_prompt_fragment()`, forking `prompt_version` and every
attribution downstream in exchange for no measurement.

## Staged mechanisms

A mechanism no longer has to be a hand-written Python function. A proposal is
data - a claim, the payer, a falsifier, and comparisons over fields the
validated forward models already declare - validated by `agent/contract_dsl.py`
and compiled into the same callable the hand-written contracts implement.

These live apart from everything above. They are registered in
`research/cache/staging.db`, run in a `:staged` scope with one isolated
candidate paper account and one paired neutral baseline account per mechanism,
and are measured on one fixed harness rather than on their own exits: first
observed price after the signal, a structure stop with a one-ATR minimum, a 2R
target, observed taker costs both sides, and a 24h timeout. Holding the outcome
contract constant is what makes a difference between two authored mechanisms a
difference in the mechanism rather than in a lucky stop distance.

They enter at `T1_HYPOTHESIS` and cannot rise from there. Their claims are
immutable once registered, they are never pooled with the registered
strategies' comparison arms, and `research.py review-staged` gives each a
coded verdict - `NEGATIVE_EXPECTANCY`, `DIED_OUT_OF_SAMPLE` and `NEVER_FIRED`
retire one, `STARVED_OF_DATA` never does because a claim evaluated on
snapshots it could not read has not been tested.

Two sources reach the same store. `research.py author` proposes from what the
evidence has killed; `research.py stage-seed` registers the hand-written
pre-registrations in `research/staged/pre-registered.yaml`, which are kept in
version control so the wording cannot drift after results exist. The `author`
column is what distinguishes them.

The authoring request includes bounded persisted evidence when it exists:
firing and opportunity rates, conditional returns, missing-data and null-model
results, near misses, fit/held-out degradation, segment summaries, feature
distributions and correlations, and mechanism families already tested. The
system omits unavailable raw feature or regime values rather than inventing
them. Authored conditions compile through the bounded deterministic DSL; exits,
horizons, stops, targets, sizing, and network/file operations remain outside
the contract. Cross-sectional rank is rejected until a complete universe
context is wired.

### Shipped pre-registrations

Registered as replacements for `momentum` rather than as variants of it: they
are new claims with new payers, not new thresholds on a rejected one. Firing
rates are measured by replaying the compiled contracts over the 13,637
recorded symbol observations of 2026-07-29..08-05, because a threshold chosen
for how it reads rather than for what it selects either fires on half the
corpus or on none of it.

| Contract | Claim | Direction | Fires on |
| --- | --- | --- | --- |
| `funding-crowd-unwind` | The price move persistent extreme funding predicts, not the carry it pays | both | 2.47% of direction-evaluations |
| `oi-buildup-fade` | New leverage arriving at the edge of a 4h range, measured before the exit rather than after | both | 1.13% |
| `basis-crowd-fade` | Perpetual premium or discount to its own index converging | both | 0.25% |

`funding-crowd-unwind` is the directional residual of `funding-carry`, which
was rejected as a carry claim: over 116 forward trades the price component
contributed +1.969% against +0.039% from funding itself. That residual was
set aside at the time as needing its own pre-registration. The 24h staged
harness is what makes it testable at all - `funding-carry`'s own 240h
contract could never reach the sample floor.

`oi-buildup-fade` is deliberately not `flush-fade`. That contract fades open
interest *drops* - forced exits that have already happened - and is
`T0_REJECTED`. This fades open interest *builds*, and the payer is the late
entrant rather than the liquidated holder.

`basis-crowd-fade` is the registered `basis-stretch` hypothesis, which never
fired once across the whole corpus because `allow_experimental_setups_in_demo`
was false for its entire life. Its calibration is the weakest of the three and
is documented as such: `perp_index_basis_pct` was unavailable on 25,990 of
27,274 direction evaluations, so it should be expected to verdict
`STARVED_OF_DATA`. That is written into its own falsifier.

A fourth, `liq-absorption-direct`, is in the seed file as `deferred` and is
not registered. `liq_notional_1h_usd` appears in none of the 13,637
observations, so its threshold cannot be calibrated and the field is not
confirmed to populate. A staged contract that cannot fire is worse than a
missing one: in the evidence it is indistinguishable from one that fired and
lost.

## Which settings can rotate in real time

Real-time rotation accepts only one exact setting axis at a time. Settings that
change multiple parameters remain valid pre-registered tournament questions,
but they are not silently treated as one runtime axis. Each strategy therefore
keeps:

- one stable baseline;
- a bounded batch of pre-registered single-axis candidates (four by shipped
  configuration, hard cap eight per lane);
- a durable queue of remaining candidates and accepted adaptive LLM selections.

Each individual assignment still tests one exact setting. Adaptive exact-value
selections are kept as isolated single-arm assignments and are not mixed into a
static candidate batch.

The default assignment closes only after both ten elapsed days and 100
comparable paired observations. Restart restores the active assignment.

Attempts pool rather than restart. A completed attempt's decisions and trades
are carried into the next attempt's verdict when scope, strategy, both variant
identities and the full code/config identity match exactly; a differing
ancestor ends the chain rather than being mixed in. This matches what
`forward-qualify` already does with eligible completed attempts, and it is
what makes the closed-trade floor reachable at all: one ten-day attempt yields
roughly 15 closed trades at a 48h horizon, so a hundred needs about seven.
`MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS` is therefore a sample budget, not a retry
allowance. Pooled verdicts carry a `POOLED_ACROSS_ATTEMPTS` limitation
recording that returns span the chain while mark-to-market drawdown covers
only the latest attempt.

Ten days is an arithmetic floor, not a preference. The confirmation window is
the last 30% of the assignment calendar and must span at least eight distinct
six-hour episodes, so an assignment shorter than 8*6h/0.30 = 6.67 days can
only ever return `INCONCLUSIVE` on cluster count. `agent/config.py` refuses
to start below the computed minimum.

## LLM selection, verdicts, and edge authority

The live analyst may select one registered strategy or exact eligible variant.
Invalid selections and their reasons persist as `REJECTED`; accepted selections
queue without preempting active work and progress through `ACCEPTED`,
`ASSIGNED`, and `TESTED`.

Every terminal assignment receives exactly one deterministic verdict:

- `WORKED`: adequate evidence passed every conservative performance gate;
- `FAILED`: adequate evidence disproved the setting or a disqualifier applies;
- `INCONCLUSIVE`: evidence is inadequate or does not establish the difference.

An assignment aborted for operational reasons - a variant missing after a
restart, a code/config identity change mid-window, a revoked paper portfolio -
is `INCONCLUSIVE` with reason `ASSIGNMENT_VOIDED`, never `FAILED`. It produced
no market evidence, so it decides nothing about the setting.

A separate research-only LLM may explain the immutable verdict and nominate
one next registered selection. It cannot revise the verdict or authorize an
order. `WORKED` creates only `RESEARCH_ONLY` edge evidence with
`promotion_allowed=false`; nothing here automatically edits `config.yaml`,
raises a tier, changes the main strategy, or places an order.
