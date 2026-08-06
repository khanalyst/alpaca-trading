# Setup — Mac and Azure VM

This is the installation and deployment authority for the shipped demo
configuration. Day-to-day research, backup, and recovery procedures are in
[OPERATIONS.md](OPERATIONS.md).

Current defaults are OKX `demo`, strategy `momentum/phase1-v3`, LLM provider
`openai`, and model/deployment identifier `gpt-5.6-sol-coding`. No credential is
stored in the repository. Use an OKX demo API key with Read and Trade only;
never enable Withdraw.

## 1. Mac/local setup

Requirements: Python 3.12+, Git, an OKX demo account, a supported model key,
and disk space for SQLite journals, recorder data, findings, tournament runs,
and backups.

```bash
git clone <repository> okx-agent-crypto
cd okx-agent-crypto
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
```

Fill `.env` with the demo credentials and selected model key. For an
OpenAI-compatible Azure AI Foundry endpoint, set the Azure key as
`OPENAI_API_KEY` and add:

```dotenv
OPENAI_BASE_URL=https://YOUR-RESOURCE.services.ai.azure.com/openai/v1
```

The repository does not provision Azure resources or deploy a model.

Validate and start:

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py strategies --verbose
./.venv/bin/python main.py run
```

In another terminal:

```bash
./.venv/bin/python main.py status
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
```

The configured momentum strategy is `T0_REJECTED`; demo use is an operations
rehearsal and data-collection path, not a profitable-edge claim. G2 must pass
before authoritative downstream research is trusted. G2 compares the full
canonical pre-risk proposal identity symmetrically with replay keys, requires
a non-vacuous exact match, and fails closed on malformed, duplicate, missing,
or extra identities. Outcome-resolution gaps remain diagnostics rather than
proposal mismatches; G2 does not reproduce full contract or execution
semantics. `INSUFFICIENT_SAMPLE` means collection is still open.

### Reviewed candidate demo startup

The ordinary `main.py run` command remains unchanged. An operator may instead
start exactly one reviewed candidate with `run --candidate-demo` only after the
research path has produced a current qualification, successful flat local
PAPER evidence, and a content-addressed `REVIEWED` T3 packet. Run `main.py
check` first; it binds the current demo key to `runtime/demo/state.json`. Pass
that file's non-secret `account_fingerprint` explicitly:

```bash
./.venv/bin/python main.py run --candidate-demo \
  --variant-id <qualified-variant-id> \
  --scope-key <authoritative-paper-scope> \
  --packet-ref t3-packet:<reviewed-packet-hash> \
  --expected-demo-account-fingerprint <account_fingerprint>
```

Use a dedicated OKX demo key with Read and Trade only and Withdraw disabled.
The account and local runtime must be flat and free of regular/algo orders.
The command fails closed on missing or stale evidence, identity/configuration
drift, account mismatch, unsupported order-state queries, or non-flat state.
It applies the reviewed variant in memory and records a demo authorization
receipt; it does not edit configuration, update the strategy registry, or
authorize live mode. The complete operating and rollback procedure is in
[OPERATIONS.md](OPERATIONS.md#71-run-one-reviewed-candidate-on-okx-demo).

## 2. Configuration summary

| Key | Shipped value/behavior |
| --- | --- |
| `mode` | `demo` |
| `strategy.id` / `strategy.version` | `momentum` / `phase1-v3` |
| `llm.provider` / `llm.model` | `openai` / `gpt-5.6-sol-coding` |
| `cycle.interval_seconds` | `60` seconds for marks, paper exits, housekeeping, and reconciliation |
| `cycle.decision_interval_seconds` | `300` elapsed seconds (95% jitter tolerance); model decisions remain slower than safety/mark cycles |
| `execution.maker_first_enabled` | `true` in shipped demo (B7.5); rejected in live mode |
| `execution.maker_first_wait_seconds` | `20` seconds before IOC fallback |
| `research.shadow_enabled` | `true` |
| `research.shadow_variants` | `[*]` |
| `research.shadow_budget_ms` | `0` |
| `research.shadow_workers` | `2` |
| `research.findings_store` | `research/cache/findings.db` |
| `strategy.execution_mode` | `shadow_only` (shipped): no order path, research lanes unchanged, because no mechanism has earned the account yet. `analyst` makes one LLM call per decision cycle; `deterministic` trades the strategy's own forward contract with no LLM call, which is how a strategy other than `momentum` can occupy the order path |
| `research.staging_store` | `research/cache/staging.db`; machine-authored mechanisms, created by `research.py author` and absent until one is staged |
| `research.forward_feed_version` | `8`; v8 repairs the depth-ladder delivery that silently starved six of seven strategies and widens the universe to 25; three long-horizon models are offline-only; v1-v7 remain historical, with v4 the market-data plumbing repair feed, v5 the immutable-provenance fork, v6 the deterministic four-lane fork, and v7 the liquidation-flow and conditioning-axis fork |
| `research.experiment_min_duration_days` | `10` |
| `research.experiment_min_observations` | `100` |
| `research.backup_target` | Unset; local-default backups until a mount is explicitly configured |

The four realtime strategies receive the same cycle snapshot/timestamp and
deterministic contract proposals, with independent paper accounts. Each
realtime strategy runs a baseline plus at most one candidate; both ten elapsed
days and 100 comparable paired observations must be met before rotation. The
three long-horizon registered models are offline-only. Only the configured main
strategy can reach the demo exchange.

The available exit policies are `fixed_rr`, `extended_rr`, and
`carry_until_normalised` (for `funding-carry`).

## 3. Local checks and tests

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py review-staged --dry-run
./.venv/bin/python research.py shortlist
./.venv/bin/python research.py research-loop --no-review
./.venv/bin/python research.py prepare-review-artifacts
./.venv/bin/python research.py report
./.venv/bin/python research.py backup
./.venv/bin/python -m pytest -q
```

