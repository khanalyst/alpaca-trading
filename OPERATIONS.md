# Operations — trader, research, and durable evidence

This is the current runbook. The shipped system uses one demo order path and
seven isolated real-time research evaluators.

## 1. Runtime locations

| Operation | Mac | Azure VM |
| --- | --- | --- |
| Development/tests | Primary | Optional pre-deployment check |
| Demo trader | Foreground `main.py run` | `okx-trader.service` |
| Market recorder | Foreground/manual | `okx-recorder.service` |
| Nightly research | Manual | `okx-research.timer` |
| Active journal | `runtime/demo/journal.db` | `/opt/okx-agent-crypto/runtime/demo/journal.db` |
| Findings store | `research/cache/findings.db` | Same repository-relative path on the VM |
| External backup | Optional mounted filesystem | Required for VM-loss protection |

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
sudo journalctl -u okx-trader -n 100 --no-pager
sudo journalctl -u okx-recorder -n 100 --no-pager
sudo journalctl -u okx-research -n 200 --no-pager
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python \
  /opt/okx-agent-crypto/research.py readiness \
  --db /opt/okx-agent-crypto/runtime/demo/journal.db
```

`readiness` returns nonzero for a failed research gate or when no verified
`external_mounted` backup exists. A default/configured-local backup is useful
for recovery from a bad query, but it does not make deletion of the VM safe.

## 3. Nightly workflow and exit behavior

`research/nightly.sh` runs in this order:

1. readiness; failure is remembered while the run continues;
2. `research-loop`, which creates missing deterministic outcomes and reviews
   at most one pending result;
3. corpus statistics and G2 replay when the journal exists;
4. funnel, cadence, sweeps, three-arm analysis, forward qualification, and
   scorecard regeneration;
5. market-history refresh and journal forward-evidence export;
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

The research service is separate from `okx-trader.service`. Its failure is
visible in systemd and does not restart or stop the trader.

Run manually:

```bash
./.venv/bin/bash research/nightly.sh
```

## 4. Real-time strategy experiments

All seven strategies receive the same cycle snapshot and timestamp. Each has
an isolated paper account and durable assignment state. The configured two
workers bound computation; they do not reduce the strategy set.

Within each strategy:

1. the stable baseline always remains active;
2. at most one candidate setting is active;
3. an assignment is not complete until both three elapsed days and 100
   comparable paired observations are recorded (unless configuration raises
   those floors);
4. accepted LLM selections queue without preempting the active assignment;
5. restart reconstructs the same active assignment from schema 14;
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
historical files and manifest, not a journal database.

On the VM:

```bash
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python research/tournament.py \
  --data runtime/research/data \
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

Against the supplied one-time fixture, never write into the fixture itself:

```bash
fixture=$(mktemp -d)
tar -xzf vm-import/2026-07-30/okx-research-files-2026-07-30.tgz \
  -C "$fixture"
./.venv/bin/python research/tournament.py \
  --data "$fixture/runtime/research/data" \
  --store "$fixture/findings.db" \
  --out "$fixture/tournament" \
  --top-n 5 --workers 2
```

Copy `vm-import/2026-07-30/okx-findings-2026-07-30.db` to the temporary
`$fixture/findings.db` first when store-backed history is needed. Do not open
the supplied WAL-backed database in place for a write operation.

## 6. Verified backups

The default command creates a versioned `local_default` backup:

```bash
./.venv/bin/python research.py backup
```

For VM-loss protection, provision and mount a separate destination first, then
require positive different-device evidence:

```bash
./.venv/bin/python research.py backup \
  --target /mnt/off-host/okx-agent-research \
  --require-external
```

The target must already exist. The command refuses to create an explicit
target, refuses same-device `configured_local` targets when external is
required, snapshots SQLite through the online backup API, writes checksums,
checks SQLite integrity/foreign keys, and appends backup history. It never
prunes older backup directories.

Verify a captured backup later:

```bash
./.venv/bin/python research.py verify-backup \
  /mnt/off-host/okx-agent-research/<backup-directory>
```

To require the mounted destination in the systemd nightly service:

```bash
sudo systemctl edit okx-research.service
```

Add:

```ini
[Service]
Environment=BACKUP_TARGET=/mnt/off-host/okx-agent-research
Environment=REQUIRE_EXTERNAL_BACKUP=1
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl start okx-research.service
sudo journalctl -u okx-research -n 200 --no-pager
```

Configuration/path alone is not off-host proof. The mount must report an
`st_dev` different from the repository and every included source. Schema 14
reclassifies legacy unproven “external” records as `configured_local`.
Different-device evidence does not prove remote retention; the operator must
also confirm the destination survives loss/deletion of the VM.

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

Before deleting or rebuilding the VM, require a recently verified
`external_mounted` backup and confirm it is readable from outside the VM.

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

The optional B7.5 maker-first order primitive remains disabled by default.
`cycle.decision_interval_seconds`, `maker_first_enabled`,
`maker_first_wait_seconds`, `research.shadow_enabled`,
`research.shadow_variants`, and `research.shadow_budget_ms` are documented
configuration controls; the shipped values/defaults are summarized in
[README.md](README.md).
