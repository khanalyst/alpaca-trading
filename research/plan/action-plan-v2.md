# Action plan v2 — strategy, logic and sequencing

**Supersedes the sequencing in** [`batched-implementation.md`](batched-implementation.md).
**Depends on** [`findings.md`](findings.md) and [`edge-hypotheses.md`](edge-hypotheses.md).
**Date:** 2026-07-28

This document does not replace the batch definitions in
batched-implementation.md. Those are sound and the acceptance criteria in them
should be kept verbatim. What changes is **the order, two new batches, and the
justification for one existing batch.**

---

## Part 1 — Strategic frame

### 1.1 The scarce resource is calendar time, and only one class of decision consumes it irreversibly

Three resources are in play and they are not interchangeable.

| Resource | Renewable? | Current constraint |
| --- | --- | --- |
| CPU | Yes, trivially | Not binding |
| LLM spend | Yes, roughly $50-95/month per variant | Binding on shadow LLM variants only |
| **Calendar time** | **No** | **Binding on everything** |

findings.md §5 establishes the arithmetic: at a handful of trades per day,
50-100 matched round trips per variant is two to four weeks each, and a
four-point sweep run sequentially is four months. Replay exists to escape that
constraint for parameters.

**But replay cannot escape it for fields you did not record.** §9.2 requires the
replay to read the recorded snapshot verbatim, and §9.6 establishes that
anything outside the recorded universe is unavailable forever. The same logic
extends beyond universe parameters to every field: a snapshot without open
interest in it can never be made to have open interest in it.

**Therefore the only truly irreversible decision in this programme is the
decision not to record something.** Every other mistake is recoverable with
CPU. This one is not.

### 1.2 The consequence: collection order and analysis order are inverted

The current plan sequences by dependency: substrate, corpus, outcomes, replay,
sweep, findings, then trading changes, then shadow. That is correct for the
analysis pipeline and it should not change.

It is wrong for data. The two hypotheses with the strongest theoretical
justification for *why the money is there* (edge-hypotheses.md H-G and H-H, both
resting on forced, price-insensitive flow) are the two that cannot be tested for
months, and they are last in the current dependency graph. By the time the plan
reaches them, twelve weeks of collectable evidence has been discarded.

**The fix is a new batch, B0.5, that ships before anything else and consumes no
research infrastructure at all.** It writes fields to the journal. Nothing reads
them for two months. That is the point.

### 1.3 Conditioning beats new setups, because conditioning reuses trades

This is the second strategic principle and it determines priority within the
test track.

A new setup type requires new trades to evaluate, and new trades cost calendar
time at a rate you cannot change. A conditioning variable partitions the trades
you already have. It costs nothing and it *increases* effective statistical
power when the partition is real, because it separates populations whose
averaging was destroying the signal.

Every one of H-I, H-J, H-L and H-K(i) is a conditioning hypothesis. None
requires a single new trade. That is why they are ranked ahead of everything
else in the test track, and it is why the sweep runner in batch 4 should support
**conditioning axes as a first-class concept alongside parameter axes**, which
the current specification does not.

### 1.4 What this plan optimises for

In priority order:

1. **Do not lose recoverable evidence.** Collection starts week one.
2. **Answer the cheapest falsifiable question first.** H-M and H-L are single
   queries. H-E is an afternoon once the harness exists.
3. **Never touch the trading path until the harness can prove the change.**
   Unchanged from the original plan, and correct.
4. **Refuse to conclude on inadequate samples.** `INSUFFICIENT_SAMPLE` is a
   success state.

---

## Part 2 — Five challenges to the plan as written

Each of these is a change I would make, with the reasoning.

### C1 — There is no data-capture batch, and that is the largest gap in the plan

**Issue.** Batches 0 through 5 are explicitly designed to touch the trading path
zero times, which is presented as a virtue. It is a virtue for *logic* changes.
It has been over-applied to *observation* changes, which carry almost none of
the same risk.

Adding a field to the snapshot dict and journalling it is additive. It does not
enter the strategy contract, does not enter the risk engine, and does not change
a single decision, provided it is withheld from the prompt (see D1 below). The
blast radius is a slightly larger journal.

