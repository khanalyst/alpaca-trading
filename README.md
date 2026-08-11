# Alpaca intraday trading agent

This repository is a research and execution skeleton for US equities, ETFs,
and listed OCC options through Alpaca. Paper mode is the default: collect
regular-session data, evaluate multiple strategy hypotheses and their variants,
and record every decision and fill for review. The supported universe is
US-listed equities/ETFs and listed OCC options only; crypto is rejected.
Options are single-leg long calls or puts
(buy-to-open, sell-to-close); multi-leg and naked/short option structures are
unsupported. No performance claim is made.

The research factory starts with seven independent rule families and four
isolated simulated-account variants per family. It evaluates independent
families in parallel, diagnoses failures from chronological fit data, mutates
bounded data-only rule specifications, and judges them on untouched held-out
data. A rule specification carries `max_hold_bars`, so every strategy has a
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
never contain executable code. `agent/contracts/ibr.py` remains the explicit
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
status, latest re-verified passing edges, edge proof reports, and the execution
journal.

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
replacement adapter with model `gpt-5`; it reads optional provider credentials
only from the separate `ALPACA_RESEARCH_LLM_SECRETS_FILE`. Missing or invalid
LLM output leaves a pending replacement and does not retire a family. Runtime
decision LLM use remains disabled (`llm.enabled: false`). The default stock
feed is IEX. Long options remain subject to liquidity and contract checks. On a
fresh ledger, `run` starts safely but will not submit entries: first collect an
initial corpus, let the strategy factory pass its backtest, and then collect a
strictly later unseen tail for shadow validation and automatic champion
selection. This delay is an intentional evidence gate, not a startup error.

For Docker or an Azure VM, follow [SETUP.md](SETUP.md). For backups,
reconciliation, session-close checks, and recovery, follow
[OPERATIONS.md](OPERATIONS.md). A paused-runtime recovery uses the
authenticated, flat-only `main.py resume` command described there;
`AZURE_DEPLOYMENT.md` is a compatibility pointer to those two authorities.

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
