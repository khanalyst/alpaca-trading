# Documentation disposition — 10 November 2026

This tracked manifest accompanies the [documentation catalogue](../README.md)
and the [full 10 November audit](README.md). It records the disposition of the
documentation set without changing any raw VM or frozen historical source
artifact.

## Archived narrative hashes

The superseded narratives were copied to the local ignored recovery archive
before their tracked paths were rewritten. The archive is recoverable from the
shared workspace at the plain paths below; it is intentionally not used as a
Markdown link because ignored files are not available from a GitHub checkout.

| original tracked path | local recovery copy | SHA-256 of archived bytes | disposition |
| --- | --- | --- | --- |
| `docs/current-findings.md` (11 October snapshot) | `outputs/deep-audit/docs-archive/docs/current-findings-2026-10-11.md` | `5c1b29b93bbcd73f635f3d49af1bdeda8872322bdc744ad0ef9f37442df1b936` | Replaced by the verified 10 November current snapshot. |
| `docs/trader-research-v6.md` | `outputs/deep-audit/docs-archive/docs/trader-research-v6.md` | `46d0867fd4bcca1261851f2e1b6e8573c0927a46722651cebf96cf2dcfe88b00` | Replaced by a tracked redirect stub; the original is retained for recovery. |

No preregistration, manifest, JSON result, script, protocol, dated audit,
review, or research report was deleted. Historical reports remain under their
dated paths and are catalogued in the [documentation index](../README.md).

## Durable classification

The complete baseline classification for all 55 tracked Markdown files, plus
the newly added catalogue row, is in
[`documentation-classification.csv`](documentation-classification.csv). The
rows distinguish active procedures, sealed protocols, current status,
historical evidence, review artifacts, smoke reports, and the archive
redirect. This is a committed-trackable copy of the earlier local inventory.

## Snapshot provenance

- The live infrastructure recheck at `2026-11-10T07:27:25Z` found 5,038,686,208
  bytes free against a 5,368,709,120-byte reserve, approximately 97% full;
  all six containers reported the `20da966` image, with the recorder unhealthy
  and the other five healthy.
- Broker and paper-account observations in the current snapshot are earlier
  checks from approximately 01:55–02:01 UTC; they are not presented as a
  second broker recheck at 07:27 UTC.
- The 2,675-test result remains explicitly historical, from the 28 October
  release record. Fresh audit validation is linked from the
  [full audit](README.md), not substituted with that historical count.
