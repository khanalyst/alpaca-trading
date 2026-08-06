# Operations — trader, research, and durable evidence

This is the operational authority. The shipped system uses one demo order path,
four isolated deterministic realtime research evaluators, and three registered
models that are offline-only.

## 1. Runtime locations

| Operation | Mac | Legacy systemd VM | Production Docker VM |
| --- | --- | --- | --- |
| Development/tests | Primary | Optional check | Image/CI build |
| Demo trader | Foreground `main.py run` | `okx-trader.service` | `trader` (one replica) |
| Market recorder | Foreground/manual | `okx-recorder.service` | `recorder` |
| Nightly research | Manual | `okx-research.timer` | `research` scheduler |
| Active journal | `runtime/demo/journal.db` | Repository runtime path | `runtime-data` volume |
| Findings store | `research/cache/findings.db` | Repository cache path | `research-cache` volume |
| Dashboard | None | CLI/reports | Loopback-only `dashboard` |
| External backup | Optional mount | Managed data disk | Explicit verified host bind override |

An ignored local `vm-import/` directory, if present, is optional read-only
historical data. Current operation never requires it or uses it as a runtime,
findings, tournament, recorder, or backup directory.

## 2. Daily checks

Mac:

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py status
./.venv/bin/python main.py strategies --verbose
./.venv/bin/python research.py readiness
./.venv/bin/python research.py corpus stats
```

VM:

```bash
sudo systemctl status okx-recorder okx-trader okx-research.timer
findmnt --target /srv/okx-agent-research-backup
df -h /srv/okx-agent-research-backup
sudo journalctl -u okx-trader -n 100 --no-pager
sudo journalctl -u okx-recorder -n 100 --no-pager
sudo journalctl -u okx-research -n 200 --no-pager
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python \
  /opt/okx-agent-crypto/research.py readiness \
  --db /opt/okx-agent-crypto/runtime/demo/journal.db
```

Docker VM:

```bash
cd /opt/okx-agent-crypto
export OKX_AGENT_SECRET_FILE=/etc/okx-agent-crypto/agent.env
export OKX_EXTERNAL_BACKUP_PATH=/srv/okx-agent-research-backup
sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml ps
sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml \
  logs --tail=100 trader recorder research dashboard
sudo -E docker compose -f compose.yaml \
  -f deploy/compose.external-backup.yaml \
  exec -T trader python main.py status
```

Check the `main` deployment automation:

```bash
sudo systemctl status okx-agent-update.timer --no-pager
systemctl list-timers okx-agent-update.timer --all
sudo journalctl -u okx-agent-update.service -n 100 --no-pager

APP_DIR=/opt/okx-agent-crypto
sudo -u okx git -C "$APP_DIR" fetch origin main
REMOTE="$(sudo -u okx git -C "$APP_DIR" rev-parse origin/main)"
LOCAL="$(sudo -u okx git -C "$APP_DIR" rev-parse HEAD)"
DEPLOYED="$(sudo cat /var/lib/okx-agent-updater/deployed-revision 2>/dev/null \
  || echo NOT_DEPLOYED)"
printf 'GitHub:   %s\nVM:       %s\nDeployed: %s\n' \
  "$REMOTE" "$LOCAL" "$DEPLOYED"
