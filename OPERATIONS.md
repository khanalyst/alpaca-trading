# Operations runbook

This runbook covers the default paper Alpaca runtime and the controls required
for an explicitly approved live scope. It assumes the checkout is
`/opt/alpaca-agent-trading`, the secret is outside Git, and one trader process
owns one account with one execution profile (`shares` or `options`). The
supported universe is US-listed equities/ETFs and listed OCC options only. The
order path is intraday: day-only orders, regular NYSE session entries, startup
cleanup, and force-flat before the close. Overnight positions are an incident,
not a supported state.

The read-only dashboard on `http://127.0.0.1:8080` is the fastest way to see
all of this at once: paper-account trials and what has earned a promotion, pinned
promotions and any pin that cannot trade, every fill attributed to the strategy
and variant that placed it, what research learned and what each proposal built
on, and the configuration audit trail. It cannot change anything — `POST`
returns 405.

## Daily checks

```bash
cd /opt/alpaca-agent-trading
docker compose ps
docker compose logs --tail=100 trader
docker compose logs --tail=100 recorder
docker compose logs --tail=100 watchdog
docker compose exec -T trader python main.py check
docker compose exec -T trader python main.py status
docker compose ps research
```

`main.py check` is authenticated by default and must report the configured
mode endpoint, credentials, stock/options feeds, current market clock, and
account state. Use `--offline` only for local configuration checks; it does not
authenticate and is not a trading preflight. A wrong endpoint, missing key, or
inactive account is a failed check. Verify recorder/research timestamps are
fresh, the dashboard is healthy, and the trader has exactly one replica.

For systemd hosts:

```bash
sudo systemctl status alpaca-recorder alpaca-trader alpaca-watchdog \
  alpaca-research.timer
sudo journalctl -u alpaca-trader -n 100 --no-pager
sudo journalctl -u alpaca-watchdog -n 100 --no-pager
sudo journalctl -u alpaca-recorder -n 100 --no-pager
sudo journalctl -u alpaca-research -n 100 --no-pager
```

## Session controls

The exact calendar returned by Alpaca is authoritative for holidays and early
closes. Production replay and live-shadow require session open/close metadata;
missing metadata is a refusal, never a promotion to a fixed 16:00 close. The
trader must:

1. complete the 09:30–09:45 America/New_York IBR window before evaluating a
   breakout;
2. reject new entries near the close and whenever the clock/calendar is stale;
3. submit only `time_in_force: day` orders and maintain stops and long-option
   exits while the session is open;
4. at startup, cancel working orders and flatten residual shares/contracts;
5. flatten all shares and long-option contracts before the configured close
   cutoff; and
6. reconcile broker positions and orders after flattening.

If the process is restarted during a session, reconcile first. Never infer a
flat account from a local journal alone. A long option contract is one risk
unit: close or cancel it and confirm no residual contract remains.

### Option protection is not equity protection

Alpaca supports market and limit day orders on options only: no bracket, no
OCO/OTO, and no stop or stop-limit. The runtime rests a `sell_to_close` limit
take-profit once an option entry fills, and that order survives the trader
dying; the stop does not — it is the 60-second poller. The `watchdog` service
bounds that gap by flattening a stale trader's open positions from a separate
process and broker session, and stays inert while the trader holds the
mode-scoped run lock. Two cases remain uncovered and must be handled by a
human: a trader that is alive but wedged (it keeps the lock, so the watchdog
will not act), and an unreachable broker or network (no local process can
close anything). `mode: live` therefore rejects `execution_mode: options`;
run the options profile on paper only.

Check the watchdog with `python deploy/health.py watchdog`. `watching` is the
normal steady state. `acted` means the watchdog authenticated the scoped
account and confirmed flattening. `degraded` or `residual_risk: true` means it
attempted to act but could not prove the account flat; treat that as an
incident, not a healthy watchdog result. The watchdog deliberately remains
inert while a trader owns the run lock, even if that process is wedged.

## Mode guard and live preflight

Paper is the default and the shipped Compose/systemd lanes set
`ALPACA_PAPER=true`. A live process is allowed only with `mode: live`,
`broker.paper: false`, `broker.allow_live: true`, and
`ALPACA_LIVE_ENABLE=true`; `ALPACA_PAPER=true` must not be set. It must use
either `strategy.selection_mode: pinned` with exactly one operator-named pin
(preferred), or the legacy `selection_mode: specific` with one exact named
validated/champion `strategy.variant_id` whose latest shadow proof carries the
parity-matched live-ingestion marker. It must keep `llm.enabled: false` —
validation and the Engine constructor both reject a live runtime decision LLM,
because a veto would deploy behavior that never passed the gates. The bounded
research strategy-replacement adapter is a separate offline setting and is
unaffected. The runtime resolves exactly that candidate/configuration, checks
it again during startup refresh, and never substitutes another champion. Keep
live credentials, config, and
`ALPACA_AGENT_RUNTIME_ROOT` separate from paper; never run both against shared
state or a shared account.