**Change.** Insert **B0.5 — snapshot enrichment and observability**, shipped
first, before B0.

### C2 — H-E is framed as a false binary and will produce a misleading answer

**Issue.** findings.md H-E and batch 3 frame the question as: does the LLM beat
the deterministic null? Two arms, LLM or null.

The most plausible truth is neither. A language model is poorly suited to
selecting setups from numeric snapshots, which is a task deterministic code does
better and more cheaply. It is much better suited to *rejecting* setups that
satisfy the contract but sit in an obviously bad context. That is a veto
function, and a two-arm test cannot detect it: if the LLM is a good vetoer and a
poor selector, the pooled LLM arm looks mediocre and the conclusion is "the LLM
does not earn its keep", which is the wrong conclusion drawn from the right data.

**Change.** Three arms, not two.

| Arm | Proposals from | Answers |
| --- | --- | --- |
| A. Null | Deterministic contract, trend-majority direction, fixed RR, confidence 1.0 | The floor |
| B. LLM | Recorded model decisions | Current system |
| C. Null-and-veto | Contract fires, LLM's recorded decision used only to *suppress* | Is the LLM's value in rejection? |

Arm C costs nothing extra: it is a different join over the same two recorded
event streams. The comparison A vs C isolates the veto value; B vs C isolates
the selection value. If C beats both A and B, the correct architecture is
deterministic proposal with LLM veto, which is roughly a third of the current
token spend and a much smaller failure surface.

### C3 — Batch 6.4 may encode the right idea in the wrong variable

**Issue.** 6.4 proposes making `range_breakout` and `trend_continuation`
mutually exclusive by requiring a breakout to occur without prior
multi-timeframe alignment. The diagnosis in findings.md H-D is correct: the two
contracts overlap and attribution is currently splitting one phenomenon.

But the proposed fix assumes the correct discriminator is trend alignment. If
edge-hypotheses.md H-I is right, the correct discriminator is **volatility
regime**, and trend alignment is a noisy proxy for it. Shipping 6.4 first bakes
the proxy into the contract and makes the regime test harder to run cleanly
afterwards, because the population will already have been filtered on a
correlated variable.

**Change.** Move H-I ahead of batch 6.4 in sequence. Run the regime partition
against the v1 contract, then decide what 6.4 should actually say. 6.1, 6.2 and
6.3 are unaffected and can ship on the original schedule.

### C4 — The promotion protocol's sample requirement is unreachable for the hypotheses it will most often be applied to

**Issue.** §9.1 requires 100 matched round trips per variant and at least three
settings along a parameter axis, so 300 round trips per hypothesis minimum.
findings.md §5 suggests the total corpus may hold a few hundred round trips in
aggregate, across all variants.

The protocol is right. The arithmetic simply means most parameter sweeps will
return `INSUFFICIENT_SAMPLE` and that will be the correct answer. That is not a
failure, but it will feel like one after the fourth consecutive sweep returns
nothing, and the temptation to relax the rule at that point is exactly what the
rule exists to resist.

**Change.** Two things, both cheap.

1. **Report the achievable-n forecast before running any sweep.** The corpus
   loader knows the total round trips available. The sweep runner should
   refuse to run and state the shortfall when grid size divided by available n
   falls below the threshold, rather than running and reporting
   `INSUFFICIENT_SAMPLE` five times.
2. **Prefer conditioning axes over parameter axes** for the first six months,
   per §1.3, because they do not divide the sample.

### C5 — Run the funnel before any sweep, because the binding constraint may not be in the strategy at all

**Issue.** findings.md §5 lists nine funnel stages and observes that nobody has
measured the base rate, and specifically flags the 100% net-direction cap as the
step that "kills most 2nd and 3rd same-side entries". The batch 4 sweep list
contains eight strategy-contract parameters and two risk parameters.

If the net-direction cap is genuinely the dominant veto, then **no setting of
any strategy-contract parameter can increase the trade count**, because the
constraint binds downstream of the contract. Sweeping them would consume weeks
to discover that the funnel narrows somewhere else.

