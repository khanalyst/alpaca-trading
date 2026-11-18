# Deployment guide

[SETUP.md](../SETUP.md) is the end-to-end installation guide and
[OPERATIONS.md](../OPERATIONS.md) covers routine operation and recovery. This
file documents the current Compose topology, update helper, and operator gates
that must remain true on every paper deployment.

## Supported defaults

The checked-in Compose/config pair is paper-only:

- `mode: paper`, `broker.paper: true`, and `broker.allow_live: false`;
- `ALPACA_PAPER=true` and `ALPACA_LIVE_ENABLE=false` in workload services;
- the equity lane uses the configured IEX feed and `shares` execution profile;
- `ALPACA_OPTIONS_FEED=indicative` is a non-executable placeholder;
- research runs the equity vehicle by default and the paper trial is disabled;
- the research provider secret is a separate required Compose secret.

Do not turn a deployment live by changing one environment variable. A live
profile is a separate reviewed authorization scope, and this guide does not
provide a live rollout procedure.

## Compose services

[`../compose.yaml`](../compose.yaml) defines one least-privilege image and
these services:

| Service | Responsibility | Writable state | Broker authority |
| --- | --- | --- | --- |
| `recorder` | Fetches configured Alpaca bars/quotes and writes session partitions. | `runtime-data` | Data access only |
| `trader` | Runs the paper execution loop through `deploy/trader_supervisor.py`. | `runtime-data`, `research-cache` | Paper orders after gates |
| `watchdog` | Watches trader heartbeat and performs cancel/flatten recovery only. | `runtime-data` | Flatten/cancel only |
| `research` | Runs the scheduled factory/replay and shadow-ingestion cycle. | `runtime-data`, `research-cache`, `research-results` | None |
| `shadow` | Runs broker-free incremental replay and writes its isolated WAL. | `shadow-data` | None |
| `dashboard` | Exposes read-only health/report views on localhost. | None | None |
| `shadow-init` | Repairs `shadow-data` ownership before `shadow` starts. | `shadow-data` | None; no network/credentials |

All workload services run as UID/GID `10001`, drop capabilities, and use a
read-only root filesystem. Dashboard mounts are read-only and it receives no
broker credentials. Named volumes survive ordinary `docker compose down`;
`docker compose down -v` removes them and is a destructive data operation.

The watchdog uses a separate broker session. When the trader heartbeat is
older than `ALPACA_WATCHDOG_MAX_HEARTBEAT_AGE` (Compose default: 300 seconds)
and broker exposure remains, it takes the mode-scoped run lock, authenticates
the expected account, cancels protective orders, and flattens. It never submits
an entry. The trader supervisor also terminates only its own unresponsive child,
confirms that it exited, and invokes locked recovery; it does not relaunch
entries automatically.

## Prepare and bootstrap Compose

Run these commands from a reviewed, clean checkout on the VM. The first start
is deliberately data-only so a new deployment can collect source evidence
before it has a validated edge:

```bash
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
test -r "$ALPACA_AGENT_SECRET_FILE"
test -r "$ALPACA_RESEARCH_LLM_SECRET_FILE"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
export ALPACA_DEPLOYMENT_COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps trader python main.py check --offline
docker compose up -d recorder research shadow dashboard
docker compose ps
```

`agent.env` contains only Alpaca paper credentials and feed settings. The
research file contains only the selected provider key. Neither file belongs in
Git. The offline check never uses a broker; the default authenticated check
queries the account, clock, exact calendar, and configured feeds.
`ALPACA_DEPLOYMENT_COMMIT` must remain the full reviewed `HEAD` commit for the
build and every Compose restart. An omitted/unknown or mismatched identity can
block session acceptance and cross-service parity. `shadow-init` starts
automatically as the shadow service dependency.

Run the authenticated check against this data-only bootstrap:

```bash
docker compose run --rm --no-deps trader python main.py check
```

On a fresh default install this normally exits nonzero with `edge_required` and
`edge_ready: false` because no validated edge exists yet. That is an expected
readiness refusal, not a successful check. Keep `trader` and `watchdog` stopped
while recorder/research/shadow collect data and produce the source-gated proof
evidence. Do not bypass or relabel the missing-edge refusal.

Research, shadow, recorder, and dashboard health are separate dimensions. A
running scheduler or container is not evidence that the corpus is ready, an
edge is validated, or the trader may enter. Start the order-capable services
only after an authenticated check succeeds with a resolved edge and the
account, state, and deployment-identity gates are satisfied:

