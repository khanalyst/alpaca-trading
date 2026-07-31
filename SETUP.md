# Setup — Mac and Azure VM

This guide installs the shipped demo configuration. Day-to-day research,
backup, and recovery procedures are in [OPERATIONS.md](OPERATIONS.md).

Current defaults are OKX `demo`, strategy `momentum/phase1-v3`, LLM provider
`openai`, and model/deployment identifier `gpt-5.6-sol`. No credential is
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
before authoritative downstream research is trusted. `INSUFFICIENT_SAMPLE`
means collection is still open.

## 2. Configuration summary

| Key | Shipped value/behavior |
| --- | --- |
| `mode` | `demo` |
| `strategy.id` / `strategy.version` | `momentum` / `phase1-v3` |
| `llm.provider` / `llm.model` | `openai` / `gpt-5.6-sol` |
| `cycle.interval_seconds` | `300` seconds for housekeeping and decisions |
| `cycle.decision_interval_seconds` | Unset; optional slower decision cadence that does not slow safety housekeeping |
| `execution.maker_first_enabled` | Omitted, validated default `false` (B7.5 disabled) |
| `execution.maker_first_wait_seconds` | Omitted, validated default `20` seconds |
| `research.shadow_enabled` | `true` |
| `research.shadow_variants` | `[*]` |
| `research.shadow_budget_ms` | `0` |
| `research.shadow_workers` | `2` |
| `research.findings_store` | `research/cache/findings.db` |
| `research.experiment_min_duration_days` | `3` |
| `research.experiment_min_observations` | `100` |
| `research.backup_target` | Unset; local-default backups until a mount is explicitly configured |

All seven research strategies receive the same cycle snapshot/timestamp and
keep independent paper accounts. Each strategy runs a baseline plus at most
one candidate; both the duration and observation floors must be met before
rotation. Only the configured main strategy can reach the demo exchange.

The available exit policies are `fixed_rr` and `extended_rr`.

## 3. Local checks and tests

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py research-loop --no-review
./.venv/bin/python research.py report
./.venv/bin/python research.py backup
./.venv/bin/python -m pytest -q
```

The default backup is `local_default`; it is not protection from loss of the
machine.

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
and `research/results`. It does not copy the complete downloaded
`runtime/research/data` cache. See [OPERATIONS.md](OPERATIONS.md) before a
zero-data-loss VM deletion or rebuild.

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

## 8. One-time VM import

`vm-import/2026-07-30/` contains a one-time journal, findings database, WAL
files, and research archive imported for development. Treat it as read-only.
Copy databases or extract the archive into a temporary directory before tests.
Never point `research.findings_store`, `JOURNAL_DB`, `DATA_DIR`, recorder
output, tournament output, or `BACKUP_TARGET` at this fixture.

## 9. B7.5 boundary

B7.5 is the optional maker-first order primitive, not the `scalp-maker` shadow
strategy. How it completes: deliberately enable it in demo and validate its
fill/cancel/timeout evidence. Why it waits: the shipped default is disabled and
exchange-only passive-order races are not proven by historical data.
