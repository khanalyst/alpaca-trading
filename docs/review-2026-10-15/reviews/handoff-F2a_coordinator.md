# Review handoff: F2a factory ledger and factory report (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `research/factory_ledger.py` (2,555 lines): full function inventory; read in full the LORD++ allocation (lines 301 to 382), connection setup (470 to 478), `record_fdr_decision` (2386 to 2452) and the trigger definitions found by pattern.
- `research/factory_report.py` (1,910 lines): function inventory plus the verdict computation (lines 370 to 476) and the wording paths found by pattern.

## Findings

### F2a-01 LORD++ is implemented correctly and atomically (positive, VERIFIED)

- `_lord_plus_plus_allocation` (`research/factory_ledger.py:319-354`) allocates `W0*gamma_i + (alpha-W0)*gamma_(i-tau_1) + alpha*sum gamma_(i-tau_j)` with `gamma_i = 1/(i(i+1))` (`:301-305`), matching Ramdas et al. 2017.
- `record_fdr_decision` reads the scope and inserts the decision inside one `BEGIN IMMEDIATE` transaction (`:2423-2448`), so concurrent workers cannot spend the same allocation twice. It is idempotent by `test_id`.
- Ledger tables are protected by `BEFORE UPDATE` and `BEFORE DELETE` triggers (append-only; for example `:656-670`, `:728-736`, `:877-915`).
- The weakness is statistical, not computational: the gamma sequence decays as 1/t², which starves the lane after a few tests (D1-08).

### F2a-02 Test order within a scope depends on wall-clock timestamps and random UUIDs (LOW, VERIFIED)

Rows are ordered `ORDER BY created_at,decision_id` (`:2427-2428`), where `created_at = datetime.now().timestamp()` and `decision_id = uuid4().hex`. A backwards clock step, or two equal timestamps, reorders the test index used for the allocation and for later audit recomputation.

Suggested fix: use an autoincrement sequence column for the test index.

### F2a-03 The research verdict's "confidence" is 1 minus the smallest p-value across all variants, without multiplicity adjustment (LOW-MEDIUM, VERIFIED)

In `research/factory_report.py:461-473`:

- `confidence = 1.0 - min(p_values)` and `p_value = min(p_values)`;
- `effect_estimate` is the plain mean of the variant effects;
- the "confidence interval" runs from the minimum of the lower bounds to the maximum of the upper bounds.

It is labelled "descriptive effect; no authorization implied" (`:474`), but with dozens of variants a "confidence 0.99" is the expected result under the null. Readers of `render_text` and `render_markdown` see it next to "PROVED EDGES" (`:1521`).

Suggested fix: report the BY-adjusted q of the best variant, or omit the field.

### F2a-04 SQLite journal mode left at the default (INFO, VERIFIED)

`_connect` (`:470-475`) sets `foreign_keys` and a 30-second busy timeout, but not WAL mode. With two factory workers plus readers (the dashboard), rollback-journal locking serialises reads during writes. This is a correctness-neutral performance note.

## Over-engineering

- Three historical FDR method generations (legacy, v5, v6) remain live, with migration code for cycle identity, learning epoch and variant-closure epoch (`:516-740`).
- `online_fdr` and `cumulative_fdr` are aliases of `record_fdr_decision` (`:2449-2450`).
- `factory_report.py` keeps legacy diagnostic-funnel paths (`:296-327`) next to current ones.
- No decision consumes the report; it is presentation only.
