# VM feed migration handoff (legacy SIP corpus to Free Basic IEX)

This is the short handoff for an existing paper VM moving to the shipped Free
Basic profile. It is intentionally a controlled corpus/proof swap, not a
rebuild program. The account, broker reconciliation journal, and operator
history remain authoritative. Do this outside an Alpaca session.

Set and validate the actual secret paths before running any command. These
exports deliberately override stale values from `.env`; the migration is
always paper-only and always the shipped IEX/equity profile:

The snapshot preserves whether each shell variable was unset or explicitly
set, so rollback can restore the shell and `.env` precedence exactly. A
systemd `research.env` containing a non-empty dataset, ledger, journal, or
calibration path is rejected before any corpus state is touched; remove or
review that override first.

```bash
cd /opt/alpaca-agent-trading
set -euo pipefail
# These must be the host files actually mounted by this deployment.  Replace
# the defaults only when the installation uses a different root-owned path.
pre_migration_agent_secret_file="${ALPACA_AGENT_SECRET_FILE:-/etc/alpaca-agent-trading/agent.env}"
pre_migration_research_llm_secret_file="${ALPACA_RESEARCH_LLM_SECRET_FILE:-/etc/alpaca-agent-trading/research-llm.env}"
export HANDOFF=/srv/alpaca-agent-migration/$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 0700 "$HANDOFF"
sudo chown "$(id -u):$(id -g)" "$HANDOFF"
snapshot_env_var() {
  local name="$1"
  if [ "${!name+x}" = x ]; then
    printf 'export %s=%q\n' "$name" "${!name}"
  else
    printf 'unset %s\n' "$name"
  fi
}
for name in \
  ALPACA_AGENT_SECRET_FILE ALPACA_RESEARCH_LLM_SECRET_FILE \
  ALPACA_RESEARCH_LLM_SECRETS_FILE ALPACA_DATA_FEED ALPACA_STOCK_FEED \
  ALPACA_OPTIONS_FEED ALPACA_RESEARCH_VEHICLES ALPACA_PAPER \
  ALPACA_LIVE_ENABLE ALPACA_RESEARCH_DATASET \
  ALPACA_RECORDED_DATASET_ROOT ALPACA_EDGE_DB ALPACA_SHADOW_DB \
  ALPACA_RESEARCH_JOURNAL ALPACA_RESEARCH_CALIBRATION_CONFIG \
  ALPACA_RESEARCH_CALIBRATION_REPORT ALPACA_RESEARCH_REPORT_DIR \
  ALPACA_RESEARCH_PROOF_DIR; do
  snapshot_env_var "$name"
done > "$HANDOFF/pre-migration.env"
if [ -f .env ]; then
  cp -p .env "$HANDOFF/pre-migration-dotenv"
  printf '%s\n' present > "$HANDOFF/pre-migration-dotenv-state"
else
  printf '%s\n' absent > "$HANDOFF/pre-migration-dotenv-state"
fi
if [ -f /etc/alpaca-agent-trading/research.env ]; then
  sudo stat -c '%u:%g:%a' /etc/alpaca-agent-trading/research.env > "$HANDOFF/pre-migration-research.meta"
  sudo cp -p /etc/alpaca-agent-trading/research.env "$HANDOFF/pre-migration-research.env"
  sudo chown "$(id -u):$(id -g)" "$HANDOFF/pre-migration-research.env"
  chmod 0400 "$HANDOFF/pre-migration-research.env"
  printf '%s\n' present > "$HANDOFF/pre-migration-research-env-state"
else
  printf '%s\n' absent > "$HANDOFF/pre-migration-research-env-state"
fi

# Force the reviewed migration profile after the pre-migration snapshot.
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
export ALPACA_DATA_FEED=iex
export ALPACA_STOCK_FEED=iex
export ALPACA_OPTIONS_FEED=indicative
export ALPACA_RESEARCH_VEHICLES=equity
export ALPACA_PAPER=true
export ALPACA_LIVE_ENABLE=false
# Do not let a stale dataset, ledger, journal, or output path select the
# pre-migration SIP epoch. Empty values below deliberately select the shipped
# runtime defaults; the recorded dataset is the only research input.
export ALPACA_RESEARCH_DATASET=
export ALPACA_RECORDED_DATASET_ROOT=runtime/research/recorded
export ALPACA_EDGE_DB=runtime/research/edge_lab.sqlite3
export ALPACA_SHADOW_DB=/app/shadow/shadow.sqlite3
export ALPACA_RESEARCH_JOURNAL=runtime/paper/journal.db
export ALPACA_RESEARCH_CALIBRATION_CONFIG=
export ALPACA_RESEARCH_CALIBRATION_REPORT=
export ALPACA_RESEARCH_REPORT_DIR=
export ALPACA_RESEARCH_PROOF_DIR=
export IEX_BACKUP=/srv/alpaca-agent-backup/iex
test -r "$ALPACA_AGENT_SECRET_FILE"
test -r "$ALPACA_RESEARCH_LLM_SECRET_FILE"
test -r "$pre_migration_agent_secret_file"
test -r "$pre_migration_research_llm_secret_file"
test "${ALPACA_DATA_FEED}" = iex
test "${ALPACA_STOCK_FEED}" = iex
test "${ALPACA_OPTIONS_FEED}" = indicative
test "${ALPACA_RESEARCH_VEHICLES}" = equity
test -z "$ALPACA_RESEARCH_DATASET"
test "$ALPACA_RECORDED_DATASET_ROOT" = runtime/research/recorded
test "$ALPACA_EDGE_DB" = runtime/research/edge_lab.sqlite3
test "$ALPACA_SHADOW_DB" = /app/shadow/shadow.sqlite3
test "$ALPACA_RESEARCH_JOURNAL" = runtime/paper/journal.db
test -z "$ALPACA_RESEARCH_CALIBRATION_CONFIG"
test -z "$ALPACA_RESEARCH_CALIBRATION_REPORT"
if [ -e /etc/alpaca-agent-trading/research.env ]; then
  test -r /etc/alpaca-agent-trading/research.env
  ! grep -Eq '^(export[[:space:]]+)?(ALPACA_RESEARCH_DATASET|ALPACA_RECORDED_DATASET_ROOT|ALPACA_EDGE_DB|ALPACA_SHADOW_DB|ALPACA_RESEARCH_JOURNAL|ALPACA_RESEARCH_CALIBRATION_CONFIG|ALPACA_RESEARCH_CALIBRATION_REPORT|ALPACA_RESEARCH_REPORT_DIR|ALPACA_RESEARCH_PROOF_DIR)=.+$' \
    /etc/alpaca-agent-trading/research.env
fi
docker compose config --quiet
compose_config="$(docker compose config)"
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_DATA_FEED: iex'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_STOCK_FEED: iex'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_OPTIONS_FEED: indicative'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_RESEARCH_VEHICLES: equity'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_PAPER: "?true"?'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_LIVE_ENABLE: "?false"?'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_RESEARCH_DATASET: "?"?'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_EDGE_DB: runtime/research/edge_lab.sqlite3'
printf '%s\n' "$compose_config" | grep -Eq 'ALPACA_SHADOW_DB: /app/shadow/shadow.sqlite3'
printf '%s\n' "$compose_config" | grep -Fq "file: $ALPACA_AGENT_SECRET_FILE"
printf '%s\n' "$compose_config" | grep -Fq "file: $ALPACA_RESEARCH_LLM_SECRET_FILE"
```