The authenticated live preflight requires the account to report
`pattern_day_trader=true` in addition to endpoint, identity, active status,
equity, asset, clock, and calendar checks. A missing or false PDT flag blocks
startup. If any mode guard or preflight fails, stop the process, preserve the
evidence, and correct the scoped configuration before retrying.

## Start, stop, and pause

```bash
docker compose up -d                         # recorder, trader, research, shadow, dashboard
docker compose stop trader                 # pause new decisions safely
docker compose start trader
docker compose down                         # preserves named volumes
```

Before stopping during a session, run
`docker compose exec -T trader python main.py flatten --reason operator` and
confirm Alpaca reports no open positions or orders. A non-zero flatten result
means residual positions remain and requires manual reconciliation. If the
trader is unhealthy, stop it, cancel open orders, flatten manually in the
scoped Alpaca account, and leave it stopped until reconciliation passes.

To recover an operator-paused runtime, use this sequence:

1. Stop the trader and confirm the process has released its run lock.
2. Run the authenticated `check` and `status` commands for the same mode and
   account.
3. If `status` shows positions, or broker inspection shows working orders, run
   `flatten` and wait for a later status/reconciliation that proves flat.
4. Run `resume`. It performs one authenticated reconciliation plus a final
   read-only broker confirmation, and clears only the operator pause when
   broker and durable state are flat and terminal.
5. Start the trader again.

```bash
docker compose stop trader
docker compose run --rm trader python main.py check
docker compose run --rm trader python main.py status
# only when positions or working orders remain:
docker compose run --rm trader python main.py flatten --reason operator
docker compose run --rm trader python main.py resume
docker compose start trader
```

`resume` is authenticated and flat-only. It never cancels, submits, or
flattens orders, and it rejects killed or daily-risk-stopped runtimes. Do not
manually edit `state.json` or the SQLite journal to force a resume.

## Reconciliation and incident response

Capture these artifacts before changing state:

```bash
docker compose logs --no-color --since=2h trader > /tmp/alpaca-trader.log
docker compose exec -T trader python main.py status > /tmp/alpaca-status.txt
docker compose exec -T trader python - <<'PY'
from agent.alpaca_provider import AlpacaProvider
from main import load_cfg
cfg = load_cfg("config.yaml")
p = AlpacaProvider(cfg)
print("paper", p.paper)
print("positions", p.positions())
print("orders", p.orders())
PY
```

Treat any mismatch between local state and Alpaca as broker truth: cancel
working orders, flatten positions, preserve logs, and keep the trader paused.
Do not delete or edit the SQLite journal to make a reconciliation pass.

## Backups and recovery

Back up the named volumes or these directories to a different device/off-host
destination:

- `runtime/` (journal, state, health, and recorder receipts);
- `research/cache/` and `research/results/`;
- generated edge proof reports under `research/results/edges/`; and
- the exact Git revision and a redacted configuration snapshot.

Example export (run only when the destination has been verified):

```bash
mkdir -p /srv/alpaca-agent-backup
docker run --rm -v alpaca-agent-trading_runtime-data:/src:ro \
  -v /srv/alpaca-agent-backup:/dst alpine \
  sh -c 'tar -C /src -czf /dst/runtime-$(date -u +%Y%m%dT%H%M%SZ).tgz .'
```

Verify the archive can be listed and copied to another host. A bind mount on
the same VM does not prove recoverability from VM loss. Restore into a new
checkout, run compile/tests and `main.py check`, then reconcile the scoped
account before starting the trader.

## Updating

Review the diff and run the focused checks before restarting:

Ensure `ALPACA_AGENT_SECRET_FILE` and the separately mounted
`ALPACA_RESEARCH_LLM_SECRET_FILE` point to readable deployment files before
running Compose validation. The research provider secret is required because
the enabled research LLM lane fails closed when it is absent or unreadable.

```bash
git fetch origin
git diff --check origin/main...HEAD
./.venv/bin/python -m compileall -q agent deploy main.py research.py
./.venv/bin/python -m unittest discover -v
docker compose config --quiet
docker compose build
docker compose up -d --remove-orphans
docker compose exec -T trader python main.py check
```

Do not update by replacing a running journal or starting a second trader.
Rollback means restoring the prior reviewed Git revision and image, then
re-running the mode-appropriate check and reconciliation. Record the revision,
operator, mode, time, and check output in the deployment log.