```

All three revisions must match. The timer polls every five minutes; start
`okx-agent-update.service` manually when an immediate post-merge deployment is
required. The service is a oneshot and being inactive after a successful run
is expected.

The dashboard is available only through host loopback. Use an SSH tunnel to
`127.0.0.1:8080`; do not publish port 8080 on a public interface. It exposes
only GET health/state/report APIs and is not given the Compose secret.

`readiness` returns nonzero for a failed research gate or when no verified
`external_mounted` backup exists. A default/configured-local backup is useful
for recovery from a bad query, but it does not make deletion of the VM safe.
The mount and capacity checks must succeed before the nightly research result
is treated as durably backed up.

## 3. Nightly workflow and exit behavior

`research/nightly.sh` runs in this order:

1. readiness; failure is remembered while the run continues;
2. `review-staged`, which gives each machine-authored mechanism a coded
   verdict and retires the ones that are finished;
3. `author`, which proposes new mechanisms and stages the ones that validate.
   It runs after the verdicts so the next generation sees the latest coded
   failures;
4. `qualify-staged`, which starts isolated local PAPER only for staged
   mechanisms whose fixed-harness evidence is supported;
5. `research-loop`, which creates missing deterministic outcomes and reviews a
   bounded queue (default eight) of pending results; one failed provider call
   is persisted and does not stop later items;
6. corpus statistics and G2 replay when the journal exists;
7. funnel, cadence, sweeps, three-arm analysis, forward qualification,
   fail-closed draft review-artifact preparation, the candidate shortlist,
   and scorecard regeneration;
8. one fresh immutable market-history snapshot under
   `runtime/research/snapshots/<UTC timestamp>` and journal forward-evidence
   export;
9. the exploratory tournament, with immutable per-run artifacts;
10. one new verified backup.

Exact exit behavior:

- a real G2 failure stops immediately with exit 3 before later research;
- G2 exit 4 means collecting; gated commands may refuse, but the workflow
  continues to tournament and backup;
- an LLM review/provider/parse failure is persisted for retry and does not
  discard the deterministic outcome or abort the workflow;
- backup failure makes the research service nonzero (exit 5 unless readiness
  was already nonzero);
- a remembered readiness failure produces final exit 4 after the backup;
- otherwise the tournament's exit status is returned.

The research service is separate from `okx-trader.service`/the `trader`
container. Its failure is visible in systemd or the Compose health/dashboard
state and does not authorize trading or disappear into trader logs.
While the nightly child is running, the scheduler atomically refreshes
`runtime/health/research.json` every 30 seconds. The 180-second health window
therefore remains green for a legitimate long run and turns stale only when
the scheduler can no longer supervise and refresh the child.

Run manually:

```bash
./research/nightly.sh
```

## 4. Real-time strategy experiments

The four realtime strategies receive the same cycle snapshot and timestamp and
use deterministic contract proposals. Each has an isolated paper account and
durable assignment state. A bounded batch of pre-registered candidates shares
one stable baseline in each lane (four by shipped config, hard cap eight).
Four packet workers bound computation; they do not reduce the realtime lane
set. The lanes are logically isolated, and packet computation may overlap while
durable writes remain serialized. Four simultaneous SQLite writers are not
required for correctness. The
registered `funding-carry`, `funding-unwind`, and `trend-multiday` models are
offline-only because their holding horizons cannot reach the closed-trade floor
in a practical realtime assignment.

The 60-second housekeeping loop remains the safety/mark cadence. The model
decision throttle is an elapsed-time check: `decision_interval_seconds: 300`
blocks a new analyst call until at least 95% of 300 seconds has elapsed since
the prior decision. It is not aligned to wall-clock or signal-bar boundaries;
safety, marks, exits, reconciliation, and shadow advancement continue on the
shorter loop.

G2 compares the full canonical pre-risk proposal identity (cycle, symbol,
direction, setup identity/type, signal timestamp, strategy version, and
baseline variant) symmetrically with replay keys. It requires a non-vacuous
exact match and fails closed on malformed, duplicate, missing, or extra
identities. Outcome-resolution gaps remain diagnostics rather than proposal
mismatches. A failed, stale, or vacuous G2 blocks downstream journal evidence
from being treated as authoritative.

The active research scope is `forward_feed_version: 8`. Feed v8 keeps the
deterministic four realtime lanes and adds the real liquidation flow and the
pre-registered conditioning axes; the active analyst's own decisions remain in
a separate `:llm` scope and are not pooled with lane evidence. Feeds v1-v7
remain immutable historical evidence. Feed v4 is the market-data plumbing
repair feed, feed v5 is the immutable-provenance fork, and feed v6 is the
deterministic four-lane fork; no older evidence is migrated or pooled with v8.

Within each strategy:

1. the stable baseline always remains active;
2. a bounded batch of pre-registered candidate settings is active, sharing
   that baseline (four by shipped config, hard cap eight per lane);
3. an assignment is not complete until both ten elapsed days and 100
   comparable paired observations are recorded (unless configuration raises
   those floors);
4. each individual assignment still tests only one candidate setting, and
   accepted LLM selections queue without preempting active assignments;
5. restart reconstructs the same active batch from schema 16;
6. terminal assignments produce one immutable `WORKED`, `FAILED`, or
   `INCONCLUSIVE` outcome with reasons and limitations.

Adequacy is evaluated before performance. An assignment can meet the rotation
clock/count and still be `INCONCLUSIVE` because trades are unresolved, paired
coverage is insufficient, two time segments cannot be formed, provenance is
mixed, or a model/operational check failed.

A `WORKED` outcome saves an immutable `RESEARCH_ONLY` `EDGE_CANDIDATE` lead
with `promotion_allowed: false`; it does not satisfy the current v8
forward-qualification protocol by itself. Qualification still requires the
eligible completed assignment attempts, their contemporaneous baselines,
held-out confirmation, and family correction. The paired cluster sign-flip
test is conditional on cluster-delta sign exchangeability under a symmetric
null, as documented in `research/protocol.md`.

Run the deterministic closure without an LLM call:

```bash
./.venv/bin/python research.py research-loop --no-review
```

Run closure plus a bounded batch of research-only LLM reviews (default eight):

```bash
./.venv/bin/python research.py research-loop
```

## 5. Tournament

The tournament needs an extracted research data directory containing the
historical files and manifest, not a journal database. Current inputs must have
an `okx-history-snapshot.v1` manifest that records every CSV's relative path,
SHA-256, row count, and timestamp range. Missing, extra, changed, linked, or
legacy files are refused before scoring.

On the VM:

```bash
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python research/tournament.py \
  --data runtime/research/snapshots/<UTC-timestamp> \
  --store research/cache/findings.db \
  --out research/results/tournament \
  --top-n 5 --workers 2
