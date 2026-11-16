# Azure VM prerequisites

This guide records the repository-specific checks to make before following
[SETUP.md](SETUP.md). The application does not provision Azure resources or
change VM storage/network settings. Use your organization’s approved process
to create the VM, attach and mount a managed data disk, configure Docker, and
set the Network Security Group.

The application’s durable state includes the recorder corpus, runtime
journal/state, edge ledger, research cache/results, and shadow WAL. Keep Docker
storage and those volumes on a durable managed disk. Treat any temporary or
resource disk as disposable; it is not an application data location.

## 1. Verify durable storage (read-only)

SSH to the VM and inspect the device and mount facts without formatting or
partitioning anything:

```bash
ssh <vm-user>@<vm-ip>
lsblk -o NAME,HCTL,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL
findmnt /datadrive
df -h /datadrive /
docker info --format 'Docker root: {{.DockerRootDir}}'
```

Use the Azure-managed-disk mount chosen by your infrastructure team. Do not
infer a device from size or `/dev/sdX`; if you need to identify an attached
disk, use the stable `/dev/disk/azure/` links and follow the infrastructure
team’s storage procedure.

Before the first Compose start, Docker’s reported data root must be on the
managed disk (for example, under `/datadrive/docker`). If the mount is absent
or Docker still uses an OS/temporary-disk location, stop and correct the host
storage configuration before building or recording. This guide intentionally
does not change host storage or Docker daemon configuration.

After Compose creates the application volume, verify its location read-only:

```bash
docker volume inspect alpaca-agent-trading_runtime-data \
  --format '{{.Mountpoint}}'
df -h /datadrive
```

The mountpoint should be under the durable Docker root. If it is not, stop
before starting recorder, research, shadow, or trader services.

## 2. Verify checkout and secret prerequisites

Keep the checkout on an approved durable OS or data volume, and keep secrets
outside Git and outside portable research data:

```bash
test -d /opt/alpaca-agent-trading
test -r /etc/alpaca-agent-trading/agent.env
test -r /etc/alpaca-agent-trading/research-llm.env
```

[SETUP.md](SETUP.md) defines the credential-file contents and permissions.
`agent.env` is for Alpaca paper credentials/feed settings; the separate
research file contains only the selected provider key. Never commit either
file or place secret values in a data-disk backup.

## 3. Verify network exposure

Configure the VM’s NSG according to organizational policy so that SSH is
available only from the administrator’s current address. Do not expose the
dashboard port or any service port publicly. The Compose dashboard binds to
localhost and has no authentication. Check listeners on the VM:

```bash
sudo ss -tlnp
```

The application needs outbound HTTPS to Alpaca and, when research is enabled,
the configured research provider. Do not add inbound rules merely to make the
dashboard reachable. Use an SSH tunnel instead:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-ip>
```

Then open `http://127.0.0.1:8080` locally.

## 4. Durable backups and lifecycle checks

Maintain an off-host, tested backup of runtime state, the journal, edge and
research artifacts, configuration, and deployment identity. Keep secret values
in the secret manager or protected credential store rather than in the backup.
Test a restore on a separate VM before relying on it.

Before host maintenance or an application update, follow the persisted pause
and flat-book procedure in [deploy/README.md](deploy/README.md) and
[OPERATIONS.md](OPERATIONS.md). A host stop/restart can leave the runtime’s
`operator_pause` set; inspect and explicitly resume only after the authenticated
flat-book checks pass. Do not delete Compose volumes as a maintenance shortcut.

## 5. Continue with application setup

After the managed-disk and network checks pass, continue with the checkout,
secrets, and validation steps in [SETUP.md](SETUP.md). Use
[deploy/README.md](deploy/README.md) for Compose topology,
updates, health checks, and the fail-closed operator gates. Nothing in this
document reports VM health or deployment status.
