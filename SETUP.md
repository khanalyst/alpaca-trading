# Complete setup: Alpaca paper trading on an Ubuntu VM

This is the primary installation and startup guide. Follow it in order for a
new machine. The supported default is:

- one Ubuntu VM;
- Docker Engine with the Compose plugin;
- one Alpaca **paper** account;
- US-listed equities/ETFs and listed OCC options only;
- regular-session day trading with no overnight positions or working orders;
- autonomous research that must prove an edge before the trader can enter.

Do not start with live credentials. Live mode is an optional, separately
guarded step at the end of this guide.

## 1. Prepare the Alpaca paper account

1. Create or sign in to an Alpaca account.
2. Open the paper-trading dashboard, not the live-trading dashboard.
3. Generate a paper API key and secret. Paper credentials are different from
   live credentials.
4. Save both values immediately in a password manager; the secret may not be
   shown again.
5. Confirm the account can access the stock feed you plan to use. This project
   defaults to `iex` for stocks and `indicative` for options. Use `sip` or
   `opra` only when the account is entitled to those feeds.
6. Do not enable or configure crypto symbols. This repository accepts only US
   equity/ETF underlyings and listed OCC option contracts.

Official references:

- [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca authentication and API domains](https://docs.alpaca.markets/us/docs/authentication)
- [Alpaca option-chain feeds](https://docs.alpaca.markets/us/reference/optionchain)

## 2. Create the VM

Use a current Ubuntu LTS x86-64 VM. A practical starting size for all four
services is 4 vCPUs, 8 GB RAM, and at least 40 GB of durable storage. Research
and recorded market data can require more disk over time.

VM checklist:

1. Put the OS and Docker data on durable storage.
2. Restrict inbound SSH (`22/tcp`) to your own IP or VPN.
3. Do **not** expose port 8080 publicly. The dashboard is bound to localhost
   and should be reached through an SSH tunnel.
4. Enable provider snapshots/backups for the durable disk.
5. Set the VM timezone however you prefer; containers run in UTC and market
   policy uses `America/New_York` explicitly.
6. Create a non-root administrative user with sudo access and connect over
   SSH.

Example connection:

```bash
ssh <vm-user>@<vm-address>
```

## 3. Install Git, Docker Engine, and Compose

Run these commands on the VM. They follow Docker's official Ubuntu repository
installation path.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and reconnect so group membership is refreshed, then verify:

```bash
docker version
docker compose version
docker run --rm hello-world
```

If the Docker repository instructions change, use the current
[Docker Engine for Ubuntu guide](https://docs.docker.com/engine/install/ubuntu/)
and [Compose plugin guide](https://docs.docker.com/compose/install/linux/).

## 4. Clone the repository

```bash
sudo install -d -o "$USER" -g "$USER" /opt/alpaca-agent-trading
git clone <repository-url> /opt/alpaca-agent-trading
cd /opt/alpaca-agent-trading
git switch codex/alpaca-paper-edge-hardening
```

Replace the branch name after it is merged into the repository's default
branch.

## 5. Create the paper credential file

Create a root-owned file outside the checkout:

```bash
sudo install -d -m 0750 /etc/alpaca-agent-trading
sudo install -m 0600 /dev/null /etc/alpaca-agent-trading/agent.env
sudoedit /etc/alpaca-agent-trading/agent.env
```

Enter only the paper credentials and selected feeds:

```dotenv
ALPACA_API_KEY=<paper-api-key>
ALPACA_SECRET_KEY=<paper-api-secret>
ALPACA_PAPER=true
ALPACA_DATA_FEED=iex
ALPACA_STOCK_FEED=iex
ALPACA_OPTIONS_FEED=indicative
```

Never put live credentials in this file. Never commit it.

## 6. Optionally configure the research LLM

The checked configuration enables bounded LLM replacement generation with
`gpt-5`. The trader's runtime decision LLM remains disabled. If no research
provider key is supplied, failed families remain pending and are not retired.

To enable replacement generation, create a separate provider-only secret:

```bash
sudo install -m 0600 /dev/null /etc/alpaca-agent-trading/research-llm.env
sudoedit /etc/alpaca-agent-trading/research-llm.env
```

For OpenAI:

```dotenv
OPENAI_API_KEY=<research-provider-key>
```

Or for Anthropic:

```dotenv
ANTHROPIC_API_KEY=<research-provider-key>
```

Do not place Alpaca credentials in the research LLM file.

## 7. Review the trading configuration

Open `config.yaml` and verify at least these values before building:

```json
{
  "mode": "paper",
  "broker": {"paper": true, "allow_live": false},
  "strategy": {
    "selection_mode": "all_proved",
    "execution_mode": "shares"
  },
  "execution": {"time_in_force": "day"},
  "research": {
    "enabled": true,
    "require_validated_variant": true
  }
}
```

`validate_config` also accepts an optional top-level `costs` block
(`spread_bps`, `slippage_bps`, `fee_bps`) — the expected per-fill cost every
research lane prices its simulated fills through, built by
`research/costs.py::CostModel.from_config`. It defaults to 2.0/3.0/0.5 bps when
the block is absent. These are expectations, not rejection caps, and the
`execution` block bounds them: a model whose expected half-spread plus
slippage exceeds `execution.max_slippage_bps`, or whose expected spread exceeds
`execution.max_spread_bps`, fails validation rather than simulating fills the
runtime would refuse to submit. Sourcing an expected slippage from the cap is
as wrong as ignoring the cap. Leave the block out unless you have measured
values; `python research.py calibrate` (see OPERATIONS.md) is how they are
checked against real fills.

Choose exactly one execution profile per trader process:

- `shares` trades the proved equity variants;
- `options` selects single-leg long listed options for proved option variants.

Keep the checked universe as US equity/ETF symbols. Option symbols are selected
from the provider chain and validated as OCC contracts.

## 8. Export deployment paths and validate Compose

Run these exports in every administrative shell, or put them in a root-owned
deployment environment file:

```bash
cd /opt/alpaca-agent-trading
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
```

If the optional research LLM file was not created, omit the second export.

Validate and build:

```bash
docker compose config --quiet
docker compose --profile research config --quiet
docker compose build
```

Any validation or build failure is a deployment blocker.

## 9. Run local safety and authentication checks in containers

Validate configuration without contacting Alpaca:

```bash
docker compose run --rm trader python main.py check --offline
```

Then authenticate against the paper endpoint:

```bash
docker compose run --rm trader python main.py check
```

The authenticated command may return non-zero on a fresh ledger because no
edge is proved yet. It must still show paper mode, the paper endpoint, account
details, and the selected feeds. A live endpoint or live-account response is a
hard stop.

## 10. Start recording market data

Start the recorder and read-only dashboard first:

```bash
docker compose up -d recorder dashboard
docker compose ps
docker compose logs --tail=100 recorder
```

The recorder writes normalized bars, quotes, and bounded option snapshots to
the `runtime-data` volume, one append-only partition per New York session date
under `runtime/research/recorded/sessions/`, with a sidecar
`.recorder-index.json` holding the watermark and dedup state. Check that its
health becomes healthy and that the logs show successful cycles rather than
credential, feed, timestamp, or data continuity errors. Continuity is judged
against the Alpaca calendar, so holidays and early closes are quiet; a gap
inside a real session still fails closed.

Two environment variables bound option sampling in the recorder process, and
both have working defaults:

- `ALPACA_RECORDER_OPTION_LIMIT` — contracts sampled per side per cycle
  (default 10, hard-capped at 25);
- `ALPACA_RECORDER_OPTION_HOLD_MINUTES` — how long an already sampled contract
  keeps being sampled (default 180), so a trade opened on a contract still has
  quotes at its exit.

Raising either increases the option quote volume written and the disk the
corpus consumes. The shipped Compose `recorder` service does not forward these
into the container, so the defaults apply unless you either add them to that
service's `environment:` block as a separately reviewed change, or set them in
the systemd lane's `EnvironmentFile`. The same is true of the research lane's
`ALPACA_RESEARCH_SESSION_WINDOW` (limit a cycle to the most recent N session
partitions), which the shipped `research` service also does not forward.

## 11. Start the trader and the watchdog in safely blocked mode

```bash
docker compose up -d trader watchdog
docker compose logs --tail=150 trader
docker compose logs --tail=50 watchdog
docker compose exec -T watchdog python deploy/health.py watchdog
```

The `watchdog` is not optional for the options profile: Alpaca offers no
broker-resident stop on options, so an option position's stop is the trader's
own 60-second poller, and the watchdog is the only thing bounding it if that
process dies or hangs. It is a separate container with its own broker session
that can cancel and flatten but never enter. It first takes the mode-scoped run
lock, so a live trader keeps it inert; when the trader heartbeat is stale
beyond `ALPACA_WATCHDOG_MAX_HEARTBEAT_AGE` (default 300s) and the broker still
reports exposure, it cancels resting protective legs and flattens.

Expect `watching` as the steady state; `acted` means it flattened and requires
reconciliation. It does **not** cover a trader that is alive but wedged (it
holds the lock, so the watchdog stays inert), and it cannot cover an
unreachable broker or network — in that window nothing local closes anything.
Run it alongside the trader in both profiles.

On a fresh deployment the trader must remain idle because no validated edge
exists. Do not disable `research.require_validated_variant` to force entries.

The runtime always uses day orders, rejects entries outside the NYSE regular
session, cancels working orders at startup, flattens residual positions, and
forces a close before the session ends.

## 12. Run the first research cycle

Wait until the recorder has produced a meaningful initial corpus. The bare
structural floor is 100 executed trades and 10 sessions per required window
(`ALPACA_FACTORY_MIN_TRADES`, `ALPACA_FACTORY_MIN_SESSIONS`), but the corpus a
first proof actually needs is considerably larger, because those floors apply
to windows that are themselves fractions of the recording:

- the latest 20% of sessions are sealed into the final qualification window
  before any worker runs (`seal_final_window(..., fraction=.2)`), leaving 80%
  as development corpus;
- the development corpus is cut chronologically into a 70% fit and a 30%
  held-out partition (`chronological_split(..., fit_fraction=.7)`), by whole
  sessions;
- **both** partitions must clear the floor, and the held-out one is the binding
  side: 10 held-out sessions out of a 70/30 session split needs about 31
  development sessions, which after the 20% seal needs about 38 recorded
  sessions with trades — roughly two trading months — before a backtest can
  even be adequate;
- the shadow evaluation is a second, strictly later corpus. It is scored as one
  held-out window, but it is sealed the same way, so it needs about 12 further
  recorded sessions of its own;
- walk-forward needs a fit block plus three test blocks, so at least four
  held-out sessions (already implied by the 10-session floor);
- the falsification gate is an empirical p-value against cluster-level
  sign-flip draws, and a cluster is one session. With C held-out sessions no
  draw distribution can put the observed mean below about 2^-C, so alpha 0.05
  is unreachable under about five held-out sessions — again already implied by
  the floor, but it is why more held-out sessions, not more trades per session,
  is what makes the null test decidable.

The trade floors bind separately: 100 executed trades in each of the fit and
held-out partitions, and the arithmetic above assumes every recorded session
produces trades, which no strategy does. Treat "about fifty trading sessions
before a first proof is even possible" as a floor on patience, not a schedule.

Start the scheduled research service:

```bash
docker compose --profile research up -d research
docker compose logs --tail=150 research
```

The scheduler runs the cycle daily at 03:00 UTC by default. To run one cycle
manually now:

```bash
docker compose --profile research run --rm research \
  /bin/bash deploy/research-cycle.sh
```

Inspect the edge and factory ledgers:

```bash
docker compose --profile research run --rm research \
  python research.py edge status
docker compose --profile research run --rm research \
  python research.py factory status
```

`completed_no_edge` is not an operational failure; it means nothing passed all
proof gates yet. `no_data` means the recorder corpus is unavailable or empty
and must be fixed. `failed` is an operational error.

An initial backtest alone cannot validate an edge. Keep the recorder running;
the factory requires a strictly later, unseen shadow tail before validation.

## 13. Verify a proved edge and reports

When an edge qualifies:

1. The SQLite candidate becomes `validated` or `champion`.
2. A deterministic edge proof report is written under
   `research/results/edges/<vehicle>/` inside the research-results volume.
3. The dashboard lists the edge only while its latest verified shadow gate
   still passes.
4. The paper trader may select it on a later cycle under the global risk
   limits.

Reach the dashboard through an SSH tunnel from your workstation:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-address>
```

Then open `http://127.0.0.1:8080` locally.

## 14. Daily operation

Use these commands from `/opt/alpaca-agent-trading` with the deployment-path
exports set:

```bash
docker compose ps
docker compose logs --tail=100 recorder
docker compose logs --tail=100 trader
docker compose logs --tail=100 watchdog
docker compose --profile research logs --tail=100 research
docker compose restart recorder trader watchdog dashboard
```

For an operator-requested exit:

```bash
docker compose run --rm trader \
  python main.py flatten --reason operator
```

A non-zero flatten result requires immediate broker reconciliation.

For a later operator recovery, stop the trader, run the authenticated checks,
flatten only if needed, then authorize the paused runtime and start it again:

```bash
docker compose stop trader
docker compose run --rm trader python main.py check
docker compose run --rm trader python main.py status
# only when positions or working orders remain:
docker compose run --rm trader python main.py flatten --reason operator
docker compose run --rm trader python main.py resume
docker compose start trader
```

`resume` is a strict authenticated, flat-only control command. It performs one
reconciliation plus a final read-only broker confirmation and does not cancel,
submit, or flatten anything. Never edit the runtime state or journal by hand.

## 15. Back up before updates

Back up the Docker volumes containing:

- `runtime/` and the execution journal;
- `research/cache/` and the edge ledger;
- `research/results/`, including generated edge proof reports;
- the reviewed `config.yaml` and deployed Git revision.

The backup must be off-host or on a different durable device. Do not use
`docker compose down -v` and do not prune volumes unless deletion is explicit
and the backup has been tested.

See [OPERATIONS.md](OPERATIONS.md) for reconciliation, backup, recovery, and
incident procedures.

## 16. Optional guarded live mode

Do this only after extended paper validation and a separate review. Do not
reuse the paper runtime directory, secrets file, or running process.

Required live configuration:

```yaml
mode: live
broker:
  paper: false
  allow_live: true
strategy:
  selection_mode: specific
  variant_id: <exact-validated-or-champion-variant>
llm:
  enabled: false
research:
  enabled: true
  require_validated_variant: true
```

The live process also requires:

```bash
export ALPACA_LIVE_ENABLE=true
export ALPACA_PAPER=false
export ALPACA_AGENT_RUNTIME_ROOT=/opt/alpaca-agent-live-runtime
```

Configuration validation rejects `mode: live` with `llm.enabled: true`: the
pinned edge was proven with the deterministic rule and no LLM in the loop, so a
runtime LLM veto would deploy a strategy that never passed the gates. It also
rejects `strategy.execution_mode: options` in live. The bounded research
strategy-replacement adapter of step 6 is a separate offline setting and is
unaffected.

Live startup fails unless the exact named vehicle-local edge has a latest
passing verified shadow proof. The edge is pinned for the process lifetime and
never auto-switches. The account must report `pattern_day_trader=true`.

The shipped Compose services are paper-scoped. Build a separately reviewed
live service/configuration rather than modifying the running paper deployment
in place.

## Local developer setup (without a VM)

For tests and offline development:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
./.venv/bin/python main.py check --offline
./.venv/bin/python -m unittest discover -v
```

Do not use the developer path as an unattended production deployment.
