# v2-hardening — development plan

> **Status: batches 0-4 delivered.** See `research/results/tournament/REPORT.md`
> for the current leaderboard. Three things the plan did not anticipate, all
> discovered by running the harness rather than by reasoning about it:
>
> - **Null baselines were unreproducible.** `build_trades` seeded them with
>   Python's per-process-randomised `str.hash()`, so identical commands gave
>   different verdicts. A tier flipped between consecutive runs. Fixed with
>   crc32.
> - **A seventh gate was needed.** `funding-carry` posted +2.008%/trade and
>   passed all six. Decomposed: funding contributed 2% of the result and
>   price movement 98%. `mechanism_is_the_source` now checks that the money
>   comes from where the mechanism says.
> - **Provenance needed enforcing.** The replacement hypothesis generated
>   from that decomposition clears every gate on the window that suggested
>   it. Pre-registrations now declare `in_sample_by_construction`, and the
>   tournament caps such hypotheses at T1 regardless of the numbers.
>
> Batch 5 is largely absorbed into batch 4. What remains is `scalp-maker`,
> which is blocked on ~3 months of recorded order-book data, and whatever
> the shadow evidence says once the loop has been running.

A batch-ordered plan to convert this repository from *one strategy that has
been measured and failed* into *a tournament that continuously evaluates many
strategies, reports a confidence tier for each, and lets one line of config
promote a winner into demo trading*.

Batches are ordered by dependency, not by calendar. Several run in parallel.
Nothing here waits on data collection except the batches that explicitly
cannot start without it — and those are the reason Batch 0 starts today.

---

## 0. The assumption change

Everything below follows from replacing one assumption. State it plainly
before touching code, because half the current architecture encodes the old
one.

**Old assumption (encoded throughout the repo).** There is an edge in a 15m
momentum entry, and the job of the system is to find its parameters. The
signal timeframe is fixed at 15m (`agent/config.py:146`), holds are capped at
48h (`agent/config.py:191`), execution is taker by construction, the strategy
id is hard-coded to `momentum` (`agent/config.py:89`), and the LLM prompt is a
single module-level constant (`agent/brain.py:29`).

**Why it is wrong.** The question that architecture asks — *"when a 15m
impulse fires in the direction of the 1h/4h trend, does price continue?"* — has
been answered across 115,929 signals: 45.6–47.3% directional hit rate at every
horizon from 15m to 24h, −0.0963 R at ordinary costs (t = −4.60), 0 of 79
walk-forward parameter variants positive out-of-sample. That is not a
parameter problem. No value of `fixed_reward_risk` or `min_stop_atr_multiple`
changes the question being asked.

**New assumption.** The system is a *portfolio of independently testable
hypotheses*. Each hypothesis owns its own signal timeframe, holding period,
execution style, cost model and risk profile. The framework's job is not to
find the right parameters for one idea — it is to run a fair, pre-registered
tournament between many ideas and report an honest confidence tier for each.

Three consequences that drive the work:

1. **`phase1-v2` is not deleted. It is demoted to the benchmark.** It becomes
   the null every new strategy must beat, alongside random-timing and
   random-direction. Keeping a measured-negative strategy as a control is more
   valuable than deleting it, because it is the only strategy in the repo
   whose true expectancy is actually known.
2. **Timeframe, hold and execution style stop being global constants.** A
   scalping hypothesis needs 1m bars and maker fills; a carry hypothesis needs
   multi-day holds. The current validation actively forbids both. Those
   constants become per-strategy declarations.
3. **Confidence is a computed property, not an opinion.** Every strategy
   carries a tier derived from a fixed rubric (§3). "Change the strategy ID in
   config and it runs in demo" is only safe if the ID carries a tier with it.

---

## 1. Target architecture

```
                        ┌──────────────────────────────────┐
   OKX live market ────►│  agent/ (main.py)                │
                        │                                  │
                        │  strategy registry               │
                        │   ├── ACTIVE id  → LLM → orders  │──► OKX demo/live
                        │   └── all others → SHADOW eval   │──┐
                        └──────────────────────────────────┘  │
                                                              │ decisions,
   OKX public REST ───► research/record_flow.py ──┐           │ no capital
        (order book, L/S ratio, taker vol, OI)    │           │
                                                  ▼           ▼
                                        runtime/research/  runtime/<mode>/journal.sqlite
                                                  │           │
                                                  └─────┬─────┘
                                                        ▼
                            ┌───────────────────────────────────────┐
                            │ research/tournament.py  (systemd timer)│
                            │  for each registered strategy:         │
                            │    6-gate battery → confidence tier    │
                            └───────────────────────────────────────┘
                                                        │
                                                        ▼
                                    research/results/tournament/REPORT.md
                                            + leaderboard.json
```

