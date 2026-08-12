# Alpaca intraday trading agent

This repository is a research and execution skeleton for US equities, ETFs,
and listed OCC options through Alpaca. Paper mode is the default: collect
regular-session data, evaluate multiple strategy hypotheses and their variants,
and record every decision and fill for review. The supported universe is
US-listed equities/ETFs and listed OCC options only; crypto is rejected.
Options are single-leg long calls or puts
(buy-to-open, sell-to-close); multi-leg and naked/short option structures are
unsupported. No performance claim is made.

The research factory runs seven independent slots, each holding one rule
hypothesis with four isolated simulated-account variants, drawn from a catalog
of eleven rule families. It evaluates independent
families in parallel, diagnoses failures from chronological fit data, mutates
bounded data-only rule specifications, and judges them on untouched held-out
data. A slot that proves an edge is reseeded with a new hypothesis in the same
cycle: the proved variant is frozen and never re-tuned, but the *slot* is
parallel research capacity, so discovery continues at full width instead of
shrinking with every success. An optional LLM proposes what to try next — new
hypotheses for free slots, replacements for exhausted families, and the
parameter variants inside a hypothesis — inside the same audited grammar, and
every such proposal must earn `backtest_passed` and a strictly later forward
shadow pass through the identical gates a deterministic one faces. The signal
primitives themselves are closed: an unknown family, filter, indicator field or
data source is rejected at the boundary, and tuning must preserve both its
root's family and its grammar version, so the model parameterizes signals and
never invents one. Every proposal records the reason it was made *before* the
gate that judges it exists, that reason is graded against the gate afterwards,
and a proposal made against a non-empty history must cite the graded lesson it
reasoned from — an uncited proposal is refused, a parameter set already proved
to fail may not be re-proposed, and the citation is stored so the chain of
learning is durable. Every seeding and tuning path falls back to a
deterministic ladder, so research works with no provider configured. A rule specification
carries `max_hold_bars`, so every strategy has a
bounded time exit as well as a stop and a target; research and runtime compute
that deadline from one shared helper. Paper
`strategy.selection_mode: all_proved` selects one best proven variant per
independent family under one global risk book, ranked by held-out evidence
rather than by family name and bounded by a correlation cap so several
expressions of one bet do not become concurrent risk. A trader process
uses one execution profile (`shares` or `options`) at a time; do not mix
profiles in one process. The market calendar is America/New_York (NYSE regular
session). New entries are rejected outside the regular session, orders are
day-only, startup cancels working orders and flattens residuals, and positions
are force-closed before the close.

## How an edge reaches money

Four states, and only one transition a machine cannot make.

| State | Who decides | Can it change on its own? |
| --- | --- | --- |
| **Research** — hypotheses, variants, gates | the factory | yes, continuously |
| **Proved** — `validated`/`champion` in the ledger | the gates | yes, on evidence |
| **Trial** — trading the demo account, live results collected | automatic | yes: a trial below its floor is parked and its failure becomes a lesson |
| **Pinned** — an id you wrote into `config.yaml` | **you only** | **no.** Guards still run and still report; they raise an alert and leave it in place |

Promotion is the one step that is never automatic. When an edge clears its
trial, `research.py edge promotable` names the variant, shows what it actually
returned on the book, and prints the exact configuration block:

```bash
./.venv/bin/python research.py edge promotable
```

```json
{ "strategy": { "selection_mode": "pinned", "pinned": [
    { "id": "pin-equity-abc12345",
      "variant_id": "rule.opening-range-breakout.abc12345…",
      "vehicle": "equity", "strategy_id": "rule",
      "promoted_at": "2026-08-12",
      "note": "live paper: 34 trades over 21 sessions, total R 6.40" } ] } }
```

Paste it, restart the trader, and that edge is frozen: pinning exempts it from
automatic demotion, from trial parking, and from every other lane that would
otherwise move it. Pinning selects, it never authorizes — a pinned entry still
has to resolve to a proved ledger record with a re-verified passing proof, so
an id in a file cannot put an unproved variant on the book. A pin that cannot
resolve is reported rather than silently substituted.

Every `id` is yours and must be unique; it is the handle the audit trail, the
dashboard, and the notifications all use. Separately, every distinct
configuration the runtime loads is recorded with a content-addressed
`config_version_id` and a diff naming each field that moved, so any changed
value is traceable to a version, a time, and an actor. Secrets are recorded as
having changed, never as values.