**Change.** The funnel report from batch 4.1 runs **before** the first sweep,
not as part of it, and the result determines the sweep order. If the
net-direction cap dominates, H-J is not merely a good hypothesis, it is the
prerequisite for every other test having enough sample to conclude anything, and
it should be promoted to first place in the test track.

---

## Part 3 — Revised dependency graph

```
  ┌──────────────────────────────────────────────────────────────┐
  │  COLLECTION TRACK  (starts week 1, analysed from ~week 10)    │
  │                                                              │
  │  B0.5 enrichment ──────────────► [accumulate] ──► H-G, H-H   │
  │   - open interest + deltas                                   │
  │   - book depth/spread every cycle                            │
  │   - BTC reference return                                     │
  │   - realised-vol ratio, session tag                          │
  │   JOURNALLED ONLY. NOT IN THE PROMPT.                        │
  └──────────────────────────────────────────────────────────────┘
                            │ (independent)
  ┌──────────────────────────────────────────────────────────────┐
  │  TEST TRACK                                                  │
  │                                                              │
  │  B0 substrate ── B1 corpus ──┬── H-M signal decay            │
  │                              ├── H-L session (needs B2)      │
  │                              └── B2 outcomes ── B3 replay    │
  │                                                    │         │
  │                                     B3.5 H-E three-arm       │
  │                                                    │         │
  │                                     B4.1 FUNNEL FIRST ───────┤
  │                                                    │         │
  │                                     B4.5 conditional edges   │
  │                                       H-J, H-I, H-K(i)       │
  │                                                    │         │
  │                                     B4 sweeps ── B5 findings │
  └──────────────────────────────────────────────────────────────┘
                            │
              B6 rejected hypotheses (6.4 gated on H-I)
                            │
              B7 shadow tier ──► H-G, H-H variants
                            │
              B7.5 execution experiment (H-K ii)
                            │
              B8 multi-strategy   B9 protocol/cadence
                            │
              B10 research tests (continuous)
```

---

## Part 4 — New and changed batches

### B0.5 — Snapshot enrichment and observability (NEW, ships first)

**Goal.** Start the irreversible clock. Record the fields that H-G and H-H
require, months before anything reads them.

**Blast radius on trading: none, and this is enforced by design commitment D1
below rather than by hope.**

#### Changes

**0.5.1 Open interest** — `agent/market.py`

Add `open_interest`, `open_interest_change_1h`, `open_interest_change_4h` to the
per-symbol snapshot. One additional OKX endpoint per universe refresh, cached at
the same cadence as funding history.

**0.5.2 Per-cycle book state** — `agent/engine.py`

The depth and spread check already runs. Journal its reading on **every**
evaluation, not only on rejection. New event `book_state`: symbol, ts, mid,
`spread_pct`, bid depth and ask depth inside 0.35% of mid, and top-of-book
sizes. This is the single cheapest high-value change in the whole programme:
one write, no logic.

**0.5.3 Market reference** — `agent/market.py`

Add BTC 15m return over 1, 4 and 24 bars to the portfolio-level snapshot block,
so residual computation (H-J) does not depend on the price cache being complete
for every symbol-day.

**0.5.4 Derived regime and session fields** — `agent/market.py`

`realised_vol_8`, `realised_vol_96`, their ratio, and `utc_hour`. All are
derivable later from cached OHLCV, so these are a convenience rather than a
necessity. Add them anyway: they make the replay cheaper and they cost nothing.

**0.5.5 Funding percentile wired into the snapshot**

`funding_percentile_30`, `funding_mean_30_pct` and `funding_samples_30` are
already computed and already unused (findings.md D2). Put them in the recorded
snapshot so H-G can use them and so the D2 fix in batch 6.2 has a corpus to be
validated against.

#### Design commitment D1 — record the fields, withhold them from the prompt

**Enrichment fields are journalled but are not added to the LLM user message
until a batch deliberately tests them.**

The reason is attribution. Changing the prompt changes model behaviour, which
forks the comparability of every pre-change and post-change observation, exactly
as `strategy.version` does in batch 6.5. If the fields go into the prompt now,
the corpus splits into a pre-enrichment and post-enrichment population and every
cross-period comparison in this programme is contaminated.

