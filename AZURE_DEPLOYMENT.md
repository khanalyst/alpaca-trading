# Deploying to Azure — a first-time walkthrough

This assumes you have never used Azure and have never written a systemd
service. Every command is meant to be copied one line at a time.

**Copy commands one line at a time, and never paste a `#` comment with them.**
Shells hand the comment to the program as an argument, which produces
confusing errors like `main.py: error: unrecognized arguments`.

By the end you will have three things running unattended:

| Service | What it does | Why it must not stop |
| --- | --- | --- |
| `okx-recorder` | Records order-book depth, liquidations and short-retention statistics every few minutes | OKX never serves this history. A gap is permanently unrecoverable |
| `okx-trader` | The trading loop, and shadow-evaluation of all six strategies | Shadow evidence only accrues while it runs |
| `okx-research` | Nightly: refresh data, resolve forward evidence, re-score every strategy | Turns accumulated data into a verdict |

---

## 0. Why a VM and not Azure Functions

Worth understanding before you spend money, because "serverless is cheaper"
is usually right and is wrong here.

The trading loop is **long-running and stateful**. It holds a single-process
lock, keeps a local SQLite journal, and runs a cycle every 300 seconds.
Functions fights all three: the Consumption plan caps a single execution at
10 minutes (the nightly tournament exceeds that), gives no durable local
disk, and its stateless model is directly at odds with a run lock whose whole
job is to guarantee one process. Premium plans work around some of it at a
price above the VM.

A single small VM is the simpler and cheaper answer here.

---

## 1. Create the virtual machine

Azure's portal is redesigned regularly, so treat the labels below as
approximate. The settings are what matter.

1. Sign in at <https://portal.azure.com>. If you have no subscription yet,
   create one — a payment method is required even on free credit.
2. **Create a resource** → search **Virtual machine** → **Create**.
3. Fill in the Basics tab:

   | Field | Value | Why |
   | --- | --- | --- |
   | Resource group | Create new, e.g. `okx-trading` | A folder for everything, so you can delete it all at once later |
   | Virtual machine name | `okx-agent` | — |
   | Region | Nearest to you | Only affects latency and price, not correctness |
   | Image | **Ubuntu Server 24.04 LTS** | The guide assumes Ubuntu |
   | Size | **Standard_B2s** (2 vCPU, 4 GB) | Trader and recorder are near-idle; the nightly tournament is the only CPU spike |
   | Authentication type | **SSH public key** | Passwords get brute-forced. Azure can generate a key pair for you |
   | Inbound ports | **SSH (22)** only | Nothing here serves web traffic. Opening more is pure risk |

4. On the **Disks** tab: 64 GB, Premium SSD.
5. On the **Networking** tab, find **Public IP** → **Create new** → set
   Assignment to **Static**.

   > **This one matters.** OKX API keys are bound to an IP address. A dynamic
   > IP changes when the VM restarts, and your keys stop working at the worst
   > possible moment — usually while a position is open.

6. **Review + create** → **Create**. If Azure offers to download a private
   key file, **download it and keep it safe**; it is the only copy.

Rough cost: a B2s plus a 64 GB premium disk lands in the tens of dollars per
month. Check the portal's own estimate — prices change and vary by region.
Set a **budget alert** under Cost Management while you are there.

---

## 2. Connect to it

Note the VM's **Public IP address** from its overview page.

```bash
chmod 600 ~/Downloads/okx-agent_key.pem
```
```bash
ssh -i ~/Downloads/okx-agent_key.pem azureuser@YOUR_VM_IP
```

Replace `YOUR_VM_IP` with the real address. `azureuser` is Azure's default
unless you chose another name. `chmod 600` is required — SSH refuses keys
that other users could read.

Everything from here runs **on the VM**, not on your Mac.

---

## 3. Bind your OKX keys to this IP

Do this now, before the software is installed, because it takes a few minutes
to propagate.

1. In OKX → **API management**, open the key you use for demo trading.
2. Set the IP restriction to your VM's public IP.
3. Confirm the key still has **Read + Trade** and **never Withdraw**.

Demo and live keys are separate. If you are running `mode: demo`, this is the
key created inside OKX's Demo Trading section.

---

## 4. Install the system packages

```bash
sudo apt update
```
```bash
sudo apt install -y python3.12 python3.12-venv git sqlite3
```
```bash
python3.12 --version
```

Must print `3.12.x` or newer. **This is not negotiable**: the pinned numpy and
pandas publish no wheels below 3.12, and pip fails the whole install with a
confusing "No matching distribution found for numpy" rather than a version
hint.

---

## 5. Create the service user and directory

Running a trading bot as your login user means any mistake in your shell can
touch its files. A dedicated user with no login shell limits the blast radius.

```bash
sudo useradd -r -m -d /opt/okx-agent-crypto -s /usr/sbin/nologin okx
```
```bash
sudo mkdir -p /opt/okx-agent-crypto
```
```bash
sudo chown okx:okx /opt/okx-agent-crypto
```

---

## 6. Give the VM read access to the repository

