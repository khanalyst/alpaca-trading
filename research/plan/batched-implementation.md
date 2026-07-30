# Batched implementation plan

Companion to [`findings.md`](findings.md). Each batch is independently
shippable, has explicit acceptance criteria, and states its blast radius on the
live trading path.

**Guiding constraint:** batches 1–5 touch the trading path **zero times**. The
research layer is built entirely on data the agent already writes. The trading
path is not modified until batch 6, and not extended until batch 7.

---

## Dependency order

```
B0 substrate ──┬── B1 corpus ── B2 outcomes ── B3 replay ──┬── B4 sweep ── B5 findings store
               │                                            │
               └────────────────────────────────────────────┴── B6 fix rejected hypotheses
                                                                      │
                                                       B7 shadow tier ─┴─ B8 multi-strategy
                                                                      │
                                                       B9 cadence/cost │
                                                                      │
                                                       B10 research test suite (continuous)
```

B10 is not last in practice — write each batch's tests inside that batch. It is
listed separately because it defines the standard.

---

## Batch 0 — Research substrate (no behaviour change)

**Goal:** make variants nameable and runtime paths injectable, so everything
downstream has a stable identity to attribute results to.

**Blast radius on trading:** none. Additive only.

### Changes

**0.1 Injectable runtime paths** — `agent/state.py`

Replace the four module constants (`state.py:20-24`) with a small holder plus a
`configure_runtime(path)` function, keeping the existing names as module-level
properties so no caller changes. This removes the hand-patching in
`tests/test_state.py:15-26` and is a prerequisite for any isolated research run.

**0.2 Variant registry** — new `agent/variants.py`

```python
@dataclass(frozen=True)
class Variant:
    variant_id: str          # "momentum.baseline", "momentum.rr.fixed_2_5"
    strategy_id: str
    base_version: str        # "phase1-v1"
    overrides: dict          # dotted-path -> value, e.g. {"strategy.fixed_reward_risk": 2.5}
    hypothesis: str          # one sentence, human-written, required
    status: str              # candidate | testing | promoted | rejected | superseded
```

- `variant_id` is human-readable and stable. It is the attribution key from here
  on, **not** the whole-config hash (findings.md D4).
- `apply(variant, base_cfg) -> cfg` deep-copies the base config, applies
  `overrides`, and runs it through the existing `validate_config()`. **A variant
  that produces an invalid config fails loudly at registration time**, matching
  the repo's fail-closed house style.
- Registry persisted as `research/variants.yaml`, loaded and validated on use.

**0.3 Strategy-scoped config fingerprint** — `agent/state.py`

Add `strategy_fingerprint(cfg)` as the compatibility name for a secret-free
executable experiment fingerprint covering mode, LLM provider/model/settings,
universe selection, the full cycle/cadence block, strategy, risk, execution and
trading costs. Leave the existing whole-config `config_version` in place.

**0.4 Journal schema additions** — `agent/state.py`

Add `variant_id TEXT` and `strategy_config_version TEXT` to `trades` and
`events` via the existing `_ensure_columns()` in-place migration pattern
(`state.py:560-567`), and add both to `set_journal_context()`'s allowed set.
Live trading writes `variant_id = "live"`.

### Acceptance criteria

- [ ] `state.configure_runtime(tmpdir)` fully redirects state, PID, lock and DB.
- [ ] `tests/test_state.py` no longer reassigns module globals by hand and still passes.
- [ ] `apply()` on an override that violates config bounds raises `ConfigError`.
- [ ] `strategy_fingerprint` is unchanged by alerts/research-store edits and
      changes on LLM, universe, cadence, strategy, risk or execution edits.
- [ ] An existing `journal.db` migrates in place with no row loss (extend
      `test_legacy_journal_is_migrated_in_place_without_losing_rows`).
- [ ] Full existing suite: 170 passed, 1 skipped.

---

## Batch 1 — Corpus loader

**Goal:** read the data you are already collecting. Nothing more.

