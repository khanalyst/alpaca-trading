# OKX AI Trading Agent

This repository runs a demo-first OKX perpetuals agent and the research system
around it. The current runtime is `momentum/phase1-v3`; it is useful for
collecting evidence and rehearsing controls, but no strategy is currently
live-eligible.

Start with [`SETUP.md`](SETUP.md) for installation and
[`OPERATIONS.md`](OPERATIONS.md) for the daily Mac/VM workflow.

## Current state

| Area | Current truth |
| --- | --- |
| Active runtime | `momentum`, version `phase1-v3`, demo mode by default |
| Current runtime tier | `T0_REJECTED`; retained as a benchmark/null and safety rehearsal |
| LLM provider | `openai`, model `gpt-5.6-terra` |
| Findings store | `research/cache/findings.db`, SQLite schema 8, append-only evidence |
| Shadow | Enabled; every active deterministic variant is enrolled with two bounded workers |
| Tournament | Exploratory OHLCV evidence; it awards no tier above `T2_CANDIDATE` |
| Live | Blocked unless a strategy is `T3_VALIDATED` or better and cites a reviewed packet |

The current strategy, runtime hypotheses, pre-registered research hypotheses,
and every named variant are indexed in
[`research/HYPOTHESES_AND_VARIANTS.md`](research/HYPOTHESES_AND_VARIANTS.md).

## Safety boundaries

- Begin in OKX demo mode. API keys need Read and Trade permissions only; never
  enable Withdraw.
- The LLM proposes decisions and, for the three runtime research hypotheses,
  bounded numeric research proposals. Deterministic strategy, risk, sizing,
  exchange, and circuit-breaker code remains authoritative.
- Shadow accounts have independent cash, positions, risk state, cooldowns,
  scheduler state, and findings. Isolation runs both ways: shadow decisions do
  not change the live account, and live decisions are withheld from everything
  on the live path's shadow account; shadow breadth is recomputed for each
  account.
- A qualified edge starts an isolated local PAPER stage only when its account
  is flat. It does not edit `agent/registry.py`, change `config.yaml`, or place
  live orders.
- The OHLCV tournament is exploratory. It is useful for withholding capital and
  ranking questions, but it cannot raise a strategy to a live-capable tier.

## Architecture

```text
OKX market/account data
        │
        ├── main.py run ──► one LLM decision set ──► deterministic risk engine
        │                                      └── live/demo execution
        │
        ├── journal.db ──► replay/G2 ──► authoritative findings
        │
        └── same decision set ──► isolated shadow variants
                                      ├── static parameter variants
                                      ├── registered hypothesis settings
                                      └── one bounded adaptive setting per strategy/cycle
                                             │
                                             ▼
                                      findings.db → forward qualification
                                                    → family correction
                                                    → immutable edge event
                                                    → local PAPER only
```

The model is called once per cycle. The same parsed decisions are evaluated by
all eligible variants; variants do not make extra LLM calls.

