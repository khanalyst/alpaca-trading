# Azure deployment pointer

This compatibility filename is intentionally short. The supported deployment
is an Ubuntu VM running the Alpaca paper-only Compose topology described in
[SETUP.md](SETUP.md), with daily checks and recovery in
[OPERATIONS.md](OPERATIONS.md).

The repository does not create Azure resources, network rules, managed disks,
Key Vault entries, or backup policies. Provision those through your
organization's controls. Keep the Alpaca paper secret outside Git and grant
the VM only the network access it needs. The dashboard must remain private or
localhost-bound.

Before selecting **Delete with VM**, verify a tested, off-host copy of
`runtime/`, `research/cache/`, `research/results/`, the reviewed
configuration, and the deployed Git revision. A second directory on the same
managed disk is not an off-host backup. Restore into a new VM, run compile and
unit checks, run `main.py check` (authenticated by default), and reconcile the
Alpaca paper account before starting `alpaca-trader`.

For a non-Compose host, the legacy units are named `alpaca-recorder.service`,
`alpaca-trader.service`, `alpaca-watchdog.service`, `alpaca-research.service`,
and `alpaca-research.timer`. They run as the restricted `alpaca` user and are
alternatives to Compose, not an additional lane.

Enable `alpaca-watchdog.service` alongside the trader. It is the only bound on
the option profile's software stop: it flattens when the trader heartbeat goes
stale while the broker still reports exposure, and a living trader holds the
mode-scoped run lock that keeps it inert. Running the trader without it leaves
an open option position unprotected for as long as the process is gone.