The default backup is `local_default`; it is not protection from loss of the
machine.

## Docker Compose on an Ubuntu VM

Docker Engine with the Compose v2 plugin is the shortest deployment path. It
does not change the safety boundary: the shipped account remains demo and no
strategy is made live-eligible by running it in a container.

Run every production Compose command in this section on the Ubuntu VM. Docker
Desktop on a Mac is a separate local engine; starting the trader there would
create a second agent and it would stop when the Mac sleeps. A Mac may inspect
the VM through an SSH Docker context, but the containers and volumes remain on
the VM.

The deployed production layout is:

| Responsibility | Production location |
| --- | --- |
| Git checkout and Docker build context | `/opt/okx-agent-crypto`, branch `main` |
| Trader, recorder, research scheduler, dashboard | Docker Compose on the Ubuntu VM |
| Runtime state and journal | Docker named volume `runtime-data` |
| Findings database | Docker named volume `research-cache` |
| Tournament/results and generated reports | Docker named volumes `research-results` and `findings-reports` |
| Credentials | `/etc/okx-agent-crypto/agent.env`, outside Git |
| Durable research backup | `/srv/okx-agent-research-backup`, mounted Azure data disk |
| Source-update polling | `okx-agent-update.timer` and `okx-agent-update.service` |
| Successfully deployed Git revision | `/var/lib/okx-agent-updater/deployed-revision` |

The legacy `okx-trader`, `okx-recorder`, `okx-research`, and
`okx-dashboard` systemd application units must remain disabled on this Docker
VM. Systemd still manages Docker itself and the GitHub update timer; it does
not run a second copy of the application processes.

```bash
git clone <repository> okx-agent-crypto
cd okx-agent-crypto
sudo install -d -o 10001 -g 10001 -m 0700 /etc/okx-agent-crypto
sudo install -o 10001 -g 10001 -m 0400 .env.example \
  /etc/okx-agent-crypto/agent.env
sudoedit /etc/okx-agent-crypto/agent.env
export OKX_AGENT_SECRET_FILE=/etc/okx-agent-crypto/agent.env
# Build the one image used by all four long-running services.
docker compose build
docker compose run --rm --no-deps trader python main.py check
docker compose up -d
docker compose ps
```

### Migrate an existing systemd VM once

Build the image while systemd is still running to minimize downtime. Then
stop every legacy service before copying state: two traders could duplicate
orders, and live SQLite files are not a consistent migration source.

```bash
cd /opt/okx-agent-crypto
export OKX_AGENT_SECRET_FILE=/etc/okx-agent-crypto/agent.env
export OKX_EXTERNAL_BACKUP_PATH=/srv/okx-agent-research-backup

sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml build

sudo systemctl stop okx-research.timer \
  okx-research.service okx-trader.service okx-recorder.service || true

sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml create
```

