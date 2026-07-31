# Operations — Mac, VM, research, and findings

This is the current operating runbook. It covers what to run, where the data
lives, how to read reports, and what an edge can or cannot change.

## 1. Where each operation runs

| Operation | Mac | Azure VM |
| --- | --- | --- |
| Development and tests | Yes | Optional |
| Demo trader | Foreground/manual | `okx-trader.service` |
| Order-book recorder | Foreground/manual | `okx-recorder.service` |
| Nightly research | Manual or local scheduler | `okx-research.timer` |
| Authoritative journal | Local runtime tree | VM runtime tree is authoritative when the VM is active |
| Tournament | Against an explicitly supplied corpus | Nightly against VM data |
| Findings review | Copy/read the findings DB or reports | Primary findings DB and generated reports |

The VM export under `vm-import/` is a test fixture only. It is not a runtime
default and must not be used as the project's production data location.

## 2. Daily health checks

### Mac

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py status
./.venv/bin/python research.py readiness
./.venv/bin/python research.py corpus stats
```

### VM

```bash
sudo systemctl status okx-recorder okx-trader okx-research.timer
sudo journalctl -u okx-trader -n 100 --no-pager
sudo journalctl -u okx-recorder -n 100 --no-pager
sudo journalctl -u okx-research -n 200 --no-pager
sudo -u okx /opt/okx-agent-crypto/.venv/bin/python \
  /opt/okx-agent-crypto/research.py readiness \
  --db /opt/okx-agent-crypto/runtime/demo/journal.db
```

If G2 is failed, stop trusting downstream research output. If the result is
`INSUFFICIENT_SAMPLE`, collection is working but the sample is not yet large
enough.

## 3. The authoritative nightly workflow

`research/nightly.sh` runs, in order:

1. readiness and corpus statistics;
2. replay fidelity G2;
3. funnel, cadence, sweeps, and three-arm analysis;
4. paired forward qualification and scorecard regeneration;
5. market-data refresh and forward export;
6. the exploratory tournament.

G2 is a hard stop for authoritative analysis. The tournament may still produce
an exploratory report, but it cannot raise a tier above `T2_CANDIDATE`.

On the VM:

```bash
sudo systemctl enable --now okx-research.timer
sudo systemctl list-timers okx-research.timer
sudo journalctl -u okx-research -n 200 --no-pager
```

On a Mac, run the same workflow manually:

```bash
./.venv/bin/bash research/nightly.sh
```

## 4. Running the tournament against the VM corpus

The tournament needs a directory containing the extracted research data, not a
journal DB. The expected directory contains the downloaded `swap/`, `spot/`,
`funding/`, `oi/` files and `manifest.json`.

### Directly on the VM

```bash
cd /opt/okx-agent-crypto
sudo -u okx .venv/bin/python research/tournament.py \
  --data /opt/okx-agent-crypto/runtime/research/data \
  --out /opt/okx-agent-crypto/research/results/tournament \
  --top-n 5 \
  --workers 2
```

### Against the supplied Mac fixture

The checked-out fixture contains an archive under
`vm-import/2026-07-30/`. Extract it to a temporary directory, then score the
extracted data:

```bash
fixture=$(mktemp -d)
tar -xzf vm-import/2026-07-30/okx-research-files-2026-07-30.tgz \
  -C "$fixture"
./.venv/bin/python research/tournament.py \
  --data "$fixture/runtime/research/data" \
  --out "$fixture/tournament-out" \
  --top-n 5 \
  --workers 2
```

`--workers 2` runs strategies concurrently while preserving deterministic
result ordering. Each strategy evaluates each pre-registered setting once in
that run. The result is exploratory and must not be treated as live authority.

### What to inspect

```bash
sed -n '1,220p' "$fixture/tournament-out/REPORT.md"
cat "$fixture/tournament-out/leaderboard.json"
```

Check the benchmark harness first. If the benchmark does not reproduce its
known failure, stop and do not interpret other rows.

## 5. Research commands and reporting

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify --scope <scope>
./.venv/bin/python research.py report
```

