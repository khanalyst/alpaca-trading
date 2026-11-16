# Operations runbook

This runbook covers the paper deployment at /opt/alpaca-agent-trading. One
trader supervisor owns one mode/account runtime. Entries are regular-session
only, orders are day orders, and exposure is forced flat before close.

## Routine checks

    docker compose ps
    docker compose exec -T trader python main.py check
    docker compose exec -T trader python main.py status
    docker compose logs --tail=100 trader recorder watchdog research shadow

Authenticated check validates mode/endpoint, credentials/account status, broker
clock/calendar, feeds, and runtime readiness. check --offline only validates
local configuration. inspect-deployment performs a read-only check of whether
an existing paper deployment is paused and flat:

    docker compose exec -T trader python main.py inspect-deployment

For systemd use systemctl status on alpaca-recorder, alpaca-trader,
alpaca-watchdog, and alpaca-research.timer; use journalctl -u alpaca-trader
and journalctl -u alpaca-watchdog for logs. The dashboard at
http://127.0.0.1:8080 is read-only; broker state is authoritative.

## Session and execution rules

The broker clock and exact calendar are authoritative; missing or stale
metadata fails closed. Entries are regular-session only. The shipped strategy
configuration sets a 15-minute range; the selected rule's own causal maturity
and signal windows also apply. Its latest-entry time is 15:00 New York;
the session policy independently rejects entries within five minutes
of the exact close and forces flat ten minutes before it. Both entry gates
apply, including on early-close sessions. Orders use time_in_force=day.
Startup and every cycle reconcile durable state with broker positions/orders
before entries.
Risk checks enforce daily loss, open risk, concurrent positions, notional,
gross exposure, freshness, spread, and stressed-cost limits.

Stops, targets, and bounded hold deadlines are persisted and monitored.
Options are a separate explicit paper profile: a long-option take-profit may
rest as sell_to_close while the stop is software-polled. The shipped live
policy rejects options.

## Start, stop, flatten

    docker compose up -d
    docker compose stop trader

An orderly SIGTERM/SIGINT shutdown persists the pause before releasing the
runtime lock. A supervisor restart does not automatically resume entries.
Verify broker positions and working orders rather than trusting local state.

The shutdown path records a `shutdown_requested` journal event with the signal,
receiving code path, receipt time, PID/PPID, run ID, and mode. Correlate it with
the supervisor's `supervisor_child_stopped` event, signal metadata, and host
logs. The supervisor records its stop/timeout trigger after reaping and any
required recovery. Signal receipt fields identify the receiver; they do not
identify the external signal sender. Force-flat and idle
cycles must continue advancing their heartbeat so the supervisor does not
mistake a safe, active process for an unresponsive one.

For deliberate cleanup:

    docker compose run --rm --no-deps trader python main.py flatten --reason operator

flatten verifies residual broker state. A non-zero result means flatness was not
proven; leave the trader stopped and reconcile in Alpaca. Do not send manual
orders while another owner holds the run lock.

## Pause and authenticated resume

Two state fields are independent:

1. runtime/<mode>/state.json has operator_pause. When true,
   trader_supervisor.wait_until_resumed parks without starting the child, writes
   a paused heartbeat, and performs no broker I/O.
2. The same file may contain paper_trial.state=running. That is the
   non-authorizing diagnostic lifecycle, not permission to start the trader.

Do not edit either field or rebind a frozen trial to bypass a block.

Safe recovery:

    docker compose stop trader
    docker compose run --rm --no-deps trader python main.py check
    docker compose run --rm --no-deps trader python main.py status
    docker compose run --rm --no-deps trader python main.py inspect-deployment

If broker or status shows positions/working orders, flatten and wait for an
authenticated reconciliation:

    docker compose run --rm --no-deps trader python main.py flatten --reason operator
    docker compose run --rm --no-deps trader python main.py resume
    docker compose start trader

