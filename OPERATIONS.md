# Operations — trader, research, and durable evidence

This is the current runbook. The shipped system uses one demo order path and
seven isolated real-time research evaluators.

## 1. Runtime locations

| Operation | Mac | systemd VM | Docker VM |
| --- | --- | --- | --- |
| Development/tests | Primary | Optional check | Image/CI build |
| Demo trader | Foreground `main.py run` | `okx-trader.service` | `trader` (one replica) |
| Market recorder | Foreground/manual | `okx-recorder.service` | `recorder` |
| Nightly research | Manual | `okx-research.timer` | `research` scheduler |
| Active journal | `runtime/demo/journal.db` | Repository runtime path | `runtime-data` volume |
| Findings store | `research/cache/findings.db` | Repository cache path | `research-cache` volume |
| Dashboard | None | CLI/reports | Loopback-only `dashboard` |
| External backup | Optional mount | Managed data disk | Explicit verified host bind override |

`vm-import/2026-07-30/` is read-only imported evidence for development and
tests. It is never a default runtime, findings, tournament, recorder, or backup
directory.

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
docker compose ps
docker compose logs --tail=100 trader recorder research
docker compose exec trader python main.py status
docker compose exec trader python research.py readiness
```

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
2. `research-loop`, which creates missing deterministic outcomes and reviews
   at most one pending result;
3. corpus statistics and G2 replay when the journal exists;
4. funnel, cadence, sweeps, three-arm analysis, forward qualification, and
   scorecard regeneration;
5. one fresh immutable market-history snapshot under
   `runtime/research/snapshots/<UTC timestamp>` and journal forward-evidence
   export;
6. the exploratory tournament, with immutable per-run artifacts;
7. one new verified backup.

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

Run manually:

```bash
./.venv/bin/bash research/nightly.sh
```

## 4. Real-time strategy experiments

All seven strategies receive the same cycle snapshot and timestamp. Each has
an isolated paper account and durable assignment state. The configured two
workers bound computation; they do not reduce the strategy set.

The active simulator identity is `forward_feed_version: 2`; all registered
forward-model IDs end in `.v2`. Evidence recorded under feed/model v1 remains
immutable history and is not pooled with the corrected execution semantics.

Within each strategy:

1. the stable baseline always remains active;
2. at most one candidate setting is active;
3. an assignment is not complete until both three elapsed days and 100
   comparable paired observations are recorded (unless configuration raises
   those floors);
4. accepted LLM selections queue without preempting the active assignment;
5. restart reconstructs the same active assignment from schema 16;
6. terminal assignments produce one immutable `WORKED`, `FAILED`, or
   `INCONCLUSIVE` outcome with reasons and limitations.

Adequacy is evaluated before performance. An assignment can meet the rotation
clock/count and still be `INCONCLUSIVE` because trades are unresolved, paired
coverage is insufficient, two time segments cannot be formed, provenance is
mixed, or a model/operational check failed.

Run the deterministic closure without an LLM call:

```bash
./.venv/bin/python research.py research-loop --no-review
```

Run closure plus one research-only LLM review:

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

The supplied one-time fixture remains read-only historical evidence. Its legacy
manifest is deliberately not accepted by the current tournament because it
does not prove exact file membership and identities. It may be extracted for
audit without treating it as current provenance-safe scoring input:

```bash
fixture=$(mktemp -d)
tar -xzf vm-import/2026-07-30/okx-research-files-2026-07-30.tgz \
  -C "$fixture"
```

Copy `vm-import/2026-07-30/okx-findings-2026-07-30.db` to the temporary
`$fixture/findings.db` first when store-backed history is needed. Do not open
the supplied WAL-backed database in place for a write operation.

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
- research manifest JSON files and `forward_evidence.json`; and
- all files under `research/results`.

It includes snapshot manifests but not the complete CSV trees beneath
`runtime/research/snapshots/`. Those immutable raw snapshots remain on the
runtime volume/VM and are read there by the tournament. Much of the history can
be downloaded again, but exact short-retention inputs and universe membership
may not be reproducible. If exact snapshot preservation is required before VM
deletion, copy the required timestamped directories to separately retained
storage or extend and verify the backup implementation before calling the
procedure zero-data-loss.

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
./.venv/bin/python research.py t3-packet \
  --variant <qualified-variant-id> --scope <scope> \
  --reviewed-by <reviewer> --registry-change-ref <change-reference>
./.venv/bin/python research.py report
```

`forward-qualify` and T3 packets remain evidence tooling. They do not edit
`agent/registry.py`, change `config.yaml`, switch the demo strategy, or deploy
an edge to live trading.

## 8. Interpreting results

- `candidate`/`testing`: registered research identity, not an edge.
- `WORKED`: conservative deterministic experiment gates passed; creates only
  `RESEARCH_ONLY` edge evidence.
- `FAILED`: adequate evidence or a persisted gate showed failure.
- `INCONCLUSIVE`: evidence cannot support success or failure.
- `QUALIFIED`: older forward-axis research event, not an order instruction.
- `REVOKED`: that evidence/account window is invalid and must not be reused.

Both positive and negative findings remain in the store. Never infer an edge
from a point estimate, a tournament rank, or an LLM explanation.

## 9. Recovery and handoff

Copy data; do not relocate the authoritative VM files. Preserve the active
journal, findings DB, recorder data, manifests/forward evidence, all tournament
run directories, and the backup manifest/checksum. The verified backup command
captures these supported sources; committed `findings/` scorecards remain in
Git.

Before deleting or rebuilding the VM:

1. require a recent `external_mounted` backup and rerun `verify-backup`;
2. confirm the managed disk mount, separate `st_dev`, free space, and Azure
   **Detach** deletion behavior;
3. preserve required `runtime/research/snapshots/<UTC timestamp>` directories
   separately when exact raw-input retention is required; and
4. preferably verify an Azure snapshot or attach a retained copy to a recovery
   VM and read its manifests there.

## 10. Troubleshooting

| Symptom | Meaning/action |
| --- | --- |
| G2 failed | Stop authoritative interpretation and investigate replay mismatch |
| `INSUFFICIENT_SAMPLE` | Keep collecting; no edge verdict exists yet |
| External backup BLOCKED | Provision/mount a different-device destination and run a required external backup |
| `configured_local` | Explicit path shares a source filesystem; not VM-loss protection |
| Research service red | Inspect its journal; trader operation is separate |
| Review deferred | Deterministic outcome is safe; retry `research-loop` later |
| Tournament benchmark failed | Keep the run as failure evidence; do not interpret rankings |
| Findings DB missing | Check `research.findings_store`; there is no temporary fallback |
| Trader stopped after Compose update | Expected safe `SIGTERM` pause; run `main.py check`, then explicitly `main.py resume` |
| Recorder unhealthy | Trader startup remains blocked until a fresh recorder CSV exists |
| Dashboard unreachable remotely | Expected loopback binding; use an SSH tunnel or private VPN |
| Docker backup says different device | This is not off-host proof; verify host/cloud retention separately |

The optional B7.5 maker-first order primitive remains disabled by default.
`cycle.decision_interval_seconds`, `maker_first_enabled`,
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