## Bootstrapping the corpus

The recorder samples forward in real time, so a new deployment has no history
and cannot clear the research floors — 100 held-out trades across 30 complete
sessions (30 session clusters), then a strictly later forward window — for a
long time. Seed the
corpus from Alpaca's historical bars instead:

```bash
python deploy/backfill.py --days 180
```

It writes recorder-shaped normalized rows, one partition per session, and the
same sidecar index, but marks each partition `source_mode: historical_backfill`
and stores exact Alpaca open/close calendar metadata, including early closes.
The fetch-time `observed_at` is retained rather than backdated. Only an explicit
diagnostic replay policy may inspect these rows at provider `as_of`; resulting
`diagnostic_historical_backfill` evidence is excluded from authorizing statistics
and cannot authorize a proof. Only *completed* sessions are written, `as_of` is
the bar's completed one-minute boundary exactly as the recorder records it, and
the run is resumable: sessions that already have a partition are skipped, so
re-running is a no-op and an interrupted run continues. `--overwrite` replaces
existing partitions, `--quotes` also
backfills quotes (far larger, and rarely needed because replay prices boundary
fills from bars and charges the modelled half-spread when a quote is absent).

Two limits are worth planning around. **Options are not backfilled** — the
recorder's option rows are sampled chain snapshots with quote-age semantics no
historical endpoint reconstructs, so the option lane still needs recorded
sessions. And **history alone is not enough**: `_simulate_trade` takes at most
one trade per symbol-session, so the 100-trade held-out floor and 30-cluster
session floor are as much a universe-width requirement as a history-length one.
The shipped default universe is 24 liquid ETFs spanning broad-market, size,
sector, international, rates/credit, metals, and semiconductor exposures (the
exact operator-approved list is in `config.yaml`). This improves opportunity
capacity, but real signal rates still require sufficient history. Floor
feasibility fails closed when 100 held-out trades
cannot be supported; widen `universe.symbols` and/or the backfill window, never
lower the evidence floor. A universe expansion requires an operator-approved
exact symbol list, recorder coverage for that list, and a new identity/proof;
do not reuse an older proof after changing the list. Event conditioning requires
a point-in-time event source with provider/as-of/observation provenance. Prior-
session, true multi-timeframe, and cross-sectional features require explicit
replay context and fail closed when it is missing or ambiguous.

Run the backfill before starting the trader, or outside market hours, so the
recorder's first live cycle resumes from a completed session boundary.

## Promoting an edge

Promotion is the one step nothing automatic performs. The system runs proved
edges on the same Alpaca paper account, judges them on their real fills, and hands you a
shortlist; deciding is yours.

**1. See what has earned it.**

```bash
./.venv/bin/python research.py edge promotable
```

Each entry names the variant, its edge/family, what it returned on the book —
trades, sessions, total and mean R, win rate, net P&L — and prints the exact
configuration block. Empty output means nothing has cleared its trial floor
yet; that is the normal state early on.

**2. Paste the block into `config.yaml`.** Copy it verbatim. Retyping a
content-addressed variant id is how a promotion silently points at a different
edge. Give the entry an `id` that means something to you (`pin-orb-2026-08`);
it is the handle used by the audit trail, the dashboard, and every notification
about that edge.

```json
"strategy": {
  "selection_mode": "pinned",
  "pinned": [
    { "id": "pin-orb-2026-08",
      "variant_id": "rule.opening-range-breakout.abc12345…",
      "vehicle": "equity", "strategy_id": "rule",
      "promoted_at": "2026-08-12",
      "note": "live paper: 34 trades over 21 sessions, total R 6.40" }
  ]
}
```

**3. Verify before restarting.**

```bash
./.venv/bin/python main.py check --offline    # the promotion must validate
./.venv/bin/python main.py check              # authenticated preflight
```

A malformed promotion fails `check --offline`. Restart the trader, then confirm
on the dashboard that "Pinned promotions" lists your entry and that "Pinned but
NOT trading" is absent — a pin that cannot resolve trades nothing rather than
substituting something else.

**What changes once an edge is pinned.** The pin records the operator-selected
identity and promotion context; it does not disable lifecycle stops. The
sequential drift test and trial review still run, and an authoritative breach
parks or demotes the edge so runtime selection fails closed. The rolling-R
monitor remains visible but advisory. The pin
context remains in the transition/audit record. Removing a pinned edge is an
edit to `config.yaml` and a restart — the same route in and out. Runtime risk
limits (daily loss limit, open risk, position caps) are unaffected: those are
safety, not lifecycle, and they still stop trading.

**Live.** `mode: live` accepts `selection_mode: pinned` with exactly one entry,
or the older `selection_mode: specific`. Everything in
"Mode guard and live preflight" still applies.