Record the exact checkout/config/environment needed for rollback. The handoff
directory is mode 0700; secret backups are copied with `sudo`, then owned by
the operator at mode 0400 so the credentials never become group/world-readable:

```bash
set -euo pipefail
git rev-parse HEAD | tee "$HANDOFF/pre-migration.commit"
git status --short | tee "$HANDOFF/pre-migration.git-status"
test -z "$(git status --porcelain --untracked-files=no)" || {
  echo 'tracked worktree is not clean; archive/review the patch before migration' >&2
  exit 1
}
cp -p config.yaml "$HANDOFF/pre-migration-config.yaml"
test -f "$pre_migration_agent_secret_file"
test -f "$pre_migration_research_llm_secret_file"
sudo stat -c '%u:%g:%a' "$pre_migration_agent_secret_file" > "$HANDOFF/pre-migration-agent.meta"
sudo stat -c '%u:%g:%a' "$pre_migration_research_llm_secret_file" > "$HANDOFF/pre-migration-research-llm.meta"
sudo cp -p "$pre_migration_agent_secret_file" "$HANDOFF/pre-migration-agent.env"
sudo cp -p "$pre_migration_research_llm_secret_file" "$HANDOFF/pre-migration-research-llm.env"
sudo chown "$(id -u):$(id -g)" "$HANDOFF/pre-migration-agent.env" "$HANDOFF/pre-migration-research-llm.env"
chmod 0400 "$HANDOFF/pre-migration-agent.env" "$HANDOFF/pre-migration-research-llm.env"
printf '%s\n' "$pre_migration_agent_secret_file" > "$HANDOFF/pre-migration-agent.path"
printf '%s\n' "$pre_migration_research_llm_secret_file" > "$HANDOFF/pre-migration-research-llm.path"
```