```

Every invocation creates:

```text
research/results/tournament/runs/<timestamp>-<run-id>/
  RUN.json
  INVOCATION.json
  INPUTS.json
  leaderboard.json
  REPORT.md
  ERRORS.json
  COMPLETION.json
```

Success and failure runs are both retained. The top-level `REPORT.md` and
`leaderboard.json` are latest-view copies only.

Each nightly run creates a new timestamped directory. `DATA_DIR`, when set,
names that run's exact output directory; it must be absent or empty. The
downloader refuses a non-empty directory instead of mixing old membership with
a new universe. A partial failed download remains failure evidence and is not a
valid tournament input; the next run uses another fresh directory.

Ignored local `vm-import/` history, if present, is optional audit material
only. Its legacy provenance is not accepted as current tournament input, and
no nightly or recovery step depends on it.

## 6. Verified backups

The default command creates a versioned `local_default` backup:

```bash
./.venv/bin/python research.py backup
```

For VM-loss protection, provision the managed disk exactly as described in
[SETUP.md](SETUP.md#6-provision-the-managed-research-backup-disk). The deployed
mount is `/srv/okx-agent-research-backup`. Do not use `/mnt`, which is the
Azure temporary resource disk on this VM.

Create a required external backup manually:

```bash
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python research.py backup \
  --store research/cache/findings.db \
  --journal runtime/demo/journal.db \
  --mode demo \
  --target /srv/okx-agent-research-backup \
  --require-external
```

The target must already exist. The command refuses to create an explicit
target, refuses same-device `configured_local` targets when external is
required, snapshots SQLite through the online backup API, writes checksums,
checks SQLite integrity/foreign keys, and appends backup history. It never
prunes older backup directories.

Verify a captured backup later:

```bash
cd /opt/okx-agent-crypto
sudo -u okx find /srv/okx-agent-research-backup \
  -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
sudo -u okx .venv/bin/python research.py verify-backup \
  /srv/okx-agent-research-backup/<backup-directory>
```

The persistent systemd override must contain both the mount dependency and the
strict backup settings:

```bash
sudo systemctl cat okx-research.service
sudo systemctl show okx-research.service -p Environment
```

Expected override:

```ini
[Unit]
RequiresMountsFor=/srv/okx-agent-research-backup

[Service]
Environment=BACKUP_TARGET=/srv/okx-agent-research-backup
Environment=REQUIRE_EXTERNAL_BACKUP=1
```

### Backup health and capacity verification

Run these after provisioning, after every reboot, after disk maintenance, and
at least weekly:

```bash
lsblk -f /dev/sdc
grep -F '/srv/okx-agent-research-backup' /etc/fstab
sudo findmnt --verify --verbose
findmnt --target /srv/okx-agent-research-backup \
  -o SOURCE,TARGET,FSTYPE,OPTIONS