**Shadow evaluation is the load-bearing idea.** Every registered strategy
evaluates its deterministic contract on every cycle and journals what it
*would* have done. Only the active `strategy.id` sends orders. This costs no
extra LLM calls (shadow decisions are contract-only, which is exactly what the
audit measured anyway) and it means every strategy accumulates genuine
forward, out-of-sample evidence from the moment it is registered — including
the ones not trading. Without this, forward-testing five strategies takes five
times as long as forward-testing one. With it, it takes the same time.

---

## 2. Strategy register

Every strategy is a declared object with an id, a **mechanism** (who loses the
money and why they cannot stop), a falsification criterion, and a tier. No
strategy enters the register without all four.

| ID | Mechanism — who pays, and why | Timeframe / hold | Execution | Data needed | Blocked on |
| --- | --- | --- | --- | --- | --- |
| `momentum-phase1-v2` | *None. Retained as the benchmark null.* | 15m / ≤48h | taker | have it | — |
| `flush-fade-v1` | Liquidation engines sell at market regardless of price. Forced flow is price-insensitive, finite, and overshoots. The payer is the over-leveraged trader whose margin ran out and who has no choice. | 15m signal / 4–24h | taker | OI history (**~60d only**) | partially — 60d is a peek, not a test |
| `funding-carry-v1` | Perp funding is the price of leverage. When positioning is crowded, the crowd pays continuously to hold. The payer is the leveraged long in a persistently positive-funding regime. | 1h signal / 2–10 days | taker entry, funding is the return | funding (~97d) | `max_hold_hours` cap (Batch 1) |
| `trend-multiday-v1` | Slow adoption flows and reflexive positioning make multi-week crypto trend persist. Cost falls from ~15% of the move to ~1%. The payer is the mean-reversion seller who is early. | 4h signal / 5–20 days | taker | have it (candles) | `signal_timeframe` lock, hold cap |
| `ls-ratio-fade-v1` | Within an instrument, retail long/short ratio rising relative to its own mean precedes outperformance. Measured +1.114% at 48h (t=2.72) on 30 days — a hypothesis, not evidence. | 1h / 16–48h | taker | L/S ratio (**~30d retention**) | recorder runtime |
| `scalp-maker-v1` | Paid the spread for providing liquidity rather than paying it for taking. The payer is the impatient taker. | 1m signal / minutes | **maker only** | order book depth (**never served historically**) | recorder — hard-blocked, see §7 |

Two honest notes on this table:

- **`scalp-maker-v1` cannot be tested today and no amount of development
  changes that.** OKX never serves historical order-book data. At taker cost a
  1m scalp pays ~0.10% round-trip against a typical 1m move of a few basis
  points; it is arithmetically dead. It only works as a maker strategy, and
  maker fill quality cannot be simulated without queue-position data that does
  not exist until `record_flow.py` collects it. Its research design is built
  in Batch 4; its first real result is ~3 months after the recorder starts.
- **`flush-fade-v1` is the only new strategy testable in Batch 2** on
  downloadable history, and even it is limited to ~60 days of OI. Treat a
  positive first-pass result as "keep collecting", never as "promote".

---

## 3. The confidence ladder

Computed by `research/tournament.py`, stored in `leaderboard.json`, printed at
the top of every report. Six gates from
`research/results/edge-discovery-method/REPORT.md` Part A, plus forward
evidence.

| Tier | Requirements | What you may do with it |
| --- | --- | --- |
| **T0 REJECTED** | Failed any gate, or placebo ≥ 50% of candidate score | Archive with the finding. Do not retest without a new mechanism. |
| **T1 HYPOTHESIS** | Mechanism written, falsification criterion declared, feature implemented | Register for shadow evaluation only |
| **T2 CANDIDATE** | Beats all three nulls (random timing, random direction, inverted) **and** positive OOS on the purged split | Shadow + continue collecting |
| **T3 VALIDATED** | T2 **and** placebo < 25% of candidate score **and** positive net of realistic costs **and** breakeven cost > 2× actual cost | Eligible for demo, one strategy at a time |
| **T4 CONFIRMED** | T3 **and** forward shadow/demo trades ≥ the power requirement for its measured effect size, same sign as backtest | Eligible for live capital discussion |

