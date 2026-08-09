# Operations runbook

This runbook covers the default paper Alpaca runtime and the controls required
for an explicitly approved live scope. It assumes the checkout is
`/opt/alpaca-agent-trading`, the secret is outside Git, and one trader process
owns one account with one execution profile (`shares` or `options`). The
supported universe is US-listed equities/ETFs and listed OCC options only. The
order path is intraday: day-only orders, regular NYSE session entries, startup
cleanup, and force-flat before the close. Overnight positions are an incident,
not a supported state.

## Daily checks

```bash
cd /opt/alpaca-agent-trading
docker compose ps
docker compose logs --tail=100 trader
docker compose logs --tail=100 recorder
docker compose exec -T trader python main.py check
docker compose exec -T trader python main.py status
docker compose --profile research ps research
```

`main.py check` is authenticated by default and must report the configured
mode endpoint, credentials, stock/options feeds, current market clock, and
account state. Use `--offline` only for local configuration checks; it does not
authenticate and is not a trading preflight. A wrong endpoint, missing key, or
inactive account is a failed check. Verify recorder/research timestamps are
fresh, the dashboard is healthy, and the trader has exactly one replica.

For systemd hosts:

```bash
sudo systemctl status alpaca-recorder alpaca-trader alpaca-research.timer
sudo journalctl -u alpaca-trader -n 100 --no-pager
sudo journalctl -u alpaca-recorder -n 100 --no-pager
sudo journalctl -u alpaca-research -n 100 --no-pager
```

## Session controls

The calendar returned by Alpaca is authoritative for holidays and early
closes. The trader must:

1. complete the 09:30–09:45 America/New_York IBR window before evaluating a
   breakout;
2. reject new entries near the close and whenever the clock/calendar is stale;
3. submit only `time_in_force: day` orders and maintain stops and long-option
   exits while the session is open;
4. at startup, cancel working orders and flatten residual shares/contracts;
5. flatten all shares and long-option contracts before the configured close
   cutoff; and
6. reconcile broker positions and orders after flattening.

If the process is restarted during a session, reconcile first. Never infer a
flat account from a local journal alone. A long option contract is one risk
unit: close or cancel it and confirm no residual contract remains.

## Mode guard and live preflight

Paper is the default and the shipped Compose/systemd lanes set
`ALPACA_PAPER=true`. A live process is allowed only with `mode: live`,
`broker.paper: false`, `broker.allow_live: true`, and
`ALPACA_LIVE_ENABLE=true`; `ALPACA_PAPER=true` must not be set. It must use
`strategy.selection_mode: specific` and one exact named validated/champion
`strategy.variant_id`. The runtime pins that candidate/configuration and does
not auto-switch. Keep live credentials, config, and
`ALPACA_AGENT_RUNTIME_ROOT` separate from paper; never run both against shared
state or a shared account.

The authenticated live preflight requires the account to report
`pattern_day_trader=true` in addition to endpoint, identity, active status,
equity, asset, clock, and calendar checks. A missing or false PDT flag blocks
startup. If any mode guard or preflight fails, stop the process, preserve the
evidence, and correct the scoped configuration before retrying.

## Start, stop, and pause

```bash
docker compose up -d recorder trader dashboard
docker compose --profile research up -d research  # uses recorder output by default
docker compose stop trader                 # pause new decisions safely
docker compose start trader
docker compose down                         # preserves named volumes
```

Before stopping during a session, run
`docker compose exec -T trader python main.py flatten --reason operator` and
confirm Alpaca reports no open positions or orders. A non-zero flatten result
means residual positions remain and requires manual reconciliation. If the
trader is unhealthy, stop it, cancel open orders, flatten manually in the
scoped Alpaca account, and leave it stopped until reconciliation passes.

To recover an operator-paused runtime, use this sequence:

1. Stop the trader and confirm the process has released its run lock.
2. Run the authenticated `check` and `status` commands for the same mode and
   account.
3. If `status` shows positions, or broker inspection shows working orders, run
   `flatten` and wait for a later status/reconciliation that proves flat.
4. Run `resume`. It performs one authenticated reconciliation plus a final
   read-only broker confirmation, and clears only the operator pause when
   broker and durable state are flat and terminal.
5. Start the trader again.

```bash
docker compose stop trader
docker compose run --rm trader python main.py check
docker compose run --rm trader python main.py status
# only when positions or working orders remain:
docker compose run --rm trader python main.py flatten --reason operator
docker compose run --rm trader python main.py resume
docker compose start trader
```

`resume` is authenticated and flat-only. It never cancels, submits, or
flattens orders, and it rejects killed or daily-risk-stopped runtimes. Do not
manually edit `state.json` or the SQLite journal to force a resume.

## Reconciliation and incident response

Capture these artifacts before changing state:

```bash
docker compose logs --no-color --since=2h trader > /tmp/alpaca-trader.log
docker compose exec -T trader python main.py status > /tmp/alpaca-status.txt
docker compose exec -T trader python - <<'PY'
from agent.alpaca_provider import AlpacaProvider
from main import load_cfg
cfg = load_cfg("config.yaml")
p = AlpacaProvider(cfg)
print("paper", p.paper)
print("positions", p.positions())
print("orders", p.orders())
PY
```

