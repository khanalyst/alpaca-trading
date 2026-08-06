# Edge platform: pipe repair, then redesign

Goal: run unattended for 30 days and return a ranked shortlist of candidate
edges with their configurations, each measured on real-time demo evidence,
so one can be chosen for promotion.

Reference branch: `main`. Batch 1 lands on
`fix/research-pipe-and-throughput`.

## What the 7-day VM corpus established

Journal 2026-07-29 to 2026-08-05: 1,440 decision cycles, 295,497 events,
76,036 shadow decisions, 238 paper trades.

**Momentum is finished.** The live demo account went 71,099 -> 64,720
(-8.97%) over 35 closed trades with 4 wins (11.4%) and a mean of -0.974% per
trade. At `fixed_reward_risk: 3.0` breakeven needs roughly 25% wins. This
confirms the 2024-2026 audit on live data rather than on a recomputed
backtest.

**scalp-maker is falsified on forward evidence.** Both arms, n=97 each:
mean R -0.286 (t=-5.88) and -0.309 (t=-6.25), win rates 21.6% and 19.6%.
Two independent arms agreeing at t=-6 is not noise. This is the first
strategy the platform has killed using its own real-time evidence.

**Everything else was starved rather than tested.** Of 76,036 shadow
decisions, 75,798 were vetoed and 238 became trades: a 0.31% conversion.
The dominant veto is missing order-book depth.

| Strategy | Decisions | Blocked on missing data |
| --- | --- | --- |
| funding-carry | 2,812 | 100.0% |
| funding-unwind | 2,812 | 84.4% |
| trend-multiday | 768 | 83.9% |
| flush-fade | 13,976 | 63.7% |
| ls-ratio-fade | 3,764 | 63.0% |
| scalp-maker | 37,628 | 62.9% |
| momentum | 14,276 | 0.0% |

`funding-carry` never produced a single evaluable decision in seven days.

## Root cause: a silent ladder rejection

Of the order-book ladders reaching `snapshot_enrichment`:

| Outcome | Count |
| --- | --- |
| populated | 2,543 |
| `null` | 6,184 |
| `[]` (fetch returned blank) | 0 |
| carrying `book_observation_error` | 0 |

The fetch never failed and never timed out. `exchange.book_state` builds
`[[float(price), float(amount)], ...]` from a successful response, and
`engine._plain_levels` then returns `None` for the whole ladder when any
single level fails validation:

```python
if (not math.isfinite(price) or price <= 0
        or not math.isfinite(amount) or amount < 0):
    return None
```

It records no reason, so seven days of starvation produced no error, no log
line, and no alert. Six of seven strategies were evaluated on roughly a fifth
of the depth data that was successfully retrieved for them.

The rejection is intermittent: a clean 50-level BTC-USDT-SWAP ladder pulled
live from OKX is accepted in full, so the trigger is a property of particular
responses rather than of the format.

## Batch 1 - repair the pipe and the configuration

Instrument before loosening. A validation that silently discards depth is the
defect; relaxing it blind risks admitting a corrupt ladder into the executable
depth the maker contract consumes, which is worse than the current problem.

- [x] B1.1 Record why a ladder is rejected: which level, which field, what
      value, in `book_observation_error`
- [x] B1.2 Truncate at the first bad level instead of discarding the ladder.
      A shorter true ladder understates depth, which is the conservative
      direction; a discarded ladder is a data-missing veto
- [x] B1.3 Record surviving depth so a contract can veto a ladder too shallow
      to trade rather than treating truncation as full depth
- [x] B1.4 Regression test built from the exact shapes OKX returns, including
      the malformed cases, proving no silent `None` survives
- [x] B1.5 `allow_experimental_setups_in_demo: true` - this flag alone caused
      3,556 vetoes and means the three registered hypotheses have never run
- [x] B1.6 `universe.top_n: 10 -> 25` for proportional signal throughput
- [x] B1.7 Retire momentum's candidate rotation; keep its baseline
- [x] B1.8 Fork the feed for the changed executable identity
- [x] B1.9 Full suite green

Gate: no ladder can be discarded without a recorded reason, and the suite
proves it.

Evidence. `tests/test_book_depth_ladder.py` fails in nine places when the
silent-discard branch is restored, and the existing observability test
caught a real defect in the first version of the repair: returning an empty
list for a fully rejected ladder would have satisfied `missing_fields`,
which treats only None as absent, so unusable depth would have reached a
contract as though it had been observed. Zero surviving levels now reads as
absent depth.

