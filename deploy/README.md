# VM deployment

The canonical setup is in [`../SETUP.md`](../SETUP.md); daily checks and safe
restart behavior are in [`../OPERATIONS.md`](../OPERATIONS.md). Two deployment
lanes are supported.

## Docker Compose (recommended for a new Ubuntu VM)

Production Compose runs on the Ubuntu VM. Docker Desktop on a Mac is a
separate local engine; use an SSH Docker context for remote inspection rather
than starting a second trader locally. The complete systemd migration,
temporary ACL procedure, SSH tunnel setup, and update helper are documented in
[`../SETUP.md`](../SETUP.md).

[`../compose.yaml`](../compose.yaml) runs four services from the exact same
image and locked Python dependency set:

- `recorder` writes irreplaceable short-retention OKX market observations;
- `trader` is the only order-capable service and is fixed to one replica;
- `research` runs `research/nightly.sh` at 03:00 UTC and catches up once after
  a missed scheduled time; every run writes a new immutable
  `runtime/research/snapshots/<UTC timestamp>` corpus;
- `dashboard` is read-only and shows heartbeat/state, positions, assignments,
  outcomes, performance JSON, readiness, backup evidence, and reports.

The trader will not start until the recorder has written a fresh CSV. Named
volumes persist runtime, cache, tournament, and generated-report data. The
dashboard mounts those volumes read-only, receives no credential secret, and
is published only on host `127.0.0.1`; reach it with an SSH tunnel or private
VPN, not a public firewall rule.

The market downloader refuses non-empty snapshot directories. The tournament
then requires an `okx-history-snapshot.v1` manifest and verifies exact file
membership, hashes, row counts, and timestamp ranges before scoring. The
verified research backup retains every regular file in complete,
manifest-bearing immutable snapshot trees and checks each file's size and
SHA-256. In-progress or non-manifested snapshot directories are excluded.

For local use, `.env` is the default read-only Compose secret. Set host variable
`OKX_AGENT_SECRET_FILE` to use another dotenv-format secret file. Inside the
container, `OKX_AGENT_SECRETS_FILE=/run/secrets/agent_credentials` selects it.
The application still clears stale process-level credential values before
reading the selected file. On Linux, Compose file secrets retain their host
ownership and permissions: the recommended VM source is a dedicated mode-0400
file owned by UID/GID 10001, as shown in `SETUP.md`. Do not make credentials
world-readable to work around a failed preflight.

The optional [`compose.external-backup.yaml`](compose.external-backup.yaml)
override has no default destination. `OKX_EXTERNAL_BACKUP_PATH` must identify
an already-mounted host path whose storage really survives VM deletion and is
writable by UID/GID 10001. A Docker bind mount or a different container
`st_dev` alone is not off-host proof; the dashboard therefore never labels it
off-host-verified. Confirm the cloud disk's retain/detach policy or a remote
snapshot/restore independently.

### Production ownership and automated updates

On the deployed VM, Docker Compose owns all four application processes.
Legacy application systemd units remain disabled; systemd manages Docker and
the separate `okx-agent-update.timer`/`okx-agent-update.service` automation.
The timer polls GitHub `main`, accepts only clean fast-forward updates, invokes
[`update-compose.sh`](update-compose.sh), and records the last successful full
SHA in `/var/lib/okx-agent-updater/deployed-revision`.

The root-owned sync wrapper lives at `/usr/local/sbin/okx-agent-sync`, outside
the Git checkout. It must use `umask 022`: tracked files are image inputs and
`config.yaml` is mounted into containers that run as UID/GID 10001. Local
Compose ignores requested config UID/GID/mode fields, so keep the host
`config.yaml` readable (`0644`). The credential file is different: keep
`/etc/okx-agent-crypto/agent.env` owned by `10001:10001` with mode `0400`.

The source checkout is `/opt/okx-agent-crypto`; runtime, cache, results, and
generated findings live in named volumes, and the verified external backup is
mounted at `/srv/okx-agent-research-backup`. Git updates therefore replace
source/image inputs without replacing operational evidence. `down -v`, volume
pruning, and automatic trader resume are not update steps.

See the automated deployment section in [`../SETUP.md`](../SETUP.md) for the
service/timer behavior, immediate trigger, journal commands, and three-SHA
verification procedure.

## systemd application units (legacy VM compatibility)

The small application units remain supported as an alternative deployment
lane, but they must not be enabled on the same VM as the Compose application
stack:

- `okx-recorder.service` records short-retention market data;
- `okx-trader.service` runs the demo-first agent;
- `okx-research.service` runs one research cycle;
- `okx-research.timer` schedules the research service.

Start the recorder before the trader. Runtime state stays under the VM runtime
directories; optional ignored local `vm-import/` history, if present, is never
used by deployment. Provision a different-device destination, then set
`BACKUP_TARGET` and `REQUIRE_EXTERNAL_BACKUP=1` through a systemd override as
documented in `../SETUP.md`.
