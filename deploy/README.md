# VM services

The canonical deployment instructions are in [`../SETUP.md`](../SETUP.md),
Section 2. The operational checks and nightly research procedure are in
[`../OPERATIONS.md`](../OPERATIONS.md).

The units in this directory are intentionally small:

- `okx-recorder.service` records short-retention market data;
- `okx-trader.service` runs the demo-first agent;
- `okx-research.service` runs one research cycle;
- `okx-research.timer` schedules the research service.

Start the recorder before the trader. Runtime state stays under the VM's
runtime directories and is not replaced by the repository's `vm-import/`
fixture.

The research service has no baked-in backup mount. Provision a different-device
destination, then set `BACKUP_TARGET` and `REQUIRE_EXTERNAL_BACKUP=1` with a
systemd override as documented in `../SETUP.md`.
