# Deploying to Azure — a first-time walkthrough

This assumes you have never used Azure and have never written a systemd
service.

**Every command block below is labelled with where to run it:**

- 🖥️ **ON YOUR MAC** — your own laptop's Terminal app
- ☁️ **ON THE VM** — the remote server, after you have connected to it over SSH

Getting this wrong is the single most common source of confusion, so the
label is on every block without exception.

**Two more rules:**

1. Copy commands **one line at a time**.
2. **Never paste a `#` comment with a command.** The shell hands the comment
   to the program as an argument, producing errors like
   `main.py: error: unrecognized arguments`.

---

## Which document to read, and when

| Document | When |
| --- | --- |
| **This one** | Deploying to Azure. Self-contained: VM creation through everyday operation |
| `SETUP.md` §2.3 | Only if you use Azure AI Foundry for the model rather than Anthropic/OpenAI directly |
| `SETUP.md` **Step 10** | After the agent has been running about a week, when the research commands start having data to read |
| `README.md` | Reference: configuration, the research layer, what is still pending |

You do not need `SETUP.md` to get running. It covers laptop and generic-VPS
installs, which this document replaces for Azure.

---

## Part 0 — The mental model: two computers

This is the part that makes everything else make sense.

```
   🖥️  YOUR MAC                          ☁️  THE AZURE VM
   ─────────────                         ────────────────
   • Where you edit code                 • Where EVERYTHING runs
   • Where you type `git push`           • Trading loop (24/7)
   • A window into the VM via SSH        • Data recorder (24/7)
                                         • Nightly research
   Can be closed, asleep, or off.        • Holds ALL the data
   Nothing breaks.                       
                                         Never sleeps. Never closes.
            │                                      ▲
            │  git push  ──────►  GitHub  ────►  git pull
            │                                      │
            └──────────  ssh (a live window)  ─────┘
```

**After deployment, your Mac does not run the bot.** The VM does. Your Mac's
copy of the repository becomes an editing workspace only — nothing on it
trades, records, or researches. You can shut your Mac down entirely and the
VM keeps trading.

### What that means in practice

| Question | Answer |
| --- | --- |
| Where do trades happen? | On the VM. It talks to OKX directly |
| Where is the journal database? | On the VM, at `/opt/okx-agent-crypto/runtime/demo/journal.db` |
| Where is the recorded order-book data? | On the VM, at `/opt/okx-agent-crypto/runtime/research/recorded/` |
| Where does the research report get written? | On the VM, at `/opt/okx-agent-crypto/research/results/tournament/REPORT.md` |
| How do I read that report? | SSH into the VM and `cat` it, or copy it to your Mac with `scp` (Part 8) |
| How do I check status? | SSH in and run `main.py status`, or use `journalctl` (Part 8) |
| If I change code on my Mac, does the VM get it? | **No.** You must `git push` from the Mac, then `git pull` on the VM, then restart the service (Part 9) |
| Does my Mac's `runtime/` folder matter now? | No. It is a separate, stale copy. The VM's is the real one |

### Why data does not sync automatically

`runtime/` is in `.gitignore`, deliberately. It holds credentials-adjacent
state, a live database, and gigabytes of recorded market data — none of which
belongs in version control. **Code flows Mac → GitHub → VM. Data stays on the
VM.** Part 8 shows how to pull a copy back when you want to look at it.

---

## Part 1 — Create the virtual machine

🖥️ **ON YOUR MAC** — in a web browser, not a terminal.

Go to <https://portal.azure.com> and sign in. Then:
**Create a resource** → search **Virtual machine** → **Create**.

You will see a row of tabs: *Basics, Disks, Networking, Management,
Monitoring, Advanced, Tags, Review + create*. Every field on every tab is
below. Where a field is not listed, leave it at its default.

> Azure redesigns this portal regularly, so a label may have moved or been
> reworded. The **values** are what matter; if you cannot find a field, its
> default is almost certainly fine.

### Tab 1 — Basics