## Trials on the paper account

An edge that is proved but not pinned trades the same Alpaca paper account so its evidence
stops being a replay. `research.trial` in `config.yaml` sets the window and the
floor:

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | run the lane at all |
| `min_sessions` | `30` | sessions before a verdict |
| `min_trades` | `100` | trades before a verdict |
| `min_total_r` | `0.0` | total R the window must beat |
| `min_mean_r` | `0.0` | mean R per trade it must beat |

The scheduled cycle reviews trials before it proposes anything new. Check what
a review would do without letting it act:

```bash
./.venv/bin/python research.py edge trials --dry-run
```

An edge below its floor is parked (demoted) and the reason is written into the
lesson ledger as live evidence, so the next tuning cycle proposes parameters
against what actually happened rather than only against the backtest. Parking
promotes nothing in its place: the replacement still has to earn a backtest
pass, a strictly later offline shadow pass, every gate, and a new
parity-matched live proof. `ALPACA_TRIAL_REVIEW_ENABLED=0`
disables the review in a cycle.

Paper outcomes are scoped to the exact passing shadow proof that authorized
the entry. If a candidate is demoted, earns a newer shadow proof, and begins a
new trial, `paper_performance` and `review_trials` use only the new proof epoch;
old losses and wins remain durable history but cannot decide the new verdict.
If the latest shadow proof is failed or cannot be re-verified, new unscoped
outcomes are rejected and lifetime history is not used as a fallback.

Exit code 3 from `edge trials` means something was parked. That is an
operator-visible outcome, not a failure.

## Configuration audit trail

Every distinct configuration the trader loads is recorded with a
content-addressed `config_version_id`, the version it replaced, and a diff
naming each field that changed. Restarting under unchanged settings records
nothing new; one changed value produces one new version.

Read it on the dashboard's "Configuration audit trail" card, or directly:

```bash
./.venv/bin/python - <<'PY'
from agent.governance import config_history
for item in config_history("runtime/paper/journal.db", limit=10):
    print(item["config_version_id"], item["created_at"], item["changed_paths"])
PY
```

Secret-bearing fields (`api_key`, `secret_key`, tokens, webhook URLs) are
redacted before hashing. The trail records that such a field changed, never
what it changed to — and the honest consequence is that a change confined to
those fields alone does not appear as a separate version.

An audit-write failure is deliberately non-blocking because halting a trader
that may already own exposure is the more dangerous failure. It is not silent:
the heartbeat becomes sticky `degraded` with reason
`config_audit_unavailable`, health reports it as unhealthy, and a later
successful audit is required to clear the condition. Investigate the journal
path, permissions, free space, and SQLite readiness while continuing to
monitor any open exposure.

## Research and evidence

Research is read-only with respect to broker authority. The shipped paper
deployment uses the free Basic IEX equity feed, keeps an equity-only universe,
and keeps `ALPACA_LIVE_ENABLE=false`; no option entitlement is needed by
default. `indicative` is a non-executable options-feed placeholder. An explicit
option lane must add option symbols/classes, set `ALPACA_RESEARCH_VEHICLES`, and
pass the OPRA preflight. The enabled research strategy LLM is a bounded
proposal lane, not a runtime decision LLM: its separate readable provider
secret is required, and the cycle fails closed before discovery when that
secret is absent, unreadable, or missing the selected provider key.
Before an expensive cycle, and after changing the provider endpoint or model,
run one bounded, non-authorizing probe:

```bash
python3 research.py llm-preflight --agent-config config.yaml
```