**Blast radius on trading:** none. Read-only against `journal.db`.

### Changes

New `research/corpus.py`:

- `load_cycles(db) -> Iterator[CycleRecord]` — parse `llm_input` events into
  `(ts, run_id, cycle_id, snapshot: dict, portfolio: dict, max_new: int)`. The
  snapshot lives inside the provider request body, so extraction differs by
  provider (`anthropic` → `messages[0].content`, `openai` → `messages[1].content`);
  handle both, and parse the two JSON blocks out of the user message built by
  `brain.LLM._user_message`.
- `load_model_outputs(db)` — `llm_output` joined on `cycle_id`: raw text, parsed
  decisions, response ID, request attempts.
- `load_outcomes(db)` — `trades`, `setup_status`, `rejected`,
  `order_execution`, `entry_liquidity_rejected` joined on `setup_id` / `trade_id`.
- `index_by_signal_bar(cycles)` — `(symbol, signal_ts) -> first snapshot seen`.
  With a 300s cycle and a 900s bar there are ~3 duplicate observations per bar;
  **take the first**, which is what the agent actually decided on.

New CLI `research.py corpus stats`:

```
corpus: runtime/journal.db
  cycles              1,284   2026-07-14 06:00 -> 2026-07-28 11:45 UTC
  runs                    7   gaps > 1h: 3 (total 9.2h downtime)
  unique symbols         23
  unique signal bars  4,117   (symbol x 15m bar observations)
  model outputs       1,284   parse failures: 11 (0.9%)
  proposals             206   executed: 41   vetoed: 165
  matched round trips    38
```

**This single command answers the question nobody can answer today: how much
data do you actually have, and is it enough to reject anything?**

### Acceptance criteria

- [ ] Loads both Anthropic and OpenAI-shaped `llm_input` payloads.
- [ ] Never raises on a malformed/truncated payload — counts and skips it.
- [ ] `corpus stats` runs against a real `runtime/journal.db` and against an
      empty DB without error.
- [ ] Reports uptime gaps explicitly — a corpus with holes must not silently
      look continuous.

---

## Batch 2 — Forward outcome resolver

**Goal:** given a setup plan (entry, SL%, TP%, direction, signal_ts), determine
what actually happened next — with a hard no-lookahead guarantee.

**Blast radius on trading:** none. Offline; writes only to a local price cache.

### Changes

New `research/prices.py`:

- Fetch 1m OHLCV per `(symbol, day)` **once**, cache to
  `research/cache/prices.db`, serve offline forever after. This is the only
  network access in the research layer, and it is idempotent and rate-limited.
- Cache is keyed and immutable; a re-fetch of an existing key is a no-op.

New `research/outcomes.py`:

- `resolve(plan, prices) -> Outcome` with fields `result` (`target` | `stop` |
  `timeout` | `no_data`), `exit_ts`, `exit_price`, `bars_held`, `mae_pct`,
  `mfe_pct`, `r_multiple`.
- **No-lookahead contract, enforced in code and tested:** the resolver may only
  read bars with `ts >= signal_ts + signal_bar_duration`. Reading anything at or
  before the signal bar's close raises.
- **Pessimistic tie-break:** if a single 1m bar's range spans both SL and TP,
  resolve as **stop**. Documented, tested, and never configurable — resolving
  ties favourably is the easiest way to manufacture fake edge.
- **Timeout** at `risk.max_hold_hours`, exiting at that bar's close, mirroring
  the live max-hold force-close.
- Costs are modelled explicitly and symmetrically: 2× `taker_fee_pct_per_side`,
  the recorded `spread_pct` from the snapshot, and
  `expected_stop_slippage_pct` on stop exits only. Funding is applied per
  `funding_rate_pct` × intervals held, direction-aware — mirroring
  `risk.py:198-202`.

### Acceptance criteria

- [ ] Golden fixture: a hand-built price path with a known SL-first outcome, a
      known TP-first outcome, and a known same-bar tie → all three resolve as
      specified.
