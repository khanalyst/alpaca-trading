# Documentation catalogue

Start with [current-findings.md](current-findings.md) for the verified
operational state as of 10 November 2026. This index separates that snapshot
from procedures, immutable research protocols, historical reports, and raw
evidence. A dated file is not current merely because it is newer than another
dated file.

## Current status and operating guidance

| file | role | use |
| --- | --- | --- |
| [current-findings.md](current-findings.md) | authoritative status snapshot | blockers, broker state, evidence status, and next authorized action |
| [deep-audit-2026-11-10/README.md](deep-audit-2026-11-10/README.md) | complete current audit | implementation, arm-level evidence, live recheck, and fresh validation detail |
| [edge-diagnostics.md](edge-diagnostics.md) | diagnostic procedure | measure without opening an account or authorizing a strategy |
| [paper-shadow-trials.md](paper-shadow-trials.md) | guarded paper/shadow protocol | identity, flat-book, acceptance, and replacement controls; check current findings first |
| [preregistered-hypotheses.md](preregistered-hypotheses.md) | sealed protocol and hypotheses | preserve manifests and sealed windows; do not rewrite for later results |
| [paper-activation-2026-09-15.json](paper-activation-2026-09-15.json) | source receipt | immutable activation evidence, not a current authorization |

The root [README](../README.md), [operations runbook](../OPERATIONS.md),
[setup guide](../SETUP.md), [architecture](../ARCHITECTURE.md), and
[research protocol](../research/protocol.md) remain the procedural references.
Operational commands must still pass the account-scoped preflight and flat-book
checks; this catalogue does not authorize a resume, replacement, or live mode.

## Historical reports (retained deliberately)

These reports are retained because later reports cite them or because they
contain reproducible source artifacts. Their conclusions are historical and
must be read with [current-findings.md](current-findings.md).

| report | dependency / status |
| --- | --- |
| [audit-2026-09-21](audit-2026-09-21/README.md) | dated audit, scripts, manifests, and result files; source evidence |
| [review-2026-10-15](review-2026-10-15/README.md) | independent review and handoffs; cited by both later research reports |
| [research-2026-10-26](research-2026-10-26/README.md) | public-bar diagnostic refresh; superseded for the corrected cohort by 28 October, retained for comparison and reproduction |
| [research-2026-10-28](research-2026-10-28/README.md) | latest dated public-bar corrected-cohort report; still diagnostic, not live evidence |
| [trader-research-v6](trader-research-v6.md) | tracked redirect stub; original narrative is preserved in the ignored archive |

The 28 October report explicitly depends on the 15 October review and 26
October refresh. The preregistration and September audit artifacts are also
kept unchanged so sealed identities, manifests, and historical claims remain
auditable. Smoke reports under the dated research trees are labeled as smoke
or diagnostic runs and are not promoted here.

## Archive and disposition

Superseded standalone narratives are moved to the local, ignored archive
`outputs/deep-audit/docs-archive/` rather than deleted. Each archived file has
an original-path and SHA-256 entry in the tracked
[documentation disposition](deep-audit-2026-11-10/documentation-disposition.md);
the archive itself is recoverable from the shared workspace but is intentionally
not a public source artifact. The tracked redirect at `trader-research-v6.md`
keeps historical inbound links resolvable without presenting its old
conclusions as current.

| original path | disposition | reason |
| --- | --- | --- |
| `docs/current-findings.md` (11 Oct snapshot) | archived copy at `outputs/deep-audit/docs-archive/docs/current-findings-2026-10-11.md`; path rewritten in place | stale deployment/test claims replaced by the verified 10 Nov snapshot |
| `docs/trader-research-v6.md` | archived copy plus tracked redirect stub | standalone v6 narrative is superseded by the 26/28 Oct dated reports |
| `docs/paper-shadow-trials.md` | retained | still defines guarded identity, acceptance, and flat-book protocol; current state is linked from the snapshot |
| `docs/edge-diagnostics.md` | retained | still defines the non-authorizing diagnostic lane |
| `docs/preregistered-hypotheses.md` | retained unchanged | sealed hypotheses and manifests are immutable evidence |
| `docs/audit-2026-09-21/**` | retained unchanged | source scripts, result artifacts, and historical evidence |
| `docs/review-2026-10-15/**` | retained unchanged | later reports cite this review and its handoffs |
| `docs/research-2026-10-26/**` | retained unchanged | required comparison input for the 28 Oct report |
| `docs/research-2026-10-28/**` | retained unchanged | latest dated diagnostic report and reproducibility instructions |

For the generated archive manifest and validation notes, see the tracked
[documentation disposition](deep-audit-2026-11-10/documentation-disposition.md).
The per-file classification covers all 55 baseline tracked Markdown files,
plus this catalogue, in
[documentation-classification.csv](deep-audit-2026-11-10/documentation-classification.csv).