df -h /srv/okx-agent-research-backup
sudo du -sh /srv/okx-agent-research-backup
stat -c '%d %n' \
  /opt/okx-agent-crypto \
  /srv/okx-agent-research-backup
sudo -u okx test -w /srv/okx-agent-research-backup \
  && echo 'backup mount writable by okx'
sudo systemctl status okx-research.timer --no-pager
sudo -u okx .venv/bin/python research.py readiness \
  --db runtime/demo/journal.db
```

The `findmnt` source should be the managed-disk partition, normally
`/dev/sdc1`; it must not be `/dev/sdb1`. The two `stat` device numbers must
differ. Readiness must show `external backup PASS`. Because every backup is a
new append-only directory and no automatic pruning occurs, investigate growth
and expand the managed disk before it fills.

Run and watch an immediate research cycle when validating a deployment:

```bash
sudo systemctl reset-failed okx-research.service
sudo systemctl start --no-block okx-research.service
sudo journalctl -fu okx-research.service
```

Press `Ctrl+C` to leave the journal follow; the service continues. A first run
can still exit 4 because readiness is evaluated before that run creates its
backup. Rerun readiness after the backup is verified.

Configuration/path alone is not off-host proof. The mount must report an
`st_dev` different from the repository and every included source. Schema 14
reclassifies legacy unproven “external” records as `configured_local`.
Different-device evidence does not prove remote retention; the operator must
also confirm in Azure that the managed disk uses **Detach**/has **Delete with
VM** disabled. A periodic Azure snapshot or recovery-VM attach test verifies
that the backup survives independently of the original VM.

### Backup scope

The supported verified backup includes:

- `research/cache/findings.db` through SQLite's online backup API;
- the active `runtime/demo/journal.db` through the same API;
- files under `runtime/research/recorded`;
- every regular file in each completed immutable tree under
  `runtime/research/snapshots` (manifest present and no in-progress marker);
- research manifest JSON files and `forward_evidence.json`; and
- all files under `research/results`.

Snapshot CSVs and their manifests retain the same path beneath
`files/runtime/research/snapshots/` in the backup. `verify-backup` size- and
SHA-256-checks each one, so a missing or changed raw input invalidates the
backup. A directory still carrying `.download-in-progress`, or lacking a final
manifest, is an incomplete download rather than immutable evidence and is not
included.

## 7. Other research commands

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify --scope <scope>
./.venv/bin/python research.py prepare-review-artifacts
./.venv/bin/python research.py t3-packet \
  --variant <qualified-variant-id> --scope <scope> \
  --reviewed-by <reviewer> --registry-change-ref <change-reference>
./.venv/bin/python research.py report
```

`prepare-review-artifacts` considers only variants with current v8
qualification. It fails closed unless persisted edge evidence and every
non-manual T3 checklist item validate, and it creates only an idempotent,
immutable/content-addressed `DRAFT_REVIEW_REQUIRED` artifact. It cannot mark
manual review complete, edit `agent/registry.py` or `config.yaml`, switch the
demo strategy, or deploy an edge to live trading. The reviewed `t3-packet`
record and any registry/configuration change remain explicit operator actions.

### 7.1 Run one reviewed candidate on OKX demo

This path is opt-in and demo-only. Do not use it for a merely `WORKED`
candidate or a draft packet. Before starting it:

1. stop the ordinary trader and confirm there is no second loop;
2. keep `mode: demo` and run `./.venv/bin/python main.py check` with the
   intended Read+Trade, no-Withdraw OKX demo key;
3. use the `account_fingerprint` bound in `runtime/demo/state.json` as the
   expected account fingerprint;
4. require a current, non-revoked qualification and a content-addressed
   `REVIEWED` T3 packet for the exact variant and scope; and
5. confirm local PAPER and the OKX demo account are flat, with no open regular
   or algorithmic orders.

```bash
./.venv/bin/python main.py run --candidate-demo \
  --variant-id <qualified-variant-id> \
  --scope-key <authoritative-paper-scope> \
  --packet-ref t3-packet:<reviewed-packet-hash> \
  --expected-demo-account-fingerprint <account_fingerprint>
```

