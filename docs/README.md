# Documentation

Use these guides for the behavior implemented in the repository. Runtime state,
service health and trading eligibility must be inspected from the deployment;
a saved research result or an old test run does not describe its current state.

## Application and operations

| Guide | Purpose |
| --- | --- |
| [Project overview](../README.md) | Application entry points, safety boundaries and test commands |
| [Setup](../SETUP.md) | Dependencies, credentials, configuration and local checks |
| [Operations](../OPERATIONS.md) | Runtime controls, pause, recovery and inspection |
| [Architecture](../ARCHITECTURE.md) | Code ownership and data flow |
| [Deployment](../deploy/README.md) | Compose services, volumes and update workflow |
| [Azure deployment](../AZURE_DEPLOYMENT.md) | VM deployment setup |

For a configured local runtime, use the explicit read-only deployment inspection:

```bash
.venv/bin/python main.py inspect-deployment
```

For an existing Compose deployment, use its configured checkout, environment and
permissions:

```bash
sudo docker compose exec -T trader python main.py inspect-deployment
sudo docker compose ps
```

`operator_pause=true` keeps the supervisor from starting its trading child.
The stored paper trial's `running` status does not clear that flag. Inspection
reports pause/flat-book verification separately from trial identity and entry
eligibility; successful deployment inspection does not authorize entries.

## Research contracts and workflows

| Guide | Purpose |
| --- | --- |
| [Research workflow](../research/README.md) | Diagnostic research, data and qualification commands |
| [Research protocol](../research/protocol.md) | Versioned evidence, statistics and authority contracts |
| [Trader research](trader-research-v6.md) | Bounded recorder projection for diagnostic replay |
| [Edge diagnostics](edge-diagnostics.md) | Non-authorizing replay, quote-cost calibration, and shadow evidence |
| [Paper/shadow trials](paper-shadow-trials.md) | Frozen trial identities and activation controls |
| [Preregistered hypotheses](preregistered-hypotheses.md) | Frozen registrations and interpretation limits |
| [Rule exit protocol](rule_strategy_v3_exit_protocol.md) | Versioned rule execution and exit contracts |
| [Liquid reversion study](../studies/liquid_reversion/README.md) | Standalone VWAP research simulator |
| [Session-edge study](../studies/session_edges/README.md) | Causal historical screening and main-candidate review |
| [Session catalogue](../studies/session_edges/EDGES.md) | Code-generated families and parameter grids |

Historical audit narratives, findings snapshots and test-result reports are not
maintained as active documentation. Raw datasets, registrations, ledgers and
receipts retain their original identities. Historical provenance strings can
still identify removed reports in Git history; they are not current guidance.
