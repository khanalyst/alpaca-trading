# Paper incumbent and persistent diagnostic shadows

This is an experiment and operations workflow, not evidence that a profitable
strategy exists. The paper incumbent uses the normal paper broker/runtime
controls while the diagnostic shadow uses independent modeled books. Neither
lane is an authorizing proof path.

Diagnostic studies, shadow candidates, and paper-trial verdicts cannot
authorize an entry or override an operator/runtime pause. A pause, stale source,
account identity mismatch, or replacement blocker remains independent of
statistical counts.

## One frozen paper incumbent

`research.paper_trial` is disabled in the shipped `config.yaml`. Enabling it
requires all of the following:

* `enabled: true`, a non-empty explicit `trial_id`, and exactly one
  `variant_id` from the fixed diagnostic catalog;
* paper mode, `research.require_validated_variant: true` retained as a
  configuration guard, and the configured `shares` execution path; and
* an account-bound activation with a confirmed flat local and broker book.

The runtime rejects `auto`, arbitrary rule specs, runtime LLM selection,
options, delayed feeds, and live mode for this exception. This block shows the
shipped disabled baseline, not an enabled profile. Enabling a trial requires
the explicit IDs and activation checks above:

```json
{
  "research": {
    "trial": {
      "enabled": true,
      "min_sessions": 20,
      "min_trades": 20,
      "min_mean_r": 0.0,
      "min_total_r": 0.0
    },
    "paper_trial": {
      "enabled": false,
      "trial_id": "",
      "variant_id": "",
      "accepted_session_report_root": "/app/shadow/session-acceptance",
      "max_review_sessions": 60
    }
  }
}
```

The main runtime pause is durable. Its orderly `SIGTERM`/`SIGINT`
`request_shutdown` path persists `operator_pause=true`; this is not limited to
manual pauses, and a restart does not clear it. A paper-trial record that still
says `running`, or a newly written trial report, does not clear the runtime
pause. Clearing it requires the authenticated flat-book control path, for
example `python main.py resume`; failed journal, preflight, reconciliation, or
flat-book checks leave the pause set.

At activation, `PaperTrialRuntime` freezes the rule spec, variant/candidate
identity, feed/provider, universe, risk, execution/cost/session policy, code
identity, shadow cohort identity, and acceptance-report root. Restart does not
choose a new winner. A configuration or incumbent identity change is refused.
Outcomes are appended atomically with closes and carry
`authorizing: false` and `proof_authority: false`; they never enter the
authorizing EdgeLedger/FDR outbox.

The retained `require_validated_variant` setting is a configuration invariant,
not ordinary EdgeLedger proof authority for this exception. When the explicit
paper-trial block is enabled, `Engine` sets its normal edge requirement aside
and runs only the frozen catalog arm. The separate account identity, flat-book
activation, frozen policy, and non-authorizing outcome checks remain mandatory.

The trial policy counts only accepted complete sessions and closed parent
outcomes. It does not manufacture observations on quiet days. The verdict
implementation in `research.trial` requires finite R values and a usable 95%
session-cluster interval before a terminal decision:

* `running` means configured session/trade floors are not both met;
* `passed` requires positive total and mean R plus a lower bound above the
  configured mean-R floor;
* `failed` requires nonpositive total and mean R plus an upper bound below the
  configured floor; and
* `inconclusive` covers unusable R, inadequate or overlapping uncertainty, and
  all other cases that establish neither side.

At `max_review_sessions` the lifecycle becomes `review_required` unless it has
already failed. This is a review pause, not a negative strategy verdict. Daily
loss, exposure, spread, stale-data, calendar, protection, shutdown, and kill
controls remain independent; tenure never forces an entry or prevents a safety
exit.

List the exact catalog ids before selecting one:

```bash
.venv/bin/python -c \
  'from research.diagnostic_shadow import _logical_arms; print("\n".join(a["variant_id"] for a in _logical_arms()))'
```

The checked-in `deploy/paper-orb.config.json` and
`deploy/paper-orf.config.json` are explicit profile examples. They are not a
current account status or a result claim. Read the selected profile and the
persisted runtime identity before any activation; do not reset runtime state to
change an incumbent.

## Activation and replacement safety

Before enabling a profile, stop the trader, run the authenticated readiness
check, and verify both local and broker positions/orders are flat. Persist the
`ALPACA_AGENT_CONFIG_FILE` choice wherever Compose obtains its environment so a
restart cannot silently select a different policy.

To cancel an evidence-empty incumbent, use the audited operator command with
the exact values read from the persisted state:

```bash
.venv/bin/python deploy/cancel_paper_trial.py \
  --config config.yaml \
  --confirm-trial-id TRIAL_ID \
  --confirm-incumbent-identity INCUMBENT_IDENTITY \
  --reason "bounded operator reason"
```

