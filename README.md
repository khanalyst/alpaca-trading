# Alpaca intraday trading agent

This repository contains a paper-first recorder, bounded research pipeline,
and deterministic intraday trader for US-listed equities/ETFs and listed OCC
options through Alpaca. It is not a performance claim or a promise that a
strategy exists. Crypto, multi-leg options, naked options, and live trading are
outside the shipped default.

The checked-in `config.yaml` is the safe paper profile:

```yaml
mode: paper
broker:
  paper: true
  allow_live: false
  data_feed: iex
  options_feed: indicative
strategy:
  selection_mode: specific
  variant_id: auto
  execution_mode: shares
research:
  enabled: true
  require_validated_variant: true
  paper_trial:
    enabled: false
```

The default runtime therefore uses shares and the configured equity universe.
The `indicative` options feed is a placeholder and is not executable. An
explicit options lane requires its own feed entitlement, configuration review,
and proof boundary.

## Read next

- [SETUP.md](SETUP.md): install the paper deployment from an empty Ubuntu VM.
- [OPERATIONS.md](OPERATIONS.md): routine operation, pause/resume, backup, and
  recovery.
- [ARCHITECTURE.md](ARCHITECTURE.md): component boundaries and data flow.
- [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md): durable Azure storage and
  network setup before following `SETUP.md`.
- [deploy/README.md](deploy/README.md): Compose topology and deployment/update
  procedure.
- [research/README.md](research/README.md): research inputs and gates.
- [docs/README.md](docs/README.md): the maintained documentation catalogue.
- [docs/paper-shadow-trials.md](docs/paper-shadow-trials.md): the opt-in,
  non-authorizing paper-incumbent trial.

## Runtime topology

`compose.yaml` defines the supported Compose deployment:

| Service | Role | Broker authority |
| --- | --- | --- |
| `recorder` | Records Alpaca bars, quotes, and optional option observations into the session-partitioned corpus. | Market-data access only |
| `shadow` | Runs broker-free incremental shadow evaluation and writes its isolated WAL. | None |
| `research` | Runs the scheduled factory/replay and shadow-ingestion work. | None |
| `trader` | Supervises one paper trading child, reconciles broker state, and enforces risk and edge gates. | Paper orders after all gates pass |
| `watchdog` | Recovers stale exposure by canceling protective orders and flattening; it never enters. | Flatten/cancel only |
| `dashboard` | Serves read-only health and reports on localhost. | None |

Workload containers run without Linux capabilities and with a read-only root
filesystem. Durable Compose volumes hold runtime state, research cache/results,
and the shadow WAL. `shadow-init` is a short-lived, credential-free ownership
initializer for that WAL.

`recorder`, `shadow`, and `research` cannot place broker orders. They are also
part of the source-gated research/shadow proof pipeline: historical or offline
replay is diagnostic, while complete forward observations and matching source,
calendar, cohort, deployment, and identity evidence are required before the
normal validated-edge path can resolve an edge for the trader. In the shipped
config, `research.enabled` and `require_validated_variant` are true and the
paper trial is disabled, so that normal path waits for a matching validated
edge. An explicitly enabled `research.paper_trial` is a separate
non-EdgeLedger incumbent path; it still requires its frozen trial identity,
account binding, and flat-book controls described below. No strategy result is
implied by this repository or by an idle/healthy process.

The recorder preserves observation provenance and exact Alpaca calendar
metadata. Historical backfill is diagnostic and cannot be relabeled as
forward-observed evidence. Missing or stale source/calendar data fails closed
at authorizing boundaries. The recorder and shadow services do not modify the
broker account.

## Safety boundaries

- Paper mode is enforced by configuration and Compose environment. Live mode
  is not an unattended deployment path; keep `broker.allow_live: false` and
  `ALPACA_LIVE_ENABLE=false`.
- Runtime orders are day-only and subject to the exact calendar, fresh market
  data, spread/liquidity, position, gross-exposure, open-risk, and daily-loss
  checks. The trader reconciles broker truth on startup and shutdown.
- Equity protection uses broker-side brackets. The current adapter uses a
  software stop plus a broker-side take-profit for long options; the watchdog
  is a flatten-only backstop.