- [ ] Test asserts the resolver raises if handed a bar at or before `signal_ts`.
- [ ] Test asserts the same-bar tie resolves to `stop`.
- [ ] Cache hit path performs zero network calls (assert with a mock).
- [ ] Cost model reproduces `risk.py`'s `estimated_loss_pct` arithmetic on a
      shared fixture — same numbers, or the two tiers are not comparable.

---

## Batch 3 — Replay engine and the null model

**Goal:** re-derive what any variant would have decided, and settle findings.md
H-E — does the LLM beat the deterministic contract?

**Blast radius on trading:** one small, safe signature change (below).

### Changes

**3.1 Clock injection** — `agent/risk.py`

`vet_open(..., now: float | None = None)` defaulting to `time.time()`. Used by
the two cooldown/backoff comparisons (`risk.py:87`, `101-103`, `129-131`).
Purely additive; every existing caller is unaffected.

**3.2 Replay engine** — new `research/replay.py`

Two proposer modes, and the distinction is the entire point:

| Mode | Proposals come from | Isolates |
| --- | --- | --- |
| `recorded_llm` | The decisions the live model actually made, from `llm_output.parsed_decisions` | The effect of **parameter changes**, holding model behaviour fixed |
| `deterministic` | A code proposer that fires whenever the evidence contract is satisfied; direction by trend majority; fixed anchor/exit policy; confidence 1.0 | The effect of **the LLM itself** |

For each cycle × symbol, replay runs the real production functions unmodified:
`strategy.setup_evidence` → `strategy.build_setup_plan` →
`RiskEngine.vet_open` → `outcomes.resolve`. **It must call the production code,
never a reimplementation** — a reimplemented strategy tests the reimplementation.

Portfolio state (open positions, gross exposure, cooldowns, setup memory) is
simulated forward through the replay so exposure caps and cooldowns bind the way
they do live.

**3.3 Self-validation** — the critical acceptance test

Replaying `variant = momentum.baseline` in `recorded_llm` mode over the
historical corpus **must reproduce the live agent's own recorded decisions**:
every `setup_proposed` event, every `rejected` reason, every derived
`stop_loss_pct` / `take_profit_pct` / `notional`.

If it does not match, the replay is wrong and every number downstream of it is
worthless. This is the single most important test in the plan.

### Acceptance criteria

- [ ] Baseline replay reproduces ≥99% of recorded `setup_proposed` and
      `rejected` events, with every mismatch individually explained (expected
      sources: `time.time()` boundary effects on cooldown expiry, and cycles
      where reconciliation set `max_new = 0`).
- [ ] Same corpus + same variant, run twice → byte-identical output.
- [ ] Replay makes zero network calls and zero LLM calls (assert with mocks).
- [ ] `research.py replay --variant momentum.baseline --mode deterministic`
      produces a scored result set.
- [ ] **H-E is answered**, with the LLM and null-model results side by side and
      a confidence interval on the difference.

---

## Batch 4 — Parameter sweep and scorer

**Goal:** intention #4 — never reject a hypothesis on a single parameter value.

**Blast radius on trading:** none.

### Changes

**4.1 Shared scorer** — new `research/score.py`

Factor `match_round_trips()` and the metric arithmetic out of `report.py` into a
shared module used by **both** the live report and the replay scorer. Two
scorers means two sets of numbers that cannot be compared. `report.py` keeps its
CLI and output; only its internals move.

Metrics per variant: matched round trips, win rate, expectancy (USDT/trade),
profit factor, mean and total R, max synthetic drawdown, trades/day, mean hold
hours, and total costs split into fees / funding / slippage.

Plus the **funnel**, which nothing reports today:

```
contract fired      412
  -> proposed       206   (50.0%)
  -> vetoed         165   by reason: confidence below floor 71,
                          net directional cap 34, already holding 28,
                          semantic cooldown 19, gross cap 13
  -> would execute   41   (10.0% of fired)
```