## Handoff sequence

1. Prove the paper account is flat, has no working orders, and is outside a
   trading session. Capture both command outputs:

   ```bash
   docker compose run --rm --no-deps trader python main.py check
   docker compose run --rm --no-deps trader python main.py status
   ```

   If anything is non-flat, stop and reconcile/flatten through the normal
   operator path; do not continue.

2. Stop both launch lanes if present. Compose users run:

   ```bash
   set -euo pipefail
   docker compose stop recorder trader watchdog research shadow dashboard
   ```

   A systemd user must instead run `sudo systemctl stop
   alpaca-research.timer alpaca-research.service alpaca-recorder.service
   alpaca-trader.service alpaca-watchdog.service` and must not start Compose
   until the handoff is complete.

3. Archive the active corpus, edge ledger, execution state, shadow volume,
   research cache, and research results before touching them. These four
   Docker-volume archives are recovery copies; the paper reconciliation journal
   is never discarded:

   ```bash
   set -euo pipefail
   docker run --rm -v alpaca-agent-trading_runtime-data:/src:ro \
     -v "$HANDOFF":/dst alpine sh -ec \
     'tar -C /src -czf /dst/runtime-data-before-iex.tgz .'
   docker run --rm -v alpaca-agent-trading_shadow-data:/src:ro \
     -v "$HANDOFF":/dst alpine sh -ec \
     'tar -C /src -czf /dst/shadow-volume-before-iex.tgz .'
   docker run --rm -v alpaca-agent-trading_research-cache:/src:ro \
     -v "$HANDOFF":/dst alpine sh -ec \
     'tar -C /src -czf /dst/research-cache-before-iex.tgz .'
   docker run --rm -v alpaca-agent-trading_research-results:/src:ro \
     -v "$HANDOFF":/dst alpine sh -ec \
     'tar -C /src -czf /dst/research-results-before-iex.tgz .'
   sha256sum "$HANDOFF"/*.tgz | tee "$HANDOFF/archives.sha256"
   ```

4. Stage the existing IEX backup and checksum it. Never relabel the old SIP
   rows as IEX; the staged files must already carry `provider=alpaca` and
   `feed=iex`:

   ```bash
   set -euo pipefail
   test -d "$IEX_BACKUP"
   sudo rsync -a --numeric-ids "$IEX_BACKUP"/ "$HANDOFF/iex-stage"/
   (cd "$HANDOFF/iex-stage" && find . -type f -print0 | sort -z | \
     xargs -0 sha256sum) | tee "$HANDOFF/iex-stage.sha256"
   (cd "$HANDOFF/iex-stage" && sha256sum -c ../iex-stage.sha256)
   ```

