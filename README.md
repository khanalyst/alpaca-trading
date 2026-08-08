# Alpaca intraday paper-trading agent

This repository is a research and execution skeleton for US equities, ETFs,
and listed options through Alpaca. The intended workflow is paper-only:
collect regular-session data, calculate an initial balance range (IBR), test a
breakout hypothesis, and record every decision and fill for review. Options
are single-leg long calls or puts (buy-to-open, sell-to-close); multi-leg and
naked/short option structures are unsupported. It is not a live-trading system
and makes no claim of a profitable edge.

The order path has one alpha family, IBR. A trader process selects one
execution profile (`shares` or `options`) in `strategy.execution_mode`; do not
mix profiles in one process. The market
calendar is America/New_York (NYSE regular session). New entries are rejected
outside the regular session, positions are force-closed before the close, and
overnight positions are not supported.

## Safety boundary

- `paper: true` and Alpaca's paper endpoint are the documented defaults.
- Live endpoints are disabled unless a future, explicit configuration and
  code review enables them. Do not set a live endpoint for this checkout.
- Keep API keys in `.env` or a host secret; never commit them. Use trading
  permissions only and disable withdrawals.
- Research, recorder, trader, and dashboard state is isolated in named
  volumes. Runtime entries require a vehicle-local `validated` or `champion`
  edge record in the SQLite ledger; research cannot place orders or mutate
  paper state.

The source and tests are the behavioural authority. [SETUP.md](SETUP.md) is
the installation authority, [OPERATIONS.md](OPERATIONS.md) is the runbook, and
the files under `research/` describe evidence collection. Prose must not be
read as a performance claim.

## Architecture

`agent/alpaca_provider.py` is the small boundary around `alpaca-py`. It
normalizes account, asset, quote/bar, calendar, option-chain, order, and trade
update data for the rest of the application. `agent/alpaca_session.py` owns
the NYSE calendar and session policy. `agent/contracts/ibr.py` builds the
opening range and evaluates one breakout per symbol/day. Risk and execution
profiles enforce sizing, single-leg long-option checks, idempotent client
order IDs, and end-of-day flattening.

The lean deployment topology is:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Public/paper Alpaca bars, quotes, and option snapshots | `runtime-data` |
| `trader` | One paper decision loop in its configured execution profile; no overnight book | `runtime-data` |
| `research` | Scheduled replay, evidence, and reports | `runtime-data`, research volumes |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |

The research service is an opt-in Compose profile, but a fresh deployment must
run it long enough to produce a champion before the trader can open risk. It
consumes the recorder's append-only mixed
`runtime/research/recorded/market.csv` by default (bars, quotes, and option
snapshots), or an explicit normalized JSONL path from
`ALPACA_RESEARCH_DATASET`. Its edge-lab lifecycle is kept in the SQLite ledger
at `runtime/research/edge_lab.sqlite3`; the dashboard only observes ledger
status and the paper journal.

## Quick start

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
./.venv/bin/python main.py check --offline
./.venv/bin/python main.py check
./.venv/bin/python research.py edge status
./.venv/bin/python main.py run
```

Set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`, and the
selected `ALPACA_DATA_FEED`/`ALPACA_OPTIONS_FEED` in `.env`. `check` is the
authenticated paper preflight by default; use `check --offline` only for local
configuration validation. Unit tests use fakes and do not need credentials.
The deterministic strategy is the default and LLM use is disabled by default;
setting `llm.enabled: true` is an explicit, credentialed opt-in. The default
stock feed is IEX. Long options remain subject to liquidity and contract
checks. On a fresh ledger, `run` starts safely but will not submit entries:
first collect an initial corpus, let the research cycle pass its backtest, and
then collect a strictly later unseen tail for shadow validation and automatic
champion selection. This delay is an intentional evidence gate, not a startup
error.

For Docker or an Azure VM, follow [SETUP.md](SETUP.md). For backups,
reconciliation, session-close checks, and recovery, follow
[OPERATIONS.md](OPERATIONS.md). `AZURE_DEPLOYMENT.md` is a compatibility
pointer to those two authorities.

## Verification

```bash
./.venv/bin/python -m compileall -q agent deploy main.py research.py
./.venv/bin/python -m unittest discover -v
docker compose config
docker compose --profile research config
docker compose build
```

Treat a failing safety, session, paper-mode, or no-overnight test as a release
blocker. Root discovery includes the deployment and research suites.

## What is deliberately not included

There is no overnight strategy, withdrawal flow, unattended live deployment,
multi-leg/naked option path, or assertion that an IBR signal has positive
expectancy. Any future live work requires a separate review of the provider
guard, risk policy, session-close behaviour, tests, and operational controls.