Authorization runs before exchange/model trading clients continue. It
revalidates the reviewed packet and artifact, exact current qualification,
current positive PAPER summary and closed-trade floor, PAPER flatness, and all
executable identity hashes. Startup then performs read-only account,
position, regular-order, algo-order, and local-state checks. Missing APIs,
unexpected responses, identity drift, non-flat state, or any open order fails
closed. The variant is applied in memory only; no registry, configuration, or
live-capital authority is changed.

After a successful preflight, confirm the attributable receipt:

```bash
sqlite3 runtime/demo/journal.db \
  "SELECT datetime(ts,'unixepoch'), variant_id, account_fingerprint, payload FROM events WHERE kind='demo_candidate_authorization' ORDER BY ts DESC LIMIT 1;"
```

To stop the rehearsal, use `./.venv/bin/python main.py pause --flatten` and
verify OKX has no remaining positions or orders. If flattening is incomplete,
close them manually in OKX and keep the agent paused. A local test pass does
not replace one real credentialed OKX demo smoke test.

### 7.2 Staged mechanisms

Every registered mechanism used to be a hand-written Python function, which
capped the registry at three hypotheses attached to one strategy. A mechanism
is now expressible as data - a claim, the payer, a falsifier and comparisons
over named market fields - and compiles into the same callable the
hand-written contracts implement.

```bash
./.venv/bin/python research.py stage-seed         # the version-controlled ones
./.venv/bin/python research.py author --dry-run   # what the proposer is asked
./.venv/bin/python research.py staged             # what is registered
./.venv/bin/python research.py review-staged --dry-run
./.venv/bin/python research.py qualify-staged
./.venv/bin/python research.py shortlist
```

`stage-seed` registers the hand-written pre-registrations in
`research/staged/pre-registered.yaml`. It is idempotent - an already
registered claim is reported and skipped - so it belongs in the deploy
sequence rather than being run once by hand. An entry marked `deferred` is
reported and never registered, which is how a claim whose threshold cannot
yet be calibrated stays visible without occupying a lane that would never
fire. Unlike `author`, a rejected entry here exits nonzero: a broken
pre-registration is a mistake in version control, not a transient provider
failure.

Staged mechanisms run in their own `:staged` scope, with one isolated
candidate paper account and one paired neutral baseline account per
mechanism, on a single fixed measurement harness: first observed price after
the signal, a structure stop with a one-ATR minimum, a 2R target, observed
taker costs both sides and a 24h timeout. The harness and proposal identities
are identical across the pair, so a difference is attributable to the
mechanism rather than a lucky stop distance.

They enter at `T1_HYPOTHESIS` and cannot rise. Live still requires
`T3_VALIDATED` and a reviewed content-addressed packet, so nothing here
shortens the path to capital; what it removes is the developer in the middle
of measuring an idea.

Mechanisms move through three funnel stages rather than facing the strictest
gate from the first bar, which is why nothing used to finish: `SCREEN` under
30 trades can retire a clearly adverse mechanism early and can never promote
one, `MEASURE` accumulates to 100 with full costs, and `CONFIRM` applies the
held-out window and the family correction.

`SUPPORTED` additionally requires surviving Benjamini-Hochberg false-discovery
control across every candidate screened alongside it. Screening fifty
candidates at 5% produces two or three that look significant with no edge at
all; the report states the family size and adjusted p-value so a reader can
see how much search was paid for. Arms too thin to judge are excluded from the
family, because counting tests that were never run would make the correction
look stricter than the search actually was.

A registered claim is immutable at the database level. Rewording one after
results exist is refused by a trigger, because a claim that can be edited
afterwards cannot be told apart from one retro-fitted to the result.

The authoring model receives bounded persisted evidence rather than only field
names: opportunity and firing rates, conditional returns, missing-data and
null/near-miss reasons, fit versus held-out results, segment summaries, feature
correlations when persisted, and tested mechanism families. It may use only
the deterministic contract DSL. Supported bounded primitives include lagged
values, rolling changes, percentile ranks, volatility/regime filters, event
sequences, feature interactions, order-book imbalance and liquidity states.
Cross-sectional rank is rejected until a valid multi-symbol context exists.
The proposer cannot choose exits, horizons, stops, targets, sizing, or network
operations.

