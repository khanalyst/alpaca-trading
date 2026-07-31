# Final implementation handoff — 2026-08-01

Branch: `codex/platform-correctness-docker`

Original assessed main commit: `94d10e1`

Implementation checkpoint commit: `d03798d`

Status: the assessed code-correctness work is implemented. The repository is
still demo/research-first: no current edge is eligible for live capital, and
fresh data, forward evidence, human review, and an explicit registry/config
change remain operational prerequisites.

## Assessment reconciliation

The consolidated assessment correctly identified material gaps in assignment
draining, baseline/candidate equivalence, paper execution, evidence provenance,
statistical qualification, backups, and deployment observability. Those gaps
were treated as correctness issues and repaired without redesigning the whole
platform.

One recommendation is intentionally narrower than the assessment proposed.
All seven research lanes receive the same market snapshot and timestamp and
keep isolated accounts/assignments, but the coordinator evaluates lanes in a
bounded sequence and serializes durable writes. They are logically concurrent
experiments, not seven simultaneous SQLite writers. Physical wall-clock
concurrency is not needed for causal comparability or correctness, and was not
added merely to satisfy an architectural preference.

## Implemented correctness work

- Assignments use fresh equivalent baseline/candidate accounts, stop opening
  new positions at the collection boundary, drain existing positions, preserve
  immutable retry lineage, and freeze one terminal outcome only after evidence
  is resolved or explicitly invalid.
- Paper execution rechecks ticker/book freshness, requires continuous one-minute
  execution-bar coverage, accepts maker fills only from full candle intervals
  contained before expiry, and uses a 120-second paper maker TTL so one fully
  observed later one-minute candle can qualify before cancellation. It records
  observed execution-bar timestamps while preserving unknown intra-bar
  chronology with conservative fill/stop ordering, charges spread once, and
  rebases filled notional/risk to observed depth without exceeding the original
  cap.
- Runtime funding history accepts explicit realized settlements only; forecast
  rates remain context and cannot be booked as realized cash flow.
- Current v3 forward qualification reconstructs all eligible completed attempts
  for each setting with that assignment's contemporaneous baseline. Missing,
  active, rejected, mixed-provenance, or invalid evidence fails closed.
- Family correction uses a calibrated paired six-hour-cluster sign-flip
  p-value. It is valid only under cluster-delta sign exchangeability under a
  null distribution symmetric about zero. That assumption is persisted and
  validated; the test is not described as assumption-free.
- Complete manifest-bearing immutable snapshot trees are included in verified
  backups. In-progress or non-manifested directories are excluded, and every
  captured file is size- and SHA-256-checked.
- The Compose research scheduler refreshes durable `RUNNING` health every 30
  seconds, inside the existing 180-second stale window.

## Edge and promotion boundary

The end-to-end lifecycle is deliberately split:

1. A terminal `WORKED` assignment saves immutable
   `research_edge_evidence.v1` as a `RESEARCH_ONLY` `EDGE_CANDIDATE` lead with
   `promotion_allowed: false` and `forward_qualification_required: true`.
2. `research.py forward-qualify` must separately produce current v3
   qualification from the complete paired assignment evidence and corrected
   axis family.
3. `./.venv/bin/python research.py prepare-review-artifacts` considers only
   qualified variants. It fails closed unless persisted edge evidence and all
   non-manual checks validate, then idempotently creates an immutable,
   content-addressed `DRAFT_REVIEW_REQUIRED` T3 artifact.
4. Draft preparation cannot mark manual review complete, edit
   `agent/registry.py` or `config.yaml`, change the active strategy, or enable
   live trading. Human review, a reviewed T3 record, and the actual
   registry/configuration/deployment change remain explicit manual actions.

`research/nightly.sh` performs qualification and draft preparation, but never
crosses this live-capital boundary.

## Imported VM evidence

The supplied `vm-import/2026-07-30/` export was inspected read-only. Its 3,520
legacy shadow decisions have no stored outcomes satisfying the current
horizons and no current manifest/provenance chain. The files remain useful
audit history, but the tournament and promotion paths correctly reject them as
current evidence. No edge was inferred or fabricated from the export.

## Remaining operational prerequisites

These are environment/evidence requirements, not unfinished application-code
defects:

- collect a fresh manifest-bearing OKX snapshot and retain it in a verified
  backup;
- run new chronological forward assignments under current feed/model/code
  identities until the sample, coverage, resolution, and regime floors are met;
- obtain current G2 fidelity and v3 qualification before preparing a draft T3
  artifact;
- complete human review and explicitly edit/review registry/configuration before
  any strategy could become live-eligible;
- provision credentials and a genuinely external backup mount in the target
  environment; and
- build/run the image on a host with a Docker daemon before deployment.

Until those prerequisites are satisfied, the correct state is collection and
research—not promotion.

## Verification status

Verification completed on 2026-08-01:

- `./.venv/bin/python -m pytest -q tests/research/test_docs_are_current.py`:
  52 passed, 50 subtests passed.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q
  -p no:cacheprovider`: 1,046 passed, 1 skipped, 274 subtests passed in 91.42
  seconds.
- Production-source `compileall`: passed.
- `bash -n research/nightly.sh`: passed.
- `docker compose config`: passed and rendered all four services. This command
  validates Compose configuration but does not build images or contact the
  Docker daemon.
- `git diff --check`: passed.

The cache provider was disabled because the sandbox would not allow its cache
file under `/Users/talhakhan/.pytest_cache`; test execution and results were
unaffected. The final audits repaired the maker-expiry candle boundary and
aligned the shipped paper TTL with its full-candle evidence granularity; the
complete suite above includes regressions for both.