The old systemd files normally belong to user `okx`, while containers run as
UID/GID 10001. Grant that UID temporary read-only ACL access, copy as the
container user, and then remove the ACL. Do not use `cp -a` plus recursive
`chown`: user-namespace or volume restrictions can reject ownership changes.
Committed/generated `findings/` reports are initialized in their named volume
from the image and are regenerated by research, so they are not copied from
the host during this migration.

```bash
sudo apt install -y acl
sudo setfacl -R -m u:10001:rX runtime research/cache research/results

sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml \
  run --rm --no-deps --user 10001:10001 \
  --entrypoint /bin/sh \
  -v "$PWD/runtime:/migration/runtime:ro" \
  -v "$PWD/research/cache:/migration/cache:ro" \
  -v "$PWD/research/results:/migration/results:ro" \
  research -c '
    set -eu
    cp -R /migration/runtime/. /app/runtime/
    cp -R /migration/cache/. /app/research/cache/
    cp -R /migration/results/. /app/research/results/
    rm -f /app/runtime/demo/state.lock /app/runtime/demo/agent.pid
  '

sudo setfacl -R -x u:10001 runtime research/cache research/results

sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml \
  run --rm --no-deps trader python main.py check
sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml up -d
sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml ps
```

Compose implementations may warn that secret/config `uid`, `gid`, and `mode`
are ignored. File-backed secrets retain host permissions, so the source must
remain owned by `10001:10001` with mode `0400`. The non-secret tracked
`config.yaml` must remain readable by the container UID; keep it owned by the
Git service user with mode `0644`. The VM updater must use `umask 022`, not
`077`, so a Git fast-forward does not recreate tracked configuration or source
files as owner-only files.

After Compose is healthy, disable the legacy units. Never use
`docker compose down -v`; `-v` deletes the named evidence volumes.

```bash
sudo systemctl disable okx-research.timer \
  okx-trader.service okx-recorder.service
```

The recorder must become healthy before Compose starts the single trader. The
research scheduler runs at 03:00 UTC and performs one missed run after a
restart. Each run downloads a fresh immutable market snapshot under
`runtime/research/snapshots/<UTC timestamp>`; it never appends to yesterday's
universe. During a long run the scheduler refreshes its durable health status
every 30 seconds. Runtime state, these snapshots, findings, tournament output,
and generated reports use named volumes; `docker compose down` preserves them,
while `down -v` deletes them and must not be used as an ordinary update command.

The dashboard is deliberately bound only to the VM loopback interface. From a
workstation, use an SSH tunnel and open `http://127.0.0.1:8080` locally:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-address>
```

For the deployed VM, save this in `~/.ssh/config` on the Mac (do not paste it
as shell commands):

```sshconfig
Host okx-agent
  HostName 74.162.41.225
  User azureuser
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
  ServerAliveCountMax 6
  TCPKeepAlive yes
  ExitOnForwardFailure yes
  LocalForward 8080 127.0.0.1:8080
```

Use `ssh okx-agent` for a shell or `ssh -N okx-agent` for a tunnel only, then
open `http://127.0.0.1:8080`. If VM-side `curl` succeeds but the Mac browser
does not, the dashboard is healthy and the SSH tunnel is the failing layer.
An idle `Broken pipe` is also an SSH transport failure; the VM containers keep
running. `Permission denied (publickey)` instead requires repairing
`azureuser`'s public key through Azure VMAccess/Run Command.

To inspect the VM Docker daemon from a Mac with Docker Desktop installed,
first add trusted `azureuser` to the VM `docker` group and reconnect, then run
on the Mac:

```bash
docker context create okx-vm --docker "host=ssh://okx-agent"
docker --context okx-vm ps
docker --context okx-vm logs --tail=100 okx-dashboard
```

Use the CLI context for the remote daemon; Docker Desktop's normal Containers
view represents its local engine. Continue running Compose updates on the VM
because the secret and backup bind paths are VM paths.

### Update an existing Compose deployment

After pulling the intended branch on the VM:

```bash
sudo APP_DIR=/opt/okx-agent-crypto \
  OKX_AGENT_SECRET_FILE=/etc/okx-agent-crypto/agent.env \
  OKX_EXTERNAL_BACKUP_PATH=/srv/okx-agent-research-backup \
  /opt/okx-agent-crypto/deploy/update-compose.sh
```

### Automated deployment from GitHub `main`