Opportunity quality is scored before a mechanism can be supported: eligible
declines contribute zero return, firing and coverage floors must be met,
candidate results must be matched to a contemporaneous neutral baseline, and
family-level multiple-testing correction and held-out confirmation still apply.
The fixed staged harness is a signal screen; automatic strategy-specific exit
and holding-period optimization is not yet wired as a second stage.

### 7.3 Running a contract without an analyst

`strategy.execution_mode` selects what decides on the order path. `analyst`
makes one LLM call per decision cycle. `deterministic` makes no LLM call at
all: the strategy's own forward contract proposes, and risk and execution
apply unchanged. `shadow_only` is shipped and has no order path: nothing
proposes and nothing opens.

Use `deterministic` when a strategy has earned promotion on shadow evidence. That evidence
was produced by its deterministic contract, so trading it under an analyst
would put an unmeasured layer on top of the thing that justified the
promotion. It is also the only way a strategy other than `momentum` can occupy
the order path at all: every other registered strategy has no analyst prompt.

```yaml
strategy:
  id: ls-ratio-fade
  version: v1
  signal_timeframe: 1h
  execution_mode: deterministic
```

Configuration refuses to start when the named strategy has no complete
forward contract, and the value must be exactly `analyst`, `deterministic` or
`shadow_only` - no case or whitespace normalisation, so a typo cannot become a
mode change nobody reviewed. Tier gating is unchanged: live still requires
`T3_VALIDATED` and a reviewed packet.

`deterministic` is the shipped state, running `ls-ratio-fade/v1`. It replaced
`momentum`, which is `T0_REJECTED`, returned -8.97% over 2026-07-29..08-05,
and at that rate reaches `risk.max_drawdown_pct` - which flattens the book and
self-kills the process, ending the research collection every other lane
depends on. The replacement is a choice among unproven mechanisms rather than
a promotion: `ls-ratio-fade` measures -0.153R at its shipped thresholds over
independent 48h episodes, which is not significantly different from random.
`research/plan/order-path-succession.md` holds the comparison and states,
pre-committed, what would earn the seat on evidence.

`shadow_only` is the state to set when nothing should trade at all. Research
lanes run unchanged in it, and open positions still exit through exchange
stops and targets, `max_hold_hours` and every risk reduction path; only
discretionary opens and closes have no source.

## 8. Interpreting results

- `candidate`/`testing`: registered research identity, not an edge.
- `WORKED`: conservative deterministic experiment gates passed; creates only
  `RESEARCH_ONLY` edge evidence.
- `FAILED`: adequate evidence or a persisted gate showed failure.
- `INCONCLUSIVE`: evidence cannot support success or failure.
- `QUALIFIED`: current v8 forward-axis research event, not an order instruction.
- `REVOKED`: that evidence/account window is invalid and must not be reused.

Both positive and negative findings remain in the store. Never infer an edge
from a point estimate, a tournament rank, or an LLM explanation.

## 9. Recovery and handoff

Copy data; do not relocate the authoritative VM files. Preserve the active
journal, findings DB, recorder data, immutable raw snapshots,
manifests/forward evidence, all tournament run directories, and the backup
manifest/checksum. The verified backup command captures these supported
sources; committed `findings/` scorecards remain in Git.

Before deleting or rebuilding the VM:

1. require a recent `external_mounted` backup and rerun `verify-backup`;
2. confirm the managed disk mount, separate `st_dev`, free space, and Azure
   **Detach** deletion behavior;
3. inspect the backup manifest for required
   `files/runtime/research/snapshots/<UTC timestamp>` inputs; and
4. preferably verify an Azure snapshot or attach a retained copy to a recovery
   VM and read its manifests there.

## 10. Troubleshooting