| Field | What to select | Why |
| --- | --- | --- |
| Subscription | Your subscription | — |
| Resource group | **Create new** → `okx-trading` | A folder holding everything. Deleting it later deletes all of it cleanly |
| Virtual machine name | `okx-agent` | — |
| Region | The one nearest you | Affects latency and price only |
| Availability options | **No infrastructure redundancy required** | Redundancy is for multi-VM services. One VM does not benefit |
| Availability zone | Leave default / **No preference** | — |
| Security type | **Standard** or **Trusted launch** (default) | Either works. Trusted launch is slightly more secure |
| Image | **Ubuntu Server 24.04 LTS - x64 Gen2** | This guide assumes Ubuntu 24.04 |
| VM architecture | **x64** | **Not Arm64.** Some Python wheels lack Arm builds |
| Run with Azure Spot discount | **UNCHECKED** | ⚠️ Spot VMs are **evicted without warning** when Azure needs capacity. That kills your bot mid-trade |
| Size | **Standard_B2s** (2 vCPU, 4 GiB) | Trader and recorder are near-idle; the nightly research run is the only CPU spike |
| Authentication type | **SSH public key** | Passwords get brute-forced within hours of a VM appearing online |
| Username | `azureuser` | The default. Remember what you chose |
| SSH public key source | **Generate new key pair** | Simplest for a first time |
| Key pair name | `okx-agent_key` | — |
| Public inbound ports | **Allow selected ports** | — |
| Select inbound ports | **SSH (22)** — and nothing else | Nothing here serves web traffic. Every extra open port is pure risk |
| Licensing | Leave unchecked | — |

Click **Next: Disks**.

### Tab 2 — Disks

| Field | What to select | Why |
| --- | --- | --- |
| OS disk size | **64 GiB** (or "Image default" then resize) | Recorder writes ~20 MB/month; the market-history dataset is the bulk |
| OS disk type | **Premium SSD (locally-redundant storage)** | Standard HDD makes the nightly research run painfully slow |
| Delete with VM | **Checked** | Avoids paying for an orphaned disk after you delete the VM |
| Key management | **Platform-managed key** (default) | — |
| Enable Ultra Disk compatibility | **Unchecked** | Not needed, costs extra |
| Data disks | **None** — do not add any | The OS disk is enough |

Click **Next: Networking**.

### Tab 3 — Networking

| Field | What to select | Why |
| --- | --- | --- |
| Virtual network | **Create new** (accept the default name) | — |
| Subnet | Default | — |
| Public IP | **Create new** → then **⚠️ set Assignment to `Static`** | See the warning below |
| NIC network security group | **Basic** | — |
| Public inbound ports | **Allow selected ports** | — |
| Select inbound ports | **SSH (22)** only | — |
| Delete public IP and NIC when VM is deleted | **Checked** | Avoids orphaned billable resources |
| Enable accelerated networking | Leave default | Irrelevant at this traffic level |
| Load balancing options | **None** | — |

> ### ⚠️ The static IP is the most important setting on this page
>
> OKX binds API keys to a specific IP address. A **Dynamic** public IP
> changes whenever the VM restarts — and when it does, OKX starts rejecting
> your keys. That will happen at the worst possible time, usually while a
> position is open.
>
> When you click **Create new** under Public IP, a panel opens on the right
> with an **Assignment** setting showing *Dynamic* and *Static*. **Choose
> Static.** It costs a small amount per month and is not optional.

Click **Next: Management**.

### Tab 4 — Management

| Field | What to select | Why |
| --- | --- | --- |
| Enable system assigned managed identity | **Unchecked** | Only needed for calling other Azure services |
| Login with Microsoft Entra ID | **Unchecked** | You are using an SSH key |
| **Enable auto-shutdown** | **⚠️ UNCHECKED** | See the warning below |
| Enable backup | **Unchecked** for now | Part 10 covers snapshots, which are simpler |
| Enable disaster recovery | **Unchecked** | — |
| Patch orchestration options | **Azure-orchestrated** (default) is fine | Keeps the OS patched |
| Reboot setting | **Customer-managed schedule** if offered, otherwise default | You would rather choose when it reboots than have it happen mid-trade |