## Quick start on a Mac

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py run
```

Useful read-only checks:

```bash
./.venv/bin/python main.py status
./.venv/bin/python main.py strategies --verbose
./.venv/bin/python research.py readiness
./.venv/bin/python research.py corpus stats
```

The complete local installation and Azure VM setup are in
[`SETUP.md`](SETUP.md). The operational procedures, reporting commands,
corpus export, and troubleshooting are in [`OPERATIONS.md`](OPERATIONS.md).

## Configuration that matters

The shipped `config.yaml` currently contains a `research:` block with these
important settings:

| Setting | Current value | Meaning |
| --- | --- | --- |
| `strategy.id` / `strategy.version` | `momentum` / `phase1-v3` | Runtime strategy identity |
| `llm.provider` / `llm.model` | `openai` / `gpt-5.6-terra` | Model route; Azure uses the OpenAI-compatible base URL |
| `cycle.decision_interval_seconds` | optional; default decision cadence | Changes decision timing without slowing safety housekeeping |
| `maker_first_enabled` | `false` | The exchange primitive exists; the maker entry path is not enabled |
| `maker_first_wait_seconds` | configured execution wait | Maximum maker wait before the configured fallback policy |
| `research.shadow_enabled` | `true` | Enables isolated deterministic shadow evaluation |
| `research.shadow_variants` | `[*]` | Enrolls active candidate/testing variants |
| `research.shadow_budget_ms` | `0` | No per-cycle wall-clock budget; evaluate all scheduled variants |
| `research.shadow_workers` | `2` | Bounded parallel variant computation; durable writes remain serialized |
| `research.findings_store` | `research/cache/findings.db` | Findings, portfolios, proposals, analyses, and evidence packets |
| `research.paper_min_closed_trades` | `100` | Minimum post-qualification PAPER sample for a T3 packet |

The findings store never
falls back to a temporary database. If the configured path is unavailable,
the command must fail rather than silently writing evidence somewhere else.

## Current hypotheses and variants

There are three layers, and they must not be conflated:

1. `agent/registry.py` records strategies, mechanism, falsification, tier,
   implementation readiness, and forward-model readiness.
2. `agent/hypotheses.py` records the three runtime hypotheses and their
   contract parameters/settings. `agent/variants.py` materializes these into
   named shadow variants.
3. `research/hypotheses/*.yaml` records pre-registered research strategies and
   their settings for the OHLCV tournament. Those settings are not live
   configuration and are not automatically promoted.

The current map, values, and storage locations are maintained in
[`research/HYPOTHESES_AND_VARIANTS.md`](research/HYPOTHESES_AND_VARIANTS.md).
The committed momentum scorecards are under `findings/momentum/` and the
runtime-generated adaptive variants are stored in `findings.db` with exact
value, setting, run, lock, and observation history.

## Research and edge qualification

The two evidence paths are deliberately different:

- **Journal replay is authoritative.** It uses the snapshots the agent actually
  saw, and G2 must pass before downstream authoritative analysis is trusted.
- **The OHLCV tournament is exploratory.** It recomputes indicators from
  downloaded data and cannot raise a tier. An exploratory result is kept
  unrevised rather than as demoted when the evidence is insufficient.

Available CLI commands:

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify
./.venv/bin/python research.py t3-packet --variant momentum.rr.fixed_2_5
./.venv/bin/python research.py report
```

G2 is a full stop. `INSUFFICIENT_SAMPLE` means the question is open, not that
the hypothesis failed. The promotion protocol requires **100 full pairs**,
**70 fit pairs**, **30 confirmation pairs**, **80% coverage**, and at least
eight independent six-hour episodes. See [`research/protocol.md`](research/protocol.md).

`forward-qualify` evaluates all valid active axes before any winner can be
qualified. It validates the immutable decision ledger, common evidence window,
strategy/version, model and code provenance, exact axis values, and identical
non-axis executable inputs. The non-axis executable configuration is part of
the proof; a caller cannot relabel an axis after seeing its result.

The current pipeline records:

- `book_state` and `snapshot_enrichment` for the research corpus;
- immutable decision-ledger rows, including every policy veto as a
  zero-return action;
- `shadow_decision`/variant records and isolated PAPER trades;
- forward analyses, family correction, findings, qualification events, and
  content-addressed T3 packets.

Schema migration 7 introduced the complete decision ledger and legacy
watermark. The current store is schema 8, which adds hypothesis identity and
adaptive proposal history.

The current exit policies are `fixed_rr` and `extended_rr`; the removed
structure-based policy is not available.

## Automation and reporting

`research/nightly.sh` is the scheduled research workflow. It runs readiness,
corpus statistics, G2, funnel/cadence, sweeps, three-arm analysis, forward
qualification, scorecard generation, data refresh, forward export, and the
exploratory tournament. The authoritative path runs first and G2 is a hard
stop.

The VM timer runs this workflow at 03:00 UTC. On a Mac, run it manually or
under the scheduler of your choice; no Mac launchd file is assumed by the
repository.

## T3 packet and the manual boundary

A T3 packet is an immutable, content-addressed evidence bundle. It records the
current G2 result, forward analysis, family correction, paper result,
provenance, checklist, reviewer, and registry-change reference.

The packet can be generated automatically, but the current command does not
mutate the strategy register. That is deliberate: changing a tier or changing
the active strategy is an external capital-control action, not a research
calculation. There is no technical reason this could never be automated; the
remaining product task is an explicit approval command that consumes a reviewed
packet and writes a new immutable registry/config change without silently
starting live capital. Until that approval command exists, the packet is the
handoff artifact, not the mutation itself.

## Current pending work

See [`MAIN_REPO_REVIEW_PLAN.md`](MAIN_REPO_REVIEW_PLAN.md) for the current
status of every R1–R4 item. The short version is:

- R1-04 still needs packet-hash resolution against the packet store. **How it
  completes:** resolve the cited hash and verify strategy ownership before a
  registry claim is accepted. **Why it waits:** no current strategy is live
  eligible.
- R2-02 still needs targeted fixtures for the highest-risk exploratory modules.
  **How it completes:** add deterministic fixtures or mark the one-shot study
  historical-only. **Why it waits:** the approved VM corpus is the evidence
  source for the next tournament review.
- R4-01 is implemented in code; a fresh approved-corpus re-score and review
  remain operational work. **How it completes:** run every declared setting,
  preserve the setting count, and review the report. **Why it waits:** the
  repository does not contain the VM's ignored runtime corpus.
- R3-01 through R3-03 are organization/documentation recommendations, not
  safety blockers. **How they complete:** separate studies or split the engine
  only at a natural future touch point. **Why they wait:** refactoring working
  code would add risk without unlocking evidence.

The B7.5 maker-first primitive is implemented but disabled by
`maker_first_enabled: false`; it completes only after its execution evidence
and forward model are reviewed.

## Known limits

The replay model cannot reproduce exchange-only fill races, liquidation details,
or every universe-selection feedback effect. `loss cooldown` and
`select_universe` behavior are recorded and reported as known limits rather
than silently treated as exact replay. The common forward window is frozen at
the pre-PAPER boundary; operational failures invalidate the window rather
than becoming missing data. PAPER is local simulation only.

The v6→v7 migration introduced the complete decision ledger and legacy
watermark; the current store is schema 8.

credentials are excluded from executable fingerprints. Fingerprints cover the
LLM provider/model, prompt, strategy/config, code, universe selection, and
decision cadence so evidence cannot be mixed casually across incompatible runs.

## Tests

```bash
./.venv/bin/python -m pytest -q
```

The test suite covers runtime safety, replay, no-lookahead behavior, findings
immutability, hypothesis/variant identity, adaptive locks, shadow isolation,
parallel evaluation, tournament settings, and documentation references.