Record everything. Show the model nothing new. When you want the model to see
open interest, that is a versioned prompt change with its own attribution fork
and its own before-and-after replay diff.

#### Acceptance criteria

- [ ] For a fixed recorded snapshot, the decisions produced with and without
      enrichment are **byte-identical**. Golden test, mandatory.
- [ ] `PROMPT_VERSION` is **unchanged** by this batch. If it changed, a field
      leaked into the prompt.
- [ ] `book_state` is written on every evaluation, pass or fail.
- [ ] The OI endpoint failing does not fail the cycle: field is null, event is
      journalled, trading continues.
- [ ] Journal growth per day is measured and stated.
- [ ] Full existing suite: 170 passed, 1 skipped.

**Target: shipped within seven days of starting.** Everything else can wait.
This cannot.

---

### B3.5 — H-E as a three-arm test (replaces the two-arm framing in B3)

**Goal.** Determine whether the LLM's value is in selection, in rejection, or
absent, per challenge C2.

#### Changes

Add a third proposer mode to `research/replay.py`:

| Mode | Behaviour |
| --- | --- |
| `deterministic` | Existing null. Contract fires, trend-majority direction, fixed RR, confidence 1.0 |
| `recorded_llm` | Existing. The model's recorded decisions |
| `deterministic_vetoed` | **New.** Contract proposes; the recorded model decision is consulted only to suppress. Where the model declined a symbol the contract fired on, the trade is skipped. Where the model proposed something the contract did not fire on, it is ignored |

`deterministic_vetoed` requires no new data. It is a different join over
`llm_input`, `llm_output` and the contract evaluation.

#### Acceptance criteria

- [ ] Three arms scored against the same corpus with bootstrap CIs on each
      pairwise difference.
- [ ] The **pre-registered decision rule is written and committed before the
      query is run**, in the run-ID and `prior_result_seen` pattern already
      established.
- [ ] A stated recommendation on architecture: keep the LLM as proposer, demote
      it to vetoer, or remove it. With the sample size and the MDE alongside.

**This is the highest-value single result in the programme and it should be
reached within four to six weeks.**

---

### B4.5 — Conditional edge tests (NEW, before B4 sweeps)

**Goal.** Test the four zero-new-data conditioning hypotheses, which are more
sample-efficient than any parameter sweep.

**Blast radius on trading: none.**

#### Order within the batch

**Step 1 — Funnel diagnostic (from B4.1), run standalone.** Per challenge C5.
Publish the veto distribution before anything else. It determines the order of
everything below.

**Step 2 — H-J claim 1, the free diagnostic.** Regress realised trade PnL on
contemporaneous BTC return. One query. If R-squared is low, H-J dies here and
you saved a week. If high, and if step 1 confirms the net-direction cap is the
dominant veto, H-J becomes the top priority because it is the only lever that
increases sample size.

**Step 3 — H-M, signal decay by latency.** One query over the existing corpus.
Determines whether batch 9.2 is a cost optimisation or an alpha fix.

**Step 4 — H-I, regime partition.** Three definitions of the regime variable,
all pre-registered. Gates batch 6.4.

**Step 5 — H-L, session buckets.** Three or four pre-registered windows, out-of-
sample split mandatory, multiple-comparisons correction mandatory. See the
warning in edge-hypotheses.md H-L: this is the test most likely to produce a
false positive that survives scrutiny it has not earned.

**Step 6 — H-K(i), retest fill analysis.** Distribution of adverse excursion in
the first K minutes; find the optimum limit offset X, and compute the missed-
trade cost at each X.

#### Conditioning axes as a first-class concept

`research/sweep.py` gains a second axis type:

```yaml
# research/sweeps/regime_conditioning.yaml
hypothesis: "range_breakout expectancy is positive only from volatility compression."
base: momentum.baseline
condition_axis:
  variable: realised_vol_ratio_8_96
  buckets: [tercile_low, tercile_mid, tercile_high]
  pre_registered: true
```

A conditioning axis partitions results rather than generating variants. It
carries no config override, so no re-replay is needed: one replay produces all
buckets.

#### Acceptance criteria