5. Run the repository recorder audit against the staged corpus, then perform a
   bounded exact provider/feed scan across CSV, JSONL, and partition files. The
   recorder audit checks structural/index/duplicate invariants; the second
   check rejects wrong providers, wrong feeds, and mixed feeds without
   modifying a row. The audit uses a normalized, writable copy because it
   creates a temporary SQLite uniqueness index under the corpus root; the
   original stage remains untouched and the provider/feed scan mounts that
   copy read-only:

   ```bash
   set -euo pipefail
   mkdir "$HANDOFF/iex-audit"
   docker run --rm \
     -v "$HANDOFF/iex-stage:/src:ro" \
     -v "$HANDOFF/iex-audit:/audit" alpine sh -ec '
       cp -a /src/. /audit/
       chown -R 10001:10001 /audit
       find /audit -type d -exec chmod u+rwx {} +
       find /audit -type f -exec chmod u+rw {} +
     '
   docker compose run --rm --no-deps \
     -v "$HANDOFF/iex-audit:/app/iex-audit" recorder \
     python deploy/recorder.py --out /app/iex-audit --audit
   docker compose run --rm --no-deps \
     -v "$HANDOFF/iex-audit:/app/iex-audit:ro" recorder \
     python - /app/iex-audit <<'PY'
import csv, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
rows = 0
max_rows = 5_000_000
max_partitions = 100_000
index_path = root / ".recorder-index.json"
assert index_path.is_file(), "staged corpus is missing .recorder-index.json"
index = json.loads(index_path.read_text(encoding="utf-8"))
assert index.get("schema") == "recorder-index.v1", index.get("schema")
assert str(index.get("data_feed") or "").strip().lower() == "iex", index.get("data_feed")
partitions = index.get("partitions")
partition_sources = index.get("partition_sources")
assert isinstance(partitions, dict), "recorder index partitions is not a mapping"
assert isinstance(partition_sources, dict), "recorder index partition_sources is not a mapping"
assert len(partitions) <= max_partitions
partition_root = root / "sessions"
for name, size in partitions.items():
    assert isinstance(name, str) and name.startswith("market-"), name
    assert isinstance(size, int) and size >= 0, (name, size)
    partition = partition_root / name
    assert partition.is_file() and partition.stat().st_size == size, name
partition_files = sorted(partition_root.glob("market-*.csv")) if partition_root.is_dir() else []
assert len(partition_files) <= max_partitions
assert {path.name for path in partition_files} == set(partitions), "partition index is stale"
marker_sources = {}
marker_files = sorted(root.rglob("*.csv.source.json"))
assert len(marker_files) <= max_partitions
for marker in marker_files:
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload.get("schema") == "recorder-partition-source.v1", marker
    partition = payload.get("partition")
    assert partition in partitions and payload.get("source_mode") == "historical_backfill", marker
    marker_sources[partition] = {"source_mode": payload["source_mode"]}
assert marker_sources == partition_sources, "partition-source metadata is stale or mixed"
paths = sorted([*root.rglob("*.csv"), *root.rglob("*.jsonl"),
                *root.rglob("*.ndjson")])
def check(row, path):
    global rows
    rows += 1
    if rows > max_rows:
        raise RuntimeError("staged feed audit exceeded bounded row limit")
    assert row.get("provider") == "alpaca", (path, row.get("provider"))
    assert row.get("feed", row.get("feed_id")) == "iex", (
        path, row.get("feed", row.get("feed_id")))
for path in paths:
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                check(row, path)
    else:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    check(json.loads(line), path)
assert rows, "staged IEX corpus is empty"
print(f"audited {rows} rows: provider=alpaca feed=iex; no mixed feeds")
PY
   ```

