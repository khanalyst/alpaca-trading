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

- [ ] B2.1 Declarative contract DSL so a proposed mechanism is executable
      without hand-written Python, bounded to known snapshot fields
- [ ] B2.2 Staging tier: LLM-authored hypotheses register at T1, get shadow
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

## What 30 days can and cannot deliver

It can deliver a ranked shortlist of candidates with positive real-time
evidence and honest confidence labels. It cannot make 30 days of one market
regime mean more than that. `funding-carry` posted +2.008% per trade over 116
trades, beat every null, survived its placebo and was better out-of-sample -
and decomposition showed funding contributed +0.039% against price movement's
+1.969%, so it was a directional bet wearing a carry label. The shortlist is
where human judgement is applied, not replaced.