- The bounded research LLM may propose rule data inside the registered grammar.
  It cannot write executable strategy code, bypass a gate, choose quantity,
  or submit an order. The optional paper-runtime LLM is a veto only.
- A deployment identity is propagated through the Compose services. The update
  helper refuses a dirty checkout, a mismatched declared commit, an active
  systemd trader, or a failed recorder probe/check. Do not bypass those checks
  to force a rollout.

## Persisted pause and trial gates

`operator_pause` is a durable field in the mode-scoped runtime state. The
supervisor stays alive with a paused status while it is true, but does not
launch the trader child; a restart does not clear it and there is no automatic
resume. Operator action, safety/reconciliation failures, and the normal
SIGTERM/SIGINT shutdown path can set the pause, so a routine stop/restart or
deployment may require the same explicit flat-only resume check.

Use the read-only deployment inspection before any operator control:

```bash
docker compose run --rm --no-deps trader python main.py inspect-deployment
```

To clear a pause, `main.py resume` performs a fresh authenticated preflight,
reconciles the local journal and broker, requires no broker positions or
non-terminal broker orders, confirms the persisted account fingerprint, writes
an authorization audit, and only then sets `operator_pause` to `false`. It
does not submit, cancel, or flatten orders. If any check fails, the pause stays
set.

```bash
docker compose run --rm --no-deps trader python main.py resume
```

The optional `research.paper_trial` is disabled in `config.yaml`. When an
operator explicitly enables it, the runtime freezes the configured trial ID,
catalog variant, incumbent/policy/spec/code/cohort identities, and shadow
cohort mode. Those identities must match the persisted state; an active
identity/configuration mismatch refuses entry and research refresh. Initial
activation also requires a broker-account-bound, reconciled flat book. Trial
metadata such as `state: running` or a completed report does not clear
`operator_pause`, authorize a normal edge, or override account/risk gates.
Do not edit the trial identity or delete runtime state to get around a block;
use the audited operator procedure in
[docs/paper-shadow-trials.md](docs/paper-shadow-trials.md).

## Quick start

The complete sequence, credentials, and provider-secret setup are in
[SETUP.md](SETUP.md). The minimum Compose flow after those prerequisites are
complete starts with a data-only bootstrap:

```bash
cd /opt/alpaca-agent-trading
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
test -z "$(git status --porcelain=v1 --untracked-files=all)"
export ALPACA_DEPLOYMENT_COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"

docker compose config --quiet
docker compose build
docker compose run --rm --no-deps trader python main.py check --offline
docker compose up -d recorder research shadow dashboard
docker compose ps
docker compose logs --tail=100 recorder research shadow dashboard
```

The separate research provider file is required by the Compose configuration;
it must contain only the key for the provider selected in `config.yaml`.
Never put provider or Alpaca credentials in the repository. The checkout must
be reviewed and clean before building. Export the full `HEAD` commit before the
build and preserve the same `ALPACA_DEPLOYMENT_COMMIT` for every Compose
command and restart; an omitted/unknown or mismatched identity can block
session acceptance and cross-service parity.

The initial authenticated check is intentionally run against the data-only
bootstrap:

```bash
docker compose run --rm --no-deps trader python main.py check
```

`check --offline` validates local configuration only. The default `check`
queries the configured paper account, clock, calendar, and feeds; it is the
broker-facing preflight. On a fresh default install it normally exits nonzero
with `edge_required` and `edge_ready: false` because no validated edge exists
yet. That is an expected readiness refusal, not a successful check: leave
`trader` and `watchdog` stopped while recorder/research/shadow collect and
produce the source-gated proof evidence. Do not bypass the refusal or label it
as deployment success.

After an authenticated check succeeds with a resolved edge and the account,
state, and identity gates are satisfied, start only the order-capable services:

```bash
docker compose up -d trader watchdog
docker compose ps
docker compose run --rm --no-deps trader python main.py inspect-deployment
```

`inspect-deployment` is a read-only paused-and-flat proof. A fresh or running
runtime may return nonzero because it is not yet explicitly paused and flat;
that result describes eligibility for this inspection, not a Docker/startup
failure. Use `status` for an operational projection.

