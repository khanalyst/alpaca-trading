# Staleness / Dead-code / Over-engineering Audit — alpaca-trading @ claude/trading-strategy-audit-zveg57

Method: AST symbol map (`scratchpad/deadcode.py` -> `defs.json`), relative-import-resolved module
graph, targeted grep. CONFIRMED = mechanically verified. SUSPECTED = judgement.

Repo size (measured): 62,273 py LOC total; tests/ = 24,957 LOC; source = 37,316 LOC.
Docs = 3,873 lines markdown (README 509 + ARCHITECTURE 823 + OPERATIONS 631 + SETUP 249
+ AZURE_DEPLOYMENT 226 + research/README 641 + research/protocol.md 454 + deploy/README 60
+ docs/AUDIT* excluded).

---
## 1. DEAD CODE

### 1.1 Module import graph (CONFIRMED)
Only these non-test source modules have **zero** non-test importers:

| module | LOC | test importers | verdict |
|---|---|---|---|
| deploy/backfill.py | 319 | 1 | entrypoint (`python -m deploy.backfill`) - LIVE |
| deploy/dashboard.py | 895 | 3 | entrypoint - LIVE |
| deploy/research_dataset.py | 210 | 1 | entrypoint - LIVE |
| deploy/scheduler.py | 381 | 2 | entrypoint - LIVE |
| deploy/shadow.py | 96 | 1 | entrypoint - see 2.1, 96L of duplication |
| deploy/watchdog.py | 286 | 1 | entrypoint - LIVE |
| **docs/audit-scripts/backfill_pit.py** | **25** | 0 | **ORPHAN — CONFIRMED DEAD** |
| **docs/audit-scripts/feas.py** | **61** | 0 | **ORPHAN — CONFIRMED DEAD** |

`docs/audit-scripts/*.py` (86 LOC) are prior-audit throwaway scripts committed to the tree;
zero importers, zero references in any doc, `.sh`, or unit file (grep: no hits outside the files).

### 1.2 Symbol-level dead code (CONFIRMED — grep shows the ONLY occurrence is the definition)

| file:line | LOC | symbol | evidence |
|---|---|---|---|
| research/ibr.py:21 | 1 | `RUNTIME_MAX_MARKET_DATA_AGE_SECONDS = 30.0` | sole occurrence in repo |
| research/ibr.py:622-624 | 3 | `_replay_session.option_exit_reference` | sole occurrence |
| research/proof.py:40-41 | 2 | `ProofResult.artifact_path` | sole occurrence |
| research/edge_ledger.py:577-585 | 9 | `EdgeLedger._trial_epoch` | sole occurrence |
| research/factory_report.py:32-33 | 2 | `_LLM_ORIGINS` frozenset | sole occurrence |
| research/costs.py:638 | 1 | `_cost_model_for_vehicle = cost_model_for_vehicle` (back-compat alias) | sole occurrence |
| research/proof_payload.py:62-65 | 4 | `_get` | sole occurrence |
| deploy/recorder.py:122 | 1 | `SESSION_CALENDAR_RETENTION_DAYS = 90` | sole occurrence — a retention policy that is never enforced |
| deploy/recorder.py:364-370 | 7 | `_existing_state` | sole occurrence |
| deploy/recorder.py:373-375 | 3 | `_existing_keys` | sole occurrence |
| agent/alpaca_session.py:31-34 | 4 | `as_utc` | sole occurrence |
| agent/alpaca_session.py:142-143 | 2 | `AlpacaSession.from_env` | sole occurrence |
| agent/alpaca_session.py:183-185 | 3 | `paper_env_guard` | sole occurrence (`trading_env_guard` is the live one) |
| agent/market.py:134-136 | 3 | `MarketData.can_exit` | sole occurrence |
| research/live_shadow.py:871-880 | 10 | `ShadowStore.replay_accounts` | CONFIRMED (prior auditor correct) |
| agent/edge.py:83-102 | 20 | `_latest_passing_proof` | CONFIRMED test-only (2 test refs, 0 source) |
| research/live_shadow.py:1697-1730 | 34 | `_parser()` + `main()` + `__main__` block | CONFIRMED dead CLI — see 2.1 |
| research/live_shadow.py:1692-1694 | 3 | `run_shadow_once` | test-only |

Subtotal strictly-dead symbols: **112 LOC**.

FALSE POSITIVE NOTE for other auditors: `deploy/dashboard.py:833/868/872` (`do_GET`,
`do_POST`, `log_message`) show as unreferenced but are `BaseHTTPRequestHandler` framework
overrides — NOT dead.

### 1.3 Test-only public API (CONFIRMED — zero non-test source references)
These are exported/public but exercised only by the test suite. They are not "dead" in the
pytest sense, but they are API surface no production path uses:

- `research/edge_ledger_proof.py:12-25` `run_engine_epoch` (14L, 10 test refs, 0 source)
- `research/edge_ledger_proof.py:28-41` `run_engine_epoch_current` (14L, 8 test refs, 0 source)
- `agent/governance.py:66-79` `redact`, `:96-113` `config_diff`, `:274-300` `config_history`,
  `:303-306` `config_version_at` — 63 LOC of governance API with **zero** source callers.
  `config_history`/`config_version_at` are pure read APIs for a config-audit trail that
  nothing in the runtime or the dashboard reads.
