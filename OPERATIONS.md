# Operations runbook

This runbook covers the paper-only Alpaca runtime. It assumes the checkout is
`/opt/alpaca-agent-trading`, the secret is outside Git, and one trader process
owns the paper account. The order path is intraday: the market calendar and
IBR strategy reject entries outside the regular session and flatten before the
NYSE close. Overnight positions are an incident, not a supported state.

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

The check must report the paper endpoint, the configured stock/options feed,
the current market clock, and a reachable credentials path. A live endpoint or
missing key is a failed check. Verify the recorder and research timestamps are
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
3. maintain stops and defined-risk option exits while the session is open;
4. flatten all shares and option legs before the configured close cutoff; and
5. reconcile broker positions and orders after flattening.

If the process is restarted during a session, reconcile first. Never infer a
flat account from a local journal alone. An option spread is a single risk
unit: close or cancel every leg and confirm no residual contract remains.

## Paper-only guard

`ALPACA_PAPER=true` is mandatory for this deployment. The provider refuses a
non-paper session unless a separate explicit live-enable control exists; no
such control is shipped or supported. If a secret, endpoint, or config change
would select live trading, stop the trader, restore the paper setting, rotate
the affected key, and record the incident. Do not attempt to work around the
guard.

## Start, stop, and pause

```bash
docker compose up -d recorder trader dashboard
docker compose --profile research up -d research  # only with a dataset
docker compose stop trader                 # pause new decisions safely
docker compose start trader
docker compose down                         # preserves named volumes
```

Before stopping during a session, use the application stop/flatten command if
available and confirm Alpaca reports no open positions or orders. If the
trader is unhealthy, stop it, cancel open orders, flatten manually in the
Alpaca paper dashboard/API, and leave it stopped until reconciliation passes.

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
- `findings/` and any generated reports; and
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
checkout, run compile/tests and `main.py check`, then reconcile the paper
account before starting the trader.

## Updating

Review the diff and run the focused checks before restarting:

```bash
git fetch origin
git diff --check origin/main...HEAD
./.venv/bin/python -m compileall -q agent deploy main.py research.py
./.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
docker compose config
docker compose build
docker compose up -d --remove-orphans
docker compose exec -T trader python main.py check
```

Do not update by replacing a running journal or starting a second trader.
Rollback means restoring the prior reviewed Git revision and image, then
re-running the paper check and reconciliation. Record the revision, operator,
time, and check output in the deployment log.

## Research and evidence

Research is read-only with respect to broker authority. It may replay IBR
signals, score share and defined-risk option profiles, and write reports. A
positive backtest or paper result is not a promotion. Keep data provenance,
session date, feed, contract identity, and costs with each result. Do not
combine regular-session evidence with pre/post-market or overnight data.

The dashboard is read-only and localhost-bound. It is an observation aid, not
an execution console. Protect SSH and host credentials using your normal
organization controls.

## Common symptoms

| Symptom | Action |
| --- | --- |
| `paper endpoint required` | Restore `ALPACA_PAPER=true`, check the endpoint, and restart only after `main.py check`. |
| `market closed` or stale calendar | Do not force an entry; refresh the calendar and wait for the next regular session. |
| Missing bars/quotes | Check the selected Alpaca feed entitlement and recorder health; mark the interval unavailable. |
| Option chain lacks a valid spread | Skip the trade. Never substitute an uncovered option leg. |
| Position remains after close cutoff | Stop new entries, cancel orders, flatten manually through Alpaca, and keep the trader paused. |
| Local/broker state differs | Broker state wins; preserve logs and reconcile before resuming. |
| Research reports disagree | Preserve both artifacts and compare feed/session/contract provenance; do not merge silently. |
