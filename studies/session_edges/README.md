# Session-edge research and exact-rule qualification

The session study is an upstream research lane for the main validation system.
It adds sixteen bounded session, swing and volatility-context mechanisms and
long-history screening. It does not authorize paper or live entries. Main's
existing proof, source provenance, qualification, risk and trial controls remain
unchanged. See [the integration record](../../docs/session-edge-integration-2026-11-13/README.md).

Historical outputs under `results/` are preserved as originally recorded. Their
v1 `confirmed` and `paper_candidate` labels are historical claims, not current
qualification. The saved 124 alpha statistics yield zero Holm and zero BY
survivors when corrected together. That check does not replay the corrected
strategy or prove the effect absent.

## Evidence and execution contracts

- `discover.py` screens on development data and applies Holm/BY to the claimed
  alpha beyond the instrument, as well as total-return checks. All labels are
  historical support or diagnostic; every book has `trade: false`.
- The 2020 and 2024 dates are historical comparison windows. They overlap
  published strategy research and carry no untouched-holdout claim.
- Daily close/RSI/streak decisions execute at the next observed session open.
  They are distinct from the minute 15:50 strategy. Daily evidence is never
  translated into minute-rule qualification.
- Minute simulations retain observed-bar masks. Unobserved signal/fill bars
  cannot become executable prices. The noise-band default exits at 15:58,
  matching the shadow runner's timing. Statistical screens reject incomplete
  sessions instead of turning an unavailable future exit into a zero return.
  Missing sessions cannot silently erase an already-owned swing position.
- Round-trip costs are scenarios, including entry and exit sides; replace
  defaults with measured instrument/time/size costs for executable evaluation.
- The cumulative ledger binds experiment name, data content, simulator/code,
  costs and protocol. Reusing a name with changed inputs fails. A new explicit
  experiment keeps prior trials in the cumulative correction.
- Import requests bind the exact bounded specification and execution form.
  Main reviews actual trade-level evidence and matched controls with its
  existing floors; session-return summaries are never relabelled as trades.
  Unsupported runtime/lifecycle or missing newer parity-shadow proof blocks
  authorization, regardless of the historical tier.

[EDGES.md](EDGES.md) is the generated signal catalogue. Options-labelled signals
read volatility indices or an expiry calendar and trade underlying shares;
they do not establish option-contract mispricing. Turn-of-month research
requires an explicit exchange calendar rather than inferring future month ends
from the supplied price dataset. Supply its full date coverage with
`--exchange-calendar PATH` (an ordered JSON list of dates or broker calendar
objects with `date` fields). Missing calendar context is reported as a skip.

## Historical rerun on the VM

Use a new output directory on the persistent runtime volume and one shared
ledger outside those output directories. Preserve config, pause, frozen trial,
active ledgers and original evidence. Research does not require stopping the
trader/watchdog, changing the trial or enabling orders.

After deploying the tested main revision, run inside the existing application
image with the persistent runtime volume mounted. Substitute the actual SIP
month-file and calendar paths; no command below fetches or prints credentials.

```bash
python -I studies/session_edges/run.py --alpaca-dir /app/runtime/session_edges/sip --symbols QQQ,SPY --calendar /app/runtime/session_edges/sip/calendar.json --out /app/runtime/session_edges/runs/registered-causal-v2
```

Initialize a new shared ledger once. If the old public discovery ledger should
be retained in the experiment history, add `--migrate-ledger` pointing to it;
legacy rows are retained as historical diagnostics and never upgraded to proof.

```bash
python -I studies/session_edges/discover.py --alpaca-dir /app/runtime/session_edges/sip --symbols QQQ,SPY,XLK,IVW --calendar /app/runtime/session_edges/sip/calendar.json --ledger /app/runtime/session_edges/experiments/ledger-v2.json --initialize-ledger --experiment sip-causal-v2 --out /app/runtime/session_edges/runs/sip-causal-v2
```

Repeat the same experiment only with identical bound inputs. A changed dataset,
cost or simulator needs a new experiment name and explicit `--new-experiment`;
use the same ledger so prior admitted trials remain counted. Do not use an old
ledger's scalar result as evidence for new code.

Discovery emits the research record, a shadow-only book and a main-candidate
review. Missing real trade evidence or lifecycle support remains a reported
blocker rather than an implied pass. A later genuinely new confirmation window
must be selected and sealed through main's existing pipeline.

The default cost assumptions are 1bp round trip on SPY/QQQ/IWM, 1.5bp on DIA/VTI
and 3bp elsewhere, stressed at twice those levels. Use `--cost-bps` with the
measured values appropriate to the intended execution. These defaults are not
claims about current broker fills.

## LLM experiments

```bash
python -m studies.session_edges.propose prompt --discovery OUT/discovery.json --symbols QQQ,SPY,IWM,XLK
python -m studies.session_edges.propose ask --model MODEL --discovery OUT/discovery.json --symbols QQQ,SPY,IWM,XLK --out proposals.json
```

The proposal envelope retains exact model/provider identity, prompt/request,
response, configuration fingerprints and thesis. Discovery retains attribution
for proposals and duplicate grid matches. Failed and rejected proposals are
experiment attempts too. The prompt exposes development statistics only;
modern model pretraining can still contain later historical knowledge.

`research/session_model_experiment.py` provides preregistered equal-budget
mechanical/model lanes, attempt accounting, frozen selections and diagnostic
paired outcome comparisons. Run its `--help` for the plan/attempt/freeze/outcome
commands. No model is considered superior without recorded future outcomes.
The checked-in v1 promoted hypotheses are all labelled grid-generated.

## Forward shadow and owned-position recovery

```bash
python -m studies.session_edges.live run --book OUT/edge_book.json
```

Shadow sends no orders. The optional `deploy/compose.session-edges.yaml` service
also runs shadow-only; the ordinary six-service update does not enable it.
Normal `--paper-orders` and `--live-orders` entries are disabled until an exact
main proof/runtime bridge exists. A book's text label or `trade` field cannot
authorize them. There is no two-day route from historical output to live entry.

For a runner that already owns positions from an earlier release, the explicit
`--recover-owned` mode uses its original state directory, saved position
ownership and pending client IDs. It can only reconcile and reduce that saved
exposure to zero; it cannot open new positions or use a replacement book.
Inspect `python -m studies.session_edges.live preflight --help` before recovery.
Pending submission timeouts retain the same identity until resolved. Position
reduction does not depend on successful market-data fetching.

The main account's operator pause and frozen trial are independent of this
study. Neither research nor deployment should resume or rebind them.

## Reproducible saved-artifact check and tests

```bash
python -m research.session_artifact_audit studies/session_edges/results/discovery-public-2026-11-12/discovery.json --out OUT/saved-artifact-audit.json
python deploy/test_suite.py
```

The first command only recomputes correction from saved statistics. A raw-data
research rerun requires the actual market corpus and is reported separately.