6. Swap the corpus directory inside the same runtime volume, with services
   still stopped. A preflight and rollback trap guard the multi-step rename:
   if either rename or a sidecar move fails, the original corpus is put back
   before the container exits. The old corpus is retained under the handoff
   name; no source-mode or provider/feed field is rewritten. Move the SIP-bound
   edge ledger and shadow WAL aside as proof state that must be freshly
   re-proved, while leaving `runtime/paper/journal.db`, broker reconciliation
   state, and operator controls in place:

   ```bash
   set -euo pipefail
   docker run --rm \
     -v alpaca-agent-trading_runtime-data:/data \
     -v alpaca-agent-trading_shadow-data:/shadow \
     -v "$HANDOFF/iex-stage:/staged:ro" alpine sh -ec '
       set -eu
       test -d /data/research/recorded
       test ! -e /data/research/recorded.sip-legacy
       test ! -e /data/research/recorded.iex-stage
       for suffix in "" -wal -shm; do
         test ! -e "/data/research/edge_lab.sip-legacy.sqlite3${suffix}"
         test ! -e "/shadow/shadow.sip-legacy.sqlite3${suffix}"
       done
       old_moved=0
       swapped=0
       edge_main_moved=0
       edge_wal_moved=0
       edge_shm_moved=0
       shadow_main_moved=0
       shadow_wal_moved=0
       shadow_shm_moved=0
       rollback() {
         code=$?
         if [ "$code" -ne 0 ]; then
           if [ "$shadow_shm_moved" -eq 1 ]; then mv /shadow/shadow.sip-legacy.sqlite3-shm /shadow/shadow.sqlite3-shm || true; fi
           if [ "$shadow_wal_moved" -eq 1 ]; then mv /shadow/shadow.sip-legacy.sqlite3-wal /shadow/shadow.sqlite3-wal || true; fi
           if [ "$shadow_main_moved" -eq 1 ]; then mv /shadow/shadow.sip-legacy.sqlite3 /shadow/shadow.sqlite3 || true; fi
           if [ "$edge_shm_moved" -eq 1 ]; then mv /data/research/edge_lab.sip-legacy.sqlite3-shm /data/research/edge_lab.sqlite3-shm || true; fi
           if [ "$edge_wal_moved" -eq 1 ]; then mv /data/research/edge_lab.sip-legacy.sqlite3-wal /data/research/edge_lab.sqlite3-wal || true; fi
           if [ "$edge_main_moved" -eq 1 ]; then mv /data/research/edge_lab.sip-legacy.sqlite3 /data/research/edge_lab.sqlite3 || true; fi
           if [ "$swapped" -eq 1 ]; then
             mv /data/research/recorded /data/research/recorded.iex-stage || true
             mv /data/research/recorded.sip-legacy /data/research/recorded || true
           elif [ "$old_moved" -eq 1 ]; then
             mv /data/research/recorded.sip-legacy /data/research/recorded || true
           fi
           rm -rf /data/research/recorded.iex-stage
         fi
         exit "$code"
       }
       trap rollback EXIT
       mkdir /data/research/recorded.iex-stage
       cp -a /staged/. /data/research/recorded.iex-stage/
       chown -R 10001:10001 /data/research/recorded.iex-stage
       mv /data/research/recorded /data/research/recorded.sip-legacy
       old_moved=1
       mv /data/research/recorded.iex-stage /data/research/recorded
       swapped=1
       if [ -f /data/research/edge_lab.sqlite3 ]; then mv /data/research/edge_lab.sqlite3 /data/research/edge_lab.sip-legacy.sqlite3; edge_main_moved=1; fi
       if [ -f /data/research/edge_lab.sqlite3-wal ]; then mv /data/research/edge_lab.sqlite3-wal /data/research/edge_lab.sip-legacy.sqlite3-wal; edge_wal_moved=1; fi
       if [ -f /data/research/edge_lab.sqlite3-shm ]; then mv /data/research/edge_lab.sqlite3-shm /data/research/edge_lab.sip-legacy.sqlite3-shm; edge_shm_moved=1; fi
       if [ -f /shadow/shadow.sqlite3 ]; then mv /shadow/shadow.sqlite3 /shadow/shadow.sip-legacy.sqlite3; shadow_main_moved=1; fi
       if [ -f /shadow/shadow.sqlite3-wal ]; then mv /shadow/shadow.sqlite3-wal /shadow/shadow.sip-legacy.sqlite3-wal; shadow_wal_moved=1; fi
       if [ -f /shadow/shadow.sqlite3-shm ]; then mv /shadow/shadow.sqlite3-shm /shadow/shadow.sip-legacy.sqlite3-shm; shadow_shm_moved=1; fi
       trap - EXIT
     '
   ```

   The old ledgers are immutable audit archives, not active proof. A fresh
   edge ledger and shadow WAL will be created on restart. This resets SIP-bound
   proof/authorization state only; it does not reset broker reconciliation,
   fills, account identity, or safety controls.

   Empty the archived feed-bound caches/results after their checksummed copies
   exist. This prevents an old SIP report, temporary view, or cache from being
   mistaken for the new IEX epoch:

   ```bash
   set -euo pipefail
   docker run --rm -v alpaca-agent-trading_research-cache:/data alpine \
     sh -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
   docker run --rm -v alpaca-agent-trading_research-results:/data alpine \
     sh -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
   ```