**Power requirement** is computed per strategy, not assumed: `n = (1.96+0.84)² ×
sd² / effect²`. At the measured dispersion of sd ≈ 1.07 R, a +0.10 R edge needs
~900 trades. The report prints `trades_so_far / trades_needed` for every
strategy so "how long until I know" is never a guess.

**The calibration that overrides instinct:** on this data a t-statistic above
2 arises routinely from nothing — the measured placebo reached t = 2.60 on
pure noise. `t > 2` is therefore *not* a gate anywhere in this ladder. The
placebo ratio is the gate.

---

## 4. Batches

### Batch 0 — Start the clock, stop the bleeding (today, ~2 hours)

Independent of everything else. Do it first because two of its items get
permanently worse while you wait.

| # | Task | File | Detail |
| --- | --- | --- | --- |
| 0.1 | Start the flow recorder | — | `nohup python research/record_flow.py --out runtime/research/recorded &`. Order-book data cannot be backfilled at any price. Every hour off is unrecoverable. |
| 0.2 | Pin Python 3.12 in the venv | `SETUP.md` | `numpy==2.5.1`/`pandas==3.0.3` require ≥3.12; on 3.11 the install fails and every research script dies at `import pandas`. Add `requests` to `requirements.txt` (currently only in the lock, arrives transitively). |
| 0.3 | Cap entry extension | `config.yaml` | `hard_max_entry_extension_atr: 2.5 → 1.2`. Two of the five losing entries explicitly sat at the boundary; the model was treating a safety ceiling as a target. Entering 2.5 ATR below the 1h EMA20 is selling the low. |
| 0.4 | Minimum hold before discretionary close | `agent/engine.py` (close loop, ~:527) | Reject `model close` inside `strategy.min_hold_minutes` (new key, default 90) unless `close_trigger == "risk_reduction"`. All three closed trades died at 17/55/75 minutes — inside the window where the signal is *measured* most negative (−0.0072% at 30 min). Stops and targets remain exchange-side and unaffected. |
| 0.5 | Hard breadth cap | `agent/risk.py` `vet_open` | Refuse new opens when `_market_context.instruments_with_a_valid_setup >= 5`. Currently breadth is advisory only (`agent/market.py:624`). Measured: crowded bars −0.3475% vs quiet −0.1627%, same sign in both walk-forward halves — the largest effect in the project. All five entries were one market-wide short. |
| 0.6 | Correlated-direction cap | `agent/risk.py` | New `max_same_direction_positions: 2`. Three shorts × 30% notional = 90% net, which passes `max_net_direction_pct: 100` and the BTC-beta cap with room to spare. Every existing cap permitted the concentration. |

**Acceptance:** demo runs a full day with no entry above 1.2 ATR extension, no
close under 90 minutes that is not a risk reduction, and no third
same-direction position. Verify from the journal, not from the log.

> These reduce the bleed rate on a strategy measured to have no edge. They are
> damage control, not a fix. The fix is Batches 1–3.

---

### Batch 1 — Multi-strategy foundation

Unblocks everything else. Nothing in Batches 3–6 can start until the registry
exists.

| # | Task | File | Detail |
| --- | --- | --- | --- |
| 1.1 | Strategy registry module | `agent/registry.py` (new) | `StrategySpec` dataclass: `id`, `version`, `signal_timeframe`, `max_hold_hours`, `execution_style`, `contract_fn`, `prompt_fragment`, `risk_profile`, `mechanism`, `falsification`, `tier`. Module-level `REGISTRY: dict[str, StrategySpec]`. |
| 1.2 | Remove the single-strategy locks | `agent/config.py` | `:89` `strategy.id must be 'momentum'` → must be a key in `REGISTRY`. `:146` `signal_timeframe must be '15m'` → must equal the spec's declared timeframe. `:191` `max_hold_hours` ceiling 48 → per-spec ceiling. **Keep every check fail-closed** — validate against the spec, do not delete the validation. |
| 1.3 | Per-strategy prompt | `agent/brain.py` | `SYSTEM` (`:29`) becomes `SYSTEM_BASE + spec.prompt_fragment`, assembled once at startup. `PROMPT_VERSION` (`:348`) already hashes the text, so each strategy gets its own stable cache key and prompt caching keeps working. Byte-identical *per strategy* is the invariant, not globally. |
| 1.4 | Contract dispatch | `agent/strategy.py` | `setup_evidence` dispatches on the active spec. Existing phase1-v2 logic moves to `contracts/momentum_phase1v2.py` unchanged — it must stay byte-equivalent so `validate_features.py` still passes. |
| 1.5 | Tag the journal | `agent/state.py` | Add `strategy_id`, `strategy_version` columns to `events` and `trades`; migrate in `schema_meta`. Without this, live results cannot be attributed once more than one strategy has run. |
| 1.6 | Per-strategy risk profile | `agent/risk.py`, `config.yaml` | Move `risk_per_trade_pct`, `max_hold_hours`, `max_concurrent_positions` into the spec, with `config.yaml` values as global ceilings the spec can only tighten, never loosen. |

