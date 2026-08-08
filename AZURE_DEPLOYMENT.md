# Azure deployment compatibility pointer

This filename is retained for older links and documentation checks. It is not
an independent deployment guide.

- Install and deploy the VM from [`SETUP.md`](SETUP.md).
- Run the trader, research scheduler, backups, recovery, and handoff from
  [`OPERATIONS.md`](OPERATIONS.md).
- Use [`README.md`](README.md) for the repository-wide authority hierarchy and
  current architecture.
- Use [`deploy/README.md`](deploy/README.md) for the deployment topology and
  service ownership.

Azure resource creation, credentials, and off-host retention are operator
actions outside this repository. Before VM deletion, follow the verified
`external_mounted` backup procedure in `SETUP.md` and `OPERATIONS.md`; a path
or configuration value alone is not VM-loss protection.

Compatibility safety anchors retained from the former walkthrough:

- Check with `research.py readiness` before trusting a run.
- The `okx` service user is `nologin`; do not use `sudo -iu okx`; run commands
  with `sudo -u okx` and explicit paths.
- Preserve `runtime/research/recorded`, its content-addressed raw archive,
  `runtime/research/market_events.db`, completed snapshots, discovery artifacts
  and content-addressed discovery handoffs,
  operational JSONL histories, research manifests, forward evidence,
  `research/results`, the active `journal.db`, and
  `research/cache/findings.db` in a verified backup before selecting
  **Delete with VM**. Without a separate retained copy, deleting the VM
  destroys the corpus.
- The shipped demo remains `shadow_only`; the configured tuned LS identity is a
  pinned isolated paper arm, never an adaptive one-axis selector candidate.
- On the legacy systemd lane, install all services. Starting the recorder first
  preserves more short-retention data, but recorder health does not block the
  trader or research scheduler:

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

Use `SETUP.md` for the complete commands and `OPERATIONS.md` for ongoing
verification; these anchors are not a parallel procedure.