7. Review the checked-in config and environment before starting anything. The
   expected values are `broker.data_feed=iex`, `broker.options_feed=indicative`,
   `universe.asset_classes=["us_equity"]`,
   `strategy.execution_mode=shares`, and `ALPACA_RESEARCH_VEHICLES=equity`.
   Then run the checks in this order:

   ```bash
   set -euo pipefail
   docker compose config --quiet
   docker compose run --rm --no-deps recorder \
     python deploy/recorder.py --out runtime/research/recorded --probe
   docker compose run --rm --no-deps trader python main.py check
   ```

8. Start the services, verify health, and rerun research against the new IEX
   corpus. The first research run is a fresh evidence epoch; it cannot reuse
   SIP proofs or shadow authorization:

   ```bash
   set -euo pipefail
   docker compose up -d --remove-orphans
   docker compose ps
   docker compose run --rm research /bin/bash deploy/research-cycle.sh
   docker compose logs --tail=100 recorder research trader
   ```

   Capture the durable post-run reports before handing the VM back to the
   operator. The dashboard response is saved as a handoff artifact as well as
   printed for a quick health check:

   ```bash
   set -euo pipefail
   docker compose run --rm --no-deps research \
     python research.py factory report --format markdown --write
   docker compose run --rm --no-deps research \
     python research.py edge status --vehicle equity
   curl -fsS http://127.0.0.1:8080/api/status | tee "$HANDOFF/post-migration-dashboard.json"
   ```

## Rollback

If the IEX probe or checks fail, stop services and restore the archived volumes
with these commands (the account must remain flat):

