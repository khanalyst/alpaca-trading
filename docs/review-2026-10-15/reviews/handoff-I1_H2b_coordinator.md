# Review handoff: I1 top-level documentation and H2b infrastructure (coordinator review)

Method: done directly by the coordinator.

- **Documentation:** `README.md` lines 1 to 200 were read in full. For all seven top-level documents (`README.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `SETUP.md`, `AZURE_DEPLOYMENT.md`, `deploy/README.md`, `research/README.md`; 5,551 lines), every line stating a concrete parameter was extracted by pattern (stress bps, floors, sessions, tests, LLM, feeds, risk) and checked against the code.
- **Infrastructure:**
  - read in full: `Dockerfile`, `.github/workflows/ci.yml`, the systemd units and timer;
  - Compose: the service, user, capability, port, resource and restart settings;
  - `deploy/health.py` (35 functions) and `deploy/dashboard.py` (25 functions): function-level structure, plus a security read of the request handler and `report_file`. Not line by line.

## Documentation findings

### I1-01 The stress scenario is described inconsistently, sometimes inside one document (MEDIUM, VERIFIED)

- `README.md:346` says "The shipped runtime uses 25 bps as its default", but `README.md:360` and `:367` say "The shipped admission scenario is 9 bps".
- `research/README.md:250-251` says the runtime check uses "the configured 25 bps scenario".
- The code has three different stress numbers:
  - `config.yaml:52` admission is 9 bps;
  - `agent/config.py:86` built-in fallback is 25 bps;
  - `research/gates.py:154` proof-time veto is 25 bps.

  Readers cannot tell which one governs a decision.

### I1-02 One 300-character paragraph is copy-pasted verbatim into six documents (LOW, VERIFIED)

"The shipped admission scenario is 9 bps against a `max_stressed_cost_to_risk_ratio` of `0.30`, ..." appears at `README.md:360`, `ARCHITECTURE.md:384`, `OPERATIONS.md:747`, `SETUP.md:322`, `deploy/README.md:442` and `research/README.md:243`. Duplicated prose is how I1-01 happened.

### I1-03 Documentation volume obstructs operation (MEDIUM, VERIFIED by count)

- The top-level documents total 5,551 lines, plus `research/protocol.md` (732) and `docs/` (1,086). That is more than 7,000 lines of dense, jargon-heavy prose (LORD++, W0, epoch quarantine, cohort identities) before an operator can run anything with confidence.
- `README.md` lines 1 to 200 alone discuss BY, LORD++, moving-block bootstraps, epoch semantics and parity proofs before explaining what the strategy trades.
- Five documents restate the same 210-session readiness arithmetic.

### I1-04 Two environment variables differ by one letter (LOW, VERIFIED)

`ALPACA_RESEARCH_LLM_SECRET_FILE` is the host path for the Compose secret (`compose.yaml:422`). `ALPACA_RESEARCH_LLM_SECRETS_FILE` is the in-container path read by `deploy/research-cycle.sh:470` and `compose.yaml:195`. This is intentional but error-prone, and the documentation uses both.

### I1-05 Stale test claim (LOW, VERIFIED)

`docs/current-findings.md:5`: "719 tests across 49 modules". The current head has 2,359 tests, and one fails.

## Infrastructure findings

### H2b-01 Container hardening is good (positive, VERIFIED)

- Every long-running service runs as UID 10001 with `read_only: true`, capabilities dropped and security options set (`compose.yaml:10-19`).
- The only root container is the short-lived `shadow-init`, with `network_mode: none` and just CHOWN and DAC_OVERRIDE, to fix volume ownership (`compose.yaml:278-300`).
- The dashboard listens on 0.0.0.0 inside the container but is published only to the host's `127.0.0.1` (`compose.yaml:380, 389-390`).
- The HTTP handler is GET-only, sets no-store, nosniff and a CSP header, and confines `report_file` to `research/results/*.md` after resolving the path (`deploy/dashboard.py:1125-1134`).

### H2b-02 Base image pinned by tag, not digest (LOW, VERIFIED)

`Dockerfile:1` uses `python:3.13.5-slim-bookworm`. A digest pin would make the image hash and the paper-trial code identity reproducible.

### H2b-03 CI runs the full suite but is slow and currently red on one test (MEDIUM, VERIFIED)

- `.github/workflows/ci.yml` runs four shards on Python 3.13 with the locked requirements, and runs `docker compose config` and build.
- The edge shard takes about 38 minutes and the factory shard about 32 minutes (local measurement).
- The research shard fails on `tests/research/test_llm_strategy.py:156` at the current head.

### H2b-04 Resource and schedule layout (INFO, VERIFIED)

| service | CPUs | memory limit |
| --- | --- | --- |
| research | 1.5 | 10 GB (default) |
| trader | 1.5 | 2 GB |
| shadow | 0.5 | 2 GB |
| recorder | 0.75 | 768 MB |
| dashboard | 0.5 | 768 MB |
| watchdog | 0.25 | 512 MB |

- Research is scheduled daily at 03:00 UTC (`deploy/alpaca-research.timer`, `deploy/scheduler.py:376`), which is outside US market hours.
- `deploy/scheduler.py:90-97` runs a missed daily run immediately, so a scheduler restart during the session starts a research run, with a 4-hour timeout, while the trader is live. This could not be checked against the VM's core count.
- Container `TZ=UTC`; the code uses `ZoneInfo("America/New_York")` everywhere that sessions matter.

## Summary

- The documentation is accurate on most mechanics, but contradicts itself on the stress scenario, duplicates prose across six files and is too long to operate from.
- Container and dashboard security are well done.
- CI is red on one stale test, slow (about 38 minutes), and the image is pinned by tag only.