`research.py report` regenerates scorecards and `findings/README.md`; it does
not call the LLM or change the strategy register. Findings are stored in
`research/cache/findings.db` and backed up to `findings.db.backup` after
successful writes.

The report separates:

- static registered variants;
- generated hypothesis settings;
- adaptive variants with exact proposed values and reasoning;
- shadow decisions and isolated PAPER trades;
- forward analyses and family correction;
- qualification events and T3 packet status.

## 6. How an adaptive hypothesis run works

1. The LLM may propose one numeric value for one registered runtime hypothesis
   setting and must provide reasoning.
2. The parser enforces known hypothesis/setting identity, numeric finiteness,
   registered bounds, and minimum reasoning length.
3. FindingsStore persists the proposal, lock window, run ID, and history. A
   duplicate or locked proposal is rejected.
4. The engine materializes an exact first-class variant ID carrying the exact
   hypothesis parameters.
5. Shadow evaluates all eligible static variants in bounded parallel workers;
   it schedules at most one adaptive setting per strategy in a cycle.
6. The same proposed decision set is evaluated for every variant; no extra LLM
   call is made for a variant.
7. Forward qualification validates the paired immutable decision ledger,
   held-out evidence, common window, provenance, and axis family correction.
8. A successful qualification appends an immutable edge event. It starts local
   PAPER only when flat; it does not change live configuration.

## 7. Findings and edge interpretation

`candidate` means registered and eligible for collection. `testing` means it
has results but has not cleared the protocol. `QUALIFIED` is an evidence event
for a scope and variant, not a live-trading instruction. `REVOKED` means the
paper/shadow account or evidence window became invalid and must be collected
again.

Read the exact hypothesis, setting, overrides, sample, findings log, and
qualification status from the scorecard. Do not infer an edge from a positive
point estimate alone. The protocol requires 100 full pairs, 70 fit pairs, 30
confirmation pairs, 80% coverage, and eight independent six-hour episodes.

## 8. T3 packet: what it is and what it does not do

A T3 packet is a content-addressed evidence bundle generated by:

```bash
./.venv/bin/python research.py t3-packet \
  --variant <qualified-variant-id> \
  --scope <scope> \
  --reviewed-by <reviewer> \
  --registry-change-ref <change-reference>
```

It binds the current G2 result, forward analysis, family correction, paper
sample, current code/config/fidelity fingerprints, and the manual review
identity into one immutable payload.

The packet can be generated automatically, but the current code deliberately
does not mutate `agent/registry.py` or `config.yaml`. That boundary prevents a
research run from silently becoming a capital-allocation change. There is no
technical impossibility here: the remaining implementation would be an
explicit approval command that validates a reviewed packet and writes a new
immutable registry/config revision. Until that command exists, the required
operator action is to review the packet and approve the exact variant/change;
there is no automatic strategy switch.

## 9. Data handoff from VM to Mac

Copy, do not relocate, the VM's data. Preserve the manifest, time window, code
fingerprint, config fingerprint, and findings DB backup alongside the export.
The Mac can then run the tournament and read reports without becoming the
runtime authority.

Before deleting or rebuilding the VM, preserve:

- the mode journal DB;
- `runtime/research/recorded/`;
- the extracted research data and manifest;
- `research/cache/findings.db` and its backup;
- tournament and findings reports.

## 10. Troubleshooting

| Symptom | Meaning/action |
| --- | --- |
| G2 failed | Stop trusting downstream authoritative output; investigate replay mismatch |
| `INSUFFICIENT_SAMPLE` | Keep collecting; it is not a rejection |
| No forward scope | Supply `--scope` when the store has zero or multiple scopes |
| Variant not in report | Run `research.py report`; confirm the findings store path |
| Tournament benchmark failed | Treat the harness as broken; do not interpret rankings |
| VM research service red | Inspect `journalctl -u okx-research`; the timer surfaces real failures |
| Findings DB missing | Check `research.findings_store`; the path is never silently replaced |
