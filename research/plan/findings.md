# Findings — historical codebase analysis against the parallel-hypothesis-testing goal

> Historical planning record. The implementation described below has since
> landed in `agent/variants.py`, `agent/shadow.py`, and `research/findings.py`:
> connected hypothesis variants, adaptive proposal/history/locks, first-class
> FindingsStore metadata, forward qualification linkage, and bounded shadow
> workers. Use `research/protocol.md` and the current code/tests for policy;
> keep this document for rationale and rejected alternatives.

**Branch:** `claude/main-hardening-v2-67ebiz` @ `a0679c9`
**Date:** 2026-07-28
**Method:** full read of every source file (`main.py`, `report.py`, `agent/*.py`,
`config.yaml`, `tests/*.py`, all docs), plus a clean run of the test suite on
Python 3.12.

The test-count snapshot below is historical and is not a current repository
status claim.

The single skip is `tests/test_okx_demo_integration.py`, which is gated behind
`OKX_RUN_DEMO_INTEGRATION=1` by design.

---

## 0. Executive summary

The repository is a **single-strategy, single-process live trading agent**. It is
unusually well built for that job: the execution, reconciliation, state and
journalling layers are defensive, well-tested and hard to fool.

It contains **none of the machinery your stated goal requires.** There is no
concept of a second strategy, a candidate hypothesis, a parameter variant, a
shadow evaluation, a replay, or a persisted learning. Three separate mechanisms
actively *forbid* a parallel strategy from existing (§3).

The good news is larger than the bad news, and it is the central finding of this
document:

> **The agent already writes a complete, timestamped, replayable market corpus to
> `runtime/journal.db` on every cycle — and no code has ever read it.**

Every cycle journals an `llm_input` event containing the exact per-symbol
snapshot dict and portfolio state that were fed to the model
(`agent/engine.py:1612-1618`). That is precisely the input that
`strategy.setup_evidence()`, `strategy.build_setup_plan()` and
`RiskEngine.vet_open()` consume — and all three are **pure functions of
`(snapshot, cfg)`**. That means an arbitrary number of parameter variants can be
replayed against exactly the data the live agent saw, offline, with zero
exchange risk, zero extra market-data calls and zero extra LLM spend.

The work is therefore not "build a parallel trading system". It is "build a
research substrate on top of a corpus you are already collecting, and stop
throwing it away."

---

## 1. What the system is today

```
main.py  ──►  Engine.cycle()  (every 300s, one process, one strategy)
                │
                ├─ equity sync, ledger/transfer rebasing        [state.py]
                ├─ circuit breakers (daily loss, max drawdown)  [engine.py]
                ├─ universe refresh, hourly                     [market.py]
                ├─ position reconciliation + protection audit   [engine.py]
                ├─ market_snapshot()  ──► per-symbol indicators [market.py]
                ├─ ONE LLM call  ──► semantic decisions as JSON [brain.py]
                ├─ strategy contract  ──► deterministic SL/TP   [strategy.py]
                ├─ RiskEngine.vet_open()  ──► sized plan        [risk.py]
                └─ IOC entry with attached exchange-side SL/TP  [exchange.py]
```

| Layer | File | Assessment for research purposes |
| --- | --- | --- |
| Strategy contract | `agent/strategy.py` | **Pure, cfg-parameterised, ideal seam.** 375 lines, no I/O, no clock except `time.time()` defaults that are already injectable via `now=`. |
| Risk sizing | `agent/risk.py` | Nearly pure; only `time.time()` for cooldown checks. Easy to make replayable. |
| Snapshot builder | `agent/market.py` | Does network I/O, but its **output** is what gets journalled. Replay consumes the output, not this module. |
| LLM | `agent/brain.py` | `SYSTEM` is a 270-line constant with no versioned variants. Prompt is a hypothesis with no A/B mechanism. |
| Journal | `agent/state.py` | Rich schema, correct attribution IDs, **write-only for the interesting tables**. |
| Report | `report.py` | stdout-only, no persistence, no parameter-level attribution, no funnel metrics. |
| Engine | `agent/engine.py` | 1947 lines, monolithic `cycle()`. No fan-out seam. |

---

## 2. Gap analysis against your seven intentions