The production VM treats GitHub `main` as the only deployment reference.
Feature branches have no effect on the VM until they are reviewed and merged
into `main`. A root-owned `/usr/local/sbin/okx-agent-sync` wrapper runs from
`okx-agent-update.timer` every five minutes. It:

1. requires `/srv/okx-agent-research-backup` to be a mounted, separately
   writable device;
2. refuses a dirty checkout or a branch other than `main`;
3. fetches `origin/main` as Git user `okx` using the VM's read-only GitHub
   deploy key;
4. accepts only a fast-forward update;
5. calls `deploy/update-compose.sh`, which builds and preflights before
   replacing the running containers; and
6. writes the successful full commit SHA to
   `/var/lib/okx-agent-updater/deployed-revision` only after deployment passes.

The wrapper must start with a valid shell header, be root-owned/executable, and
use a normal repository umask:

```bash
#!/bin/bash
set -Eeuo pipefail
umask 022
```

The systemd unit invokes Bash explicitly so a malformed executable is reported
clearly rather than only as `203/EXEC`:

```ini
[Service]
Type=oneshot
ExecStart=/bin/bash /usr/local/sbin/okx-agent-sync
```

Enable and inspect automation on the VM:

```bash
sudo systemctl enable --now okx-agent-update.timer
systemctl list-timers okx-agent-update.timer --all
sudo systemctl status okx-agent-update.timer --no-pager
sudo journalctl -u okx-agent-update.service -n 200 --no-pager
```

Trigger an immediate update after merging to `main`:

```bash
sudo systemctl start okx-agent-update.service
```

A successful oneshot service normally returns to `inactive (dead)`; inspect
its result and journal rather than expecting it to remain `active`. Never add
an unconditional `main.py resume` to the updater. A safe container replacement
may deliberately leave the trader paused for operator review.

Verify GitHub, checkout, and deployed state independently:

```bash
APP_DIR=/opt/okx-agent-crypto
sudo -u okx git -C "$APP_DIR" fetch origin main
REMOTE="$(sudo -u okx git -C "$APP_DIR" rev-parse origin/main)"
LOCAL="$(sudo -u okx git -C "$APP_DIR" rev-parse HEAD)"
DEPLOYED="$(sudo cat /var/lib/okx-agent-updater/deployed-revision 2>/dev/null \
  || echo NOT_DEPLOYED)"
printf 'GitHub:   %s\nVM:       %s\nDeployed: %s\n' \
  "$REMOTE" "$LOCAL" "$DEPLOYED"
```

All three SHAs must match. `GitHub != VM` means the update was not fetched;
`VM != Deployed` means Git advanced but build, preflight, or Compose startup
did not complete. Generated runtime/research data is not stored in the Git
checkout, so a fast-forward changes application source without replacing the
named volumes or the external backup disk. Never use `docker compose down -v`
as part of this flow.

The dashboard has no write endpoint and receives no API-key/LLM secret. It is
an operational view, not an administration console. Container health, bounded
logs, CPU/memory limits, and the exact mounted volumes are visible with:

```bash
docker compose ps
docker compose logs --tail=200 trader recorder research dashboard
docker compose config
```

For a secret file outside the checkout, set
`OKX_AGENT_SECRET_FILE=/secure/path/agent.env` when invoking Compose. The file
uses the same format as `.env`; the container receives it read-only under
`/run/secrets`, not as a list of inspectable Compose environment variables.
Compose file-backed secrets are bind mounts, so Docker Compose does not apply
the requested target UID/GID/mode. On Linux, keep the source in a dedicated
directory and make it readable only by container UID/GID 10001, as in the
quickstart above; never relax it to a world-readable credential file. Keep
`OKX_AGENT_SECRET_FILE` set for subsequent Compose commands (or put only
that non-secret path setting in the shell/service environment). The application
preflight remains authoritative.

Docker sends `SIGTERM` during updates. The trader finishes its current bounded
operation, persists `PAUSED` with `operator_pause=true`, leaves exchange-side
protection in place, and exits. After rebuilding, explicitly validate and
resume:

```bash
docker compose stop trader
docker compose build
docker compose up -d
docker compose exec trader python main.py check
docker compose exec trader python main.py resume
```

For an independently retained backup mount, first provision and verify it on
the host, make it writable by UID/GID 10001, then opt in explicitly:

```bash
OKX_EXTERNAL_BACKUP_PATH=/srv/okx-agent-research-backup \
  docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml up -d research
```

Neither a named volume nor a normal bind-mounted directory on the VM OS disk
survives VM deletion. A container-visible different device is still not proof
of off-host retention; verify the cloud disk's detach/retain setting and test a
snapshot or restore outside the source VM.

## 4. Azure VM host

Use Ubuntu 24.04 x64 with a static public IP. Disable Spot eviction and
auto-shutdown. Bind the static IP in the OKX API-key restrictions.

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git sqlite3 parted
sudo useradd -r -m -d /opt/okx-agent-crypto -s /usr/sbin/nologin okx
sudo -u okx git clone <repository> /opt/okx-agent-crypto
cd /opt/okx-agent-crypto
sudo -u okx python3.12 -m venv .venv
sudo -u okx .venv/bin/pip install -r requirements.lock.txt
```

Copy `.env` securely, then:

```bash
sudo chown okx:okx /opt/okx-agent-crypto/.env
sudo chmod 600 /opt/okx-agent-crypto/.env
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python \
  /opt/okx-agent-crypto/main.py check
```

The service account is `nologin`. Do not use `sudo -iu okx`; run each command
with `sudo -u okx` and an explicit working directory/path.

## 5. Services

```bash
cd /opt/okx-agent-crypto
sudo cp deploy/okx-trader.service /etc/systemd/system/
sudo cp deploy/okx-recorder.service /etc/systemd/system/
sudo cp deploy/okx-research.service /etc/systemd/system/
sudo cp deploy/okx-research.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now okx-recorder
sudo systemctl enable --now okx-trader
sudo systemctl enable --now okx-research.timer
```

Start the recorder before the trader. The timer is scheduled for 03:00 UTC,
has up to 10 minutes randomized delay, and is persistent across missed runs.

```bash
sudo systemctl status okx-recorder okx-trader okx-research.timer
sudo systemctl list-timers okx-research.timer
sudo journalctl -u okx-trader -n 100 --no-pager
sudo journalctl -u okx-recorder -n 100 --no-pager
sudo journalctl -u okx-research -n 200 --no-pager
```

The research oneshot is separate from the trader. A research failure does not
restart the trader.

## 6. Provision the managed research-backup disk

The Azure VM needs a persistent managed data disk in addition to its OS and
temporary resource disks. Use `/srv/okx-agent-research-backup` as the permanent
mount point. Do not use `/mnt`: on the deployed VM it is the Azure temporary
resource disk and is not durable storage.

This is a one-time host setup. In Azure, create and attach an empty managed
data disk sized for expected backup growth. Set its VM deletion behavior to
**Detach**/disable **Delete with VM** so deleting the VM retains the disk.

### 6.1 Identify the empty disk

```bash
lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS,MODEL,SERIAL
```

Before this disk was provisioned, the current VM's observed mapping was:

```text
/dev/sda   64G   OS disk; /dev/sda1 is mounted at /
/dev/sdb   16G   Azure temporary resource disk; /dev/sdb1 is mounted at /mnt
/dev/sdc   64G   new empty managed data disk
```

Resolve this mapping from `lsblk` every time. The commands below deliberately
target `/dev/sdc`; stop if it has a filesystem, partitions, or a mount point,
or if the VM's mapping differs. Never run them against `/dev/sda` or
`/dev/sdb`.

The following read-only check should show no existing signatures:

```bash
sudo wipefs --no-act /dev/sdc
```

### 6.2 Partition and format the disk

These commands destroy existing contents on `/dev/sdc` and are run only once
after the empty-disk check:

```bash
sudo parted --script /dev/sdc mklabel gpt
sudo parted --script /dev/sdc mkpart primary ext4 0% 100%
sudo partprobe /dev/sdc
sudo udevadm settle
lsblk -f /dev/sdc
sudo mkfs.ext4 -L okxresearch /dev/sdc1
```

### 6.3 Create a persistent UUID mount

```bash
sudo mkdir -p /srv/okx-agent-research-backup

backup_uuid="$(sudo blkid -s UUID -o value /dev/sdc1)"
test -n "$backup_uuid"
echo "$backup_uuid"

sudo cp -a /etc/fstab /etc/fstab.before-okx-research-disk
if ! grep -q "UUID=$backup_uuid " /etc/fstab; then
  printf 'UUID=%s /srv/okx-agent-research-backup ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n' \
    "$backup_uuid" | sudo tee -a /etc/fstab