For a deliberate one-share paper connectivity smoke test, stop the trader
first and use the explicit confirmation. The account must start flat with no
open orders; the command buys one equity share, closes it, and verifies the
broker is flat:

```bash
docker compose stop trader watchdog
docker compose run --rm --no-deps trader \
  python main.py paper-smoke --symbol SPY --confirm PAPER
docker compose up -d trader watchdog
```

If the trader had been running before the smoke test, its normal stop may have
persisted `operator_pause=true`. Inspect the state after restarting and use the
flat-only `resume` command above only when the intended paper account is flat.

If a smoke test is interrupted, inspect the returned result and reconcile the
paper account before using the flatten command:

```bash
docker compose run --rm --no-deps trader python main.py flatten --reason paper-smoke-recovery
```

`flatten` is an order-affecting command; use it only after confirming the
target is the intended paper runtime.

## Data and research operations

The recorder's supported one-shot checks are:

```bash
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --probe
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --audit
```

`--probe` checks recent configured market-data access without appending corpus
rows. `--audit` validates the full corpus and duplicate keys. Backfill is
explicitly historical:

```bash
docker compose run --rm --no-deps recorder \
  python deploy/backfill.py --out runtime/research/recorded --days 180
```

Add `--quotes` only when the larger historical quote dataset is required. The
backfill tool records historical provenance; it does not satisfy forward
shadow or paper authorization. The scheduler is already a long-running Compose
service; inspect its existing process rather than starting a duplicate:

```bash
docker compose logs --tail=100 research
```

For a bounded shadow repair, stop the existing writer first. Set the container
paths to the values in `docker compose config` for this deployment; the values
below are only the shipped defaults and must be changed when
`ALPACA_RECORDER_CORPUS_ROOT` or the deployment’s shadow command paths differ:

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

The shadow cycle has no broker-order authority, but it supplies source evidence
to the proof pipeline. A quarantine or identity mismatch must be repaired at
the source and replayed; it is not an instruction to skip a session or rebind
an identity.

## Updating a VM deployment

Use the repository helper on the Linux VM after reviewing and backing up the
durable runtime state. It requires a clean checkout and the exact secret path:

```bash
cd /opt/alpaca-agent-trading
git pull --ff-only
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
deploy/update-compose.sh
```

`deploy/update-compose.sh` verifies Linux, the checkout commit and cleanliness,
Compose availability, and the absence of an active `alpaca-trader.service`.
It validates Compose, builds the trader image, runs the recorder probe, runs
`main.py check`, starts the stack without rebuilding, checks the running trader,
and prints Compose status. It refuses a configured
`ALPACA_DEPLOYMENT_COMMIT` that disagrees with `HEAD`. A failed probe or check
must be diagnosed; unlike the data-only bootstrap, a missing validated edge
makes this helper fail and abort the update. Do not bypass the helper, label
that refusal as success, or resume a paused runtime merely to complete
deployment.

For the systemd alternative, use the units under `deploy/` instead of running
the Compose trader at the same time. The trader supervisor and watchdog share
the same persisted pause and broker-reconciliation rules.

## Local verification

These are commands, not a claim about a fixed result set:
The optional `.venv` setup is documented in [SETUP.md](SETUP.md); Compose
containers remain the VM runtime path.

```bash
./.venv/bin/python main.py --help
./.venv/bin/python main.py check --offline
./.venv/bin/python deploy/test_suite.py --list
./.venv/bin/python deploy/test_suite.py --shard edge
./.venv/bin/python deploy/test_suite.py --shard factory
./.venv/bin/python deploy/test_suite.py --shard research
./.venv/bin/python deploy/test_suite.py --shard runtime
./.venv/bin/python -m compileall -q agent deploy main.py research.py
docker compose config --quiet
```

Use either all four disjoint shards above or the complete unittest suite:

```bash
./.venv/bin/python -m unittest discover -v
```

Use the source and runtime reports as the authority for current behavior; do
not infer readiness, edge quality, or deployment health from a historical
README statement.