```bash
set -euo pipefail
docker compose stop recorder trader watchdog research shadow dashboard
(cd "$HANDOFF" && sha256sum -c archives.sha256)
docker run --rm -v alpaca-agent-trading_runtime-data:/data \
  alpine sh -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
docker run --rm -v alpaca-agent-trading_shadow-data:/data \
  alpine sh -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
docker run --rm -v alpaca-agent-trading_research-cache:/data \
  alpine sh -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
docker run --rm -v alpaca-agent-trading_research-results:/data \
  alpine sh -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
(cd "$HANDOFF" && sha256sum -c archives.sha256)
docker run --rm -v alpaca-agent-trading_runtime-data:/data \
  -v "$HANDOFF":/handoff:ro alpine sh -ec \
  'tar -xzf /handoff/runtime-data-before-iex.tgz -C /data'
docker run --rm -v alpaca-agent-trading_shadow-data:/data \
  -v "$HANDOFF":/handoff:ro alpine sh -ec \
  'tar -xzf /handoff/shadow-volume-before-iex.tgz -C /data'
docker run --rm -v alpaca-agent-trading_research-cache:/data \
  -v "$HANDOFF":/handoff:ro alpine sh -ec \
  'tar -xzf /handoff/research-cache-before-iex.tgz -C /data'
docker run --rm -v alpaca-agent-trading_research-results:/data \
  -v "$HANDOFF":/handoff:ro alpine sh -ec \
  'tar -xzf /handoff/research-results-before-iex.tgz -C /data'
git -C /opt/alpaca-agent-trading switch --detach "$(cat "$HANDOFF/pre-migration.commit")"
cp -p "$HANDOFF/pre-migration-config.yaml" /opt/alpaca-agent-trading/config.yaml
if [ -f "$HANDOFF/pre-migration-dotenv" ]; then
  cp -p "$HANDOFF/pre-migration-dotenv" /opt/alpaca-agent-trading/.env
elif [ "$(cat "$HANDOFF/pre-migration-dotenv-state")" = absent ]; then
  rm -f /opt/alpaca-agent-trading/.env
fi
if [ "$(cat "$HANDOFF/pre-migration-research-env-state")" = present ]; then
  research_meta="$(cat "$HANDOFF/pre-migration-research.meta")"
  research_owner="${research_meta%%:*}"
  research_group="${research_meta#*:}"; research_group="${research_group%%:*}"
  research_mode="${research_meta##*:}"
  sudo install -o "$research_owner" -g "$research_group" -m "$research_mode" \
    "$HANDOFF/pre-migration-research.env" /etc/alpaca-agent-trading/research.env
else
  sudo rm -f /etc/alpaca-agent-trading/research.env
fi
pre_migration_agent_secret_file="$(cat "$HANDOFF/pre-migration-agent.path")"
pre_migration_research_llm_secret_file="$(cat "$HANDOFF/pre-migration-research-llm.path")"
agent_meta="$(cat "$HANDOFF/pre-migration-agent.meta")"
research_llm_meta="$(cat "$HANDOFF/pre-migration-research-llm.meta")"
agent_owner="${agent_meta%%:*}"
agent_group="${agent_meta#*:}"; agent_group="${agent_group%%:*}"
agent_mode="${agent_meta##*:}"
research_llm_owner="${research_llm_meta%%:*}"
research_llm_group="${research_llm_meta#*:}"; research_llm_group="${research_llm_group%%:*}"
research_llm_mode="${research_llm_meta##*:}"
sudo install -o "$agent_owner" -g "$agent_group" -m "$agent_mode" \
  "$HANDOFF/pre-migration-agent.env" "$pre_migration_agent_secret_file"
sudo install -o "$research_llm_owner" -g "$research_llm_group" -m "$research_llm_mode" \
  "$HANDOFF/pre-migration-research-llm.env" "$pre_migration_research_llm_secret_file"
set -a
. "$HANDOFF/pre-migration.env"
set +a
docker compose config --quiet
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --probe
docker compose run --rm --no-deps trader python main.py check
docker compose up -d --remove-orphans
```

If the archives are unavailable, do not guess or relabel rows; preserve the
stopped VM and escalate for recovery. A rollback restores the prior reviewed
image/config and its SIP-bound proof state; a later return to IEX repeats this
handoff and requires a new proof epoch.

Never run `docker compose down -v`, never prune these named volumes, and never
rewrite a row's provider, feed, `source_mode`, or observation timestamps to
make an old proof appear to be IEX. A feed change always requires a fresh
research and shadow re-proof.