```bash
docker compose up -d trader watchdog
docker compose ps
docker compose exec -T trader python main.py check
docker compose run --rm --no-deps trader python main.py inspect-deployment
```

`inspect-deployment` is a read-only paused-and-flat proof. A fresh or running
runtime may return nonzero because it is not explicitly paused and flat; that
describes inspection eligibility, not a Docker/startup failure.

## Recorder and shadow maintenance

On a full-tick `forward_only` recorder, set
`ALPACA_RECORDER_LATEST_QUOTE_PREFETCH=1` to request the latest equity quotes
before fetching completed bars. Shadow can then evaluate a bar with a quote
already observed by the recorder. The historical quote request still fetches
the ticks in the original window. Quotes keep their provider timestamp and
receipt time, and the configured freshness checks still apply.

Recorder health reports the bytes available above the configured disk reserve
under `storage_headroom`. Its session estimate uses the largest of the last
five closed partition files. A partial recording can understate future usage.

The recorder supports bounded, one-shot maintenance operations:

```bash
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --probe
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --audit
docker compose run --rm --no-deps recorder \
  python deploy/backfill.py --out runtime/research/recorded --days 180
```

`--probe` checks recent configured market-data access without writing corpus
rows. `--audit` scans the full corpus for schema/duplicate-key problems.
`backfill.py` writes historical partitions with historical provenance; those
rows are diagnostic and cannot satisfy forward-shadow or paper authorization.
Add `--quotes` only when the larger historical quote corpus is deliberately
required.

The broker-free shadow service can be run once for a bounded retry or repair,
but only after its long-running writer is stopped. Set the container paths to
the values in `docker compose config`; the values below are only the shipped
defaults and must be changed when `ALPACA_RECORDER_CORPUS_ROOT` or the
deployment’s shadow command paths differ:

```bash
SHADOW_CORPUS=/app/runtime/research/recorded/market.csv
SHADOW_EDGE_DB=/app/runtime/research/edge_lab.sqlite3
SHADOW_DB=/app/shadow/shadow.sqlite3
SHADOW_HEALTH=/app/shadow/health.json
SHADOW_ACCEPTANCE=/app/shadow/session-acceptance

docker compose stop shadow
shadow_status=0
docker compose run --rm --no-deps shadow \
  python deploy/shadow.py --config /app/config.yaml \
  --corpus "$SHADOW_CORPUS" \
  --edge-db "$SHADOW_EDGE_DB" \
  --shadow-db "$SHADOW_DB" \
  --health-file "$SHADOW_HEALTH" \
  --acceptance-root "$SHADOW_ACCEPTANCE" --once || shadow_status=$?
docker compose start shadow
test "$shadow_status" -eq 0
```

An incomplete or mismatched shadow session is quarantined. Correct the source
or identity, then rerun the bounded cycle; do not skip the session or rebind a
frozen identity to make ingestion advance.

`ALPACA_RESEARCH_ACCEPTANCE_ROOT` selects the acceptance directory for both the
shadow writer and research reader. An explicitly enabled paper trial must name
the same directory in `accepted_session_report_root`. Use a new directory for
an audited new trial/evidence epoch; preserve the prior reports. Changing this
path does not migrate old reports or make them count for the new identity.

Authorizing replay uses `--authorizing-event-budget` and
`--authorizing-decision-budget` to bound each poll. Its durable cursor is
scoped to the candidate and evidence/code identity. `backlog_deferred` means
work remains; incomplete sessions cannot become complete replay evidence.
An already capped legacy WAL records a migration baseline before allowing
bounded forward continuation. The ordinary `--max-decisions` storage guard
remains fail-closed. Preserve old rows and inspect progress/capacity rather
than deleting history to clear a limit.

## Deployment updates

Use `update-compose.sh` on the Linux VM:

```bash
cd /opt/alpaca-agent-trading
git pull --ff-only
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
deploy/update-compose.sh
```

The script requires Linux, `compose.yaml`, the credential file, Compose v2,
and a clean Git checkout. It resolves the full `HEAD` object ID, rejects a
mismatched `ALPACA_DEPLOYMENT_COMMIT`, validates Compose, builds the trader,
runs a recorder probe, runs `python main.py check`, starts with
`up -d --no-build --remove-orphans`, checks the running trader, and prints
`docker compose ps`. If `alpaca-trader.service` is active, it refuses to start
a second trader. Stop and reconcile the existing lane instead of bypassing
this check. Unlike the data-only bootstrap, this helper remains strict: a
missing validated edge makes its check fail and aborts the update. Do not label
that refusal as success or bypass it.