Treat any mismatch between local state and Alpaca as broker truth: cancel
working orders, flatten positions, preserve logs, and keep the trader paused.
Do not delete or edit the SQLite journal to make a reconciliation pass.

## Backups and recovery

Back up the named volumes or these directories to a different device/off-host
destination:

- `runtime/` (journal, state, health, and recorder receipts);
- `research/cache/` and `research/results/`;
- generated edge proof reports under `research/results/edges/`; and
- the exact Git revision and a redacted configuration snapshot.

Example export (run only when the destination has been verified):

```bash
mkdir -p /srv/alpaca-agent-backup
docker run --rm -v alpaca-agent-trading_runtime-data:/src:ro \
  -v /srv/alpaca-agent-backup:/dst alpine \
  sh -c 'tar -C /src -czf /dst/runtime-$(date -u +%Y%m%dT%H%M%SZ).tgz .'
```

Verify the archive can be listed and copied to another host. A bind mount on
the same VM does not prove recoverability from VM loss. Restore into a new
checkout, run compile/tests and `main.py check`, then reconcile the scoped
account before starting the trader.

## Updating

Review the diff and run the focused checks before restarting:

```bash
git fetch origin
git diff --check origin/main...HEAD
./.venv/bin/python -m compileall -q agent deploy main.py research.py
./.venv/bin/python -m unittest discover -v
docker compose config
docker compose build
docker compose up -d --remove-orphans
docker compose exec -T trader python main.py check
```

Do not update by replacing a running journal or starting a second trader.
Rollback means restoring the prior reviewed Git revision and image, then
re-running the mode-appropriate check and reconciliation. Record the revision,
operator, mode, time, and check output in the deployment log.

## Research and evidence

Research is read-only with respect to broker authority. It discovers the
recorder's mixed bars/quotes/options dataset at
`runtime/research/recorded/market.csv` by default (or uses
`ALPACA_RESEARCH_DATASET`), runs the seven-family autonomous strategy factory
plus the explicit IBR baseline, scores shares and single-leg long-option
vehicles separately, and writes evidence. Each variant has its own simulated
cash/equity account; default capacity is seven parallel strategy workers and
four variants per strategy. Paper `selection_mode: all_proved` keeps one best
proven variant per independent family under one global risk book. The edge
ledger is
initialized at `runtime/research/edge_lab.sqlite3`; inspect it with
`python research.py edge status`. The autonomous lifecycle requires an initial
corpus backtest followed by later unseen shadow evidence. Validation requires
fit and held-out structural floors, matched controls, placebo/falsification,
family-level FDR, and a durable verified gate. Underpowered data is not a
failure. Retirement waits until all intended variants are adequately tested
and fail, and a valid bounded LLM replacement is registered first when that
lane is enabled. Paper outcomes are appended for forward monitoring and may
demote a champion. Passing gates advance validated/champion state without
manual promotion. Manual `edge promote` remains an audited control subject to
lifecycle/evidence rules. Backward rollback is rejected; explicit demotion is
the operator safety action. Good edges emit deterministic,
content-addressed edge proof reports and may send an optional HTTPS webhook. Keep data
provenance, session date, feed, contract identity, and costs with each result.
Do not combine regular-session evidence with pre/post-market or overnight data.

Inspect autonomous lineage and isolated-account counts with
`python research.py factory status`. Tune bounded capacity with
`ALPACA_FACTORY_STRATEGIES`, `ALPACA_FACTORY_VARIANTS`, and
`ALPACA_FACTORY_WORKERS`; hard validation caps these at 16, 8, and 16.

The paper journal is the source for realized performance summaries:
`python report.py runtime/paper/journal.db --json`. The dashboard reads this
journal and edge ledger in read-only mode.

The scheduler records one of four terminal research statuses:
`completed` (proof produced), `completed_no_edge` (valid run, no eligible
edge), `no_data` (input unavailable/empty), or `failed` (validation or job
failure). Treat `completed_no_edge` and `no_data` as distinct from scheduler
failure; neither permits bypassing the runtime edge gate.

The dashboard is read-only and localhost-bound. It is an observation aid, not
an execution console. Protect SSH and host credentials using your normal
organization controls.

## Common symptoms

| Symptom | Action |
| --- | --- |
| mode/endpoint guard failure | Restore the scoped paper or live guard, check the endpoint, and restart only after authenticated `main.py check`; live also needs `pattern_day_trader=true`. |
| `market closed` or stale calendar | Do not force an entry; refresh the calendar and wait for the next regular session. |
| Missing bars/quotes | Check the selected Alpaca feed entitlement and recorder health; mark the interval unavailable. |
| Option chain lacks a valid single-leg long contract | Skip the trade. Never substitute a multi-leg, uncovered, or short option. |
| Position remains after close cutoff or startup cleanup | Stop new entries, cancel orders, flatten manually through the scoped Alpaca account, and keep the trader paused. |
| Local/broker state differs | Broker state wins; preserve logs and reconcile before resuming. |
| Research reports disagree | Preserve both artifacts and compare feed/session/contract provenance; do not merge silently. |