The probe is non-authorizing and does not touch the dataset; its model/deployment
contract and fatal/degraded classification are documented in
[research/README.md](research/README.md#provider-preflight). The scheduled
wrapper retains its bounded, redacted result in terminal cycle JSON, status,
and history, including transient `degraded` outcomes. If all runtime LLM calls
fail, the cycle reports terminal `llm_provider_failure` unless an authorizing
proof already exists.
It discovers the
recorder's mixed bars/quotes/options corpus under
`runtime/research/recorded/sessions/` by default (or uses
`ALPACA_RESEARCH_DATASET`; `ALPACA_RESEARCH_SESSION_WINDOW` loads only the most
recent N session partitions), runs the autonomous strategy factory
plus the explicit IBR baseline, scores shares and single-leg long-option
vehicles separately, and writes evidence. Each variant has its own simulated
cash/equity account; default capacity is twelve logical strategy slots and four
variants per strategy, covering the catalog of twelve rule families on a fresh
ledger. Each isolated book is processed by one
bounded worker. The shipped paper runtime defaults to
`selection_mode: specific` with `variant_id: auto`, resolving exactly one
globally strongest validated/champion variant after the evidence gate. An
explicit paper `selection_mode: all_proved` keeps one strongest proven variant
per verified frozen prior-cycle dependence cluster under one global risk book;
families without a verified assignment use the held-out correlation-safe
fallback. The edge
ledger is
initialized at `runtime/research/edge_lab.sqlite3`; inspect it with
`python research.py edge status`. The autonomous lifecycle requires an initial
corpus backtest followed by later unseen offline forward-shadow evidence.
Offline historical/forward replay may persist only a `lane=shadow` candidate
status; it never authorizes runtime entries. Validation requires
fit and held-out structural floors, matched controls, placebo/falsification,
fixed-rule rolling-origin forward stability, family-local, frozen-dependence-
cluster, and cycle-global FDR, and a durable verified gate. Offline post-selection
records an explicit cumulative-FDR deferral rather than spending alpha on data
that cannot authorize deployment. A family pass
with a global failure is a normal marginal result and cannot authorize
selection. Final qualification evidence binds its declared sessions, bounded
candidate/baseline observations, and content digests so it can be recomputed.
The gate envelope also records per-arm candidate, baseline, and randomized-null
counts, fill sources, quote ages, gross/cost/net economics, matched and dropped
keys, and directional/pair coverage. Quote density can change null/control
evidence even when the candidate count is unchanged.
Underpowered data is not a failure: a shadow worker advances no durable
boundary until all intended variants are adequately powered, so the tail is
reconsidered on a later cycle. The sealed qualification window is released
once by one preselected candidate alone; other variants remain diagnostic.
Refinement is coordinate-first: every initial child changes exactly one
executable field. Bounded two-field interactions are formed only from the best
measured coordinate lessons, followed by an unchanged confirmation. The
executable exit grammar is versioned: v3 already provides the bounded equity
`breakeven_r` transition, while v4 adds frozen session VWAP/rolling-mean
targets, a monotone trailing stop, and an `exit-before` deadline. The effective
equity stop floor is `max(30 bps, active stressed-cost scenario /
max-cost-to-risk ratio)`; when it binds, fixed-R targets are recomputed and
authored/effective geometry plus binding telemetry are retained. A non-gap
stop/target resting-bracket leg is conservatively cost-charged even without a
trigger-time quote; gap, time, and deadline exits still require a fresh
executable quote. Fit-only diagnostics (prefix and first-signal rates, floor
binding, planned exits, configured/stressed economics, power, behavioral
aliases, intended-versus-delivered risk, provider/feed provenance, pricing
source, configured limits, and pass/fail/unknown row counts) are operator-review
telemetry only; they do not expand exits or authorize proof.
immutable floors are 100 trades plus 30 complete sessions/clusters for
backtest/factory evidence, 100 trades plus 30 complete sessions/clusters for
the sealed qualification window, and 150 trades plus 30 complete sessions for
the parity-matched live-shadow tail. Readiness is reported as 150 offline
sessions plus 30 shadow-selection and 30 disjoint shadow-confirmation sessions
(210 total; the compatibility shadow count is 60). Retirement then requires the same powered
floors, a 95% clustered upper-bound rejection of a 0.05R minimum useful edge,
and at least two negative forward windows for every point. Replay epoch 6
retains epoch-5 point-in-time, executable-row, vehicle-cost, raw-confirmatory-p,
and stressed-cost boundaries, and additionally seals paired synthetic
root-control shadow decisions/replays, diagnostic historical-backfill provenance
with exact calendar metadata, durable live-shadow FDR binding, chronological
paired inference, finite BY input validation, and conservative broker-tick
equity rounding. Epoch-5 evidence remains readable for audit but is quarantined
and cannot authorize until re-derived under epoch 6. Authorization requires
exact epoch equality with current epoch 6; future epochs are audit-only too.
Each current-epoch run seals one immutable verified gate proof, and re-derivation
appends a new proof instead of rewriting history. A valid bounded LLM replacement
is registered first when that lane is enabled. Demoted candidates
may re-prove on a newer shadow run. Paper
outcomes are appended for forward monitoring, scoped to their authorizing proof epoch, and may demote a
deployed edge. Only the broker-free ShadowRunner plus research-side `edge
ingest-shadow` can append a complete parity-matched live proof and advance
`shadow` to `validated`/`champion`; manual/offline promotion cannot bypass that
marker. Legacy validated/champion rows without it can be evaluated or migrated
but remain ineligible until a new authorized live proof. Manual `edge promote`
remains an audited control subject to lifecycle/evidence rules. Backward rollback is rejected; explicit demotion is
the operator safety action. Good edges emit content-addressed edge proof reports
and may send an optional HTTPS webhook. The OpenAI research path uses the
Responses API; prompt/request/schema/configuration/response hashes (with the
effective sampling setting included in configuration) reproduce the evidence
for that invocation, not a guaranteed bit-for-bit identical later model output.
OpenAI requests send `temperature: 0` when supported; when the configured
model or deployment is exactly `gpt-5.6-terra`, the adapter omits that
unsupported parameter and records `temperature: null` in configuration
evidence. Anthropic requests keep `temperature` at `0`; the hashes preserve
the actual request and response. Keep data
provenance, session date, feed, contract identity, and costs with each result.
Do not combine regular-session evidence with pre/post-market or overnight data.
Replay uses the runtime `ReplayPolicy`: `execution.strict_market_data` defaults
to `true`, with strict 30-second market/quote freshness,
DTE/spread/liquidity checks, latest-entry and force-flat cutoffs, and portfolio
position/notional/gross/open-risk/daily-loss limits.
Required records become actionable at the maximum of event timestamp, `as_of`,
and `observed_at`. Delayed recorder bars may signal when observed; execution
enters at that decision/observation time using fresh IEX/OPRA evidence. Delayed
full OHLC never backfills an earlier entry, partial pre-entry ranges are
excluded, and historical bar-fallback rows are diagnostic only and excluded
from authorizing statistics. Fit diagnostics may count planned signal/exit
geometry as quote-required, non-authorizing measurement.
Authorizing fill quality requires both legs to retain provider/feed/source and
quote age: IEX for equity entry and exit, OPRA for option entry and exit, each
no older than 30 seconds. Bar-only, partial-feed, missing, or stale legs remain
diagnostic and cannot authorize a proof. Serial inference uses deterministic
seeded moving-block day/session-cluster bootstrap; its draw count, seed, and
block length are persisted. Effective breadth is persisted/re-verified as a
matched symbol/session correlation diagnostic and never counts as additional N.
Before each factory cycle, prior-cycle family deltas are frozen into a
hash-verified dependence map; the cluster-level BY veto and runtime
one-strongest-per-verified-cluster allocation are authorizing controls, not
breadth diagnostics.

### Broker-free live shadow and ingestion

The broker-free lane is part of the plain Compose startup: `docker compose up -d`
starts `shadow-init` and `shadow` alongside research. ShadowRunner reads
recorder events and eligible ledger candidates, evaluates
each candidate in its own virtual book, and writes only its isolated SQLite WAL.
For each complete session it records candidate, paired synthetic root-control,
and randomized-entry-null replays; mismatch or incomplete rows remain quarantined
and are not gate input. The service has no broker credentials and cannot mutate
orders, broker state, runtime state, or the EdgeLedger.

Replay-diff metadata is retained for 180 days by default; Compose exposes this
as `ALPACA_SHADOW_RETENTION_DAYS`. Pruning reports its floor and count in the
shadow heartbeat without deleting immutable source events, decisions, accounts,
or trades. A non-authorizing prune watermark makes any candidate boundary that
predates deleted replay metadata an explicit `retention_gap`; ingestion remains
blocked until the missing evidence is restored. A corrected malformed recorder
row is retried from its prior byte offset and its session remains out of gate
input until the replay is complete.

The scheduled research cycle invokes `edge ingest-shadow` by default when
`ALPACA_SHADOW_INGEST_ENABLED=1`. The command opens the shadow WAL read-only
from the shared `shadow-data` volume and is a no-op when the WAL is absent. It
accepts only strictly newer,
complete, parity-matched rows with prior qualification and matching
source/config/code/provenance/replay/gate hashes; family and global BY plus the
frozen-dependence-cluster veto and durable online-FDR are applied before an
immutable `lane=shadow` proof and
live-ingestion marker are appended. The `shadow-confirmation-v6`
ingester first splits the tail into
older chronological selection sessions and a newer disjoint confirmatory
window: BY uses selection raw p-values, while only the selected candidate's raw
confirmatory p-value is sent to LORD++. Its preregistered `W0=alpha/2` spends
`(alpha/2)*gamma_t` before discovery, rewards the first discovery with
`alpha/2`, and gives later discoveries the standard `alpha` stream. Historical
v5 rows retain `W0=alpha` and are audit-only, isolated from v6; legacy
v2/v3/v4 rows remain auditable but quarantined. Epoch-6
verification also binds the proof to the durable FDR allocation (scope/test id,
method/version, p-value, alpha, allocation, and decision). The
confirmatory p-value resolution scales
to the next allocation; if the bounded simulation cap cannot resolve it, the
ingester reports `confirmatory_resolution_exhausted` without spending alpha or
advancing a boundary. A failed/mismatched/incomplete tail likewise leaves the
candidate unchanged and ineligible. There is no unsafe auto-skip: unresolved
quarantine blocks the shadow watermark and FDR boundary until source correction
and a bounded parity replay complete.

The same scheduled cycle automatically runs the configured-vs-measured cost
comparison when bars, quotes, and the factory report are available. Its cycle
JSON and scheduler history retain the diagnostic status, artifact path, and
measured-minus-configured delta. This is non-authorizing and non-fatal: a
missing input or failed rerun is visible for investigation and cannot mutate
the ledger, FDR allocation, proof, or promotion state.

Scheduled research evaluates the equity vehicle only by default because the
shipped trader remains on the `shares` execution profile. Set
`ALPACA_RESEARCH_VEHICLES=all` explicitly to evaluate equity and single-leg
option evidence independently; each lane retains separate calibration and
authorization evidence. Option research does not authorize option orders.
`python research.py vehicles` shows the selected research lanes; a
comma-separated subset is an explicit narrowing decision.
Selecting the separate `options` execution profile remains paper-only and
requires reviewed OPRA evidence and controls. The dashboard reports proved
option edges that the default `shares` runtime cannot execute, so that evidence
is visible rather than silently discarded.
Multi-symbol expansion remains deferred until a known-positive end-to-end
reproduction; partial exits remain unimplemented while broker lifecycle and
position reconciliation risks are unresolved.

Execution calibration is disabled by default. Offline discovery/factory work
continues, but shadow ingestion reports `calibration_disabled` until
`ALPACA_RESEARCH_CALIBRATION_ENABLED=1` is set deliberately. Stress/quote
calibration is a separate diagnostic opt-in through
`ALPACA_RESEARCH_STRESS_CALIBRATION_ENABLED=1`.

When execution calibration is enabled, Compose defaults a truly empty journal to
`ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN=1`. The persisted
`bootstrap_unknown` state keeps `authorization_exit_code=2`: it only permits
shadow evidence collection and never claims measured execution calibration or
authorizes production. Set the variable to `0` to require measured calibration.
Existing, thin, mixed-vehicle, stale, or optimistic history remains blocked.

ShadowRunner writes `replay_quarantine` metadata for each incomplete or
mismatched candidate/session replay. A blocked `stale_tail` includes the
session and source/shadow/replay digests. Repair the recorder input and run a
bounded replay (`docker compose run --rm --no-deps shadow --once`); all paired
arms must replay the exact session before the entry is durably marked
`repaired`. Until then `edge ingest-shadow` leaves its boundary and FDR state
unchanged. An authoritative recorder-calendar gap is reported as
`missing_sessions`; a missing/stale calendar is `catalog_unavailable`/unknown.
Both are likewise non-authorizing and never inferred from weekdays.

A slot whose hypothesis proves an edge is reseeded with a new hypothesis in the
same cycle, so logical research capacity stays constant instead of shrinking
with every success. Each cycle result reports `reseeds`, `revived`, and
`active_slots`; `active_slots` below the configured `--strategies` for more
than one cycle is the signal that the factory has run out of unused hypotheses
and needs attention. Slots left idle by an older ledger, or added by raising
`--strategies`, are revived automatically at the start of the next cycle.

To see what research actually did — every slot, every hypothesis it has held,
who proposed each one, every variant with its performance, and exactly why
anything was retired and after how many variants:

```bash
python research.py factory report                      # readable narrative
python research.py factory report --slot 3             # one slot
python research.py factory report --format markdown    # shareable document
python research.py factory report --format json        # for your own tooling
```

Each variant line carries its trade counts split fit/held-out, net P&L, max
drawdown, the held-out edge over baseline and its lower confidence bound, the
multiple-testing-corrected q-value, and a plain-English list of which of the
gates it missed. Each retirement carries the reason, the number of variants
tested, the dominant failure mode, and the hypothesis that replaced it. An
LLM-proposed hypothesis carries its model, attempt count, per-run call budget
and circuit state, full-schema hashes, and per-attempt prompt/request/response
hashes or errors — the audit trail without storing raw model output. The report
is strictly derived and read-only; it cannot change a
lifecycle state.

Inspect raw lineage rows and isolated-account counts with
`python research.py factory status`. Tune bounded capacity with
`ALPACA_FACTORY_STRATEGIES`, `ALPACA_FACTORY_VARIANTS`, and
`ALPACA_FACTORY_WORKERS`; hard validation caps these at 12, 8, and 16.

See how each deployed edge is performing on live paper outcomes:

```bash
python research.py edge paper --vehicle equity --deployed
```

This reports per-edge trade and session counts, total and mean R, win rate, net
P&L, the advisory rolling-R monitor with its floor, and the sequential drift
statistic against the held-out distribution the edge was validated on. It is
the forward counterpart to `edge status`, which reports only lifecycle state
and the confidence an edge was promoted with. The command is read-only and
cannot change a lifecycle state. The same table is on the dashboard as
"Live paper results by edge".

Score the shared cost model against what the account actually paid:

```bash
python research.py calibrate runtime/paper/journal.db
```

The command is read-only. It reconstructs each referenced fill's plan price and
reports adverse cost, model bias, runtime-cap overruns, and a verdict of
`conservative`, `optimistic`, or `insufficient_data`. Calibration is stratified
by runtime mode, vehicle, execution profile, and both entry and exit when
journal references are present; partial fills use plan/reference fields, thin
or missing strata stay insufficient, and equity and options are never pooled.
Calibration is an authorization veto: missing, stale, or insufficient evidence,
an optimistic cost verdict, a terminal material underfill (<80% of requested
quantity), or a partial-cancel rate above 20% exits non-zero and blocks shadow
authorization. Offline discovery/factory diagnostics still run. In-flight
orders are excluded, and the model is never adjusted automatically. Optional
`costs.vehicles.equity`/`.option` schedules are selected and recorded per
vehicle; otherwise the shipped cost model is 4 bps spread, 6 bps slippage,
0.5 bps per-side fee, plus a 0.65 option fee per contract side. Runtime risk
abstains when the configured stress scenario exceeds its cost-to-risk limit and
persists scenario/cost/ratio telemetry, while order journal rows retain
intended/delivered risk, delivery ratio, and shortfall; proof stress diagnostics
are 9/15/25/50 bps with 25 bps as the required veto scenario. Stress charges
scenario bps against entry notional and adds listed-option round-trip fees for
both per-contract sides; it is not a per-side bps charge. The shipped
`max_stressed_cost_to_risk_ratio` is `0.30`, so a 30-bps-floor trade is about
`0.833` cost-to-risk at the 25-bps stress and is vetoed before option fees.

The paper journal is the source for realized performance summaries:
`python report.py runtime/paper/journal.db --json`. The dashboard reads this
journal and edge ledger in read-only mode.

The scheduler records terminal research statuses: `completed` (proof
produced), `completed_no_edge` (valid run, no eligible edge), `no_data` (input
unavailable/empty), `search_exhausted` (the bounded hypothesis space has no
unused successor), `llm_provider_failure` (all bounded provider calls failed),
or `failed` (validation or job failure). The factory CLI keeps proof/no-proof
exit codes at 0/2, reserves 4 for an unevaluable corpus, and uses 5 for
`bounded_space_exhausted` and 6 for `llm_all_calls_failed`; the cycle wrapper
normalizes the latter two to structured terminal statuses and exits 0 so the
reason remains available in scheduler history. Treat `completed_no_edge`,
`search_exhausted`, and `llm_provider_failure` as explicit research outcomes,
not as proof; none permits bypassing the runtime edge gate. Configure the
confirmatory retry budget with `ALPACA_FACTORY_MAX_CONFIRMATORY_ATTEMPTS`
(default 3).

The dashboard is read-only and localhost-bound. It is an observation aid, not
an execution console. Protect SSH and host credentials using your normal
organization controls.

## Common symptoms

| Symptom | Action |
| --- | --- |
| mode/endpoint guard failure | Restore the scoped paper or live guard, check the endpoint, and restart only after authenticated `main.py check`; live also needs `pattern_day_trader=true`. |
| `market closed` or stale calendar | Do not force an entry; refresh the calendar and wait for the next regular session. |
| Missing bars/quotes | Run the recorder `--probe`, inspect `failure_kind`/`last_error`, and verify the exact VM credentials can read the configured IEX feed (and, only when enabled, OPRA). `Up` is process liveness, not proof of successful writes; mark the interval unavailable. |
| Option chain lacks a valid single-leg long contract | Skip the trade. Never substitute a multi-leg, uncovered, or short option. |
| Position remains after close cutoff or startup cleanup | Stop new entries, cancel orders, flatten manually through the scoped Alpaca account, and keep the trader paused. |
| Local/broker state differs | Broker state wins; preserve logs and reconcile before resuming. |
| Watchdog reports `degraded` or residual risk | Treat the account as potentially exposed; inspect broker positions/orders directly, flatten through the scoped account, and do not mark the service healthy merely because it attempted to act. |
| Trader reports `config_audit_unavailable` | Keep exposure monitoring active, repair journal storage/permissions/SQLite, and confirm a later successful audit clears the degraded heartbeat. |
| Research reports disagree | Preserve both artifacts and compare feed/session/contract provenance; do not merge silently. |