| # | Your intention | Status | Blocking issue |
| --- | --- | --- | --- |
| 1 | One main LLM agent trading the demo account | ✅ **Exists and works** | — |
| 2 | Parallel strategies/hypotheses tested alongside it | ❌ **Nothing exists** | Three hard blockers, §3 |
| 3 | Parallel tests learn from live data + demo trades | ⚠️ **Data exists, no consumer** | `llm_input` never read; no replay harness |
| 4 | Test parameter variants before rejecting a hypothesis | ❌ **Nothing exists** | Config is single-valued; variant identity is an opaque whole-config hash |
| 5 | Persist learnings/recommendations per strategy and per variant | ❌ **Nothing exists** | `report.py` prints to stdout and forgets |
| 6 | Reject hypotheses that make no sense up front | ⚠️ **Several are rejectable now** | Full audit in §6 |
| 7 | Tests are outdated / not for this purpose | ⚠️ **Half right** | They are *current* and *good*, but they only test safety. §8 |

---

## 3. The three hard blockers on parallel strategies

These are not "missing features". They are code that will actively refuse.

**3.1 — The config validator forbids a second strategy by name.**

```python
# agent/config.py:88-91
if strategy["id"] != "momentum":
    raise ConfigError(
        "strategy.id must be 'momentum' until another isolated strategy "
        "implementation exists")
```

Any second strategy fails validation before an exchange client is constructed.
This was a deliberate, correct guard for a single-strategy product. It is now
the first thing in the way.

**3.2 — A process-wide run lock forbids a second instance.**

`state.acquire_run_lock()` (`agent/state.py:430`) takes an exclusive `flock` on
`runtime/agent.pid`. `Engine.run()` raises if it cannot get it, and `cmd_run`
refuses to start. One agent per runtime directory, enforced.

**3.3 — Runtime paths are module-level constants, not configuration.**

```python
# agent/state.py:20-24
RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
STATE_FILE = RUNTIME / "state.json"
PID_FILE   = RUNTIME / "agent.pid"
DB_FILE    = RUNTIME / "journal.db"
STATE_LOCK_FILE = RUNTIME / "state.lock"
```

There is no way to point a second instance at a different runtime directory
without reassigning four module globals — which is exactly what the test suite
does by hand (`tests/test_state.py:15-26`). The README's own answer to this is
"copy the whole folder per OKX sub-account", which gives you **capital
isolation but zero shared learning** — the opposite of what you want, since your
intention #3 is explicitly that the parallel tests learn *from* the live agent's
data.

---

## 4. The buried asset — you are already collecting the corpus

This is the finding that changes the shape of the whole project.

**4.1 What gets written, every cycle**

| Event kind | Payload | Read by anything? |
| --- | --- | --- |
| `llm_input` | Full provider request: **the entire per-symbol snapshot dict** + portfolio + max_new | **No** |
| `llm_output` | Every request attempt, response ID, raw response text, parsed decisions | **No** |
| `decisions` | Parsed decision list | No |
| `rejected` | Per-proposal veto reason | Yes, `report.py` counts them |
| `setup_proposed` / `setup_status` | Setup lifecycle | No |
| `order_execution` | Submission audit, fills, shortfall | No |
| `universe_selection` | Full inclusion/exclusion audit | Yes, latest only |
| `entry_liquidity_rejected` / `entry_execution_failed` | Depth + exchange failures | Yes, counted |

`grep -rn "llm_input\|llm_output"` returns only the writer in `engine.py` and
its unit tests. **Nothing consumes the richest data in the system.**

**4.2 Why this matters**

The snapshot recorded in `llm_input` is byte-identical to the dict passed into:

- `strategy.setup_evidence(snapshot, cfg)` — pure
- `strategy.build_setup_plan(decision, symbol_snapshot, cfg)` — pure
- `RiskEngine.vet_open(decision, equity, positions, snapshot, ...)` — pure but
  for `time.time()`

