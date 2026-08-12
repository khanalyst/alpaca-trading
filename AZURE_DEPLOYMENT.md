# Azure deployment: VM, disks, and network

This guide covers the Azure-specific work that has to happen **before**
[SETUP.md](SETUP.md): laying out storage so research data survives, and closing
the network down. Once the data disk is mounted and Docker is pointed at it,
return to SETUP.md step 3 and follow it normally.

The repository does not create Azure resources. Provision the VM, disks,
network security group, and backup policy through your organization's controls.

Throughout, replace `<vm-user>` and `<vm-ip>` with your own values.

---

## The one Azure mistake that destroys research

An Azure Linux VM presents up to three kinds of block device, and they are not
equally durable:

| Device | Typical path | Survives a reboot? | Survives **deallocate/resize**? |
| --- | --- | --- | --- |
| OS disk | `/dev/sda` → `/` | Yes | Yes |
| **Temporary resource disk** | `/dev/sdb` → `/mnt` | Usually | **No — wiped** |
| Managed data disk | `/dev/disk/azure/scsi1/lun0` | Yes | Yes |

The temporary resource disk is local SSD attached to the physical host. Azure
documents it as ephemeral: stopping (deallocating) the VM, resizing it, or a
host migration **erases it without warning**. Ubuntu images mount it at `/mnt`
and it often looks like the biggest, fastest disk available, which is exactly
why people put data on it.

**The edge ledger is the accumulated result of every research cycle you have
ever run.** Losing it means restarting the search from zero. It must live on
the managed data disk, never on `/mnt`.

If you are unsure which device is which, the checks in step 2 tell you
definitively. Do not guess from size or device letter.

---

## 1. Confirm what the VM has

SSH in:

```bash
ssh <vm-user>@<vm-ip>
```

Then gather the facts before changing anything:

```bash
lsblk -o NAME,HCTL,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL
ls -l /dev/disk/azure/ 2>/dev/null
ls -l /dev/disk/azure/scsi1/ 2>/dev/null
findmnt /mnt 2>/dev/null || echo "nothing mounted at /mnt"
free -h
df -h /
```

Read the output like this:

- `HCTL` column `0:0:0:0` is the **OS disk**. `1:0:0:1` (or a device Azure
  labels `resource`) is the **temporary resource disk**. `1:0:0:0` under
  `/dev/disk/azure/scsi1/lun0` is your **managed data disk at LUN 0**.
- `/dev/disk/azure/scsi1/lun<N>` symlinks are the reliable identifiers. Kernel
  names like `/dev/sdc` can change across reboots; the LUN symlinks cannot.
- On VM sizes with NVMe (many v5/v6 series) the devices appear as `/dev/nvme*`.
  The `/dev/disk/azure/` symlinks still resolve correctly on current Ubuntu
  images — prefer them either way.

**If `/dev/disk/azure/scsi1/` does not exist or is empty**, no data disk is
attached. Attach one in the Azure portal (VM → Settings → Disks → Create and
attach a new disk), then re-run the commands above. A reboot is not required.

## 2. Prove which device is the data disk

Before formatting anything, confirm the target is the disk you think it is and
that it holds nothing:

```bash
readlink -f /dev/disk/azure/scsi1/lun0
sudo blkid /dev/disk/azure/scsi1/lun0
sudo file -s /dev/disk/azure/scsi1/lun0
```

**Safe to proceed when:** `blkid` prints nothing and `file -s` says `data`.
That means a raw, unformatted disk.

**Stop if** either command reports an existing filesystem or partition table
you did not create. Something is already on that disk. Find out what before
continuing — the next step is destructive and irreversible.

## 3. Partition, format, and mount the data disk

> **This erases the target disk.** Only run it after step 2 confirmed the disk
> is raw, and only against the LUN symlink you verified.

```bash
sudo parted /dev/disk/azure/scsi1/lun0 --script mklabel gpt
sudo parted /dev/disk/azure/scsi1/lun0 --script mkpart primary ext4 0% 100%
sudo partprobe /dev/disk/azure/scsi1/lun0
sleep 2
sudo mkfs.ext4 -L alpacadata /dev/disk/azure/scsi1/lun0-part1
```

Mount it at `/datadrive` and make the mount survive reboots by **UUID**:

```bash
sudo mkdir -p /datadrive
UUID=$(sudo blkid -s UUID -o value /dev/disk/azure/scsi1/lun0-part1)
echo "UUID=$UUID  /datadrive  ext4  defaults,nofail  0  2" | sudo tee -a /etc/fstab
sudo mount -a
df -h /datadrive
```

Two details that matter:

- **`nofail` is not optional.** Without it, a VM that boots before the data
  disk attaches drops to an emergency shell instead of coming up. On Azure that
  means an unreachable VM.
- **Mount by UUID, never by `/dev/sdX`.** Device letters are not stable across
  reboots or disk additions; a wrong-device mount is silent data loss.

Verify the fstab entry is correct *before* you ever reboot:

```bash
sudo findmnt --verify --verbose
```

## 4. Point Docker at the data disk

The application's durable state lives in Docker named volumes, which live under
Docker's data root. Moving that root is what actually puts the corpus, the edge
ledger, and the journal on the durable disk.

Do this **before** building images, while there is nothing to copy.

```bash
sudo systemctl stop docker docker.socket
sudo mkdir -p /datadrive/docker
sudo rsync -aHAX /var/lib/docker/ /datadrive/docker/
printf '{\n  "data-root": "/datadrive/docker"\n}\n' | sudo tee /etc/docker/daemon.json
sudo systemctl start docker
```