**4.2 Small-sample honesty** — same module

At these sample sizes a point estimate is noise. Every reported metric carries a
bootstrap 95% CI, and the scorer emits the **minimum detectable effect** at the
current n. If the MDE is larger than any plausible edge, the scorer says
`INSUFFICIENT_SAMPLE` and refuses to rank. This is the guard against rejecting a
good hypothesis on 12 trades.

**4.3 Sweep runner** — new `research/sweep.py`

```yaml
# research/sweeps/exit_policy.yaml
hypothesis: "A 2R fixed target is not optimal; the correct multiple is unknown."
base: momentum.baseline
axis:
  strategy.fixed_reward_risk: [1.5, 2.0, 2.5, 3.0, 4.0]
```

Generates a named variant per point (`momentum.rr.fixed_1_5` …), replays each,
scores each, writes results. Multi-axis sweeps are supported but the runner
**warns loudly** when the grid size × sample size implies uninterpretable
results.

### Priority sweep axes (all pure, all replayable)

| Axis | Current | Why |
| --- | --- | --- |
| `strategy.fixed_reward_risk` | 2.0 | Never validated |
| `strategy.extended_reward_risk` | 3.0 | Never validated |
| `strategy.min_stop_atr_multiple` | 1.0 | Sets the noise floor; drives everything |
| `strategy.structure_buffer_atr_multiple` | 0.15 | Stop-hunt sensitivity |
| `strategy.hard_max_entry_extension_atr` | 2.5 | No-chase boundary |
| `strategy.breakout_range_threshold_pct` | 85 | Contract selectivity |
| `strategy.breakout_min_relative_volume` | 1.0 | Contract selectivity |
| `risk.min_confidence` | 0.65 | See findings.md H-F — **replayable only downward via recorded confidences** |
| `risk.max_net_direction_pct` | 100 | Suspected to be the dominant veto |
| `risk.max_hold_hours` | 24 | Timeout exits are free to test |

**Not sweepable by replay** (findings.md §9.6): `universe.top_n`,
`universe.min_24h_quote_volume_usd`, `cycle.timeframes` — snapshots for symbols
the live agent never watched do not exist. Forward-test only.

### Acceptance criteria

- [ ] `report.py` output is unchanged after the scorer extraction (golden-file test).
- [ ] Scorer reproduces `test_report.py`'s existing expected values exactly.
- [ ] `INSUFFICIENT_SAMPLE` triggers on a deliberately tiny corpus.
- [ ] A 5-point sweep runs end to end and emits five scored variants.
- [ ] Funnel counts reconcile: fired = proposed + not-proposed; proposed = vetoed + executed.

---

## Batch 5 — Findings store and scorecards

**Goal:** intention #5 — every learning and recommendation persisted, per
strategy and per parameter variant, reviewable later.

**Blast radius on trading:** none.

### Changes

**5.1 Store** — `research/cache/findings.db`

```
variants(variant_id PK, strategy_id, base_version, overrides_json,
         hypothesis, status, created_ts, updated_ts)
variant_runs(run_id PK, variant_id, corpus_from_ts, corpus_to_ts,
             corpus_cycles, mode, code_version, scorer_version, ts)
variant_results(run_id, metric, value, ci_low, ci_high, n)
findings(finding_id PK, variant_id, ts, author, kind, text)
   -- kind: observation | recommendation | decision
```

`findings` is append-only. A rejection is a row, not a deletion — you must be
able to see later *why* something was rejected and on what sample.

**5.2 Generated scorecards** — `findings/<strategy_id>/<variant_id>.md`

One markdown file per variant, regenerated by `research.py report`, **committed
to the repo** so the history is diffable:

```markdown
# momentum.rr.fixed_2_5
Status: testing        Updated: 2026-08-04
Hypothesis: A 2.5R fixed target outperforms the default 2.0R.
Overrides: strategy.fixed_reward_risk = 2.5

## Sample
corpus 2026-07-14 -> 2026-08-04 | 4,117 signal bars | 47 round trips
MDE at n=47: 0.34R  -- effects below this are undetectable

## Results (vs momentum.baseline)
metric        variant            baseline           delta
expectancy    +2.41 [-1.2,+6.0]  +1.88 [-1.4,+5.1]  +0.53  (not significant)
profit factor 1.31               1.22               +0.09
win rate      44.7%              48.9%              -4.2pp
trades/day    2.2                2.4                -0.2

## Funnel
[...]

## Findings log
2026-08-04  observation      Lower win rate, higher expectancy - consistent
                             with a wider target, as expected.
2026-08-04  recommendation   Continue to 100 round trips. Do not promote:
                             delta is inside the MDE.
```

**5.3 Index** — `findings/README.md`, auto-generated: every variant, status,
sample size, headline metric, last updated.

**5.4 Live report gains machine-readable output** — `report.py --json`, and a
per-`variant_id` breakdown once batch 7 lands.

### Acceptance criteria

- [ ] `research.py report` regenerates every scorecard deterministically —
      running it twice with no new data produces no diff.
- [ ] A rejected variant retains its full findings log and results.
- [ ] Scorecards render correctly with n=0 (a registered but unrun variant).
- [ ] `report.py --json` validates against a schema and contains everything the
      text output shows.

---

## Batch 6 — Act on the rejected hypotheses

**Goal:** stop collecting statistics on things that cannot produce a signal
(findings.md §6). **This is the first batch that touches trading logic.**

**Blast radius on trading:** real. Gated behind a replay comparison.

### Changes

**6.1 `structure_target` (D1 / H-A)** — `agent/strategy.py:226-232`

Delete it, or redefine the target as the next opposing swing on a higher
timeframe (which requires a new snapshot field in `market.py`). **Deleting is
the honest default** — an inert choice in the model's option set is worse than
no choice, because it makes the decision space look richer than it is.

**6.2 `funding_squeeze` (D2/D3 / H-B)** — `agent/strategy.py:136-138, 171-173`

- Threshold on `funding_percentile_30` (already computed, `market.py:102-109`),
  not a raw absolute rate. Require a minimum `funding_samples_30` before the
  contract can fire at all.
- Add the price/trend confirmation the prompt already promises: 1h trend flat or
  turning, and price not making new extremes.
- **Require `invalidation_anchor == "structure"`** — extend the check at
  `strategy.py:171-173` to cover `funding_squeeze`. A counter-trend entry must
  not get the system's tightest stop.

**6.3 `other` → registered hypothesis IDs (H-C)** — `strategy.py:186-188`, `brain.py`

Replace the unlabelled escape hatch with `hypothesis_id`, chosen by the model
from an explicit versioned list injected into the prompt. Each has its own
(possibly permissive) contract and its own attribution. Until this lands, set
`allow_experimental_setups_in_demo: false` so demo statistics mean something.

**6.4 `range_breakout` vs `trend_continuation` (D6 / H-D)** — `strategy.py:115-130`

Make them mutually exclusive: a breakout requires the *absence* of prior
multi-timeframe alignment (a transition out of chop), not merely a high range
position. Otherwise the two labels split one phenomenon.

**6.5 Version bump**

`strategy.version: phase1-v1 -> phase1-v2`. `PROMPT_VERSION` recomputes
automatically from `SYSTEM` (`brain.py:303`). Attribution forks here
deliberately — pre-v2 and post-v2 results must never be pooled. Update
`config.py`'s allowed-value checks, `tests/helpers.py`, `examples.md`, and the
README strategy table.

### Acceptance criteria

- [ ] **Replay the v1 and v2 contracts over the same corpus and publish the diff
      before merging.** How many setups does v2 add, remove, or reprice? This is
      the harness's first real job and the gate on this batch.
- [ ] `funding_squeeze` with an `atr` anchor is rejected by `build_setup_plan`.
- [ ] `funding_squeeze` cannot fire below the minimum funding sample count.
- [ ] No snapshot produces both `range_breakout` and `trend_continuation` as
      satisfied for the same direction.