The command above is a host-repository example. If it is run inside Compose,
use the container interpreter and /app/config.yaml together; do not mix a
host .venv/bin/python with a container-only path. Select the actual frozen
profile and persisted identity before substituting these values.

The command requires paper mode, `operator_pause=true`, an existing running or
already-cancelled evidence-empty trial, a flat local book, and a broker GET
snapshot showing zero positions and zero open orders. It writes an immutable
operator-cancellation audit before changing lifecycle state. The cancellation
path cannot delete outcomes or bypass the incumbent identity check. Replacing a
terminal trial likewise requires an audited terminal snapshot and another
confirmed flat book.

Paper and shadow calculate the same code identity from the agent and research
Python sources, recorder, recorder market-data adapter, dataset reader, and
dependency lock. After a rollout, compare the paper descriptor and shadow
health for both code and cohort identity. A changed identity requires a new
trial and report directory through the normal audited transition.

Large samples in the session journal use lossless per-line compression when
that saves space. The session acceptance reader supports both encodings in
the same journal and checks size limits before finalization.

## Persistent shadow books

`deploy/shadow.py` and `research.live_shadow` maintain independent virtual
accounts. Every arm has its own cash, positions, modeled orders, fills, event
cursor, and diagnostic P&L. State and the consumed-event cursor commit
together, so a retry cannot credit a close twice. Pooling arm balances or
returns is not a portfolio result.

Entries and market exits use causal exact-feed/provider quotes (ask for buys,
bid for sells) with the shared cost model. Completed-bar stop/target handling,
gap behavior, thesis deadlines, and force-flat use the shared rule transition.
Missing executable boundary quotes remain unpriced/refused in strict lanes;
diagnostic bar fallback is labelled and cannot become proof evidence. Broker
shortability, buying power, pending orders, and actual fills are not modeled
broker authority.

The direct shadow CLI defaults to the 24-rule cohort. Compose may set
`ALPACA_SHADOW_INCLUDE_IBR=1`; direct use can pass `--diagnostic-include-ibr`
to add seven registered IBR arms. The environment value must be exactly `0` or
`1`, and `--diagnostic-include-ibr` requires diagnostic mode.

```bash
ALPACA_SHADOW_INCLUDE_IBR=1 .venv/bin/python deploy/shadow.py \
  --corpus runtime/research/recorded/market.csv \
  --edge-db runtime/research/diagnostic-edge-lab.sqlite3 \
  --shadow-db runtime/research/diagnostic-shadow.sqlite3 \
  --config config.yaml \
  --health-file runtime/research/diagnostic-shadow-health.json \
  --acceptance-root runtime/research/diagnostic-shadow-acceptance \
  --acceptance-recorder-root runtime/research/recorded \
  --diagnostic --diagnostic-include-ibr --once
```

The paths above are intentionally isolated for a one-off diagnostic. Stop any
existing shadow writer before retrying with a path it already owns, or choose
new database, health, and acceptance paths; never run two writers against the
same shadow store.

The other bounded flags include `--health-file`, `--acceptance-root`,
`--acceptance-recorder-root`, `--acceptance-gap-tolerance`, `--interval`,
`--max-candidates`, `--max-events`, `--diagnostic-session-max-events`,
`--replay-session-max-events`, `--max-decisions`, `--max-workers`, and
`--retention-days`.

## Full-session operational evidence

Each shadow poll writes a health heartbeat and bounded acceptance samples under
the configured acceptance root. A complete session requires exact calendar
coverage, stable deployment/code/cohort/activation identities, all active arms,
genuine post-activation event progress, and current symbol observations. A
heartbeat that is unchanged, stale, or missing does not count. Quote age and
completed-bar publication lag remain bounded; re-fetching an old bar cannot
refresh its completion watermark.

The scheduler may run a metadata-only acceptance census before expensive
provider/research work. Insufficient accepted forward history reports
`waiting_for_forward_sessions`; it does not create accepted sessions or turn a
historical corpus into forward evidence.

## Paper epoch store

For a separately isolated paper-primary/shadow control-plane epoch, use
`research.py paper-epoch` with a dedicated SQLite path. The store is append-only
and integrity-checked; it must not share tables with EdgeLedger:

```bash
.venv/bin/python research.py paper-epoch status --db PATH
.venv/bin/python research.py paper-epoch verify --db PATH
.venv/bin/python research.py paper-epoch export \
  --db PATH --epoch EPOCH_ID --output-root research/results/paper-epochs
```

Lifecycle operations are `create`, `start`, `record`, `stop`, `complete`, and
`seal`; each requires its bounded JSON schema. The export is immutable and
content-addressed. A paper epoch is operational evidence and remains separate
from authorizing candidate proofs.