The repository is private, so the VM needs its own credential. A **deploy
key** is the right tool: read-only, tied to this one repository, and
revocable without touching your personal GitHub account.

```bash
sudo -u okx ssh-keygen -t ed25519 -f /opt/okx-agent-crypto/.ssh/id_ed25519 -N ""
```
```bash
sudo cat /opt/okx-agent-crypto/.ssh/id_ed25519.pub
```

Copy the printed line. In GitHub: **your repository** → **Settings** →
**Deploy keys** → **Add deploy key** → paste it → **leave "Allow write
access" unchecked** → **Add key**.

Then clone:

```bash
sudo -u okx git clone -b codex/main-hardening-v2 git@github.com:khanalyst/okx-agent-crypto.git /tmp/okx-clone
```
```bash
sudo -u okx cp -r /tmp/okx-clone/. /opt/okx-agent-crypto/
```
```bash
sudo rm -rf /tmp/okx-clone
```

(The two-step copy exists because `git clone` refuses a directory that
already contains the `.ssh` folder you just created.)

---

## 7. Install the Python dependencies

```bash
cd /opt/okx-agent-crypto
```
```bash
sudo -u okx python3.12 -m venv .venv
```
```bash
sudo -u okx .venv/bin/pip install --upgrade pip
```
```bash
sudo -u okx .venv/bin/pip install -r requirements.lock.txt
```

Verify before going further:

```bash
sudo -u okx .venv/bin/python -m unittest discover -s tests -t . -q
```

Expect `Ran 304 tests ... OK (skipped=1)`.

> Two lines in that output look like failures and are not:
> `event journal write failed: disk full` and
> `Corrupt state detected ...; agent forced to KILLED`.
> They are tests deliberately triggering those guards to prove they fire.
> **The verdict is the final `OK`.**

---

## 8. Configure credentials

```bash
sudo -u okx cp .env.example .env
```
```bash
sudo -u okx nano .env
```

Fill in your OKX key, secret and passphrase, plus the LLM API key. If you are
using Azure AI Foundry for the model, `SETUP.md` section 2.3 covers it — the
Azure key goes in `OPENAI_API_KEY` and a separate line points the client at
Azure's endpoint.

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

Lock the file down — it holds credentials:

```bash
sudo chmod 600 .env
```
```bash
sudo chown okx:okx .env
```

Now check everything actually talks to OKX:

```bash
sudo -u okx .venv/bin/python main.py check
```

This validates the configuration, the keys, the IP binding and connectivity.
**Do not continue until it passes.** If it fails on credentials, the usual
cause is the IP binding from step 3 not having propagated, or demo keys being
used with `mode: live` (or the reverse).

---

## 9. Install the services

```bash
sudo cp deploy/okx-trader.service deploy/okx-recorder.service deploy/okx-research.service deploy/okx-research.timer /etc/systemd/system/
```
```bash
sudo systemctl daemon-reload
```

Start the **recorder first**:

```bash
sudo systemctl enable --now okx-recorder
```
```bash
sudo systemctl status okx-recorder
```

It should say `active (running)`. Press `q` to exit the status view.

> The recorder captures order-book depth and filled liquidations, which OKX
> serves for a short window and never historically. It is the sole blocker on
> the scalping hypothesis and on a direct test of the liquidation mechanism.
> Leave it running even when the trader is paused — it costs almost nothing
> and every hour it is off is data you cannot buy back.

Then the trader:

```bash
sudo systemctl enable --now okx-trader
```
```bash
sudo systemctl status okx-trader
```

Then the nightly research timer:

```bash
sudo systemctl enable --now okx-research.timer
```
```bash
systemctl list-timers okx-research.timer
```

`enable` means "start automatically at boot"; `--now` means "and also start it
this moment". Together they are what makes this survive a reboot.

---

## 10. Do the first research run by hand

The timer fires at 03:00 UTC. The first download is 730 days across 26
instruments and takes a while, so run it once now and watch it:

```bash
sudo -u okx PYTHON=/opt/okx-agent-crypto/.venv/bin/python ./research/nightly.sh
```

Then read the result:

```bash
cat research/results/tournament/REPORT.md
```

**Check the header before trusting any number in it:**

```
- instruments: 20+          ← if this says 8, the download did not complete
- bars per instrument: ~70000   ← ~19200 means only 200 days were fetched
```

And check this line:

```
**PASS** - benchmark reproduces its measured failure
```

If it says **FAIL**, stop and investigate: the research harness is broken and
every other number in that run is unverified. `nightly.sh` exits non-zero in
that case, so `systemctl status okx-research` going red means the same thing.

To force a completely clean re-download:

```bash
sudo -u okx rm -rf runtime/research/data
```

---

## 11. Confirm shadow evaluation is running

Wait about 15 minutes after starting the trader (three cycles), then:

```bash
sudo -u okx sqlite3 runtime/demo/journal.db "SELECT strategy_id, COUNT(*) FROM events WHERE kind='shadow_summary' GROUP BY strategy_id;"
```