> ### ⚠️ Auto-shutdown will silently kill your bot every day
>
> Azure offers a daily auto-shutdown to save money on development VMs. If you
> enable it, **the VM powers off at that time every day** — no trading, no
> recording, and a permanent hole in your recorded data that cannot be
> recovered. Leave it **unchecked**.

Click **Next: Monitoring**.

### Tab 5 — Monitoring

| Field | What to select | Why |
| --- | --- | --- |
| Alerts / recommended alert rules | **Unchecked** (optional) | You can add a CPU or availability alert later if you want email warnings |
| Boot diagnostics | **Enable with managed storage account** (default) | Free, and lets you see the console if the VM will not boot |
| Enable OS guest diagnostics | **Unchecked** | Extra cost, not needed |
| Application health monitoring | **Unchecked** | — |

Click **Next: Advanced**.

### Tab 6 — Advanced

| Field | What to select | Why |
| --- | --- | --- |
| Extensions | **None** | — |
| VM applications | **None** | — |
| Custom data / cloud-init | **Leave empty** | You will install manually so you can see each step work |
| User data | **Leave empty** | — |
| Performance (NVMe) | Leave default | — |
| Host group / Proximity placement / Capacity reservation | **None** | All are for multi-VM or latency-critical setups |

Click **Next: Tags**.

### Tab 7 — Tags

Optional and free. Useful for tracking cost:

| Name | Value |
| --- | --- |
| `project` | `okx-agent` |
| `env` | `demo` |

Click **Review + create**.

### Tab 8 — Review + create

Azure validates and shows a monthly cost estimate. Check:

- Image is **Ubuntu Server 24.04 LTS**
- Size is **Standard_B2s**
- **Spot is not enabled**
- Public IP is **Static**

Click **Create**. A dialog appears offering to **Download private key and
create resource**. Click it.

> **This is your only chance to download that key file.** It lands in your
> Mac's `Downloads` folder as `okx-agent_key.pem`. Without it you cannot log
> in and would have to reset access.

Deployment takes 1–3 minutes.

### Cost

A B2s plus a 64 GiB Premium SSD and a static IP lands in the region of
US$40–60/month depending on region — trust the portal's estimate over this
sentence, since prices change. While you are in the portal, set a budget
alert: **Cost Management + Billing** → **Budgets** → **Add**.

---

## Part 2 — Find your VM's IP address

🖥️ **ON YOUR MAC** — in the browser.

When deployment finishes, click **Go to resource**. You land on the VM's
Overview page. On the right you will see:

```
Public IP address        20.123.45.67
Private IP address       10.0.0.4
```

**You want the Public IP.** It looks like four numbers separated by dots —
for example `20.123.45.67` or `52.180.12.34`. Yours will be different.

The **Private** IP (starting `10.` or `172.`) is internal to Azure and is
**not** what you connect to.

Write the public IP down. You will use it many times. Wherever this guide
says `YOUR_VM_IP`, substitute that number.

---

## Part 3 — Bind your OKX API keys to that IP

🖥️ **ON YOUR MAC** — in the browser, at okx.com.

Do this **now**, before installing anything, because the change takes a few
minutes to take effect.

1. Log in to OKX → profile menu → **API management**
2. Find the API key you use for demo trading → **Edit**
3. In **IP address** (or *Link IP address*), enter your VM's public IP,
   e.g. `20.123.45.67`
4. Confirm permissions are **Read** and **Trade** only — **never Withdraw**
5. Save

> Demo and live keys are separate in OKX. If your `config.yaml` says
> `mode: demo`, this must be the key created inside OKX's **Demo Trading**
> section.

---

## Part 4 — Connect to the VM

🖥️ **ON YOUR MAC** — open the **Terminal** app.

Find it with Spotlight (`Cmd + Space`, type "Terminal"), or in
Applications → Utilities → Terminal.

First, fix the key file's permissions:

```bash
chmod 600 ~/Downloads/okx-agent_key.pem
```

SSH refuses to use a key file that other users on your Mac could read, so
this step is mandatory.

Now connect, substituting your real IP:

```bash
ssh -i ~/Downloads/okx-agent_key.pem azureuser@20.123.45.67
```

The first time, you will see:

```
The authenticity of host '20.123.45.67' can't be established.
ED25519 key fingerprint is SHA256:...
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Type `yes` and press Enter. That is normal and only happens once.

**You will know it worked** when your prompt changes from something like
`talhakhan@Mac okx-agent-crypto %` to something like:

```
azureuser@okx-agent:~$
```

**That prompt is how you tell which machine you are on.** If it says
`azureuser@okx-agent`, you are on the VM. If it says `talhakhan@Mac`, you are
on your Mac.

To leave the VM and return to your Mac, type:

```bash
exit
```

You can reconnect any time with the same `ssh` command.

---

## Part 5 — Install system packages

☁️ **ON THE VM** — your prompt should read `azureuser@okx-agent:~$`.

```bash
sudo apt update
```
```bash
sudo apt install -y python3.12 python3.12-venv git sqlite3
```
```bash
python3.12 --version
```

That last command must print `Python 3.12.x` or newer.

> **This is not negotiable.** The pinned numpy and pandas publish no
> installable builds below Python 3.12. On an older version, pip fails the
> entire install with `No matching distribution found for numpy` — which does
> not mention Python versions at all and sends people hunting the wrong
> problem.

---

## Part 6 — Create the service user and give the VM repo access

☁️ **ON THE VM**

### 6a. A dedicated user

Running a trading bot as your login user means any typo in your shell can
touch its files. A separate user with no login shell limits the damage.

```bash
sudo useradd -r -m -d /opt/okx-agent-crypto -s /usr/sbin/nologin okx
```
```bash
sudo mkdir -p /opt/okx-agent-crypto
```
```bash
sudo chown okx:okx /opt/okx-agent-crypto
```

Everything from here uses `sudo -u okx`, which means "run this as the `okx`
user" **while you remain logged in as `azureuser`**.

> ⚠️ **Do not become the `okx` user.** `sudo -iu okx` or `su okx` will leave
> you as an account with no password and no sudo rights, and the next
> `sudo systemctl ...` will prompt for a password that does not exist. If
> that happens, type `exit` to get back to `azureuser` and try again.
>
> The `-r` and `-s /usr/sbin/nologin` flags above are what make `okx` a
> service account rather than a login: it is meant to own files and run
> units, never to be typed into.

### 6b. A read-only deploy key

The repository is private, so the VM needs its own credential. A **deploy
key** is right for this: read-only, tied to one repository, revocable without
touching your personal GitHub account.

```bash
sudo -u okx mkdir -p /opt/okx-agent-crypto/.ssh
```
```bash
sudo -u okx ssh-keygen -t ed25519 -f /opt/okx-agent-crypto/.ssh/id_ed25519 -N ""
```
```bash
sudo cat /opt/okx-agent-crypto/.ssh/id_ed25519.pub
```

That last command prints one line starting `ssh-ed25519 AAAA...`. **Select it
with your mouse and copy it.**

🖥️ **ON YOUR MAC** — in the browser:

1. Go to your repository on GitHub
2. **Settings** → **Deploy keys** → **Add deploy key**
3. Title: `azure-vm`
4. Key: paste the line you copied
5. **Leave "Allow write access" UNCHECKED** — the VM should never be able to
   push
6. **Add key**

### 6c. Clone the repository

☁️ **ON THE VM**

```bash
sudo -u okx git clone -b codex/main-hardening-v2 git@github.com:khanalyst/okx-agent-crypto.git /tmp/okx-clone
```

If asked `Are you sure you want to continue connecting?`, type `yes`.

```bash
sudo -u okx cp -r /tmp/okx-clone/. /opt/okx-agent-crypto/
```
```bash
sudo rm -rf /tmp/okx-clone
```

(The clone goes to a temporary directory first because `git clone` refuses a
destination that already contains the `.ssh` folder you just made.)

Confirm it landed:

```bash
ls /opt/okx-agent-crypto
```

You should see `main.py`, `agent`, `research`, `deploy`, `config.yaml`.

---

## Part 7 — Install and verify

☁️ **ON THE VM**

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

That takes a few minutes. Then prove the code works before wiring anything up:

```bash
sudo -u okx .venv/bin/python -m unittest discover -s tests -t . -q
```

Expect `Ran 304 tests ... OK (skipped=1)`.

> **Two lines in that output look like failures and are not:**
> `event journal write failed: disk full` and
> `Corrupt state detected ...; agent forced to KILLED`.
> Those are tests deliberately triggering safety guards to prove they fire.
> **The verdict is the final `OK`.**

### Credentials

```bash
sudo -u okx cp .env.example .env
```
```bash
sudo -u okx nano .env
```

`nano` is a text editor inside the terminal. Fill in your OKX key, secret and
passphrase, plus your LLM API key. Then:

- `Ctrl + O`, then `Enter` — save
- `Ctrl + X` — exit

If you use Azure AI Foundry for the model, `SETUP.md` section 2.3 covers it:
the Azure key goes in `OPENAI_API_KEY` and a separate line points the client
at your Azure endpoint.

Lock the file down — it holds credentials:

```bash
sudo chmod 600 .env
```
```bash
sudo chown okx:okx .env
```

### The connectivity check

```bash
sudo -u okx .venv/bin/python main.py check
```

This validates configuration, credentials, the IP binding and OKX
connectivity. **Do not go further until this passes.**

If it fails on credentials, it is almost always one of: the IP binding from
Part 3 has not propagated yet (wait 5 minutes), or demo keys are being used
with `mode: live` (or vice versa).

---

## Part 8 — Start the three services

☁️ **ON THE VM**

```bash
sudo cp deploy/okx-trader.service deploy/okx-recorder.service deploy/okx-research.service deploy/okx-research.timer /etc/systemd/system/
```
```bash
sudo systemctl daemon-reload
```

### Recorder first — this one matters most

```bash
sudo systemctl enable --now okx-recorder
```
```bash
sudo systemctl status okx-recorder
```

Look for `active (running)`. Press `q` to exit the status view.

> The recorder captures order-book depth and filled liquidations. **OKX never
> serves this data historically** — it exists only if you are recording it.
> Every hour it is off is a permanent hole. Leave it running even when the
> trader is paused.

### Then the trader

```bash
sudo systemctl enable --now okx-trader
```
```bash
sudo systemctl status okx-trader
```

### Then the nightly research timer

```bash
sudo systemctl enable --now okx-research.timer
```
```bash
systemctl list-timers okx-research.timer
```

**What `enable --now` means:** `enable` = start automatically at every boot.
`--now` = also start it this instant. Together they are what makes this
survive a VM reboot.

---

## Part 9 — Run the research once by hand

☁️ **ON THE VM**

The timer fires at 03:00 UTC. The first run downloads 730 days across 26
instruments and takes a while, so do it now where you can watch:

```bash
cd /opt/okx-agent-crypto
```
```bash
sudo -u okx PYTHON=/opt/okx-agent-crypto/.venv/bin/python ./research/nightly.sh
```

Then read the result:

```bash
cat research/results/tournament/REPORT.md
```

**Check the header before believing anything in it:**

```
- instruments: 20+              ← if it says 8, the download did not finish
- bars per instrument: ~70000   ← ~19200 means only 200 days were fetched
```

And this line:

```
**PASS** - benchmark reproduces its measured failure
```

If it says **FAIL**, stop: the research harness is broken and every other
number in that run is unverified. `nightly.sh` exits non-zero in that case,
so a red `okx-research` unit means exactly this — not that a strategy
performed badly.

### Then check what the research layer says

The nightly run does two things: the journal-replay path, which is
authoritative, and the OHLCV tournament above, which is exploratory. The one
command that summarises where you stand:

```bash
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python research.py readiness
```

On a fresh VM everything will say "run the agent", which is correct — the
research corpus is written by the trader as a side effect of running, so
there is nothing to read on day one. Come back to it after a week.

`SETUP.md` **Step 10** explains what each check answers and roughly when it
becomes meaningful. That is the section to read once the agent has been
running; the rest of `SETUP.md` covers ground this document already did.

To force a completely clean re-download:

```bash
sudo -u okx rm -rf runtime/research/data
```

---

## Part 10 — Verify shadow evaluation is recording

☁️ **ON THE VM** — wait about 15 minutes after starting the trader.

```bash
sudo -u okx sqlite3 runtime/demo/journal.db "SELECT strategy_id, COUNT(*) FROM events WHERE kind='shadow_summary' GROUP BY strategy_id;"
```

**Expect six rows:** `flush-fade`, `funding-carry`, `funding-unwind`,
`ls-ratio-fade`, `momentum`, `trend-multiday`.

- One row → the VM is on old code
- Zero rows → the trader is not running

Then see which contracts are actually firing:

```bash
sudo -u okx sqlite3 runtime/demo/journal.db "SELECT strategy_id, COUNT(*) FROM events WHERE kind='shadow_decision' GROUP BY strategy_id;"
```

This is legitimately empty at first — contracts only fire when their
conditions are met. Give it a day. If after 24 hours `flush-fade` and
`ls-ratio-fade` are still at zero while others fire, the open-interest and
long/short snapshot fields are not populating, and that is worth reporting.

---

## Part 11 — Everyday operation

### Watching what it is doing

☁️ **ON THE VM**

Live trading log, updating as it runs (`Ctrl + C` to stop watching):

```bash
journalctl -u okx-trader -f
```

Account status — state, equity, day PnL, drawdown, open positions:

```bash
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python main.py status
```

Last nightly research run:

```bash
journalctl -u okx-research -n 200
```

Are all three services alive?

```bash
systemctl status okx-trader okx-recorder okx-research.timer
```

### Reading the research report on your Mac

The report lives on the VM. To read it in a proper editor on your Mac:

🖥️ **ON YOUR MAC** — in a terminal that is **not** connected to the VM
(type `exit` first if you are logged in):

```bash
scp -i ~/Downloads/okx-agent_key.pem azureuser@20.123.45.67:/opt/okx-agent-crypto/research/results/tournament/REPORT.md ~/Desktop/
```

That copies the file to your Desktop. `scp` is "secure copy" — same
credentials as `ssh`, but it moves files instead of opening a session.

To pull the whole results folder:

```bash
scp -r -i ~/Downloads/okx-agent_key.pem azureuser@20.123.45.67:/opt/okx-agent-crypto/research/results ~/Desktop/okx-results
```

> If `scp` gives a permission error, the files belong to the `okx` user. Fix
> it ☁️ **ON THE VM** with
> `sudo chmod -R a+r /opt/okx-agent-crypto/research/results`.

### Pausing and resuming

☁️ **ON THE VM**

```bash
cd /opt/okx-agent-crypto
```
```bash
sudo -u okx .venv/bin/python main.py pause
```

`pause` stops new entries. Existing positions **keep their exchange-side
stop-loss and take-profit**, so they remain protected.

```bash
sudo -u okx .venv/bin/python main.py resume
```

Close everything immediately:

```bash
sudo -u okx .venv/bin/python main.py flatten
```

Always use these rather than killing the process, so state stays consistent.

### Shipping a code change from your Mac to the VM

This is the loop you will use most.

🖥️ **ON YOUR MAC** — edit code in VS Code as usual, then:

```bash
cd ~/Projects/okx-agent-crypto
```
```bash
git add -A
```
```bash
git commit -m "describe your change"
```
```bash
git push
```

☁️ **ON THE VM** — connect with `ssh`, then:

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

Run the tests **before** restarting. That order is what stops a broken commit
from taking the loop down.

### Changing which strategy trades

☁️ **ON THE VM**

```bash
sudo -u okx .venv/bin/python main.py strategies --verbose
```
```bash
sudo -u okx nano config.yaml
```

Change `strategy.id` and `strategy.version`, save (`Ctrl+O`, `Enter`,
`Ctrl+X`), then:

```bash
sudo systemctl restart okx-trader
```

Every other registered strategy keeps being shadow-evaluated regardless of
which one is active, so switching does not reset anyone else's evidence.

Live mode additionally requires the chosen strategy to be `T3_VALIDATED` or
better. Nothing currently is, and configuration validation will refuse to
start. **That is intended behaviour, not a bug to work around** — it is what
stops a strategy measured as negative from reaching real money by editing one
line.

---

## Part 12 — Back up what cannot be re-downloaded

Candles can always be re-fetched from OKX. Three things cannot:

- `/opt/okx-agent-crypto/runtime/research/recorded/` — order-book depth and
  liquidations, which OKX never serves historically
- `/opt/okx-agent-crypto/runtime/demo/journal.db` — your own decision corpus,
  trade history and shadow evidence. **This is what gate G2 and every
  research command read.** Losing it resets the research clock to zero
- `/opt/okx-agent-crypto/research/cache/findings.db` — the append-only
  findings store: which variants were rejected, on what sample, and why.
  Deliberately not in git, so nothing else holds it

> ⚠️ **You set "Delete with VM: Checked" in Part 1.** That is right for cost
> — an orphaned disk keeps billing — but it means deleting the VM destroys
> all three of the above permanently. Take a snapshot before you delete
> anything, and take one periodically regardless.

The committed `findings/*.md` scorecards are a readable summary of the store
and they live in git, so they survive. The SQLite behind them does not.

🖥️ **ON YOUR MAC**, in the browser: **VM** → **Disks** → click the OS disk →
**Create snapshot**. Or set up a Backup policy for automatic daily snapshots.

To keep a copy locally, 🖥️ **ON YOUR MAC**:

```bash
scp -r -i ~/Downloads/okx-agent_key.pem azureuser@20.123.45.67:/opt/okx-agent-crypto/runtime/research/recorded ~/Desktop/okx-recorded-backup
```

---

## Part 13 — When something goes wrong

| Symptom | Cause and fix |
| --- | --- |
| `No matching distribution found for numpy` | Python below 3.12. Recreate the venv with `python3.12 -m venv .venv` |
| `main.py: error: unrecognized arguments` | A `#` comment was pasted with the command. Paste one clean line |
| `Permission denied (publickey)` when connecting | Wrong key path, wrong username, or you skipped `chmod 600` on the `.pem` file |
| OKX rejects credentials | IP binding does not match the VM's public IP, or demo keys are being used with `mode: live` |
| Bot stopped overnight | Auto-shutdown was enabled during VM creation. **VM → Operations → Auto-shutdown → Off** |
| VM vanished / was recreated | Spot instance eviction. Spot must be disabled — it cannot be changed after creation, so recreate the VM |
| `systemctl status okx-research` is red | The tournament's benchmark check failed. The harness is broken; do not trust that run's report |
| Report shows `instruments: 8` | The download did not complete. `rm -rf runtime/research/data` and re-run |
| Service missing after reboot | `enable` was skipped: `sudo systemctl enable okx-trader okx-recorder okx-research.timer` |
| `another agent loop already holds the run lock` | The service is already running. Use `main.py pause`, not a second `run` |
| Restart refused after a self-kill | Deliberate. A drawdown self-kill requires `run --acknowledge-kill`, forcing human review between a blow-up and the next trade |

Log locations, ☁️ **ON THE VM**:

```bash
journalctl -u okx-trader --since "1 hour ago"
```
```bash
sudo -u okx tail -100 /opt/okx-agent-crypto/runtime/demo/agent.log
```

---

## Part 14 — What to expect, honestly

Once this is running, nothing dramatic happens for weeks. That is the design
working, not failing.

- **`momentum` is registered `T0_REJECTED`.** It trades on demo as an
  operations rehearsal — proving orders place, stops attach exchange-side,
  and reconciliation survives a process kill — **not** because it should make
  money. It will not. Read its demo PnL as noise.
- **`funding-unwind` is the one live lead.** It needs roughly **58 days** of
  shadow evidence to reach the trade count its own effect size requires. The
  promotion is automatic; no decision from you.
- **A forward result that disagrees in sign with its backtest triggers an
  alert.** That is a *good* outcome: it means a backtest was fitted, and you
  found out without risking capital.
- **`scalp-maker` needs about three months** of recorded order-book depth
  before it can be tested at all.

The single highest-value thing on this page is Part 8's recorder. Everything
else can be redone later. Recording time cannot be recovered.