- [ ] `examples.md` worked arithmetic recomputed against v2 (it currently walks
      through the v1 numbers line by line).
- [ ] Full safety suite still green.

---

## Batch 7 — Shadow tier

**Goal:** intentions #2 and #3 — variants evaluated in parallel on live data,
continuously, without trading.

**Blast radius on trading:** additive, isolated by construction, and fail-safe.

### Changes

**7.1 Evaluator** — new `agent/shadow.py`

```python
class ShadowEvaluator:
    """Advances isolated portfolios and evaluates recorded cycle proposals."""
    def __init__(self, variants: list[Variant], base_cfg: dict,
                 findings: FindingsStore) -> None: ...
    def advance(self, snapshot: dict, now: float) -> list[dict]: ...
    def evaluate(self, snapshot: dict, portfolio: dict, now: float,
                 proposals: list[dict]) -> list[dict]: ...
```

It receives **no `Exchange` instance and no live-trading state handle.** Its
only persistence dependency is the research findings store, where each variant
has a separate content-bound portfolio. The isolation is a type boundary, not
a discipline, and a test asserts it.

**7.2 Hooks** — `agent/engine.py`

```python
self._advance_shadow_variants(snapshot, now)
self._run_shadow_variants(snapshot, portfolio, now,
                          decisions=decisions, advance_accounts=False)
```

Non-negotiable properties, each individually tested:

- Every shipped variant advances on each available common real-time snapshot,
  before pause, cadence, day-stop, LLM-failure, and execution early returns.
- Evaluation reuses the cycle's already-recorded LLM proposals and confidence;
  it never makes an extra LLM call.
- Wrapped in `try/except Exception`; a shadow failure is journalled and swallowed.
- `research.shadow_budget_ms: 0` is unlimited. A positive budget is checked only
  between variants, so an evaluated variant always receives the complete proposal
  set; remaining whole variants are prioritized next cycle.
- It never touches live-trading `LOOP_KEYS` and therefore cannot corrupt trading
  state.

**7.3 Common proposals, variant-specific decisions**

All variants consume the same LLM proposal stream, including the proposal's
recorded confidence. Each arm independently recomputes deterministic setup,
risk, sizing, and exit behavior under its own overrides. This makes confidence
floors active without paying for divergent or untracked LLM calls. Prompt/model
experiments require a new, explicit variant identity and provenance instead of a
second hidden configuration path.

Every arm persists the accept/veto action atomically with its portfolio and any
opened trade. Paired inference assigns an explicit 0R to a veto and the resolved
trade R to an acceptance. Without this complete action ledger, policy axes would
compare only the trades both arms accepted and could never measure the edge in
incremental proposals admitted by confidence, exposure, or discriminator rules.

**7.4 Config** — `agent/config.py`

New optional `research:` block, validated fail-closed in the existing house
style (`_keys`, `_number`, `_boolean`):

```yaml
research:
  shadow_enabled: true
  shadow_variants: ["momentum.rr.fixed_2_5", "momentum.conf.floor_0_50"]
  shadow_budget_ms: 0          # unlimited; positive values skip only whole variants
```

**7.5 Scoring closes the loop**

Isolated shadow portfolios feed the batch-4 scorer, so variants accumulate a
live-data track record continuously. Each portfolio is bound to immutable
strategy/config/code/model provenance; changed experiment identity must use a
new variant id and cannot silently inherit old evidence. **This is intention #3,
fully satisfied:** the parallel hypotheses learn from the same proposal stream
and the same real-time snapshots while retaining independent outcomes. Legacy
executed-trades-only evidence remains auditable but cannot qualify an edge after
schema migration 7, because its missing historical vetoes are unknowable.

### Acceptance criteria

- [ ] `ShadowEvaluator` has no attribute reachable to an `Exchange` (asserted).
- [ ] A variant that raises does not fail the cycle; the trading decisions of
      that cycle are byte-identical with and without shadow enabled.