| Symptom | Meaning/action |
| --- | --- |
| G2 failed | Stop authoritative interpretation and investigate proposal-key replay mismatch |
| `INSUFFICIENT_SAMPLE` | Keep collecting; no edge verdict exists yet |
| External backup BLOCKED | Provision/mount a different-device destination and run a required external backup |
| `configured_local` | Explicit path shares a source filesystem; not VM-loss protection |
| Research service red | Inspect its journal; trader operation is separate |
| Review deferred | Deterministic outcome is safe; retry `research-loop` later |
| Tournament benchmark failed | Keep the run as failure evidence; do not interpret rankings |
| Findings DB missing | Check `research.findings_store`; there is no temporary fallback |
| All shadow variants are `VETOED` for missing book levels or basis | Treat this as market-data plumbing failure, not evidence that every strategy failed. Verify `book_bid_levels`, `book_ask_levels`, and `perp_index_basis_pct`; repaired observations belong to feed v4, feed v5 is the immutable-provenance fork, feed v6 is the deterministic four-lane realtime fork, feed v7 added the real liquidation flow and conditioning axes, feed v8 repairs the depth-ladder delivery that silently starved six of seven strategies, and all v1-v7 rows remain historical. |
| Trader stopped after Compose update | Expected safe `SIGTERM` pause; run `main.py check`, then explicitly `main.py resume` |
| Recorder unhealthy | Trader startup remains blocked until a fresh recorder CSV exists |
| Dashboard unreachable remotely | Expected loopback binding; use an SSH tunnel or private VPN |
| VM `curl /healthz` works but Mac dashboard does not | The dashboard container is healthy; repair/restart the Mac SSH `LocalForward` tunnel and check whether local port 8080 is occupied. |
| Dashboard scheduler is `missing` | The legacy systemd timer does not maintain the Compose scheduler heartbeat. Under Compose inspect the `research` container and `runtime/health/research.json`. |
| SSH reports `Broken pipe` | The transport expired; VM services continue. Configure `ServerAliveInterval 30` and `ServerAliveCountMax 6` on the Mac. |
| SSH reports `Permission denied (publickey)` | Repair `azureuser`'s public key and `.ssh` ownership/modes through Azure VMAccess or Run Command. Never copy the private key to the VM. |
| `/opt/...` command fails on the Mac | `/opt/okx-agent-crypto` is a VM path. SSH to the VM and run it there. |
| Migration cannot read systemd runtime files | Stop the services, grant temporary `u:10001:rX` ACL access, copy as UID 10001, then remove the ACL. |
| Migration `chown` is not permitted | Do not force ownership changes. Copy as UID/GID 10001 without `cp -a`; `findings/` is initialized from the image and regenerated. |
| Docker backup says different device | This is not off-host proof; verify host/cloud retention separately |
| GitHub SHA differs from VM SHA | The timer has not fetched/deployed `main`; inspect `okx-agent-update.timer` and the update-service journal. |
| VM SHA differs from deployed marker | Git fast-forward succeeded but build, preflight, or Compose startup failed; the journal contains the failing stage. |
| Deployed marker is `NOT_DEPLOYED` | No update has completed successfully since the updater was installed. Run the service manually and inspect its journal. |
| Updater fails with `203/EXEC` or `Exec format error` | `/usr/local/sbin/okx-agent-sync` is malformed or lacks a valid first-line shebang. Validate with `bash -n`; the unit should use `ExecStart=/bin/bash ...`. |
| Compose preflight cannot read `/app/config.yaml` | Compose ignores config UID/GID/mode fields outside Swarm. Keep tracked `config.yaml` mode `0644` and the updater at `umask 022`. |
| Compose warns that config/secret UID/GID/mode are ignored | Expected for file-backed local Compose. Enforce host permissions: secret `10001:10001` mode `0400`; tracked `config.yaml` mode `0644`. |
| `docker compose ps` is empty after an update | The updater failed before `compose up`; fix the first journal/preflight error and rerun the same revision. |

The B7.5 maker-first order primitive is enabled in the shipped demo
configuration and rejected in live mode. `cycle.decision_interval_seconds`, `maker_first_enabled`,
`maker_first_wait_seconds`, `research.shadow_enabled`,
`research.shadow_variants`, and `research.shadow_budget_ms` are documented
configuration controls; the shipped values/defaults are summarized in
[README.md](README.md).


## 11. Inspecting funding-unwind assignments

Example inspection of the most recent funding-unwind assignments:

```bash
cd /opt/okx-agent-crypto

sudo -u okx .venv/bin/python -c '
from research.findings import FindingsStore
store = FindingsStore("research/cache/findings.db")
rows = store.experiment_assignments(strategy_id="funding-unwind")
keys = (
    "candidate_variant_id", "setting_id", "status",
    "observed_count", "minimum_observations",
    "duration_satisfied", "ready_to_complete"
)
print(*[{key: row.get(key) for key in keys} for row in rows[-5:]], sep="\n")
'
```