So for any candidate `cfg`, you can compute *exactly* what the strategy contract
and risk engine would have produced on every historical cycle, deterministically,
offline. Change `fixed_reward_risk` from 2.0 to 2.5 and re-derive every setup
plan the agent ever built. That is your parameter sweep (intention #4) and it
costs nothing but CPU.

**4.3 The one thing replay cannot tell you**

Replay reconstructs *decisions*, not *fills*. It cannot tell you whether the IOC
entry would have filled, what the real slippage was, or whether depth existed.
Those live in `order_execution` and `entry_liquidity_rejected` for trades that
actually happened, and are simply unknown for counterfactual trades.

**This is why the architecture must be three-tier, not one:**

| Tier | Answers | Cost | Risk |
| --- | --- | --- | --- |
| **Replay** (offline, journal) | Does the *rule* have edge on the data we saw? | CPU only | None |
| **Shadow** (live, non-trading) | Does it still hold on fresh data, with live depth/spread observed? | ~0 for deterministic variants | None (cannot order) |
| **Demo** (live, trading) | Does it survive real execution and reconciliation? | LLM + time | Paper money |

Replay ranks candidates. Shadow filters them on fresh data. Demo confirms the
survivors. Anything else wastes the only scarce resource you have — **calendar
time**, see §5.

---

## 5. The arithmetic that forces this architecture

You cannot A/B test strategies sequentially on the demo account. The trade rate
is far too low to reject anything.

The funnel per 15-minute signal bar is:

1. Symbol must be in the top-10 universe (`top_n: 10`)
2. Model must propose it — bounded by `max_new = 3 - open_positions`
3. `setup_type` must satisfy the deterministic evidence contract
4. Entry must be inside the 2.5-ATR no-chase boundary
5. `confidence >= 0.65`
6. Not already held, not in post-loss cooldown (45 min), not in semantic setup
   cooldown (45 min), not in liquidity or execution backoff
7. Must fit inside per-position (40%), gross (150%) and **net-direction (100%)**
   caps — the net-direction cap alone kills most 2nd and 3rd same-side entries
8. Sized notional must clear `MIN_NOTIONAL_USD = 10`
9. Order book must have depth inside 0.35% of mid

And a symbol that reaches step 3 and *fails* is burned for the entire 15-minute
bar (`agent/engine.py:1661-1686` records a `risk_rejected` setup record, and
`strategy.evaluated_signal()` then blocks every further evaluation of that
symbol for that `signal_ts` — regardless of direction or relabelling).

Nobody has measured the resulting base rate, because **`report.py` does not
report trades per day or any funnel conversion.** That is itself a finding. But
the design bounds it at 3 new positions per cycle and, in practice, the
conjunction of steps 3–9 will put it far lower.

**The consequence:** to reject a hypothesis with any statistical confidence you
want on the order of 50–100 matched round trips per variant. At a handful of
trades per day, one variant is 2–4 weeks. Testing four parameter values of one
threshold sequentially is **four months**. Testing them in parallel on live
capital requires four accounts and quadruples your LLM bill.

Replay is not an optimisation here. It is the only tier where the sample size
exists.

---

## 6. Hypothesis audit — what to reject now (intention #6)

You did not hand me a hypothesis list, so I audited the hypotheses **already
embedded in the code**. Three should be rejected or reformulated on inspection,
before spending a single cycle testing them.

### ❌ REJECT NOW — H-A: `exit_policy: "structure_target"` is a real third option

```python
# agent/strategy.py:226-232
elif exit_policy == "structure_target":
    target_field = ("swing_high_pct" if direction == "long" else "swing_low_pct")
    structure_target = _finite(symbol_snapshot.get(target_field), 0.0) or 0.0
    take_pct = max(stop_pct * fixed_rr, structure_target)
```

`swing_high_pct` is the distance **up to the highest high of the last 20 15m
bars** (`agent/market.py:494`). For a long, `structure_target` is therefore how
much room is left before the recent high.

- For a `range_breakout` long, the contract requires `range_pos_pct >= 85`
  (`strategy.py:115-121`) — price is *at* the highs, so `swing_high_pct ≈ 0`.
- For a `trend_continuation` pullback long, price is near the 1h EMA20, so
  `swing_high_pct` is maybe 1–2%, while `stop_pct * fixed_rr` is ~4%.

In both cases `max()` selects `stop_pct * fixed_rr` — **which is identical to
`fixed_rr`.** The policy is degenerate precisely in the setups it was designed
for. The model has three exit policies and only two do anything.

**Verdict:** reject as specified. Either delete it (so the model's choice space
is honest) or redefine the structure target as the *next* opposing swing on a
higher timeframe, which is not currently computed.

### ❌ REJECT NOW — H-B: `funding_squeeze` as currently contracted

Three independent problems, any one of which is disqualifying:

**(i) The contract is a single threshold and ignores everything the prompt asks
for.** `SYSTEM` describes it as "funding deeply negative **while price stops
making new lows and the 1h trend flattens**" (`brain.py:192-196`). The code
enforces only:

```python
# agent/strategy.py:136-138
"funding_squeeze": {
    "long":  funding is not None and funding <= -funding_extreme,
    "short": funding is not None and funding >=  funding_extreme,
},
```

No price condition. No trend condition. The label can be attached to any
symbol whose funding crosses ±0.03%.

**(ii) The threshold is not extreme.** `funding_extreme_pct: 0.03` (per funding
interval) — but the prompt itself tells the model that "values beyond roughly
+/-0.05% per interval are strong crowd-positioning signals"
(`brain.py:69-71`). The config and the prompt disagree by ~1.7x, and 0.03% per
8h is routine for a mildly directional perp, not a squeeze. **This contract will
fire constantly on nothing.**

Worse: `market.py` already computes `funding_percentile_30` and
`funding_mean_30_pct` (`market.py:102-109`) — a relative, self-normalising
measure that is the *correct* signal for "extreme" — and the contract does not
use it.

**(iii) It is the only setup allowed the tightest stop in the system.** The
structure-anchor requirement is applied to two setup types only:

```python
# agent/strategy.py:171-173
if setup_type in {"trend_continuation", "range_breakout"} \
        and anchor != "structure":
    return None, f"{setup_type} requires a structure invalidation"
```

So `funding_squeeze` may use `anchor: "atr"`, which yields
`stop_pct = atr * min_stop_atr_multiple` = **exactly 1.0 ATR** — the narrowest
stop the system can produce. This is a counter-trend, mean-reversion entry into a
crowded book, and it gets a tighter stop than the trend-following setups. That is
backwards; counter-trend needs *more* room, not less.

**Verdict:** reject the current specification outright. If you want to keep the
idea, it needs a percentile-based threshold, a price/trend confirmation
condition, and a structure anchor requirement. That is a new hypothesis, not a
parameter tweak.

### ❌ REJECT NOW — H-C: `setup_type: "other"` produces learnings

`allow_experimental_setups_in_demo: true` lets the model label any idea `other`,
which **bypasses the evidence contract entirely**:

```python
# agent/strategy.py:186-188
if setup_type != "other" and (
        not isinstance(contract, dict) or contract.get(direction) is not True):
    return None, f"{setup_type} evidence contract is not met"
```

Two fatal problems for a research programme:

1. **`other` is an unlabelled bucket.** Every distinct experimental idea the
   model ever has gets pooled under one string. `report.py` groups by
   `setup_type` (`report.py:457-463`), so all experiments collapse into a single
   row. You cannot learn anything from a category that means "everything else".
2. **It makes demo results non-transferable.** `other` is demo-only. So the demo
   agent trades a population of setups that live will silently refuse, and every
   aggregate demo statistic is contaminated by trades that can never occur in
   production.

**Verdict:** reject as an experimentation mechanism. The correct replacement is
a **registered hypothesis ID** — the model picks from an explicit, versioned list
of named candidate setups, each with its own contract (possibly a permissive
one), each separately attributed. That gives you exactly what intention #5 asks
for. Until that exists, set `allow_experimental_setups_in_demo: false` so demo
statistics mean something.

### ⚠️ REFORMULATE — H-D: `range_breakout` and `trend_continuation` are distinct

`trend_continuation` fires when ≥2 of the 15m/1h/4h trends align
(`strategy.py:130`). `range_breakout` fires at `range_pos_pct >= 85` with
`relative_volume_1h >= 1.0` and positive momentum (`strategy.py:115-121`), where
`range_pos_pct` is position within the **24h ticker high/low**
(`market.py:498-501`).

A symbol in a strong aligned uptrend is almost always in the top 15% of its 24h
range with above-median volume. **The two contracts overlap heavily and are not
mutually exclusive.** Which label a trade receives depends on which word the LLM
chose, not on a difference in the market. Since `report.py` attributes
performance by `setup_type`, you will be splitting one phenomenon across two
buckets and drawing conclusions from the split.

**Verdict:** not rejectable on the merits — breakouts may well be a real effect —
but **not currently a testable hypothesis.** Make the contracts mutually
exclusive (e.g. breakout requires *no* prior multi-timeframe alignment, i.e. a
transition out of chop) before attributing anything to either.

### ✅ TEST FIRST — H-E: the LLM adds edge over the deterministic contract

This is the highest-value untested hypothesis in the project and it is currently
unfalsifiable, because there is no null model to compare against.

The evidence contract is fully deterministic. So a null model exists for free:
*"take every setup where the contract is satisfied, pick direction by trend
majority, always `fixed_rr`, always confidence 1.0."* Replay both against the
same corpus.

If the LLM does not beat that null, you are paying **~288 calls/day (≈$50–95/mo)
for a random number generator with good manners.** You cannot know this today
and the replay harness answers it in an afternoon. **This should be the first
thing built and the first thing measured.**

### ⚠️ NOTE — H-F: confidence is calibrated

The README says to watch the calibration table, and `report.py:466-485` produces
it. But `min_confidence: 0.65` **truncates the sample**: you only ever observe
outcomes for proposals ≥0.65, so you can compare 0.65–0.70 against 0.90+, but you
can never learn whether the floor is set correctly, because the trades it
rejects are never observed.

Shadow evaluation solves this exactly: run a 0.0-floor variant that records what
it *would* have done without trading it. This is a textbook case for the shadow
tier.

---

## 7. Concrete defects and design smells found while reading

Independent of the research goal, these came out of the read.

| # | Location | Issue | Severity |
| --- | --- | --- | --- |
| D1 | `strategy.py:226-232` | `structure_target` degenerates to `fixed_rr` (§6 H-A) | **High** — a model choice is inert |
| D2 | `strategy.py:136-138`, `config.yaml:40` | `funding_extreme_pct: 0.03` contradicts the prompt's own 0.05% guidance; ignores the already-computed `funding_percentile_30` | **High** |
| D3 | `strategy.py:171-173` | `funding_squeeze` (mean-reversion) is the only setup permitted the tightest ATR stop | **High** |
| D4 | `state.py:512-516` via `engine.py:44` | `config_version = stable_fingerprint(cfg)` hashes the **entire** config, including `alerts.timeout_seconds` and `llm.max_tokens`. Changing anything irrelevant forks the attribution bucket and fragments an already-tiny sample. Variant identity is also an opaque 16-hex string — `report.py` prints it but you cannot tell what differed. | **High** for research |
| D5 | `report.py` (whole file) | stdout only. No persistence, no machine-readable output, no time bucketing, no trades/day, no evaluation→proposal→veto→fill funnel. Intention #5 needs all of it. | **High** for research |
| D6 | `strategy.py:115-128` + `market.py:498-501` | `range_breakout` overlaps `trend_continuation` (§6 H-D) | Medium |
| D7 | `brain.py:494-538` | `parse_decisions` silently drops malformed opens; only the parsed list reaches the `decisions` event. The model's malformed-output rate is invisible except by re-parsing raw `llm_output` text. | Medium for research |
| D8 | `risk.py:152` | `if take_pct <= 0: take_pct = stop_pct * 2` hardcodes 2 instead of `fixed_reward_risk`. Unreachable today (`build_setup_plan` always sets it >0) but it is a latent inconsistency. | Low |
| D9 | `engine.py:283-559` | `cycle()` is a single ~280-line method mixing equity sync, breakers, reconciliation, snapshot, LLM, and execution. There is no seam to fan out N evaluators over one snapshot. | Medium — blocks the shadow tier |
| D10 | `config.yaml:52` vs `strategy.signal_timeframe: 15m` | 300s cycle against a 900s signal bar: ~2 of every 3 LLM calls cannot produce a fresh evaluation for an already-evaluated symbol. See §9. | Medium — pure cost |

---

## 8. Test suite assessment (intention #7)

You said the tests seem outdated and were not made for this purpose. **Half of
that is right, and the half that is wrong matters.**

**They are not outdated.** All 170 pass against current code. They track the
current schema (`tests/helpers.py` mirrors `config.yaml` field-for-field,
including `phase1-v1` and the liquidity/entry-failure blocks). They are current
and they are genuinely good.

**They are absolutely not built for your purpose.** Categorised by intent:

| Category | Tests | What they prove |
| --- | --- | --- |
| Auth / credentials / clock | 20 | The agent won't trade with bad keys |
| Exchange execution / fills / protection | 27 | Orders are verified, never naked |
| Reconciliation / emergency close | 15 | Exchange state wins, positions never orphaned |
| State / journal integrity | 17 | Corruption fails closed, journal never silently drops |
| Config validation | 14 | A typo can't become live trading |
| Controls / kill semantics | 12 | Pause/kill are durable |
| Risk vetoes | 17 | Caps cannot be bypassed |
| Market/universe hygiene | 11 | Non-crypto and thin instruments excluded |
| Decision flow plumbing | 24 | Idempotency, ordering, backoff persistence |
| Report arithmetic | 8 | PnL matching doesn't double-count |
| Strategy contract | 7 | Labels must match evidence; code owns SL/TP |

**Every single one is a containment test.** They answer "can this system hurt
itself?" — and the answer is a well-earned no.

**Not one of them can detect a bad strategy.** There is no test that:

- asserts a setup contract selects setups with better-than-random forward returns
- replays a fixed corpus and asserts a deterministic, reproducible result
- asserts a shadow evaluator cannot reach the exchange
- asserts variant identity is stable under irrelevant config changes
- asserts no-lookahead in any counterfactual evaluation
- validates scorer arithmetic (expectancy, R, profit factor) against known inputs

`tests/test_strategy.py` is the closest and it still only tests *plumbing*: that
`other` is demo-only, that setup IDs are stable, that code overrides model
numerics. Correct and necessary; silent on whether the strategy is any good.

**Recommendation: keep the existing suite untouched.** It is the safety net that
lets you move fast on the research layer. Add a *second, separate* suite
(`tests/research/`) with a different job. Do not merge them and do not weaken the
first to accommodate the second.

---

## 9. Measurement hazards — how your results will lie to you

Before you score anything, know these. Each will systematically bias a naive
comparison.

**9.1 — Demo fills are idealised, so demo R-multiples understate the strategy.**
Sizing reserves `max_order_book_slippage_pct` (0.35%) + `expected_stop_slippage_pct`
(0.15%) + spread + 2×fees inside `estimated_loss_pct` (`risk.py:217-219`), and
`risk_usd = notional * estimated_loss_pct / 100` becomes the R denominator.
In demo those reserved costs largely do not materialise, so realised R is divided
by an inflated risk figure. **Demo R is pessimistic by roughly the cost ratio
(~30% on a typical 2% stop).** Do not compare demo R against a replay that models
zero costs.

**9.2 — Replay must use the recorded snapshot verbatim, never recomputed.**
Some snapshot fields come from the live 24h ticker (`range_pos_pct`,
`chg_24h_pct`, `vol_24h_musd` — `market.py:498-501`) and cannot be reconstructed
after the fact. Others come from completed candles. If a replay re-derives
indicators from a later OHLCV fetch, it silently mixes revised data with the
original and every variant will look better than it was. **Rule: replay reads
`llm_input` and nothing else.**

**9.3 — Outcome evaluation must be strictly forward-looking.** Whether SL or TP
hit first must be resolved using price data strictly *after* the signal bar
closed, at a resolution fine enough to order the two events. Coarse bars make
"which touched first" ambiguous and the ambiguity is not neutral — resolving ties
in the target's favour manufactures edge.

**9.4 — The confidence floor truncates the sample.** §6 H-F. Any calibration
conclusion drawn from ≥0.65 trades alone is conditional on the floor.

**9.5 — `config_version` fragmentation.** D4. Two variants differing only in an
irrelevant field land in separate report buckets, halving an already-tiny sample
without warning.

**9.6 — Survivorship in the universe.** The universe rebuilds hourly from current
top-volume symbols. A replay over historical `llm_input` inherits whatever was
liquid *then*, which is correct — but a variant that changes `top_n` or
`min_24h_quote_volume_usd` **cannot be replayed**, because snapshots for the
symbols it would have added were never collected. **Universe parameters are not
replayable and must be tested forward only.** This is an easy trap.

---

## 10. Recommended architecture

The shape that satisfies all seven intentions without touching the trading path:

```
                    runtime/journal.db  (already being written)
                              │
        ┌─────────────────────┴──────────────────────┐
        │                                            │
   TIER 1: REPLAY                             TIER 2: SHADOW
   offline, batch                             in-process, live, non-trading
        │                                            │
   corpus loader (llm_input)                  same snapshot as the live cycle
   variant registry (named)                   deterministic variants only
   strategy+risk re-derivation                journals `shadow_decision`
   forward outcome resolver                   HARD isolation: no Exchange handle
   scorer                                            │
        │                                            │
        └─────────────────┬──────────────────────────┘
                          │
                 findings store  (SQLite + generated markdown)
                 one scorecard per strategy × variant
                          │
                 promotion protocol → TIER 3: DEMO (the existing agent)
```

**Key design commitments:**

1. **Variants are named and registered, not hashed.** A variant is
   `{variant_id, strategy_id, base_version, parameter_overrides, hypothesis,
   status}`. `variant_id` is human-readable (`momentum.rr.fixed_2.5`). Attribution
   keys off `variant_id`, never off a whole-config hash.

2. **Shadow variants are deterministic by default.** An LLM-driven variant costs a
   full extra call per cycle — 288/day each. Ten LLM variants is ~$500–950/month.
   Deterministic variants (parameter changes to the contract, sizing, exits) cost
   nothing and cover intention #4 completely. LLM variants (prompt A/B) must be
   opt-in, budgeted, and rare.

3. **Shadow cannot trade — enforced by construction, not discipline.** The shadow
   evaluator receives no `Exchange` instance. It gets a snapshot dict and a cfg and
   returns a record. A unit test asserts the type boundary holds.

4. **Shadow cannot break or slow the trading loop.** It runs *after* the trading
   decisions are committed, inside `try/except`, under a wall-clock budget, and a
   shadow failure is journalled and swallowed.

5. **Rejection requires a parameter sweep, per your intention #4.** A hypothesis
   is never rejected on one parameter setting. The promotion protocol encodes
   this: minimum sample per variant, minimum number of parameter settings tried,
   and a pre-registered decision rule written *before* results are inspected —
   otherwise, at these sample sizes, you will p-hack.

6. **Universe parameters are forward-test-only.** §9.6.

---

## 11. What I would do first

In strict order, with reasoning:

1. **Answer H-E.** Build the corpus loader + replay + the deterministic null
   model, and find out whether the LLM beats "the contract fired, trade it".
   Everything else — every parameter sweep, every new setup type — is downstream
   of knowing whether the expensive component earns its keep. This needs no
   changes to the trading path at all.

2. **Fix the variant identity problem (D4).** Until a variant has a stable,
   readable, strategy-scoped ID, every result you record is attributed to an
   opaque hash and cannot be compared across runs. This is cheap and it is a
   prerequisite for intentions #4 and #5.

3. **Reject H-A, H-B, H-C now, in code.** Delete or redefine `structure_target`,
   re-specify `funding_squeeze` against `funding_percentile_30` with a structure
   anchor, and replace `other` with registered hypothesis IDs. You will otherwise
   spend weeks collecting statistics on three things that cannot produce a signal.

4. **Then build the sweep + findings store**, and only then the shadow tier.

Detailed sequencing, batch boundaries and acceptance criteria are in
[`batched-implementation.md`](batched-implementation.md).

---

## Appendix — verification notes

- Test run: historical Python 3.12 verification snapshot; do not use its
  result as the current suite status. The repo requires the pinned research
  dependencies and a compatible interpreter.
- `llm_input` / `llm_output` consumer search:
  `grep -rn "llm_input\|llm_output" --include=*.py .` → writer + its own unit
  tests only.
- Parallel-strategy infrastructure search:
  `grep -rin "hypothes\|backtest\|shadow\|walk.forward\|sweep"` → three
  incidental matches (a docstring about shell shadowing, two class names). No
  infrastructure.
- All file:line references were read directly and quoted verbatim above.