- [ ] Zero budget evaluates every variant; positive budget skips only complete
      variants and rotates skipped variants forward next cycle.
- [ ] Shadow never writes any key in `state.LOOP_KEYS`.
- [ ] `shadow_enabled: false` (or an absent `research:` block) is a complete no-op.
- [ ] Shadow decisions score through the same pipeline as replayed ones.

---

## Batch 8 — Multi-strategy enablement

**Goal:** intention #2 in its strongest form — a genuinely different strategy
implementation, not just a parameter variant.

**Blast radius on trading:** moderate. Do this **only after** batches 1–7 have
shown you need it.

### Changes

**8.1 Strategy protocol** — new `agent/strategies/` package

Extract a protocol from the current module: `identity()`, `setup_evidence()`,
`enrich_snapshot()`, `build_setup_plan()`, and the setup-memory helpers. Move
the current implementation to `agent/strategies/momentum.py` unchanged.

**8.2 Relax the config guard** — `agent/config.py:88-91`

```python
if strategy["id"] not in registered_strategies():
    raise ConfigError(f"strategy.id must be one of: {sorted(registered_strategies())}")
```

Still fail-closed — it just consults a registry instead of a hardcoded string.

**8.3 Second strategy as a shadow variant first**

A new strategy enters as a shadow variant (batch 7), accumulates a live-data
record, and is promoted to demo trading only after passing the batch-9 protocol.
It never starts by trading.

**8.4 Separate trading instances remain the last resort**

Batch 0.1 makes a second *trading* process on a sub-account possible. It is
still the wrong default: it doubles OKX rate-limit load for identical market
data, doubles LLM spend, and — most importantly — introduces timing skew, so the
two agents no longer see identical inputs and cannot be compared cleanly. Use it
only for genuinely capital-isolated experiments.

### Acceptance criteria

- [ ] `momentum` behaves identically after the move (full suite green, replay
      over the corpus produces byte-identical output).
- [ ] An unregistered `strategy.id` still fails config validation.
- [ ] A second registered strategy runs as a shadow variant and scores.

---

## Batch 9 — Promotion protocol and cadence

**Goal:** make promote/reject a rule, not a judgement call — and stop
overpaying for LLM calls.

### 9.1 Promotion protocol — `research/protocol.md` + enforcement in the scorer

Pre-registered, applied by code, **written before results are inspected**. At
these sample sizes, deciding after looking is p-hacking with extra steps.

A variant may be **promoted** to demo trading only when all hold:

1. ≥ 100 matched round trips in replay **or** shadow.
2. Tested at ≥ 3 settings along its parameter axis (intention #4 — this is the
   rule that stops a good idea being killed by one bad value).
3. Expectancy CI lower bound > baseline's point estimate.
4. Max synthetic drawdown ≤ baseline's.
5. Survives one common chronological split with at least 70 valid paired fit
   observations and 30 valid paired confirmation observations; each window
   independently clears coverage, duplicate and dependence checks.

A variant may be **rejected** only when:

1. ≥ 3 parameter settings tested, **and**
2. every setting's expectancy CI upper bound < baseline's point estimate, **or**
3. it is structurally invalid (findings.md §6 — reject on inspection, no sample
   required, and record the reasoning as a `finding` row).

Everything else stays `testing`. **`INSUFFICIENT_SAMPLE` is a valid, common, and
correct outcome** — the scorer must say so rather than rank noise.

### 9.2 Decouple decision cadence from housekeeping cadence (D10)

`cycle.interval_seconds: 300` against a 900s signal bar means ~2 of every 3 LLM
calls cannot produce a fresh evaluation for a symbol already evaluated this bar
(`strategy.evaluated_signal`, `engine.py:1661-1664`).

Split the two:

```yaml
cycle:
  interval_seconds: 300           # housekeeping, reconciliation, breakers
  decision_interval_seconds: 900  # LLM call, aligned to the signal bar
```