- [ ] Every conditioning test has a pre-registered bucket definition committed
      before the query runs, with `prior_result_seen: false` recorded.
- [ ] Multiple-comparisons correction is applied across the full family of tests
      in this batch, and the corrected figure is the only one quoted in any
      recommendation.
- [ ] Out-of-sample split (70/30 by time) reported for every conditional result.
- [ ] Each of the six steps produces a `finding` row in the batch-5 store, even
      when the result is null. **Null results are the most valuable rows in the
      table and must not be omitted.**

---

### B6 — Revised sequencing only

Unchanged except: **6.4 is gated on the result of B4.5 step 4.** 6.1, 6.2 and
6.3 proceed as specified. 6.5's version bump waits until 6.4 is settled, so the
attribution forks once rather than twice.

---

### B7.5 — Execution experiment, H-K(ii) (NEW, after B6)

**Goal.** Test maker-first entry with taker fallback on the demo account.

**Blast radius on trading: real. This modifies the entry path.** It comes after
batch 6 deliberately, so that the change-control process (replay the diff,
publish before merging) has already been exercised once on a lower-risk change.

Forward-only: passive fill rates cannot be inferred from history.

#### Acceptance criteria

- [ ] Fill rate at T seconds measured and journalled per attempt.
- [ ] Fallback to taker is guaranteed within the bar, or the setup is abandoned
      and journalled, never left resting.
- [ ] Realised entry price versus the IOC counterfactual, per trade.
- [ ] Existing protection guarantees (attached exchange-side SL/TP) hold
      unchanged. The safety suite is the gate.

---

## Part 5 — Decision gates

Written before results, per the pre-registration discipline already in use.

| Gate | When | Rule | Consequence if failed |
| --- | --- | --- | --- |
| **G1** | End of B0.5 | Decisions byte-identical, `PROMPT_VERSION` unchanged | Do not ship. A leaked prompt field forks the corpus |
| **G2** | End of B3 | Baseline replay reproduces >= 99% of recorded `setup_proposed` and `rejected` events | **Stop the programme.** Every downstream number is worthless. This is the keystone |
| **G3** | B3.5 | Three arms scored, CI on each difference | If all three overlap at n available, the answer is `INSUFFICIENT_SAMPLE`, not "the LLM is fine" |
| **G4** | B4.5 step 1 | Funnel published | If net-direction cap > 30% of vetoes, H-J is promoted to first priority |
| **G5** | B4.5 step 4 | H-I resolved | Batch 6.4 cannot merge until this returns a result or `INSUFFICIENT_SAMPLE` |
| **G6** | ~Week 12 | H-G / H-H sample sufficiency | If OI-conditioned cells are still below 150 observations, extend collection rather than concluding |
| **G7** | Any promotion | Full §9.1 protocol | No exceptions, including for a hypothesis you like |

**G2 deserves emphasis.** batched-implementation.md already calls the
self-validation test the single most important test in the plan, and that is
correct. If baseline replay does not reproduce live decisions, the failure is
silent: every subsequent number is precise, plausible, internally consistent and
wrong. Treat a G2 failure as a full stop, not a debugging task to work around.

---

## Part 6 — Sequenced calendar

Indicative, assuming part-time work.

| Window | Collection track | Test track |
| --- | --- | --- |
| **Week 1** | **B0.5 ships.** Enrichment live, journalling everything, prompt untouched | B0 substrate |
| **Weeks 2-3** | Accumulating | B1 corpus. First `corpus stats` output. **H-M runs here** |
| **Weeks 3-5** | Accumulating | B2 outcomes, price cache built. **H-L runs here** |
| **Weeks 5-7** | Accumulating | B3 replay. **G2 gate.** Then B3.5, three-arm H-E |
| **Weeks 7-9** | Accumulating | B4.5: funnel, H-J, H-I, H-K(i). **G4 and G5 gates** |
| **Weeks 9-11** | Accumulating | B4 sweeps (informed by the funnel), B5 findings store |
| **Weeks 11-13** | ~10-12 weeks of OI and book data | B6, with 6.4 informed by H-I |
| **Weeks 13-16** | **H-G and H-H become testable** | B7 shadow tier, carrying the H-G and H-H variants |
| **Week 16+** | | B7.5 execution, B8, B9 |

