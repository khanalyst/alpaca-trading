# Setup — Mac and Azure VM

This is the current setup guide. It replaces the old split between the local
setup guide and the Azure walkthrough. The two sections below cover the two
supported operating locations; day-to-day monitoring and research procedures
live in [`OPERATIONS.md`](OPERATIONS.md).

The safe starting point is always OKX demo mode. Do not enable Withdraw on an
API key. The shipped LLM is provider `openai`, model `gpt-5.6-terra`.

## Section 1 — Mac/local setup

### 1. Requirements

- macOS with Python 3.12+ available;
- Git;
- an OKX demo account and demo API key with Read and Trade only;
- an OpenAI-compatible model key, or Azure AI Foundry credentials routed by
  `OPENAI_BASE_URL`;
- enough disk for the journal, findings database, research cache, and any
  downloaded corpus.

### 2. Install

```bash
git clone <repository> okx-agent-crypto
cd okx-agent-crypto
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
```

Fill `.env` with the OKX demo credentials and the selected model key. Keep
secrets out of `config.yaml` and out of Git. For Azure AI Foundry, use the
Azure key as `OPENAI_API_KEY` and set:

```dotenv
OPENAI_BASE_URL=https://YOUR-RESOURCE.services.ai.azure.com/openai/v1
```

The repository uses the standard OpenAI client and environment routing; it
does not provision Azure resources or deploy containers.

### 3. Verify and run demo mode

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py strategies --verbose
./.venv/bin/python main.py run
```

In another terminal:

```bash
./.venv/bin/python main.py status
./.venv/bin/python research.py readiness
```

The current runtime is `momentum/phase1-v3`. Demo operation may be used to
rehearse the controls even though its research tier is `T0_REJECTED`; live
startup requires `T3_VALIDATED` or better.

### 4. Local configuration

The active configuration is in `config.yaml`. Important current values are:

| Key | Current meaning |
| --- | --- |
| `strategy.id` / `strategy.version` | `momentum` / `phase1-v3` |
| `llm.provider` / `llm.model` | `openai` / `gpt-5.6-terra` |
| `cycle.decision_interval_seconds` | Decision cadence; safety housekeeping remains separate |
| `maker_first_enabled` | `false`; the maker entry path is not enabled |
| `maker_first_wait_seconds` | Bounded maker wait when that path is enabled |
| `research.shadow_enabled` | `true` |
| `research.shadow_variants` | `[*]` |
| `research.shadow_budget_ms` | `0`, so all scheduled variants are considered |
| `research.shadow_workers` | `2`, bounded parallel computation |
| `research.findings_store` | `research/cache/findings.db` |

The removed legacy shadow setting is not accepted. The LLM is called once per
cycle; shadow variants reuse its parsed decisions. The current B7.5 maker-first
primitive is documented in `research/plan/B7.5-record.md` and remains disabled
until its execution evidence and forward model are reviewed.

### 5. Stop and restart

Use `main.py pause` to stop opening new positions while leaving protection and
housekeeping active. Use `main.py resume` to resume after the reason for the
pause is understood. A process stop is not a substitute for checking the OKX
account and the journal.

### 6. Tests

```bash
./.venv/bin/python -m pytest -q
```

## Section 2 — Azure VM setup and deployment

The VM is the always-on location for the trader, order-book recorder, and
nightly research timer. A Mac is the development/inspection workstation. Code
must be deliberately deployed to the VM; its runtime data must not be copied
into the repository as a default.

### 1. Create the VM

Use an Ubuntu 24.04 x64 VM with a static public IP. A small two-vCPU/four-GB
VM is sufficient for the trader and recorder; the nightly tournament is the
main CPU spike. Disable Spot eviction and auto-shutdown. Bind the static IP in
the OKX API-key restrictions before starting the agent.

Azure portal creation is outside this repository. There are no ARM, Bicep,
Terraform, container, or Azure provisioning files here.

### 2. Install the host

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git sqlite3
sudo useradd -r -m -d /opt/okx-agent-crypto -s /usr/sbin/nologin okx
sudo -u okx git clone <repository> /opt/okx-agent-crypto
cd /opt/okx-agent-crypto
sudo -u okx python3.12 -m venv .venv
sudo -u okx .venv/bin/pip install -r requirements.lock.txt
```

Copy `.env` securely to the VM, then:

```bash
sudo chown okx:okx /opt/okx-agent-crypto/.env
sudo chmod 600 /opt/okx-agent-crypto/.env
sudo -u okx .venv/bin/python main.py check
```

### 3. Install the services

```bash
sudo cp deploy/okx-trader.service /etc/systemd/system/
sudo cp deploy/okx-recorder.service /etc/systemd/system/
sudo cp deploy/okx-research.service /etc/systemd/system/
sudo cp deploy/okx-research.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now okx-recorder
sudo systemctl enable --now okx-trader
sudo systemctl enable --now okx-research.timer
```

Start the recorder before the trader. The recorder captures order-book depth
and short-retention series that cannot be recovered from historical candles.
The research timer runs at 03:00 UTC and is persistent across missed windows.

### 4. VM service checks

```bash
sudo systemctl status okx-recorder okx-trader okx-research.timer
sudo journalctl -u okx-trader -n 100
sudo journalctl -u okx-recorder -n 100
sudo journalctl -u okx-research -n 200
sudo systemctl list-timers okx-research.timer
```

The nightly service is red when G2 or readiness reports a real failure. A
small corpus or `INSUFFICIENT_SAMPLE` is a collection state, not an edge and
not a reason to promote anything.

### 5. Backup the irreplaceable data

Back up before deleting or rebuilding the VM:

- `runtime/demo/journal.db` or the active mode journal;
- `runtime/research/recorded/`;
- `runtime/research/data/` and its manifest;
- `research/cache/findings.db` and `findings.db.backup`;
- `research/results/` reports and leaderboard outputs.

An Azure disk snapshot or an encrypted copy to separate storage is required.
Selecting “Delete with VM” is acceptable only when a current snapshot exists;
deleting the VM without a snapshot destroys the corpus.

### 6. Azure model routing

Azure AI Foundry is only a model endpoint choice. Set `OPENAI_API_KEY` to the
Azure key and `OPENAI_BASE_URL` to the Azure `/openai/v1` endpoint. The code
still uses `llm.provider: openai` and `llm.model: gpt-5.6-terra` unless the
deployed model is intentionally changed and documented in the same change.

### 7. Deploying code changes

The VM does not see Mac changes automatically. Deploy a reviewed commit or
working-tree export, then on the VM:

```bash
sudo -iu okx
cd /opt/okx-agent-crypto
git pull --ff-only
.venv/bin/python -m pytest -q
exit
sudo systemctl restart okx-recorder okx-trader
```

Run the research workflow manually once after a deployment and inspect the
report before relying on the timer:

```bash
sudo -u okx .venv/bin/bash research/nightly.sh
```

For the full operating and reporting sequence, use
[`OPERATIONS.md`](OPERATIONS.md). For current hypothesis and variant identity,
use [`research/HYPOTHESES_AND_VARIANTS.md`](research/HYPOTHESES_AND_VARIANTS.md).