**Acceptance:** `strategy.id: momentum-phase1-v2` reproduces today's behaviour
exactly — `validate_features.py` clean, all 194 tests green. A second
registered id starts and trades without touching `agent/` again.

---

### Batch 2 — Fresh test protocol

Runs in parallel with Batch 1 (touches `research/` only). This is the "forget
the old tests, write a new plan" batch — the *protocol* is rebuilt; prior
results are retained only as the benchmark's known values.

| # | Task | File | Detail |
| --- | --- | --- | --- |
| 2.1 | Pluggable contract | `research/edge_lab.py` | `Contract` (`:371`) is a hard-coded phase1-v2 copy. Make it a protocol with per-strategy implementations of `evidence_masks` / `derive_levels`. |
| 2.2 | The six-gate battery | `research/gates.py` (new) | One function per gate: `beat_nulls`, `survive_costs`, `survive_oos`, `survive_placebo`, `has_mechanism` (asserts registry metadata present), `is_detectable` (power calc). Each returns a structured pass/fail with the number, not a bool. |
| 2.3 | Pre-registration | `research/hypotheses/<id>.yaml` (new) | Mechanism, prediction, falsification criterion, features used, horizons, hypothesis count — **written before the test runs**. The battery refuses to score a strategy with no pre-registration file. This is what makes the multiple-testing count honest. |
| 2.4 | Tournament runner | `research/tournament.py` (new) | For each registered strategy: load data → build trades → run six gates → compute tier → emit `leaderboard.json`. Idempotent, resumable, single command. |
| 2.5 | Purged walk-forward as the default | `research/gates.py` | 60/40 split with a 3-day purge gap, applied automatically. No result is reported without its OOS number next to it. |
| 2.6 | `flush-fade-v1` features | `research/signal_lab.py` | Add `flush_fade`, `build_follow`, `oi_chg_16b` + `xs_` variants to `build_panel` (~:158) and `FEATURES` (:179). Use `np.nan` outside each condition, not `0.0` — `evaluate_pair` drops NaN, and a zero fill creates a point mass that breaks the quantile buckets. |

**Acceptance:** `python research/tournament.py --data runtime/research/data`
produces a leaderboard where `momentum-phase1-v2` scores **T0 REJECTED** with
approximately its known values (−0.096 R at base costs). If the new harness
does not reproduce the old verdict on the old strategy, the harness is wrong —
that is the regression test for the entire research rebuild.

---

### Batch 3 — Shadow evaluation and the live→research bridge

Depends on Batch 1. This is what makes point 4 — *lab validates everything
while demo trades one thing* — actually true rather than aspirational.

| # | Task | File | Detail |
| --- | --- | --- | --- |
| 3.1 | Shadow evaluator | `agent/engine.py` | After the snapshot is built, evaluate **every** registered contract, not just the active one. Journal each as `shadow_decision` with `strategy_id`. No LLM call, no orders, negligible cost. |
| 3.2 | Shadow outcome resolution | `agent/engine.py` or a sidecar | For each shadow decision, record the deterministic stop/target/max-hold outcome from subsequent bars. This is the forward trade record for strategies with no capital. |
| 3.3 | Journal → dataset exporter | `research/export_live.py` (new) | Read `runtime/<mode>/journal.sqlite`, emit the `edge_lab` dataset layout. Currently **nothing** reads the journal into research — I verified there is no reference to `research/` or `edge_lab` anywhere in `main.py`, `agent/engine.py` or `agent/state.py`. |
| 3.4 | Forward-vs-backtest tracker | `research/tournament.py` | Per strategy: backtest expectancy, forward shadow expectancy, trade count, power requirement, and whether the signs agree. Sign disagreement is the earliest overfitting alarm available. |