The critical path to the most expensive open question, whether the LLM earns its
keep, is B0 to B3.5 and runs about six weeks. The critical path to the two
hypotheses with the strongest structural justification runs about thirteen
weeks, and **twelve of those weeks are waiting, not working**, which is the
entire argument for shipping B0.5 in week one.

---

## Part 7 — Risk register

| Risk | Why it bites here | Mitigation |
| --- | --- | --- |
| **Multiple comparisons** | Six hypotheses, each with several conditioning cells, against a few hundred round trips. Something will look significant | Pre-register every bucket. Family-wise correction across each batch. Quote only corrected figures. Mandatory out-of-sample split |
| **Replay infidelity** | A subtle mismatch produces confident, wrong numbers with no error message | G2, treated as a full stop. Re-run G2 after every change to `strategy.py` or `risk.py` |
| **Prompt contamination** | An enrichment field reaching the prompt forks the corpus silently | D1. `PROMPT_VERSION` assertion in the B0.5 acceptance criteria |
| **Sample starvation misread as rejection** | Four consecutive `INSUFFICIENT_SAMPLE` results will feel like failure and invite relaxing the rule | Forecast achievable n before running. Prefer conditioning axes. Treat the outcome as informative and log it as a finding |
| **Regime shift mid-corpus** | A corpus spanning a volatility regime change makes the out-of-sample split a regime test rather than a robustness test | Report the realised-vol profile of the train and test windows alongside every split result |
| **Enrichment cost drift** | Extra endpoint calls against OKX rate limits | Cache OI at the universe-refresh cadence, not per cycle. Measure and state the added call budget in B0.5 |
| **Scope creep into H-K(ii)** | Execution work is satisfying and visible, and it will be tempting to start early | It is gated behind B6 for a reason: the change-control process must be exercised on something lower-risk first |
| **The LLM turns out not to earn its keep** | Genuinely possible, and the project's framing as an LLM-mediation experiment makes it uncomfortable | This is a **result**, not a failure. The experiment was whether LLM-mediated hypothesis selection beats a deterministic baseline. A negative answer is a publishable, valuable finding and it is cheaper to learn in week six than in month six |

---

## Part 8 — Explicitly not doing

Extends the list in batched-implementation.md.

| Not doing | Why |
| --- | --- |
| Adding enrichment fields to the prompt in B0.5 | Forks corpus attribution silently. Record now, show the model later, deliberately, with its own version bump |
| Testing H-G or H-H before ~week 12 | The sample does not exist yet. Testing early produces a null that will be misread as a rejection |
| Sweeping parameters before the funnel is published | If the binding veto is downstream of the contract, the entire sweep is wasted calendar time |
| Twenty-four hourly buckets for H-L | Guaranteed false positive. Three or four pre-registered economic windows or nothing |
| Merging batch 6.4 before H-I resolves | Bakes a proxy variable into the contract and contaminates the population the regime test needs |
| Building a general OHLCV backtester | Unchanged from the original plan and still correct. The recorded snapshot is the only faithful input |
| Treating a null result as a non-result | Null results are rows in the findings store. A programme that only records positives is a programme that only records noise |

---

## Part 9 — The first seven days, concretely

1. **Write B0.5.** OI fields, `book_state` event on every evaluation, BTC
   reference return, realised-vol ratio, `utc_hour`, funding percentile into the
   snapshot.
2. **Write the golden test first**, before the enrichment code: fixed snapshot
   in, decisions out, byte-identical. Then make the enrichment pass it.
3. **Assert `PROMPT_VERSION` is unchanged** in the same test file.
4. **Ship it.** The clock starts.
5. **Write down the pre-registered decision rules** for H-E three-arm, H-I, H-L
   and H-J, with run IDs and `prior_result_seen: false`, before any harness
   exists to tempt you. Commit them.
6. **Then start B0.**

Steps 1 to 4 are perhaps two days of work and they are worth more than any other
two days available in this programme, because they are the only two days whose
value decays if deferred.