- `research/trial.py:44-62` `trial_policy` (19L) — 3 test refs, 0 source
- `research/calibration.py:184-186` `load_entry_fills` — 3 test refs, 0 source
- `deploy/scheduler.py:106-114` `configured_mode` — 1 test ref, 0 source
- `deploy/dashboard.py:722-731` `report_file` — 2 test refs, 0 source

### 1.4 `__all__`-only exports (CONFIRMED: defined, listed in `__all__`, never called)
Symbols whose only two occurrences are the `def` line and the `__all__` entry:
- `research/fit_diagnostics.py:588-594` `filter_behavior_aliases` (7L, `__all__` :628)
- `research/fit_diagnostics.py:597-600` `fit_behavior_fingerprint` (4L, `__all__` :629)
- `research/fit_diagnostics.py:603-613` `audit_exit_grammar` (11L, `__all__` :627)
- `research/stats.py:134-209` `cluster_bootstrap_lower_bound` (**76L**, `__all__` :794) —
  a full cluster-bootstrap CI routine that nothing calls. 
Subtotal: **98 LOC** of `__all__`-laundered dead code (the `__all__` entry is what keeps
naive greps from flagging it).

### 1.5 Orphan scripts
- `docs/audit-scripts/backfill_pit.py` (25L) + `docs/audit-scripts/feas.py` (61L) = **86 LOC**,
  zero references anywhere in the repo.

---
## 2. DUPLICATION / DRIFTABLE REIMPLEMENTATION

### 2.1 Two shadow CLIs (CONFIRMED — CRITICAL for ops correctness)
`deploy/shadow.py` (96L) and `research/live_shadow.py:1697-1730` are two independent
argparse CLIs over the same `ShadowRunner`, with **divergent flag sets**:

| flag | deploy/shadow.py | live_shadow.main |
|---|---|---|
| `--corpus`, `--edge-db`, `--shadow-db`, `--interval`, `--once` | yes | yes |
| `--health-file` | yes (:29) | **absent** |
| `--max-candidates` (32) | yes (:33) | **absent -> ShadowConfig default** |
| `--max-events` (20000) | yes (:34) | **absent** |
| `--max-decisions` (100000) | yes (:35) | **absent** |
| `--retention-days` (14) | yes (:36) | **absent** |
| heartbeat/health JSON | yes (`_write_health` :41-62) | **absent** |

`compose.yaml:259` runs `deploy/shadow.py`, so `research/live_shadow.py:1699-1730`
(`_parser`+`main`+`__main__`) is **34 LOC of dead, silently-weaker duplicate entrypoint**.
Anyone who runs `python -m research.live_shadow --once` gets no health file, so
`compose.yaml:274`'s healthcheck (`deploy/health.py shadow --path /app/shadow/health.json`)
would report stale forever. Delete it.
Also `deploy/shadow.py:88` re-`import time` inside `main()` while `time` is already
imported at `deploy/shadow.py:11` — shadowing bug-in-waiting, 1 LOC.

### 2.2 The "one evaluator" claim: PARTLY TRUE (CONFIRMED)
Signal generation IS shared for the rule family:
- runtime: `agent/engine_cycle.py:457,495` -> `agent/contracts/rule.generate_rule_signal`
- shadow: `research/live_shadow.py:1186-1188` -> same functions
- factory replay: `research/factory_core.py:17-21` imports `evaluate_rule_signal`
So rule *signal* logic has ONE implementation. Good.

**But account/exit simulation has FOUR independent implementations:**

| # | implementation | file:line | LOC |
|---|---|---|---|
| 1 | `factory_core._simulate_trade` + `simulate_account` | research/factory_core.py:275-607, 630-822 | 333 + 193 = **526** |
| 2 | `ibr._replay_session` | research/ibr.py:346-772 | **427** |
| 3 | `edge_discovery_core.null_control_account` | research/edge_discovery_core.py:629-911 | **283** |
| 4 | runtime broker lifecycle | agent/execution_lifecycle.py (whole) | 1536 |

(3) explicitly re-derives entry-visibility, stop geometry, quote fill and equity accounting
that (1) already has (`record_is_available`/`record_available_at`/`index_quotes` are
imported and re-called independently in each). (2) is a fifth cost/exit ladder for the IBR
family. Each carries its own copy of "is this bar visible at this cutoff" and
"which side of the quote fills" decisions — the exact class of logic that drifts silently.