Suite 1,135 -> 1,298. The retirement forked `prompt_version`
608188bfb1314d2e -> 86d91dc0c41fee7d because retired arms leave the live
selector list, and the feed forks 7 -> 8 for the changed executable
identity.

### Why momentum cannot simply be removed

`momentum` is the only strategy with `analyst_ready=True`; `runnable_ids()`
returns exactly one entry. Every other registered strategy is shadow-only and
has no analyst prompt or schema, so none can occupy the order path today.
Removing momentum would stop the demo account, and with it the
`setup_proposed` corpus that gate G2 needs - currently 49 events against a
floor of 100.

What is retired here is momentum's candidate rotation: 18 of the 36 catalog
variants belong to a falsified strategy, and at its measured throughput
testing them all would occupy a research lane for years. Its baseline stays,
because it is both the order-path rehearsal and the benchmark null that any
replacement has to beat.

Choosing the replacement is deferred deliberately. Five of seven strategies
have never been meaningfully tested, so today there is no evidence about which
is better - only evidence that they were starved. Two weeks of post-repair
collection answers that question with data instead of a guess.

## Batch 2 - redesign for edge production

- [x] B2.1 Declarative contract DSL so a proposed mechanism is executable
      without hand-written Python, bounded to known snapshot fields
- [x] B2.2 Staging tier: LLM-authored hypotheses register at T1, get shadow
      lanes immediately, and remain barred from capital by `LIVE_MIN_TIER`
- [ ] B2.3 Three-stage funnel - screen on ~30 observations, measure on 100
      with full costs, confirm on held-out evidence - instead of applying the
      strictest adequacy gate to every candidate from the first bar
- [ ] B2.4 Parallel arms with false-discovery control per axis family,
      replacing serial one-candidate-per-lane rotation
- [ ] B2.5 Reason-coded verdicts so the reviewer learns why a candidate died,
      not merely that it did
- [ ] B2.6 Ranked shortlist report: mechanism, payer, configuration,
      observations, after-cost expectancy with interval, nulls beaten,
      held-out result, honest confidence label

Gate: a generation of LLM-authored candidates runs end to end and produces a
shortlist without a human in the inner loop.

B2.1/B2.2 landed. `agent/contract_dsl.py` validates an authored proposal and
compiles it into the same `(snapshot, direction, cfg, params) -> (fired,
reason)` callable the hand-written contracts implement, so a proposed
mechanism reaches a shadow lane through the path a reviewed one already uses.
The bounds are derived rather than declared: the 26 proposable fields come
from what the validated forward models require, so an author can only name
inputs the pipeline is known to populate.

`agent/staging.py` gives an authored mechanism an identity that is append-only
and immutable at the database level - a registered claim cannot be reworded
after results exist, which is the difference between a pre-registered
hypothesis and a retro-fitted one. Everything enters at `T1_HYPOTHESIS`, and
the constant is duplicated rather than imported so a future edit to the tier
ladder cannot silently raise the tier a machine-authored contract is born at.

Verified by removing each guardrail in turn: dropping the field allowlist, the
substance floor and the immutability trigger fails nine tests.

### Why the analyst layer is the wrong way to promote a strategy

Making every strategy `analyst_ready` would let the LLM trade any of them on
the order path, but it would also put an unmeasured layer on top of a
mechanism whose evidence came from its deterministic contract. The lanes
measure contracts; promoting one and then running it under an analyst trades
something other than the thing that earned the promotion.

The mechanism that serves the same goal without breaking the evidence chain is
a deterministic order path: the contract proposes, risk vets, execution
places, and no analyst call happens at all. That is what the shadow lanes
already do minus the exchange, so a promoted edge would trade exactly what was
measured. Per-strategy analyst prompts remain worth adding later, one at a
time, once a contract has earned one - and each addition should fork
attribution deliberately.

### Remaining in Batch 2

- [ ] B2.7 Deterministic order path so a promoted contract can trade as
      measured, replacing blanket `analyst_ready`
- [ ] B2.8 Wire `StagingStore.evaluators()` into the shadow coordinator so a
      staged contract gets a lane on the next cycle
- [x] B2.9 Authoring prompt and response schema, on its own cadence

## What 30 days can and cannot deliver

It can deliver a ranked shortlist of candidates with positive real-time
evidence and honest confidence labels. It cannot make 30 days of one market
regime mean more than that. `funding-carry` posted +2.008% per trade over 116
trades, beat every null, survived its placebo and was better out-of-sample -
and decomposition showed funding contributed +0.039% against price movement's
+1.969%, so it was a directional bet wearing a carry label. The shortlist is
where human judgement is applied, not replaced.


