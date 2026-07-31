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
- the active `journal.db`;
- `research/cache/findings.db` and its backup;
- the corpus manifest and research reports.

Before selecting **Delete with VM**, take an Azure disk **snapshot** or an
encrypted external copy. Deleting the VM without one destroys the corpus.

## Service order

The service user is deliberately `nologin`; use `sudo -iu okx` only for an
operator shell when required. Install and enable the units from `deploy/`,
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

Azure resource creation is outside this repository. There is no provisioning
code here; Azure AI Foundry is only an OpenAI-compatible model endpoint choice.
