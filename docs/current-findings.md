# Current findings — last verified October 11, 2026

The deployment at `2026-10-11T16:28:25Z` (`20:28` Dubai) used source commit `20da96612b21962091837a2efcba9180193b1cc0` and image `sha256:1b9b84b09b4d9928207b3eb27d69331b4b40668d60841e248c1eb6e821c16e73`. At `16:43:42Z` (`20:43` Dubai), all six containers were healthy on that image, with no OOMs or restarts. The paper account was paused and flat with zero open orders; broker history was unchanged; frozen ORB trial fields showed zero accepted sessions and outcomes.

The exact image passed 719 tests across 49 modules. The diagnostic research plan selected 12 new children, 12 direct parents, and 7 IBR comparisons (31 arms), plus 24 signal reports across 17 sessions. At verification, projection had reached session seven and no result receipt existed. No strategy was eligible for activation; later unseen, accepted, complete sessions are required for forward confirmation.

Closed-market warm polls had a median duration of 10.307583 seconds versus 21.619145 seconds for the baseline. This comparison does not establish market-open freshness, which remained pending at the check. Research completion and market-open status after October 11 were not checked here.

Critical backup and restore checks passed on the Mac and VM for 71 files and five SQLite checks. Raw 64 GB recorder CSVs were excluded and remain on the VM. The October 11 cleanup removed 1,482 files totaling 107,082,708 bytes. Private audit evidence and backups remain off GitHub. See the [operations runbook](../OPERATIONS.md) for procedures.