**Expect six rows** — `flush-fade`, `funding-carry`, `funding-unwind`,
`ls-ratio-fade`, `momentum`, `trend-multiday`. One row means the deployment
is on old code. Zero means the trader is not running.

Then check which contracts are actually firing:

```bash
sudo -u okx sqlite3 runtime/demo/journal.db "SELECT strategy_id, COUNT(*) FROM events WHERE kind='shadow_decision' GROUP BY strategy_id;"
```

This is legitimately empty at first — contracts only fire when their
conditions are met. Give it a day. If after 24 hours `flush-fade` and
`ls-ratio-fade` are still at zero while others fire, the open-interest and
long/short snapshot fields are not populating and that is worth reporting.

---

## 12. Day-to-day operation

**Watching:**

```bash
journalctl -u okx-trader -f
```
```bash
journalctl -u okx-research -n 200
```
```bash
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python main.py status
```

**Controlling the loop** — always through `main.py`, not by killing the
process, so state stays consistent:

```bash
sudo -u okx .venv/bin/python main.py pause
```
```bash
sudo -u okx .venv/bin/python main.py resume
```
```bash
sudo -u okx .venv/bin/python main.py flatten
```

`pause` stops new entries. Existing positions keep their exchange-side
stop-loss and take-profit, so they stay protected.

**Updating to new code:**

```bash
cd /opt/okx-agent-crypto
```
```bash
sudo -u okx .venv/bin/python main.py pause
```
```bash
sudo -u okx git pull
```
```bash
sudo -u okx .venv/bin/pip install -r requirements.lock.txt
```
```bash
sudo -u okx .venv/bin/python -m unittest discover -s tests -t . -q
```
```bash
sudo -u okx .venv/bin/python main.py check
```
```bash
sudo systemctl restart okx-trader
```
```bash
sudo -u okx .venv/bin/python main.py resume
```

**Changing which strategy trades:**

```bash
sudo -u okx .venv/bin/python main.py strategies --verbose
```
```bash
sudo -u okx nano config.yaml        # set strategy.id and strategy.version
```
```bash
sudo systemctl restart okx-trader
```

Every other registered strategy carries on being shadow-evaluated regardless
of which one is active, so switching does not reset anyone else's evidence.

Live mode additionally requires the chosen strategy to be `T3_VALIDATED` or
better. Nothing currently is, and configuration validation will refuse to
start. **That is intended behaviour, not a bug to work around** — it is what
stops a strategy measured as negative from reaching real money by editing one
line.

---

## 13. Back up what cannot be re-downloaded

Candles can always be re-fetched. Two things cannot:

- `runtime/research/recorded/` — order-book depth and liquidations, which OKX
  never serves historically
- `runtime/demo/journal.db` — your own trade and shadow-decision history

Simplest protection is an Azure disk snapshot on a schedule (**VM → Disks →
your disk → Create snapshot**, or a Backup policy). If you prefer files, sync
the recorded directory to Blob Storage periodically.

---

## 14. When something is wrong

| Symptom | Likely cause and fix |
| --- | --- |
| `No matching distribution found for numpy` | Python below 3.12. Re-create the venv with `python3.12 -m venv .venv` |
| `main.py: error: unrecognized arguments` | A `#` comment was pasted with the command. Paste one clean line |
| OKX rejects the credentials | IP binding does not match the VM's public IP, or demo keys are being used with `mode: live` |
| `systemctl status okx-research` is red | The tournament's benchmark check failed. The harness is broken; do not trust that run's report |
| Report shows `instruments: 8` | The download did not complete. `rm -rf runtime/research/data` and re-run |
| Service not running after reboot | `enable` was skipped. `sudo systemctl enable okx-trader okx-recorder okx-research.timer` |
| `another agent loop already holds the run lock` | The service is already running. Use `main.py pause`, not a second `run` |
| Restart refused after a self-kill | Deliberate. A drawdown self-kill requires `run --acknowledge-kill`, forcing a human review between a blow-up and the next trade |

**Logs live in:**

```bash
journalctl -u okx-trader --since "1 hour ago"
```
```bash
sudo -u okx tail -100 /opt/okx-agent-crypto/runtime/demo/agent.log
```

---

## 15. What to expect, honestly

Once this is running, nothing dramatic happens for weeks, and that is the
design working rather than failing.

- `momentum` is registered `T0_REJECTED`. It trades on demo as an operations
  rehearsal — to prove orders place, stops attach exchange-side,
  reconciliation survives a process kill — **not** because it is expected to
  make money. It is not. Read its demo PnL as noise.
- `funding-unwind` is the one live lead. It needs roughly **58 days** of
  shadow evidence to reach the trade count its own effect size requires. The
  promotion is automatic and needs no decision from you.
- A forward result that **disagrees in sign** with the backtest triggers an
  alert. That is a good outcome, not a bad one: it means a backtest was
  fitted and you learned it without risking capital.
- `scalp-maker` needs about three months of recorded order-book depth before
  it can be tested at all.

The single highest-value thing on this page is step 9's recorder. Everything
else can be redone later; recording time cannot be recovered.