An external backup overlay may be selected explicitly:

```bash
export ALPACA_EXTERNAL_BACKUP_PATH=/path/to/reviewed/backup-target
deploy/update-compose.sh
```

Review the target before use. Do not use `down -v` or prune volumes as an
update shortcut.

The overlay only mounts the target into `research` and sets `BACKUP_TARGET`
and `REQUIRE_EXTERNAL_BACKUP`. The current research cycle does not consume
those variables or create a backup. Use a separate verified backup procedure;
enabling this overlay alone does not protect the runtime or its volumes.

## Persisted operator pause

The mode-scoped state file persists `operator_pause`. It may be set by an
operator or by a safety/reconciliation failure. While true,
`trader_supervisor.py` keeps a paused heartbeat and does not launch the trader
child. The normal SIGTERM/SIGINT shutdown path also persists the pause, so a
routine stop, restart, or deployment may leave it set. A restart, deployment,
or paper-trial report does not clear it. There is no automatic resume.

Inspect existing state without initializing the runtime or changing broker
state:

```bash
docker compose run --rm --no-deps trader python main.py inspect-deployment
```

To clear the pause, use the authenticated flat-only command:

```bash
docker compose run --rm --no-deps trader python main.py resume
```

`resume` acquires the run lock, checks the journal and persisted state, runs a
fresh broker preflight, reconciles once, requires no broker positions and no
non-terminal broker orders, confirms the account fingerprint, writes an audit
event and heartbeat, then atomically sets `operator_pause=false`. It never
submits, cancels, or flattens an order. Any failed check leaves the pause set.
The same explicit check is required after a normal stop/restart or deployment
if that shutdown persisted the pause. Use `main.py flatten --reason ...` as a
separate, explicit recovery action when exposure must be closed.

The normal status commands are:

```bash
docker compose run --rm --no-deps trader python main.py status
docker compose run --rm --no-deps trader python main.py check
```

An explicit order-affecting recovery is separate from status inspection. Use it
only after confirming the selected mode/account and understanding that it sends
close requests:

```bash
docker compose run --rm --no-deps trader python main.py flatten --reason operator
```

## Paper-trial identity gate

The checked-in `config.yaml` has `research.paper_trial.enabled: false`. An
explicit trial profile must name a catalog `variant_id` and `trial_id`; it is a
non-authorizing, account-bound incumbent path separate from the normal
validated-edge selection path. It grants no live authority and does not change
the normal validated-edge policy.

When enabled, the runtime freezes the trial ID, candidate/variant, incumbent
identity, policy/spec/code/cohort identities, and `include_ibr` cohort mode in
the persisted state. On every refresh, these values must match the mounted
configuration. A mismatch is a hard entry/readiness blocker, not a prompt to
overwrite state. The active trial is also account-bound: an account identity
change is refused. Initial activation requires local and broker-confirmed
flatness; replacement requires the audited terminal-incumbent path and another
confirmed flat book. A trial state of `running`, `passed`, or `review_required`
does not authorize a normal edge and cannot clear `operator_pause`.

Use [paper-shadow-trials.md](../docs/paper-shadow-trials.md) for an explicit
trial profile, activation, or audited cancellation. Do not delete `runtime/` or
edit a persisted identity to bypass a gate.

## Systemd alternative

Compose and systemd are alternative deployment lanes. If systemd is selected,
install and review the units in this directory:

- `alpaca-recorder.service`
- `alpaca-trader.service`
- `alpaca-watchdog.service`
- `alpaca-research.service`
- `alpaca-research.timer`

The units use `/opt/alpaca-agent-trading`, the restricted `alpaca` user, and
separate credential files under `/etc/alpaca-agent-trading`. Do not run the
systemd trader and Compose trader concurrently. Enable the watchdog with the
trader; it is the flatten-only recovery path when the heartbeat is stale.

## Health and dashboard

Health checks are available inside the corresponding container:

```bash
docker compose exec -T trader python deploy/health.py trader
docker compose exec -T recorder python deploy/health.py recorder \
  --path /app/runtime/research/recorded --config config.yaml --max-age 300
docker compose exec -T shadow python deploy/health.py shadow \
  --path /app/shadow/health.json --max-age 180
```

Compose binds the dashboard to `127.0.0.1:8080`; it has no authentication and
must not be exposed through an NSG or public reverse proxy. Reach it through an
SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-ip>
```

Then open `http://127.0.0.1:8080` locally. Health is an observation aid, not a
replacement for authenticated account reconciliation or runtime gates.