**Acceptance:** after 24h of demo running, `export_live.py` produces a dataset
containing shadow trades for every registered strategy, and the tournament
report shows a forward column for all of them — including the five not
trading.

---

### Batch 4 — Automation, reporting, Azure

Depends on Batches 1–3.

| # | Task | File | Detail |
| --- | --- | --- | --- |
| 4.1 | Nightly research job | `research/nightly.sh` (new) | Refresh history → export live journal → run tournament → regenerate report. Idempotent; safe to kill and rerun. |
| 4.2 | Report generator | `research/report_writer.py` (new) | `research/results/tournament/REPORT.md`: leaderboard by tier, per-strategy gate table with numbers, forward-vs-backtest, power progress, and an explicit **recommendation line** — promote / hold / reject, with the reason. |
| 4.3 | Recommendation rules | `research/report_writer.py` | Deterministic, not discretionary: promote only on T3 with no open gate failure; recommend demotion if forward sign has disagreed with backtest for ≥ 100 trades. |
| 4.4 | systemd units | `deploy/` (new) | `okx-trader.service`, `okx-recorder.service`, `okx-research.timer` (daily 03:00 UTC). Restart-on-failure, journald logging. |
| 4.5 | Azure provisioning runbook | `SETUP.md` | See §5. |
| 4.6 | Alert on tier change | `agent/alerts.py` | Reuse the existing retried-webhook path to notify on any promotion/demotion. You should not have to read a report to learn something crossed a gate. |

**Acceptance:** VM reboots; trader, recorder and research timer all come back
without intervention. A fresh report appears daily. Changing `strategy.id` in
`config.yaml` and restarting the trader is the entire promotion procedure.

---

### Batch 5 — Strategy build-out

Depends on Batches 1–2. Each strategy is one self-contained unit of work; do
them in the order the data allows.

| # | Strategy | Work | Data gate |
| --- | --- | --- | --- |
| 5.1 | `flush-fade-v1` | Contract + pre-registration + first tournament run | ~60d OI — testable now, result is a peek |
| 5.2 | `trend-multiday-v1` | Extend `HORIZONS` in `signal_lab.py` past 48h to 4–14 days; re-run existing features. Cheapest test in the plan — no new features, one constant. | none — candles only |
| 5.3 | `funding-carry-v1` | Contract + multi-day hold (needs Batch 1.2 hold-cap change) | ~97d funding |
| 5.4 | `ls-ratio-fade-v1` | Contract; the +1.114%/t=2.72 result is 30 days and ~210 observations — exactly the shape that dissolves under a placebo | needs ≥90d recorder |
| 5.5 | `scalp-maker-v1` | See §7 | needs ≥90d recorder, hard-blocked |

---

### Batch 6 — Live-readiness (only if something reaches T4)

Do not build ahead of a T4 result. Written down so the finish line is defined:
maker execution path (currently taker-by-construction so positions are never
unprotected), per-strategy capital allocation, and a live-trading go/no-go
checklist. Note the measured ceiling on maker execution: ~+0.099% at perfect
fills, negative beyond ~10–15 bps of required penetration, and even the most
optimistic case left `phase1-v2` at −0.128%. It is necessary for scalping and
insufficient for everything else — build it for `scalp-maker-v1` or not at all.

---

## 5. Azure deployment

**Recommendation: one Azure VM, not Functions.**

The trading loop is a long-running stateful process with a single-process
lock, a local SQLite journal, and a 300s cycle. Azure Functions is a poor fit:
the Consumption plan caps execution at 10 minutes (fatal for backtests),
provides no durable local disk, and its stateless model fights the
single-process lock directly. Durable/Premium plans work around some of this
at a cost that exceeds the VM.

| Component | Sizing | Notes |
| --- | --- | --- |
| VM | `Standard_B2s` (2 vCPU, 4 GB), Ubuntu 24.04 | Trader + recorder are near-idle; the tournament is the only CPU spike |
| Disk | 64 GB Premium SSD | Recorder ≈ 20 MB/month; history dataset is the bulk |
| Network | Static public IP | **Required** — OKX API keys must be IP-bound |
| Backup | Nightly snapshot, or rsync `runtime/research/recorded` to Blob Storage | Recorded data is irreplaceable; a lost disk cannot be re-downloaded |

Setup outline for `SETUP.md`:

```bash
# 1. Provision: Ubuntu 24.04, B2s, static IP. Bind that IP in OKX API settings.
sudo apt update && sudo apt install -y python3.12 python3.12-venv git sqlite3

# 2. Clone + venv (3.12 is required by the numpy/pandas pins)
git clone <repo> ~/okx-agent-crypto && cd ~/okx-agent-crypto
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt

# 3. Services
sudo cp deploy/okx-trader.service deploy/okx-recorder.service \
        deploy/okx-research.service deploy/okx-research.timer /etc/systemd/system/
sudo systemctl enable --now okx-trader okx-recorder okx-research.timer
```

Keep `.env` at `0600`, owned by the service user, never in git. If the LLM
runs through Azure AI Foundry, that is already documented at `SETUP.md:169`
and needs no change.

**Optional:** if the tournament outgrows the B2s, move *only* that step to an
Azure Container Apps job triggered by the timer. Keep the trader and recorder
on the VM regardless — they need persistent local state.

---

## 6. Parallelism

| Batch | Depends on | Can start |
| --- | --- | --- |
| 0 | — | now |
| 1 | — | now (parallel with 0) |
| 2 | — | now (parallel with 0 and 1 — `research/` only) |
| 3 | 1 | after registry lands |
| 4 | 1, 2, 3 | after shadow evaluation works |
| 5.1, 5.2 | 1, 2 | as soon as the battery runs |
| 5.3 | 1.2, 2 | needs the hold-cap change |
| 5.4, 5.5 | 0.1 + ~90 days | **data-gated, not development-gated** |

Batches 0, 1 and 2 touch disjoint files and can be developed simultaneously.
The only true serialization is 1 → 3 → 4.

---

## 7. Scalping — what is honestly possible

Requested, and worth being precise about rather than promising.

**Mechanism.** A maker earns the spread for supplying liquidity; the payer is
the impatient taker. This is a real and durable mechanism — it is how market
makers exist — and it does not require predicting direction.

**Why it cannot be a taker strategy.** Round-trip taker is ~0.10% before
spread. A typical 1m move on a major is a few basis points. The cost exceeds
the entire opportunity; no signal quality fixes that arithmetic.

**Why it cannot be backtested today.** Maker profitability is decided by fill
quality, and fill quality depends on queue position, which depends on
order-book state that OKX has never served historically. The existing
`maker_study.py` parameterises this as "how far must price penetrate the
resting limit before it counts as filled" and finds the crossover between 5
and 15 bps — below it maker wins, above it adverse selection swamps the fee
saving. Which side of that crossover you land on is an empirical question
about a specific venue, instrument and order placement policy, and it is
unanswerable without recorded depth.

**So the plan is:**

1. Recorder runs from Batch 0.1 (already collecting 1–25 bps depth bands and
   `book_span_bps`).
2. Batch 4 adds a **queue-position simulator** fed by recorded depth: given a
   resting order at a level, estimate fill probability from observed depth
   ahead and subsequent trade volume at that level.
3. Batch 5.5 registers `scalp-maker-v1` at **T1 HYPOTHESIS** and shadow-runs
   it — logging hypothetical maker placements and whether recorded book state
   would have filled them.
4. First honest result: **~3 months after the recorder starts.** Any number
   before that is a guess dressed as a backtest.

Two measured facts that shape the design: spread varies ~90× across the
universe (BTC 0.015 bps, DOGE 1.39 bps), so scalping is a **tight-spread
majors-only** strategy; and depth is not the binding constraint at retail size
— even thin names offer ~$20k within 25 bps.

---

## 8. What this plan deliberately does not do

- **It does not tune `phase1-v2`.** That search has been run 79 ways with 0.0%
  positive out-of-sample. Batch 0 reduces its bleed rate; nothing tries to
  rescue it.
- **It does not widen the universe.** Tested and refuted — `top_n` 10 → 30 made
  expectancy monotonically worse, before costs as well as after.
- **It does not build maker execution for the momentum strategy.** Bounded at
  ~+0.10% and it still leaves that strategy at −0.128%.
- **It does not promise an edge.** Six strategies with mechanisms is six
  chances, not six edges. Prior odds on any single one are low; ~2,250
  hypotheses have already been tested here without a survivor. What the plan
  buys is that the next result will be *trustworthy* — pre-registered,
  placebo-controlled, forward-tracked, and reported with a tier instead of a
  feeling.
