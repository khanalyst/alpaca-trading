# Azure deployment pointer

The old standalone Azure walkthrough has been merged into [`SETUP.md`](SETUP.md),
Section 2. Daily operations and reporting are in [`OPERATIONS.md`](OPERATIONS.md).
This file remains as a compatibility pointer because older deployment links and
the documentation checks still refer to it.

## Canonical documents

- First-time Mac and Azure setup: [`SETUP.md`](SETUP.md).
- Current configuration and research model: [`README.md`](README.md).
- Readiness check: `research.py readiness`.
- Mac/VM operations, reporting, corpus handoff, tournament, and T3 packet:
  [`OPERATIONS.md`](OPERATIONS.md).

## Data that must be backed up

The VM contains data that cannot be recreated from the repository. Preserve:

- `runtime/research/recorded`;
- completed immutable trees under `runtime/research/snapshots`;
- the active `journal.db`;
- `research/cache/findings.db` and the versioned verified backup set;
- the corpus manifest and research reports.

Use the versioned `research.py backup` workflow. A local-default or same-device
configured-local copy is not VM-loss protection. Before selecting **Delete with
VM**, require a verified `external_mounted` backup on a separately provisioned
different-device mount and confirm it is readable outside the VM. An Azure disk
**snapshot** may be an additional control, but configuration/path alone is not
proof. Deleting the VM without a separate verified copy destroys the corpus.

## Service order

The service user is deliberately `nologin`; do not use `sudo -iu okx`. Run
commands with `sudo -u okx` and explicit paths. Install and enable the units from `deploy/`,
starting the recorder before the trader:

```bash
sudo cp deploy/okx-recorder.service /etc/systemd/system/
sudo cp deploy/okx-trader.service /etc/systemd/system/
sudo cp deploy/okx-research.service /etc/systemd/system/
sudo cp deploy/okx-research.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now okx-recorder
sudo systemctl enable --now okx-trader
sudo systemctl enable --now okx-research.timer
```

After provisioning the external mount, add `BACKUP_TARGET` and
`REQUIRE_EXTERNAL_BACKUP=1` with `systemctl edit okx-research.service`; see
`SETUP.md` for the exact override and verification commands.

Azure resource creation is outside this repository. There is no provisioning
code here; Azure AI Foundry is only an OpenAI-compatible model endpoint choice.
