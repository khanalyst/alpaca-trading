# Alpaca intraday paper-trading agent

This repository is a research and execution skeleton for US equities, ETFs,
and listed options through Alpaca. The intended workflow is paper-only:
collect regular-session data, calculate an initial balance range (IBR), test a
breakout hypothesis, and record every decision and fill for review. It is not
a live-trading system and makes no claim of a profitable edge.

The order path has one alpha family, IBR. Shares/ETF trades and defined-risk
option structures are execution profiles of that same signal. The market
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
  volumes. Research results do not authorize an order or change configuration.

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
profiles enforce sizing, defined-risk option structures, idempotent client
order IDs, and end-of-day flattening.

The lean deployment topology is:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Public/paper market-data snapshots and bars | `runtime-data` |
| `trader` | One paper decision loop; no overnight book | `runtime-data` |
| `research` | Scheduled replay, evidence, and reports | `runtime-data`, research volumes |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |

## Quick start

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
./.venv/bin/python main.py check
./.venv/bin/python main.py run
```

Set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`, and the
selected `ALPACA_DATA_FEED`/`ALPACA_OPTIONS_FEED` in `.env`. A credentialed
check is required before market-data or order calls; unit tests use fakes and
do not need credentials. The default stock feed is IEX. Options are used only
for defined-risk, intraday structures and remain subject to liquidity and
contract checks.

For Docker or an Azure VM, follow [SETUP.md](SETUP.md). For backups,
reconciliation, session-close checks, and recovery, follow
[OPERATIONS.md](OPERATIONS.md). `AZURE_DEPLOYMENT.md` is a compatibility
pointer to those two authorities.

## Verification

```bash
./.venv/bin/python -m compileall -q agent deploy main.py research.py
./.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
docker compose config
docker compose --profile research config
docker compose build
```

Some research tests cover historical migration fixtures and may be retired as
the stock/ETF/options model is completed. Treat a failing safety, session,
paper-mode, or no-overnight test as a release blocker.

## What is deliberately not included

There is no overnight strategy, withdrawal flow, unattended live deployment,
or assertion that an IBR signal has positive expectancy. Any future live work
requires a separate review of the provider guard, risk policy, session-close
behaviour, tests, and operational controls.