Confirm:

```bash
docker info --format 'Docker root: {{.DockerRootDir}}'
```

**Expected:** `Docker root: /datadrive/docker`. If it still says
`/var/lib/docker`, the daemon did not pick up the config — check
`sudo journalctl -u docker -n 50` for a JSON syntax error in
`/etc/docker/daemon.json`.

Once verified and the stack is running happily, the old tree can be reclaimed:

```bash
sudo du -sh /var/lib/docker        # confirm it is the stale copy
sudo rm -rf /var/lib/docker
```

## 5. Put the checkout and secrets in sensible places

The checkout is small and can stay on the OS disk:

```bash
sudo install -d -o "$USER" -g "$USER" /opt/alpaca-agent-trading
```

Secrets stay off both the checkout and the data disk, root-owned on the OS
disk, exactly as SETUP.md step 5 describes:

```bash
sudo install -d -m 0750 /etc/alpaca-agent-trading
```

Do not put credentials on `/datadrive` — data disks get snapshotted, detached,
and re-attached to other VMs.

## 6. Optional: swap on the temporary disk

Seven parallel research workers on 8 GB can get tight. Swap is the one thing
the ephemeral resource disk is genuinely good for — it is fast local SSD, and
losing swap on deallocate costs nothing.

Check whether the image already configured it:

```bash
swapon --show
```

If empty and `/mnt` exists, add a swapfile there via cloud-init so it is
recreated after every deallocation:

```bash
sudo tee /etc/cloud/cloud.cfg.d/90-swap.cfg > /dev/null <<'EOF'
disk_setup:
  ephemeral0:
    table_type: mbr
    layout: [[100, 82]]
    overwrite: true
fs_setup:
  - device: ephemeral0.1
    filesystem: swap
mounts:
  - ["ephemeral0.1", "none", "swap", "sw,nofail", "0", "0"]
EOF
```

This takes effect on the next boot. A manually created swapfile under `/mnt`
works too but **disappears on deallocate**, leaving a stale `/etc/fstab` entry —
which is why the cloud-init route is preferable.

## 7. Lock the network down

In the VM's Network Security Group, inbound rules should be:

| Priority | Port | Source | Action |
| --- | --- | --- | --- |
| 100 | 22 (SSH) | **Your own IP only** (`x.x.x.x/32`) | Allow |
| 4096 | Any | Any | Deny (default) |

Specifically:

- **Do not open 8080.** The dashboard binds to localhost and is reached through
  an SSH tunnel. There is no authentication on it.
- **Do not leave SSH open to `Any`/`0.0.0.0/0`.** A VM holding broker
  credentials should not accept SSH from the internet at large.
- Outbound to Alpaca (and your LLM provider, if configured) over HTTPS is all
  the egress that is needed.

Confirm from the VM that nothing unexpected is listening:

```bash
sudo ss -tlnp
```

Anything bound to `0.0.0.0` other than SSH deserves an explanation.

Reach the dashboard from your workstation with a tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-ip>
```

Then open `http://127.0.0.1:8080` locally.

## 8. Hand off to SETUP.md

Storage and network are now correct. Continue at **[SETUP.md](SETUP.md) step 3**
(install Docker — you may already have done this) and follow it through. When
you reach step 10, the backfill writes into the Docker volume, which now lives
on `/datadrive`.

After the stack is up, confirm the data actually landed on the data disk:

```bash
docker volume inspect alpaca-agent-trading_runtime-data \
  --format '{{.Mountpoint}}'
df -h /datadrive
```

The mountpoint must be under `/datadrive/docker/`.

---

## Azure-specific operational hazards

**Auto-shutdown deallocates the VM.** Azure's DevTest auto-shutdown, and any
"stop" from the portal, deallocates rather than merely powering off. Two
consequences: the temporary disk is wiped (fine, if only swap is there), and
every service stops. If the trader is holding a position when that happens,
**nothing local flattens it** — not the watchdog, which is on the same VM.
Either disable auto-shutdown, or schedule it comfortably after the session
force-flat time, never during market hours.

**Snapshots are not backups until they are tested.** Before selecting *Delete
with VM* on any disk, verify a tested, off-host copy of the runtime state, the
edge ledger, the research results, the reviewed configuration, and the deployed
Git revision. A second directory on the same managed disk is not an off-host
backup. Restore into a new VM, run the compile and unit checks, run
`main.py check` (authenticated by default), and reconcile the Alpaca paper
account before starting the trader.

**Resizing the VM wipes `/mnt`.** Expected, and harmless if you followed step 6.

**Unattended upgrades can restart Docker.** The containers restart with it, and
the trader reconciles against the broker on startup, which is the designed
path. Prefer a maintenance window outside market hours anyway.

## Non-Compose hosts

For a systemd host, the legacy units are `alpaca-recorder.service`,
`alpaca-trader.service`, `alpaca-watchdog.service`, `alpaca-research.service`,
and `alpaca-research.timer`. They run as the restricted `alpaca` user and are
alternatives to Compose, not an additional lane. Point their working directory
and `EnvironmentFile` at the same paths chosen above.

Enable `alpaca-watchdog.service` alongside the trader. It is the only bound on
the option profile's software stop: it flattens when the trader heartbeat goes
stale while the broker still reports exposure, and a living trader holds the
mode-scoped run lock that keeps it inert. Running the trader without it leaves
an open option position unprotected for as long as the process is gone.