CONFIRMED counter to the stated lead: `factory_core.py:30` **does** import
`record_is_available` and calls it at `:150`. The lead ("factory_core never imports
record_is_available") is **FALSE** — it does (`research/factory_core.py:30`, `:150`).

### 2.3 Circular-facade forwarding shims (CONFIRMED — pure ceremony)
`research/edge_discovery_core.py:31-119` = **89 LOC** of `def name(*a, **k): return
_facade_dependency("name")(*a, **k)` wrappers (17 of them) that resolve names back through
`research.edge_lab` and `research.factory_core` at call time, because
`research/edge_lab.py:37-40` imports *from* `edge_discovery_core`. A circular import broken
by runtime `getattr`. Every one of these 17 names is available by direct import from
`research/gates.py`, `research/market_data.py`, `research/ibr.py`, `research/factory_core.py`.
This is 89 LOC that exists only to preserve a module cycle that should not exist.

### 2.4 `research/edge_lab.py` is a 809-line module that is mostly a re-export facade
Lines 12-42 re-export ~45 names from `edge_ledger`, `gates`, `costs`, `ibr`,
`market_data`, `stats`, `edge_discovery_core`, `edge_identity`. Its `__all__`
(`:804-809`) is 23 names of which **only `discover` and `DiscoveryError` are defined
here**; the other 21 are pass-throughs. Measured: 9 of 10 source importers of `edge_lab`
take only `EdgeLedger`/`DEFAULT_DB_PATH`/`content_hash`/`canonical_json` — all of which
live in `research/edge_ledger.py`. Real content of the module is `discover()`
(`:160-803`, 644L) plus 5 private helpers (110L). The facade layer is ~55 LOC of imports
plus the 89 LOC of shims it forces in 2.3.

### 2.5 Test-seam indirection in production code (CONFIRMED)
`agent/engine_cycle.py:122-137` — 16 LOC of `def generate_ibr_signal(*a,**k): from . import
engine; return engine.generate_ibr_signal(*a,**k)`. The module docstring
(`agent/engine_cycle.py:4-6`) states the purpose outright: *"preserving existing patch
seams"*. `agent/engine.py:19-23` already imports these names directly from
`agent/contracts/*`. Production indirection whose only consumer is `unittest.mock.patch`.

---
## 3. DOCUMENTATION DRIFT

### 3.0 Claims VERIFIED TRUE (so other auditors do not re-derive them)
- "eleven slots / four variants": TRUE. `agent/contracts/rule.py:31-47` = 11 families;
  `research/factory_core.py:49` `DEFAULT_STRATEGIES = len(RULE_FAMILIES)` = 11;
  `:50` `DEFAULT_VARIANTS = 4`.
- Floors 100 / 30 / 150: TRUE. `research/gates.py:72-79`
  (`PROTOCOL_BACKTEST_MIN_TRADES=100`, `..._MIN_SESSIONS=30`, `PROTOCOL_SHADOW_MIN_TRADES=150`,
  `PROTOCOL_SHADOW_MIN_SESSIONS=30`, `PROTOCOL_SHADOW_MIN_CLUSTERS=30`,
  `PROTOCOL_QUALIFICATION_MIN_TRADES=100`, `..._MIN_SESSIONS=30`);
  `research/live_shadow_ingest.py:34-35` = 150/30. Trial default 30/100 at
  `research/trial.py:38-39`. All match every doc that states them.
- 30 bps stop floor: TRUE. `agent/contracts/rule.py:55` `MIN_STOP_DISTANCE_BPS = 30.0`.
- Cost defaults 4 / 6 / 0.5 bps and $0.65/contract/side: TRUE.
  `research/costs.py:33` (4.0), `:39` (6.0), `:41` (0.5), `:45` (0.65).
- 8-ETF universe SPY/QQQ/IWM/DIA/XLF/XLK/XLE/XLV: TRUE, `config.yaml:18`.
- Compose service names (`recorder`, `trader`, `watchdog`, `research`, `shadow-init`,
  `shadow`, `dashboard`) match every `docker compose ... <svc>` invocation in
  OPERATIONS.md/SETUP.md/README.md. Volumes `runtime-data`, `research-cache`,
  `research-results`, `shadow-data` match `compose.yaml:326-330`.
- systemd units named in AZURE_DEPLOYMENT.md:295-297 all exist in `deploy/`.
- Paths `runtime/research/edge_lab.sqlite3`, `runtime/research/recorded/sessions/`,
  `research/results/edges`, `research/results/factory` all match code
  (`deploy/dashboard.py:648`, `deploy/recorder.py:105-142`, `agent/config.py:97`,
  `research/factory_report.py:799`).

### 3.1 FALSE / STALE claims

| # | doc:line | claim | contradicting code | severity |
|---|---|---|---|---|
| D1 | research/protocol.md:88 | "Preregistered all-in stress scenarios are 9, 15, 25, and 50 **bps**" | consistent with `research/costs.py:52` / `gates.py:98` (`COST_STRESS_SCENARIOS_BPS`) — **but** contradicted by the same file at protocol.md:246 | HIGH (internal contradiction) |
| D2 | research/protocol.md:246 | "stressed cost-to-risk (**9/15/25/50x**) summaries" — states multipliers | `research/gates.py:98` `COST_STRESS_SCENARIOS_BPS = (9.0,15.0,25.0,50.0)` and `gates.py:736` iterates them as `scenario_bps`; `gates.py:794` compares `item["round_trip_bps"] == 25.0`. They are **absolute bps, not multipliers**. protocol.md:246 is FALSE. | HIGH |
| D3 | (code, feeds D2) `research/fit_diagnostics.py:30` | constant named `COST_STRESS_MULTIPLIERS` | used at `:441` as `for scenario_bps in COST_STRESS_MULTIPLIERS` — misnamed; it is a bps tuple. Third copy of the same literal (also `costs.py:52`, `gates.py:98`). | HIGH |
| D4 | README.md:425 | "Provider credentials are read **only** from `ALPACA_RESEARCH_LLM_SECRET_FILE`" | The loader is `deploy/research-cycle.sh:85`, which reads `ALPACA_RESEARCH_LLM_SECRETS_FILE` (**plural**). The singular name appears in code exactly once, at `compose.yaml:324`, as the *host* path of the docker secret. In a systemd deployment the singular is a no-op. | CRITICAL |
| D5 | README.md:441, SETUP.md:299 | `export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env` presented in the Compose section | correct for Compose (`compose.yaml:324`), but SETUP.md:290-299 says "Run these exports in every administrative shell" — under systemd, `deploy/alpaca-research.service:17` hardcodes the **plural** var, so the exported singular does nothing. Two similarly named vars, one live per deployment mode, documented as one. | HIGH |
| D6 | SETUP.md:216 | "…`ALPACA_RESEARCH_LLM_SECRETS_FILE`; never put provider keys in `agent.env`" | uses the **plural**; README.md:418/425/441 and OPERATIONS.md:210 and deploy/README.md:160/177/229 all use the **singular**. 6 doc sites vs 1 — the docs disagree with each other about the name of the variable that gates the whole LLM lane. | HIGH |
| D7 | `.env.example:17` | ships `ALPACA_RESEARCH_LLM_SECRET_FILE=` only | `compose.yaml:172` also injects `ALPACA_RESEARCH_LLM_SECRETS_FILE: /run/secrets/research_llm_credentials` (hardcoded), so the plural is never settable from `.env` — undocumented asymmetry. | MEDIUM |
| D8 | README.md:409-411 / SETUP.md:298 imply both secret paths are required | `compose.yaml:319` `file: ${ALPACA_AGENT_SECRET_FILE:-.env}` — the **broker** secret silently defaults to the repo's `.env`, while `:324` uses `:?` and hard-fails. Docs say "Compose refuses to render… when that provider path is missing" (README.md:447-449) — true only for the LLM secret; a missing broker secret path silently binds `.env`. | HIGH |
| D9 | research.py:868-873, 897-898 | 12 flat CLI aliases `edge-init/-status/-promote/-ingest/-ingest-shadow/-paper/-discover/-trials/-promotable`, `factory-run/-status/-report` justified in-code as "so cron jobs can stay terse" | grep across every `.sh`, `.md`, `.yaml`, `.service`: **zero uses**. `deploy/research-cycle.sh:735,764,805,841` all use the nested form. Dead CLI surface (~12 parser registrations). | MEDIUM |
| D10 | `research.py:742` | `--variants` default hardcoded `4` | `research/factory_core.py:50` `DEFAULT_VARIANTS = 4` is the constant; `research.py:38` imports `DEFAULT_STRATEGIES, DEFAULT_WORKERS` but **not** `DEFAULT_VARIANTS`. Two independent sources of the same default — silent drift on change. | MEDIUM |
| D11 | `compose.yaml:261` | shadow `--corpus /app/runtime/research/recorded/**market.csv**` | `deploy/shadow.py:24` default is `runtime/research/recorded/**data.csv**`; the recorder actually writes `sessions/market-<date>.csv` (`deploy/recorder.py:142`). Three different names for the same corpus handle; only `_corpus_root()`'s parent-directory fallback makes any of them work. | MEDIUM |
| D12 | env vars read by code but documented in **no** `.md` | `ALPACA_AGENT_CONFIG`, `ALPACA_AGENT_RUNTIME_SCOPE`, `ALPACA_EDGE_WEBHOOK_URL`, `ALPACA_RESEARCH_PROOF_DIR`, `ALPACA_SHADOW_DB` (5 vars) | mechanical scan of all `.py` vs all `.md` | MEDIUM |
| D13 | `deploy/README.md` | references `ALPACA_FACTORY_` (bare prefix, truncated identifier) | not a real variable | LOW |

### 3.2 `.env.example` drift (CONFIRMED, mechanical)

| # | site | finding | severity |
|---|---|---|---|
| E1 | `.env.example:22` | ships `ALPACA_RESEARCH_VEHICLES=all` — the **opt-in ON**. Every other source ships `equity`: `compose.yaml:178` (`:-equity`), `deploy/research.env.example:9`, `deploy/alpaca-research.service:16`. Eight doc sites (README.md:430, SETUP.md:263/287, OPERATIONS.md:521, research/README.md:19, research/protocol.md:20, deploy/README.md:100/290, ARCHITECTURE.md:405) all say `all` must be set **explicitly**. `.env.example` is the file the operator copies to `.env`, which `docker compose` reads — so the shipped example silently enables the option lane the docs say is opt-in. | **CRITICAL** |
| E2 | `.env.example:71` `ALPACA_TRIAL_REVIEW_ENABLED=1` | documented with a 4-line comment (`:68-71`), read only at `deploy/research-cycle.sh:871` (`${...:-1}`), and **absent from `compose.yaml`'s `research:` environment block (:170-196)**. Setting it to `0` in `.env` is a **silent no-op** under the supported Compose deployment. | HIGH |
| E3 | `.env.example:82` `ALPACA_RESEARCH_REPORT_DIR=` | read at `research.py:612`; **absent from `compose.yaml`** — same silent no-op under Compose. | MEDIUM |
| E4 | `.env.example:45` `ALPACA_EXTERNAL_BACKUP_PATH=` | referenced only by `deploy/update-compose.sh:6,35` and `deploy/compose.external-backup.yaml:11`; not in `compose.yaml`. Correct-by-design but undocumented as overlay-only in `.env.example`. | LOW |
| E5 | `.env.example` comment/variable misalignment | the comment blocks are attached to the **wrong** variables throughout: `:18-21` (LLM secret file requirement) sits above `ALPACA_RESEARCH_VEHICLES=all`; `:23-26` (execution_mode) sits above `ALPACA_AGENT_IMAGE_TAG`; `:59-61` (immutable floors) sits above `ALPACA_FACTORY_MIN_SESSIONS` rather than `MIN_TRADES`; `:76-80` (epoch-3 stop floor, cost schedule, stress scenarios) sits above `ALPACA_RESEARCH_REPORT_DIR`. Four separate misattributions in an 89-line file. | MEDIUM |
| E6 | `.env.example:80` | "Preregistered stress is 9/15/25/50 **bps**; 25 bps is the authorization scenario" — this is the CORRECT reading; `research/protocol.md:246`'s "9/15/25/50x" is the wrong one. | (supports D2) |
| E7 | `.env.example` missing keys | `ALPACA_SHADOW_DB`, `ALPACA_SHADOW_INGEST_ENABLED`, `ALPACA_SHADOW_INTERVAL_SECONDS`, `ALPACA_RESEARCH_TMPDIR` are all `${VAR:-default}` substitutions in `compose.yaml` (:193, :194, and the shadow block) but appear in **neither** `.env.example` nor any `.md`. 4 undocumented Compose knobs. | MEDIUM |

---
## 4. CONFIG DRIFT (both directions)

`config.yaml` has **74 leaf keys**. `agent/config.py` `DEFAULT_CONFIG` (:73-108) has **86**.

### 4.1 Code default DISAGREES with config.yaml (CONFIRMED)

| key | `agent/config.py` DEFAULT | `config.yaml` | note |
|---|---|---|---|
| `research.strategy_llm.max_attempts` | `1` (:93) | `2` | shipped config doubles the LLM retry budget vs the code default; no doc states either number |
| `llm.model` | `""` (:86) | `""` | with `llm.provider="openai"` and `llm.enabled=false` — an empty model name that would fail if ever enabled. `research.strategy_llm.model` is `"gpt-5"`. Two provider blocks, one populated, one empty. |
| `research.trial.*` | `min_sessions=20, min_trades=20` (:102-103) | **absent** | see 4.2 — this is a documentation lie, not just a default |

### 4.2 CRITICAL: the documented trial floor is not the effective one (CONFIRMED)
- `research/README.md:522`: "After the trial window — `research.trial` config, **default 30 sessions and 100 trades**".
- `research/trial.py:38-39`: `DEFAULT_MIN_SESSIONS = 30`, `DEFAULT_MIN_TRADES = 100`.
- `agent/config.py:102-103`: `"trial": {..., "min_sessions": 20, "min_trades": 20, ...}`
  and `:462-463` `_int(trial, "min_sessions", ..., 5, 500, **20**)` / `_int(trial, "min_trades", ..., 5, 5000, **20**)`.
- `research.py:479-480` `cmd_edge_trials` calls `_agent_config(args)` (=`load_agent_config(config.yaml)`)
  and passes it into `review_trials(config=config)` -> `research/trial.py:134` `trial_policy(config)`.
  `validate_config` **always injects** the `research.trial` block, so `trial_policy`'s own
  `DEFAULT_MIN_SESSIONS/DEFAULT_MIN_TRADES` are **never reached** on any real invocation.

**Effective trial floor is 20 sessions / 20 trades, not 30/100.** The docs are wrong, and
`research/trial.py:38-39` is dead code masquerading as the source of truth. The comment above
it ("A trial that concludes from four trades is measuring noise") applies to constants
nothing reads.

### 4.3 Accepted-but-never-read config keys (CONFIRMED)

| key | validated at | read by | verdict |
|---|---|---|---|
| `universe.min_price` | `agent/config.py:78,188,205` | **nothing** (grep: only config.py) | dead config key, shipped in `config.yaml:20` |
| `strategy.max_ibr_width_pct` | allowlisted `agent/config.py:235`, **never coerced/validated** | `agent/contracts/ibr.py:135,157,346` only | accepted silently, no range check, inert under `strategy.id="rule"` |
| `strategy.rule_spec` | allowlisted `agent/config.py:235`, **never validated** | not read from the config block anywhere | accepted-and-ignored |
| `research.db_path` | allowlisted + type-checked `:403,411-412` | `agent/engine.py:95` | live, but **absent from `config.yaml` and from every `.md`** |
| `risk.max_total_open_risk_pct` | allowlisted `:291`, **no default, no coercion** | `agent/risk.py:596`, `research/costs.py:198-200`, `agent/engine_cycle.py:602` | live risk limit that is undocumented and not in `config.yaml` |
| `risk.max_gross_exposure_pct` | allowlisted `:291`, **no default, no coercion** | `agent/risk.py:599`, `research/costs.py:197`, `research/factory_core.py:767-769` | same — a real portfolio cap with no shipped value and no doc |

`risk.max_total_open_risk_pct` and `risk.max_gross_exposure_pct` are the sharper finding:
both gate real sizing decisions in three modules each, both are accepted by the validator,
neither has a `DEFAULT_CONFIG` entry, neither is in `config.yaml`, neither appears in any
`.md`. An operator has no way to learn they exist.

### 4.4 Inert IBR tuning block in the shipped config (CONFIRMED)
`config.yaml` ships `strategy.id: "rule"`. These six keys are read **only** by
`agent/contracts/ibr.py`, which `agent/registry.py:125-129` selects only for
`strategy_id != "rule"`:
`strategy.min_ibr_width_atr`, `strategy.max_ibr_width_atr` (ibr.py exclusively),
and `strategy.range_minutes`, `breakout_buffer_bps`, `min_relative_volume`,
`max_entry_extension_r` (ibr.py + `agent/strategy.py`'s IBR plan path).
6 of 74 shipped config leaves (8%) are inert for the shipped strategy id.

### 4.5 Config keys in `DEFAULT_CONFIG` but absent from `config.yaml` (12)
`strategy.pinned`, `strategy.atr_period`, `strategy.max_spread_bps`, `strategy.stale_minutes`,
`risk.min_confidence`, `research.trial.{enabled,min_sessions,min_trades,min_mean_r,min_total_r}`.
`config.yaml` is therefore not a complete example of the accepted surface, and
SETUP.md's config walk-through (SETUP.md:249ff) does not mention any of them.

---
## 5. DEPLOYMENT REALITY

### 5.1 CRITICAL — CI's Compose validation step cannot pass (CONFIRMED EMPIRICALLY)
`.github/workflows/ci.yml:26-31`:
```
cp .env.example .env
docker compose config --quiet
```
`.env.example:17` ships `ALPACA_RESEARCH_LLM_SECRET_FILE=` (**empty**), and
`compose.yaml:324` interpolates it with `${...:?...}`, which fails on empty as well as unset.
Reproduced in this session with the repo's own files:
```
$ cp .env.example .env && docker compose config --quiet
error while interpolating secrets.research_llm_credentials.file:
required variable ALPACA_RESEARCH_LLM_SECRET_FILE is missing a value
$ echo $?     -> 1
```
With the variable set, exit=0. So the "Validate and build the Compose deployment" job
**fails on every push**, and `docker compose build` never runs. Either CI is permanently
red or it has never been executed on this branch (consistent with "system has never run").

### 5.2 CRITICAL — `deploy/update-compose.sh` has the same defect (CONFIRMED)
`deploy/update-compose.sh:16-19` explicitly validates that the **broker** secret file
exists, exports it at `:32`, then runs `docker compose config --quiet` at `:38`. It never
sets or checks `ALPACA_RESEARCH_LLM_SECRET_FILE`. On any host where the operator has not
already exported it, the documented update path (`deploy/README.md`, SETUP.md migration)
aborts at the first compose command with the interpolation error above. The script guards
the one secret that has a `:-` fallback and ignores the one that has `:?`.

### 5.3 HIGH — `--profile research` is a no-op (CONFIRMED EMPIRICALLY)
`.github/workflows/ci.yml:30` runs `docker compose --profile research config --quiet`
as a distinct validation step. **`compose.yaml` defines no `profiles:` key anywhere**
(grep: the only "profile" hit is a prose comment at `:321`). Verified: with and without the
flag, `docker compose config` renders the identical 7 services. The step is a duplicate of
line 29.

### 5.4 HIGH — `deploy/compose.external-backup.yaml` is inert (CONFIRMED)
The overlay (12 lines) bind-mounts `${ALPACA_EXTERNAL_BACKUP_PATH}` to `/external-backup`
and sets `BACKUP_TARGET=/external-backup` and `REQUIRE_EXTERNAL_BACKUP=1` on the `research`
service (`:7-8`). Grep across the entire repo: **`BACKUP_TARGET` and
`REQUIRE_EXTERNAL_BACKUP` are read by nothing** — not `deploy/research-cycle.sh`,
not any `.py`, not any unit. Enabling the overlay mounts a directory nothing writes to and
sets a "REQUIRE" flag nothing enforces. The actual documented backup is a manual
`docker run ... alpine cp` (OPERATIONS.md:194-196). `deploy/update-compose.sh:34-36`
conditionally layers this inert file. Stale: yes.

### 5.5 MEDIUM — image ships the test suite and the audit docs
`.dockerignore` excludes `.git`, `.github`, `.env`, `.venv`, `runtime`, `research/cache`,
`__pycache__`, `vm-import`, `work`. It does **not** exclude `tests/` (24,957 LOC),
`docs/` (including `docs/AUDIT-2026-08.md` and the two orphan `docs/audit-scripts/*.py`),
or the `*.md` tree. `Dockerfile:26` `COPY --chown=10001:10001 . /app` therefore ships all
of it into the production runtime image.

### 5.6 MEDIUM — Python version split
`Dockerfile:1` and `.github/workflows/ci.yml:17` pin **3.13**; the checked-in bytecode in
the working tree is `cpython-311` (all `__pycache__/*.cpython-311.pyc`), i.e. the
development environment is 3.11. No `python_requires` / `.python-version` anywhere. Tests
have never been executed against the deployed interpreter locally.

### 5.7 Unit / service / healthcheck reference check (CONFIRMED OK)
- All 6 compose healthchecks (`compose.yaml:61,96,144,215,274,303`) name subcommands that
  exist in `deploy/health.py:273-295` (`trader`, `recorder`, `research`, `shadow`,
  `watchdog`, `dashboard`) — 6/6 match.
- All `ExecStart` paths in `deploy/*.service` point at files that exist; every flag passed
  to `deploy/scheduler.py` in `deploy/alpaca-research.service:19` exists in
  `deploy/scheduler.py:358-368` — 8/8 match.
- No broken relative Markdown links anywhere in the repo (mechanical scan of all `[..](..)`).

### 5.8 Dependencies
- `requirements.txt` (5 direct) vs `requirements.lock.txt` (35 pinned): **no version
  conflicts** — all 5 direct pins appear identically in the lock.
- `anthropic==0.117.1` is a **real** dependency despite `config.yaml` shipping
  `provider: openai` everywhere: `research/llm_strategy.py:772-777` and
  `agent/brain.py:130-135` import it lazily, and `deploy/research-cycle.sh:184-187`
  handles the `anthropic` provider. Not unused — but note both imports are already
  `# optional dependency` lazy imports, so it need not be in `requirements.txt` at all
  for the shipped `openai` configuration.
- `setuptools==82.0.1` and `tqdm==4.69.0` in the lock are transitive-only and are not
  imported by any first-party module (grep: 0 hits).

---
## 6. OVER-ENGINEERING VERDICT

### 6.1 The 5-mixin Engine is ceremony, not modularity (CONFIRMED, QUANTIFIED)
`agent/engine.py:41-42` composes `Engine(ExecutionLifecycleMixin, RuntimeControlMixin,
StartupEdgePolicyMixin, MarketEntryRiskMixin, EngineCycleMixin)`.
Measured cross-mixin coupling — `self.X` attributes each mixin **uses but does not define**:

| mixin | file | LOC | self-attrs used | not locally defined | sibling mixins depended on |
|---|---|---|---|---|---|
| ExecutionLifecycleMixin | agent/execution_lifecycle.py | 1536 | 26 | **11** | market_entry_risk, engine |
| RuntimeControlMixin | agent/runtime_control.py | 693 | 33 | **15** | execution_lifecycle, market_entry_risk, startup_edge_policy, engine_cycle, engine |
| StartupEdgePolicyMixin | agent/startup_edge_policy.py | 415 | 37 | **20** | market_entry_risk, execution_lifecycle, runtime_control, engine_cycle, engine |
| MarketEntryRiskMixin | agent/market_entry_risk.py | 529 | 21 | **11** | startup_edge_policy, runtime_control, engine |
| EngineCycleMixin | agent/engine_cycle.py | 647 | 40 | **35** | all four siblings + engine |
| Engine itself | agent/engine.py | 240 | 38 | 6 | all |
| **total** | | **4060** | | **98 unresolved-locally refs** | |

Every mixin depends on 2-5 siblings. `EngineCycleMixin` reaches 35 attributes it does not
own — while its own docstring (`agent/engine_cycle.py:3-4`) claims it is *"intentionally
independent from `Encgine`"*. There is no encapsulation boundary; the mixins cannot be
instantiated, tested, or reasoned about separately. This is **one 4,060-line class split
across six files**, with 341 lines of per-file preamble and 77 import statements to
re-assemble what a single module would express directly. The split buys zero substitutability
(each mixin has exactly one consumer and one implementation) and costs a mutual-dependency
graph with cycles in it.
Removable with no behavioral change: ~341 preamble lines + 16 patch-seam shims
(`engine_cycle.py:122-137`) = **~357 LOC**, plus the elimination of a 5-way cyclic
`self.*` contract that no tool can check.

### 6.2 Plugin registry for two hardcoded types (CONFIRMED)
`agent/contracts/__init__.py:17-23` — `EVIDENCE_BUILDERS` dict + `register()` with a
duplicate-registration guard. Registrations happen as import side-effects
(`agent/contracts/rule.py:891`, `agent/contracts/ibr.py`). Exactly **2** entries ever.
And `agent/registry.py:125-129` bypasses the registry anyway:
`("agent.contracts.rule:setup_evidence" if strategy_id == "rule" else "agent.contracts.ibr:setup_evidence")`
— a hardcoded ternary over the same two cases. Registry + guard + side-effect imports:
~25 LOC of plugin machinery serving a hardcoded binary choice.

### 6.3 A preregistered-variant subsystem for a strategy the deployment does not run
`research/variants.yaml` registers **7 variants, all `ibr.*`, zero `rule.*`**.
`config.yaml` ships `strategy.id: "rule"`. `agent/registry.py:132-136` and `:149-150`
both short-circuit with `if strategy_id == "rule": ...` before the registry is consulted.
So `agent/variants.py` (106L) + `research/variants.yaml` (11L) + the variant-resolution
paths in `agent/registry.py` serve the IBR family exclusively, which the shipped config
never selects. Live only through the separate `edge discover` (IBR) lane.

### 6.4 The ledger / proof / identity layer
Measured: `research/edge_ledger.py` 878 + `edge_ledger_proof.py` 922 +
`edge_ledger_store.py` 268 + `edge_identity.py` 135 + `proof.py` 165 +
`proof_payload.py` 574 + `factory_ledger.py` 1062 + `gates.py` 2331 + `edge_lab.py` 809
= **7,144 LOC**. The prior auditor's ~6,500 estimate was low by ~10%.
Structural observations (not domain judgement — other auditors own the statistics):
- `research/edge_lab.py` contributes 809 of those lines while defining only 2 of the
  23 names in its own `__all__` (see 2.4); it exists to hold `discover()` and to be a
  re-export point, and it forces the 89-line shim block in `edge_discovery_core`.
- `research/proof.py` is 165 lines of which `ProofResult` (`:32-47`) has a dead method and
  `send_webhook` (`:50-109`, 60L) is reachable only when `research.proof.webhook_url` is
  non-empty — `config.yaml:93` ships `""`, and no doc, unit, or compose file ever sets it.
  60 LOC of HTTP notification code on a path that has never had a URL.
- `research/stats.py:134-209` `cluster_bootstrap_lower_bound` (76L) is exported and unused.

### 6.5 Two full CLIs for one runner, two full CLIs registered twice
- shadow: 2 entrypoints, one dead (2.1) — 34 LOC.
- `research.py`: every `edge`/`factory` subcommand registered twice (nested + flat-alias);
  the 12 flat aliases have zero users anywhere (D9).

### 6.6 TOTAL REMOVABLE WITH NO BEHAVIORAL CHANGE

| category | LOC | confidence |
|---|---|---|
| Orphan scripts (`docs/audit-scripts/`) | 86 | CONFIRMED |
| Strictly-dead symbols (1.2) | 112 | CONFIRMED |
| `__all__`-laundered dead functions (1.4) | 98 | CONFIRMED |
| `edge_discovery_core` circular-facade shims (2.3) | 89 | CONFIRMED |
| `engine_cycle` patch-seam shims (2.5) | 16 | CONFIRMED |
| Dead `research.py` flat CLI aliases (D9) | ~14 | CONFIRMED |
| Test-only public API kept in production modules (1.3) | 131 | CONFIRMED (dead in prod; tests would need rewriting) |
| Inert external-backup overlay (5.4) | 12 | CONFIRMED |
| Dead CI step (`--profile research`, 5.3) | 2 | CONFIRMED |
| **Subtotal: pure deletions** | **560** | |
| Mixin-split preamble + import overhead (6.1) | ~341 | CONFIRMED (refactor, not deletion) |
| `edge_lab` facade re-export block (2.4) | ~55 | CONFIRMED |
| `agent/contracts` plugin registry (6.2) | ~25 | CONFIRMED |
| `send_webhook` never-configured path (6.4) | 60 | SUSPECTED (config could enable it) |
| **Subtotal: structural collapse** | **~481** | |
| **TOTAL** | **~1,041 LOC (2.8% of the 37.3k source tree)** | |

The honest headline is **not** the line count. It is that a 37,300-line source tree with a
7,144-line proof/ledger layer, a 4,060-line five-way-cyclic Engine, four independent
account simulators, two shadow CLIs, and a CI job that cannot pass **has never executed a
single cycle** (no `runtime/`, no `research/results/`). The ratio of verification machinery
to verified behavior is the finding.

### 3.3 README mermaid diagram vs the real module graph (CONFIRMED)
`README.md:29-49`. Nodes that map cleanly to real modules: Recorder/backfill
(`deploy/recorder.py`, `deploy/backfill.py`), factory (`research/strategy_factory.py`),
gates (`research/gates.py`), ShadowRunner (`research/live_shadow.py`),
`edge ingest-shadow` (`research/live_shadow_ingest.py`), edge ledger
(`research/edge_ledger.py`), edge resolver (`agent/edge.py`), trader
(`agent/engine.py`), watchdog (`deploy/watchdog.py`), dashboard (`deploy/dashboard.py`).

**Omitted stages that the pipeline actually executes** (`deploy/research-cycle.sh`):

| stage | code | in diagram? |
|---|---|---|
| scheduler / cycle driver | `deploy/scheduler.py`, `deploy/research-cycle.sh` | **NO** — the orchestrator of the entire right-hand side is absent |
| fill-cost calibration | `research/calibration.py`, `research-cycle.sh:598-601` | **NO** — and it is a **gate**: `research-cycle.sh:799-801` blocks `shadow_ingest` when calibration is stale/blocked |
| paper-trial judge | `research/trial.py`, `research-cycle.sh:841` | **NO** — the `J --> F` arrow implies fills feed the ledger directly; the trial judge that parks failing edges is not shown |
| dataset view build | `deploy/research_dataset.py` | NO |

The diagram shows 17 nodes; three executed stages, one of them a hard gate, are missing.

### 3.4 Progress-protocol allowlists are mostly unreachable (CONFIRMED)
`deploy/scheduler_output.py:16-22` allows **25** phase names; the only two producers
(`deploy/research-cycle.sh` emits 8: `preparing, validation, backtest, discovery, factory,
shadow_ingest, trial, completed`; `research.py:643-650` emits 4: `diagnosing, evaluating,
aggregating, persisting`) can emit **12**. **13 allowed phases are unreachable**:
`bootstrap, startup, resolve, record, recording, validate, discover, shadow, shadow-ingest,
review, report, cleanup, complete`.
Note the allowlist carries **both spellings** of five pairs (`validate`/`validation`,
`discover`/`discovery`, `shadow-ingest`/`shadow_ingest`, `complete`/`completed`,
`record`/`recording`) — defensive hedging against producers that do not exist.
`RESEARCH_PROGRESS_UNITS` (`:23-28`) allows **20**; producers emit **6**
(`steps, tasks, cycles, hypotheses, accounts, candidates`) — **14 unreachable**.

### 3.5 Other doc/code mismatches
- `report.py:139-141` implements a `--csv` output branch (`csv_report`, `:113-122`) that
  appears in **no** doc; OPERATIONS.md:606 and SETUP.md:749 document only `--json`.
- `research/proof.py:50-109` `send_webhook` (60 LOC) fires only when
  `research.proof.webhook_url` is non-empty. `config.yaml:93` ships `""`, `agent/config.py:97`
  defaults `""`, and no `.env.example`, compose file, unit, or doc ever sets it.
  `ALPACA_EDGE_WEBHOOK_URL` exists in code but in no `.md` (see D12).