Roughly **3× LLM cost reduction with no loss of safety reaction time** — margin
guards, max-hold and reconciliation keep running every 300s. Simply raising
`interval_seconds` to 900 would *also* slow the margin guard to 15 minutes,
which is not an acceptable trade; this split avoids it.

**Validate against the corpus before shipping:** how many *acted-upon* decisions
occurred on cycles that were not the first of their signal bar? The batch-1
corpus answers this exactly, and if the answer is "more than a handful", don't
do it.

### Acceptance criteria

- [ ] The scorer emits `PROMOTE` / `REJECT` / `CONTINUE` / `INSUFFICIENT_SAMPLE`
      per the rules above, with the governing criterion named.
- [ ] Out-of-sample split is implemented and reported.
- [ ] Corpus analysis of decision-cycle waste is published before 9.2 merges.
- [ ] With `decision_interval_seconds` set, housekeeping still runs at
      `interval_seconds` (asserted).

---

## Batch 10 — Research test suite

**Goal:** the standard for every batch above. Written *within* each batch, not
afterwards.

**New `tests/research/`, kept entirely separate from the safety suite.** Do not
merge them and do not weaken the existing suite to accommodate the new one.

| Test | Asserts |
| --- | --- |
| `test_corpus.py` | Both provider payload shapes parse; malformed payloads are counted not raised; signal-bar dedup takes the first observation |
| `test_no_lookahead.py` | Resolver raises on any bar at or before `signal_ts`; same-bar tie resolves to `stop` |
| `test_replay_determinism.py` | Same corpus + variant twice → byte-identical; zero network and zero LLM calls |
| `test_replay_fidelity.py` | **Baseline replay reproduces the live agent's recorded decisions** — the keystone test |
| `test_shadow_isolation.py` | `ShadowEvaluator` cannot reach an `Exchange`; a raising variant leaves trading decisions byte-identical; `LOOP_KEYS` untouched |
| `test_variant_identity.py` | `strategy_fingerprint` is stable under irrelevant config edits and changes under relevant ones; invalid overrides raise `ConfigError` |
| `test_scorer.py` | Golden expectancy/PF/R values; funnel counts reconcile; `INSUFFICIENT_SAMPLE` triggers correctly |
| `test_findings_store.py` | Scorecard regeneration is idempotent; rejected variants keep their history |

---

## Explicitly not doing

| Not doing | Why |
| --- | --- |
| Running N full agent processes in parallel | Blocked by three mechanisms (findings.md §3); N× the LLM cost; N× the OKX rate-limit load for identical data; timing skew makes the variants incomparable |
| A general backtester over arbitrary historical OHLCV | The recorded snapshot is the *only* faithful input (findings.md §9.2). A recomputed backtest tests a different system than the one you run |
| Letting shadow variants place orders "just to see" | Defeats the entire isolation design. Promotion to demo is the mechanism for that |
| Weakening the existing safety tests to fit research code | They are the reason it's safe to move fast on the research layer |
| Sweeping universe parameters in replay | Snapshots for unwatched symbols do not exist (findings.md §9.6). Forward-test only |
| Optimising the prompt before answering H-E | If the LLM does not beat the deterministic null, prompt tuning is rearranging deck chairs |

---

## Suggested sequencing

**First** — B0 → B1 → B2 → B3. This is the whole game: it ends with H-E answered
and a self-validating replay harness, and it does not touch trading once.

**Second** — B6. Stop collecting statistics on the three rejected hypotheses,
with the B3 harness as the gate on the change.

**Third** — B4 → B5. Sweeps and the persisted findings store; intentions #4 and
#5 land here.

**Fourth** — B7. Shadow tier; intentions #2 and #3 land here.

**Then** — B8 and B9 as the need proves itself.

**Throughout** — B10.

The critical path to answering your most expensive open question (does the LLM
earn its keep?) runs through B0–B3 only, and none of it can hurt the running
agent.
