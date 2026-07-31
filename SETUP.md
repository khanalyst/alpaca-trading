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
sudo apt install -y python3.12 python3.12-venv git sqlite3
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

## 6. Provision the external backup destination

Repository code cannot create off-host storage. Provision and mount a separate
disk/share before relying on the VM. The destination must already exist and
its `st_dev` must differ from the repository and every included source.

Example service override:

```bash
sudo systemctl edit okx-research.service
```

```ini
[Service]
Environment=BACKUP_TARGET=/mnt/off-host/okx-agent-research
Environment=REQUIRE_EXTERNAL_BACKUP=1
```

Test it before enabling deletion/rebuild procedures:

```bash
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python research.py backup \
  --target /mnt/off-host/okx-agent-research \
  --require-external
sudo -u okx .venv/bin/python research.py readiness \
  --db runtime/demo/journal.db
```

`local_default` and same-device `configured_local` backups do not make VM
deletion safe. A path setting by itself is not external proof. The application
never prunes prior backup directories, but an administrator or storage policy
can still delete them. Different `st_dev` proves a separate mounted device, not
that the storage is outside the VM's deletion domain; confirm location and
retention separately.

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