fi

sudo findmnt --verify --verbose
sudo mount -a
sudo chown okx:okx /srv/okx-agent-research-backup
sudo chmod 750 /srv/okx-agent-research-backup
```

If `findmnt --verify` or `mount -a` reports an error, fix `/etc/fstab` before
continuing. Verify the mount, free space, device separation, and service-user
write access:

```bash
findmnt --target /srv/okx-agent-research-backup
df -h /srv/okx-agent-research-backup
stat -c '%d %n' \
  /opt/okx-agent-crypto \
  /srv/okx-agent-research-backup
sudo -u okx touch /srv/okx-agent-research-backup/.write-test
sudo -u okx rm /srv/okx-agent-research-backup/.write-test
```

The two `stat` device numbers must differ.

### 6.4 Require the mount in the nightly service

```bash
sudo mkdir -p /etc/systemd/system/okx-research.service.d
sudo tee /etc/systemd/system/okx-research.service.d/backup.conf >/dev/null <<'EOF'
[Unit]
RequiresMountsFor=/srv/okx-agent-research-backup

[Service]
Environment=BACKUP_TARGET=/srv/okx-agent-research-backup
Environment=REQUIRE_EXTERNAL_BACKUP=1
EOF

sudo systemctl daemon-reload
sudo systemctl cat okx-research.service
```

`RequiresMountsFor` prevents the research service from silently writing into
the empty local mount-point directory when the managed disk is unavailable.
The backup command independently requires different-`st_dev` evidence.

### 6.5 Create and verify the first backup

```bash
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python research.py backup \
  --store research/cache/findings.db \
  --journal runtime/demo/journal.db \
  --mode demo \
  --target /srv/okx-agent-research-backup \
  --require-external
sudo -u okx .venv/bin/python research.py readiness \
  --db runtime/demo/journal.db
```

The backup must report `target: external_mounted` and `EXTERNAL MOUNT
VERIFIED`. Readiness must report `external backup PASS`; unrelated research
gates may still report that they are collecting evidence.

After the next reboot, repeat the `findmnt`, `df`, `stat`, write-access, and
readiness checks before relying on unattended research runs.

`local_default` and same-device `configured_local` backups do not make VM
deletion safe. The application never prunes prior backup directories, so
monitor capacity. Different `st_dev` proves a separate mounted device, not its
Azure deletion policy; verify **Detach**/disabled **Delete with VM** in Azure.

The managed disk is a backup destination, not the application's active data
directory. The supported backup captures the findings database, active
journal, `runtime/research/recorded`, research manifests and forward evidence,
complete manifest-bearing immutable trees under
`runtime/research/snapshots/`, and `research/results`. `verify-backup` checks
the size and SHA-256 of every captured raw snapshot file. See
[OPERATIONS.md](OPERATIONS.md) before a zero-data-loss VM deletion or rebuild.

## 7. Deployment updates

Deploy reviewed code deliberately; the VM does not receive Mac changes
automatically.

```bash
sudo -u okx git -C /opt/okx-agent-crypto pull --ff-only
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python -m pytest -q
sudo systemctl restart okx-recorder okx-trader
sudo systemctl start okx-research.service
sudo journalctl -u okx-research -n 200 --no-pager
```

The first research run after provisioning an external mount can still finish
with readiness exit 4 because readiness runs before that run's backup. Verify
the backup, then rerun readiness or the service.

## 8. Optional local historical data

An ignored local `vm-import/` directory may exist on some development
machines. If present, it is optional read-only historical data, is not part of
the clone, and is never required for setup or current operation. Never point
`research.findings_store`, `JOURNAL_DB`, `DATA_DIR`, recorder output,
tournament output, or `BACKUP_TARGET` at it.

For current nightly operation, an explicit `DATA_DIR` must name one absent or
empty snapshot directory. The downloader refuses non-empty output, and the
tournament accepts only an `okx-history-snapshot.v1` manifest whose exact file
identities still match the directory.

## 9. B7.5 boundary

B7.5 is the maker-first order primitive, not the `scalp-maker` shadow strategy.
The shipped demo configuration enables it for measurement: validate its
fill/cancel/timeout evidence before any live-mode consideration. Configuration
validation rejects maker-first in live mode; exchange-only passive-order races
are not proven by historical data.