resume requires PAUSED state with operator_pause=true, no kill reason/daily
stop, empty local trades/protection, terminal local orders, authenticated
preflight, no broker positions, no non-terminal broker orders, a final flat
broker confirmation, and the unchanged account fingerprint. The preflight and
entry paths reject changes to a running frozen paper trial. An evidence-empty
incumbent can first be cancelled with `deploy/cancel_paper_trial.py`, using its
frozen profile and exact trial/identity confirmations. Preserve that cancellation
audit. A different trial can then activate only after the incumbent is terminal
and the local and authenticated broker books are flat; reconciliation journals
the terminal snapshot before installing the replacement. `resume` applies those
same activation checks and authorizes the operator pause separately. It
never cancels, submits, or flattens. Preserve failure reasons; do not clear
flags, rewrite fingerprints, or rebind an existing frozen trial.

## Paused and degraded recovery limits

- A live but not-entry-ready child still reconciles and invokes not-ready
  force-flat at the cutoff. ExecutionLifecycle monitors stop/target/max-hold/
  force-flat and close intents. Close or durability failure persists pause and
  reports residual risk.
- An unresponsive child is signalled only in its own process group. The
  supervisor waits for confirmed exit (15 seconds by default), then invokes
  authenticated watchdog recovery. Recovery locks, binds account identity,
  persists pause, may flatten, and never relaunches entries.
- The watchdog acts only after acquiring that lock. watching means another owner
  holds it; acted means flattening was confirmed; degraded/residual_risk means
  flatness was not proven. Broker/network outage prevents confirmation.
- A supervisor parked on operator pause writes heartbeats without a child or
  broker I/O; the watchdog is the separate recovery path.
- Killed state is not auto-cleared, and a wedged lock owner prevents watchdog
  action until it exits or is reaped.

    docker compose exec -T watchdog python deploy/health.py watchdog

## Research and promotion

Research and shadow have no broker-order authority. The main research/proof
pipeline can produce authorizing evidence in its ledger; a diagnostic
candidate is not executable. The trader resolves a compatible validated or
champion record only when its latest shadow proof has the required
parity-matched ingestion marker; a paper trial is explicitly non-authorizing.

    docker compose exec -T research python research.py edge status
    docker compose exec -T research python research.py edge promotable --vehicle equity
    docker compose exec -T research python research.py factory status

The session-edge study is diagnostic and does not replace main proof/runtime
controls; see [studies/session_edges/README.md](studies/session_edges/README.md).

## Backups and evidence

Before an update or recovery, record the checkout/image identity, selected
configuration, Compose overrides, and authenticated deployment inspection.
Preserve the mode-scoped state/journal, recorder corpus and sidecars, edge
ledger, shadow WAL/acceptance reports, and research cache/results. Keep
credentials in their protected store rather than in portable research backups.

Use SQLite's backup API for a consistent live database snapshot, or stop every
writer before copying its database and sidecars. Copying only a live `.sqlite3`
or `journal.db` file can omit committed WAL data. Verify backup hashes and
integrity, copy the backup off-host, and test restoration in an isolated
location. Never restore over an active runtime to clear a pause or identity
blocker. The external-backup Compose overlay supplies a mount only; the current
research cycle does not perform the backup automatically.

## Live guardrails

Live is disabled by shipped files. Source guards require reviewed mode/endpoint
and environment agreement, one exact proved operator-selected edge,
authenticated preflight, runtime llm.enabled=false, and separate live
state/credentials/account. Stop and preserve evidence if any guard fails.

## Incident checklist

1. Stop the trader if ownership or broker state is unclear.
2. Query Alpaca positions and open orders in the configured account.
3. Run status, inspect-deployment, and the watchdog probe.
4. Use scoped flatten and verify broker flatness if exposure exists.
5. Keep operator_pause=true until resume authorizes a matching book.
6. Preserve state/logs; do not rewrite evidence.

See [SETUP.md](SETUP.md) and [ARCHITECTURE.md](ARCHITECTURE.md).