### B2.9 landed: generation is now a cadence, not a consequence

`research/authoring.py` plus `research.py author` ask for new mechanisms and
stage the ones that validate. It runs in `nightly.sh` *before* the reviewer
and is deliberately not gated on a terminal outcome: the reviewer needs a
finished assignment to have something to explain, so on a corpus where none
has finished it never runs at all, which is exactly the state in which new
ideas matter most.

The request carries the derived field list and what has already been
falsified, so a proposer cannot name an input the pipeline does not populate
and can see why the last generation died. One malformed proposal is recorded
and skipped rather than discarding the generation, because most proposals are
expected to be wrong.

Demonstrated end to end: an authored intraday mechanism - filled liquidations
are price-insensitive sells that must complete regardless of price - compiled
and fired correctly on a flush snapshot, vetoed the wrong direction, and
vetoed a quiet hour, with no code written for it.

### Still open, and why the loop is not yet closed

- [x] B2.8 Staged contracts get shadow lanes: the coordinator evaluates each
      one on the shared harness every cycle, in its own scope
- [ ] B2.3 Three-stage funnel
- [ ] B2.4 Parallel arms with false-discovery control
- [ ] B2.5 Reason-coded verdicts
- [ ] B2.6 Ranked shortlist report
- [ ] B2.7 Deterministic order path

B2.8 needs one design decision first. A staged contract produces a signal but
not a paper trade, because a trade needs entry, stop, target, cost and holding
assumptions - a forward model. The right answer is a single fixed measurement
harness shared by every staged mechanism rather than letting a proposer choose
its own exits: holding the outcome contract constant is what makes differences
between mechanisms attributable to the mechanism instead of to the exit
policy. That harness is the first task of the next batch.


### The fixed harness, and one distinction it enforces

`forward_models.STAGED_HARNESS` is a single outcome contract shared by every
staged mechanism: first observed price after the signal, structure stop at one
ATR, fixed 2R target, observed taker costs both sides, realized funding, 24h
timeout. A staged contract states when to enter and nothing else, so holding
the exits fixed is what makes a difference between two authored mechanisms
attributable to the mechanism instead of to a lucky stop distance. The values
are the ordinary ones, not favourable ones: a mechanism that cannot clear this
has been measured against the same bar as everything else.

It is deliberately absent from `MODELS` and `BY_STRATEGY`. Those map registered
strategies to their contracts, and adding a measurement instrument there made
four existing "one model per registered strategy" invariants quietly false -
which the suite caught.

`staged_lane.proposals_for` emits the same proposal shape the registered
contracts emit, so a staged mechanism flows through the existing paper path
rather than a parallel one, and `coverage()` separates three outcomes that a
single refusal string would blur:

| Outcome | Meaning |
| --- | --- |
| fired | the mechanism triggered |
| declined | the mechanism was evaluated and said no |
| starved | the mechanism was never evaluated; the data was absent |

Data problems outrank the contract's own opinion for exactly the reason six
strategies looked falsified for a week: a mechanism evaluated on a snapshot it
could not read has not been tested, and counting that as a decline turns
starvation into apparent evidence against the claim.


### B2.8: the loop is closed

`StrategyShadowCoordinator` now evaluates staged mechanisms each cycle, in
`<scope>:staged`, isolated the same way every other lane is: a fault is
recorded and swallowed rather than costing the registered strategies their
cycle.

One evaluator per contract, not one shared evaluator. `ShadowEvaluator`
applies one common proposal set to every variant it holds, which is right for
a baseline and its candidate and wrong here: two staged mechanisms are
different claims, so a shared evaluator would have each one trading on the
other's signals and destroy attribution entirely. That is the kind of mistake
that produces confident, meaningless results, so it is held by a test.

Enrolment needed three corrections the suite surfaced in turn: a contract id
may contain hyphens and a variant id may not, so a contract enrols as
`staged.<slug>`; configuration resolves `strategy.id` against the registry, so
the staged config keeps a registered id and only overrides the outcome
parameters exactly as `_research_cfg` does, with the staged identity carried
on the variant; and `research.staging_store` had to be declared before the
config would accept it.

The end-to-end path now runs: `research.py author` proposes, validation and
staging register, the coordinator evaluates each mechanism on the fixed
harness every cycle, and results accrue to its own paper account under its
own variant id.

### Remaining

- [ ] B2.6 Ranked shortlist report
- [ ] B2.5 Reason-coded verdicts
- [ ] B2.3 Three-stage funnel
- [ ] B2.4 Parallel arms with false-discovery control
- [ ] B2.7 Deterministic order path