## Safety boundary

- Paper mode (`mode: paper`, `broker.paper: true`, `ALPACA_PAPER=true`) and
  Alpaca's paper endpoint are the documented defaults.
- Live mode is an explicit, separately reviewed configuration only: set
  `mode: live`, `broker.paper: false`, `broker.allow_live: true`, and
  `ALPACA_LIVE_ENABLE=true`; use `strategy.selection_mode: specific` with one
  exact named validated/champion `strategy.variant_id`. Live mode pins that
  edge and does not auto-switch. Keep live configuration, credentials, and
  `ALPACA_AGENT_RUNTIME_ROOT` in a separate runtime scope from paper.
- `strategy.execution_mode: options` is paper-only. Alpaca offers no bracket,
  OCO, or stop order on options, so an option position's protective stop is
  software (the 60-second poller, bounded by the separate `watchdog` process)
  and `mode: live` rejects the options profile. If the broker or network is
  unreachable, nothing local protects an open option position.
- `mode: live` rejects `llm.enabled: true`. The validated edge was proven with
  the deterministic rule and no LLM in the loop, so enabling the runtime LLM
  veto in live would deploy a strategy that is not the one that passed the
  gates. Runtime decision LLM use stays off; the bounded research replacement
  adapter is a separate, offline setting.
- Keep API keys in `.env` or a host secret; never commit them. Use trading
  permissions only and disable withdrawals.
- Research, recorder, trader, and dashboard state is isolated in named
  volumes. Runtime entries require a vehicle-local `validated` or `champion`
  edge record in the SQLite ledger; research cannot place orders or mutate
  broker state. A live preflight additionally requires the account to report
  `pattern_day_trader=true`.

The source and tests are the behavioural authority. [SETUP.md](SETUP.md) is
the installation authority, [OPERATIONS.md](OPERATIONS.md) is the runbook, and
the files under `research/` describe evidence collection. Prose must not be
read as a performance claim.

## Architecture

For the detailed process, module, state, research, execution, safety, and
decomposition map, see [ARCHITECTURE.md](ARCHITECTURE.md).

`agent/alpaca_provider.py` is the small boundary around `alpaca-py`. It
normalizes account, asset, quote/bar, calendar, option-chain, order, and trade
update data for the rest of the application. `agent/alpaca_session.py` owns
the NYSE calendar and session policy. `agent/contracts/rule.py` is the safe
strategy grammar shared by research and runtime; generated specifications can
never contain executable code. It has two versions: `rule-strategy.v1` is
unchanged and keeps every existing variant id, and `rule-strategy.v2` is a
strict superset adding entry-side predicates only — a multi-filter confirmation
list, a session-time entry window, and an ATR volatility band — so a hypothesis
can express a conditional edge without any extension reaching sizing, exits, or
order placement. `agent/contracts/ibr.py` remains the explicit
IBR replay/baseline. Risk and execution
profiles enforce sizing, single-leg long-option checks, idempotent client
order IDs, and end-of-day flattening.

The lean deployment topology is:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Alpaca bars, quotes, and option snapshots (paper by default) | `runtime-data` |
| `trader` | One intraday decision loop in its configured execution profile; no overnight book | `runtime-data` |
| `research` | Scheduled replay, evidence, and reports | `runtime-data`, research volumes |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |

The research service is an opt-in Compose profile, but a fresh deployment must
run it long enough to produce a champion before the trader can open risk. It
consumes the recorder's mixed corpus (bars, quotes, and option snapshots),
which is written one append-only partition per New York session date under
`runtime/research/recorded/sessions/` with a sidecar index; the cycle script
concatenates those partitions in session order.
`ALPACA_RESEARCH_SESSION_WINDOW` limits it to the most recent N sessions and
`ALPACA_RESEARCH_DATASET` overrides the source with normalized JSONL. By default each cycle schedules seven strategies,
four isolated accounts per strategy, and up to seven worker processes. Its
edge-lab and factory lineage are kept in the SQLite ledger
at `runtime/research/edge_lab.sqlite3`; the dashboard only observes ledger
status, latest re-verified passing edges, per-edge live paper results, edge
proof reports, and the execution journal.

