# Deployment topology

Installation, host provisioning, migration, and update commands are the
authority in [`../SETUP.md`](../SETUP.md). Daily checks, safe restarts, backup,
and recovery are in [`../OPERATIONS.md`](../OPERATIONS.md). This file records
which process owns which responsibility and which deployment lanes may coexist.

## Production Docker Compose

[`../compose.yaml`](../compose.yaml) runs four services from one image and one
locked dependency set on the Ubuntu VM:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Collects short-retention OKX market observations; must become healthy first | `runtime-data` volume / recorder output |
| `trader` | The only order-capable process; exactly one replica; shipped account is demo | `runtime-data` volume / journal |
| `research` | Runs `research/nightly.sh`, catches up one missed schedule, and writes a new immutable snapshot per run | `runtime-data`, `research-cache`, `research-results`, `findings-reports` |
| `dashboard` | Read-only health, state, assignments, outcomes, reports, and readiness view | Read-only mounts of the volumes above |

The dashboard binds only to host `127.0.0.1`; use an SSH tunnel or private VPN,
not a public firewall rule. It receives no credential secret. Named volumes
survive ordinary `docker compose down`; `down -v`, volume pruning, or a second
trader can destroy or duplicate operational state and are not update steps.

The checkout/build context is `/opt/okx-agent-crypto`. Credentials are outside
Git at `/etc/okx-agent-crypto/agent.env`. A verified external research backup
is mounted at `/srv/okx-agent-research-backup` only when the operator has
provisioned a destination that survives VM loss; a Docker bind mount or a
different container `st_dev` alone is not off-host proof.

The production VM uses Docker Compose for all four application processes.
Legacy application systemd units stay disabled; systemd manages Docker and the
separate `okx-agent-update.timer`/`okx-agent-update.service` updater. The
updater accepts clean fast-forward changes from GitHub `main`, runs the
preflight/build flow, and records the successful SHA at
`/var/lib/okx-agent-updater/deployed-revision`. It must not automatically
resume a paused trader.

## Legacy systemd lane

The unit files remain supported for an existing non-Compose VM only:

- `okx-recorder.service` — market recorder;
- `okx-trader.service` — one demo-first order process;
- `okx-research.service` and `okx-research.timer` — one research cycle and its
  schedule.

Start the recorder before the trader. Do not enable this lane on the same VM as
Compose. Configure `BACKUP_TARGET` and `REQUIRE_EXTERNAL_BACKUP=1` through the
systemd override described in [`../SETUP.md`](../SETUP.md), and run the
operational checks in [`../OPERATIONS.md`](../OPERATIONS.md).
