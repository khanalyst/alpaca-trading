# Running this 24/7 on an Azure VM

> **First time doing this?** Use
> [`../AZURE_DEPLOYMENT.md`](../AZURE_DEPLOYMENT.md) instead — it is the same
> deployment written as a step-by-step walkthrough, including creating the VM,
> the deploy key, IP binding and what to check afterwards. This page is the
> condensed reference for someone who has done it before.


## Why a VM and not Azure Functions

The trading loop is long-running and stateful: it holds a single-process
lock, keeps a local SQLite journal, and runs a 300s cycle. Functions fights
all three — the Consumption plan caps execution at 10 minutes (fatal for the
nightly tournament), gives no durable local disk, and its stateless model is
directly at odds with the run lock. Premium/Durable plans work around some of
that at a cost above the VM.

| Component | Sizing | Notes |
| --- | --- | --- |
| VM | `Standard_B2s` (2 vCPU, 4 GB), Ubuntu 24.04 | Trader and recorder are near-idle; the nightly tournament is the only CPU spike |
| Disk | 64 GB Premium SSD | Recorder ~20 MB/month; the history dataset is the bulk |
| Network | **Static public IP** | Required — OKX API keys are IP-bound |
| Backup | Nightly snapshot, or rsync `runtime/research/recorded` to Blob Storage | Recorded order-book data is irreplaceable; a lost disk cannot be re-downloaded |

## Provision

```bash
# Ubuntu 24.04, Standard_B2s, static IP. Bind that IP in OKX API settings.
sudo apt update
sudo apt install -y python3.12 python3.12-venv git sqlite3

sudo useradd -r -m -d /opt/okx-agent-crypto okx
sudo -u okx git clone <your-fork> /opt/okx-agent-crypto
cd /opt/okx-agent-crypto
sudo -u okx python3.12 -m venv .venv
sudo -u okx .venv/bin/pip install -r requirements.lock.txt
```

Python 3.12 is not optional: the pinned numpy and pandas publish no wheels
for 3.11, and pip fails the whole install rather than warning.

```bash
sudo -u okx cp .env.example .env   # then fill it in
sudo chmod 600 .env
sudo chown okx:okx .env
sudo -u okx .venv/bin/python main.py check   # validates config, keys, OKX
```

## Install the services

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now okx-recorder      # start this FIRST
sudo systemctl enable --now okx-trader
sudo systemctl enable --now okx-research.timer
```

Start the recorder first and leave it running even when the trader is
paused. It captures order-book depth and the short-retention statistics
series that OKX never serves historically — every hour it is off is data
that cannot be recovered at any price, and it is the sole blocker on the
scalping hypothesis.

## Operate

```bash
systemctl status okx-trader okx-recorder okx-research.timer
journalctl -u okx-trader -f                  # live loop
journalctl -u okx-research -n 200            # last nightly run
systemctl list-timers okx-research.timer     # when it next fires

sudo -u okx .venv/bin/python main.py status
sudo -u okx .venv/bin/python main.py strategies --verbose
sudo -u okx .venv/bin/python main.py pause   # stop opening; SL/TP stay live
```

The nightly unit exits non-zero when the tournament's benchmark check
fails, so `systemctl status okx-research` going red means the research
harness has broken and the latest report should not be trusted — not that a
strategy performed badly.

## Switching the traded strategy

```bash
sudo -u okx .venv/bin/python main.py strategies   # see tiers
sudo -u okx nano config.yaml                      # set strategy.id + version
sudo systemctl restart okx-trader
```

Every other registered strategy keeps being shadow-evaluated regardless of
which one is active, so switching does not restart anyone else's evidence.

Live mode additionally requires the chosen strategy to be `T3_VALIDATED` or
better. Nothing currently is, and config validation will refuse to start.
That is the intended behaviour.