The read-only dashboard (`http://127.0.0.1:8080`) is the detailed reporting
surface. Beyond trader health it shows: demo trials with each edge's live
record against its floor; promotable edges with the config block to paste;
pinned promotions and any pin that cannot currently trade; every recorded fill
attributed to the strategy and variant that placed it, plus a per-variant
roll-up of what the broker actually did; the graded reason history with what
each proposal built on; and the configuration audit trail. It is read-only —
`POST` returns 405 — and nothing on it can change a lifecycle.

The CLI answers the same questions without a browser:

```bash
python research.py edge promotable          # what has earned a promotion
python research.py edge trials --dry-run    # what a trial review would do
python research.py edge paper --deployed    # how each deployed edge is doing
python research.py factory report           # the full discovery narrative
```

Three read-only views answer three different questions, and none can change a
lifecycle state:

- **What is research doing?** `research.py factory report` renders the full
  discovery narrative — every slot, every hypothesis it has held, who proposed
  each one and on what rationale, every variant with the reason it was tried,
  its performance and the named gates it missed, why anything was retired and
  after how many variants, and the graded reason history: what was tried, why,
  and what the gates then said. `--format markdown` produces a shareable
  document. Each cycle archives it under `research/results/factory/`, so the
  dashboard lists it without anyone running a command.
- **What evidence was an edge promoted on?** `research.py edge status` and the
  dashboard's "Proved edges" table.
- **What has it done since?** `research.py edge paper` and the dashboard's
  "Live paper results by edge" table — trades, sessions, total and mean R, win
  rate, net P&L, the rolling-R demotion guard, and the sequential drift
  statistic against its validated held-out distribution.

## Quick start

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
./.venv/bin/python main.py check --offline
./.venv/bin/python main.py check
./.venv/bin/python research.py edge status
./.venv/bin/python research.py factory status
./.venv/bin/python main.py run
```

Set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`, and the
selected `ALPACA_DATA_FEED`/`ALPACA_OPTIONS_FEED` in `.env`. `check` is the
authenticated preflight by default; `check --offline` validates local
configuration only and is not a trading preflight. Unit tests use fakes and do
not need credentials. The checked config enables the bounded research
discovery and replacement adapters with model `gpt-5`; they read optional
provider credentials only from the separate
`ALPACA_RESEARCH_LLM_SECRETS_FILE`. Missing or invalid LLM output leaves a
pending replacement or falls back to deterministic discovery; it never retires
a family or authorizes trading. Runtime
decision LLM use remains disabled (`llm.enabled: false`). The default stock
feed is IEX. Long options remain subject to liquidity and contract checks. On a
Research studies only the vehicle the configured execution profile can trade
(`python research.py vehicles`); `ALPACA_RESEARCH_VEHICLES=all` runs both lanes
deliberately. Seed months of history in one command instead of waiting for the
recorder to accumulate it:

```bash
./.venv/bin/python deploy/backfill.py --days 180
```

Backfill writes the same partitions, `as_of` semantics, and index the recorder
writes, so no gate is weakened; options are not backfilled and still need
recorded sessions. Clearing the 100-trade held-out floor depends on universe
width as much as history — four symbols over 120 sessions yields roughly 84
held-out trades — so widen `universe.symbols` too. On a
fresh ledger, `run` starts safely but will not submit entries: first collect an
initial corpus, let the strategy factory pass its backtest, and then collect a
strictly later unseen tail for shadow validation and automatic champion
selection. This delay is an intentional evidence gate, not a startup error.

For Docker or an Azure VM, follow [SETUP.md](SETUP.md). For backups,
reconciliation, session-close checks, and recovery, follow
[OPERATIONS.md](OPERATIONS.md). A paused-runtime recovery uses the
authenticated, flat-only `main.py resume` command described there;
[AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) covers the Azure-specific work that
precedes SETUP.md: attaching and mounting a managed data disk, pointing Docker
at it so the edge ledger is durable, and closing the network security group.

## Verification

```bash
./.venv/bin/python -m compileall -q agent deploy main.py research.py
./.venv/bin/python -m unittest discover -v
docker compose config
docker compose --profile research config
docker compose build
```

Treat a failing safety, session, mode-boundary, or no-overnight test as a
release blocker. Root discovery includes the deployment and research suites.

## What is deliberately not included

There is no overnight strategy, withdrawal flow, default unattended live
deployment, multi-leg/naked option path, crypto path, or assertion that any
generated signal has positive expectancy. Any live launch requires a separate
reviewed config/runtime scope, provider guard, named edge, risk policy,
session-close behaviour, tests, and operational controls.
