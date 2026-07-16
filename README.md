# pve-pci-assignment-manager

`pvepci` — a CLI for moving PCI devices (in practice: GPUs) between Proxmox VE
VMs in one command, instead of hand-editing every VM's config in the UI and
power-cycling them all by hand.

Runs **on the PVE node**, driving `qm` and `pvesh` locally. Only PVE
**mapped resources** are managed — device identity is always a mapping name
(`gpu0`), never a raw PCI address.

# ⚠️ Vibecode Alert ⚠️

The code for this project is mostly written by LLM so it may have unexplainable issues.

## Install

Install the `.deb` **on the PVE node**. Grab it from
[Releases](https://github.com/danya02/pve-pci-assignment-manager/releases), or
from the build artifacts of any CI run:

```bash
apt install ./pvepci_0.1.0_all.deb
cp /usr/share/doc/pvepci/examples/config.example.yaml /etc/pve/pvepci.yaml
```

The package is built on Debian 13 (what PVE 9 is based on) and depends only on
`python3-yaml`, which the node already has.

`/etc/pve` is the pmxcfs mount, so the config replicates across the cluster and
lives beside the `mapping/pci.cfg` it references.

<details>
<summary>Running from a checkout instead</summary>

```bash
apt install python3-yaml
python3 -m pvepci.cli status
```
</details>

## Usage

```bash
pvepci status                              # who holds what, and VM power states
pvepci profiles                            # profiles defined in the config
pvepci check [VMID...]                     # run the safety checks only

pvepci apply --profile split-2-1 --dry-run # show the diff, touch nothing
pvepci apply --profile split-2-1
pvepci apply --set 100=gpu:2 101=gpu:1     # ad-hoc, no profile needed
pvepci apply --set 100=devices:nic-sriov-0 # non-fungible devices, by name
```

### `status`

```
devices
  MAPPING  PATH          GROUP  HELD BY                   ALSO CLAIMED
  gpu0     0000:01:00.0  49     VM 100 (migrated-ubuntu)  claimed by VM 101
  gpu1     0000:81:00.0  13     VM 100 (migrated-ubuntu)
  gpu2     0000:c1:00.0  2      free
```

## Configuration

See [config.example.yaml](config.example.yaml). Pools group mapped resources:

```yaml
pools:
  gpu: { members: [gpu0, gpu1, gpu2], fungible: true }

profiles:
  all-to-one:
    100: { gpu: 3 }
  split-2-1:
    100: { gpu: 2 }
    101: { gpu: 1 }
```

**Fungible** pools are interchangeable and requested by *count*. **Non-fungible**
pools are distinct devices, requested by *name* under `devices:`.

## How it decides

**Running VMs are canonical.** A running VM's `hostpci` lines define ownership,
because that is what cannot be changed without a reboot. A stopped VM's
`hostpci` line is a *stale claim*, not ownership — PVE allows conflicting
claims in config and only fails at VM start. `status` shows the difference.

**Minimum churn.** The solver satisfies the requested shape with the fewest VM
restarts: a VM already holding a valid set is left alone and never rebooted, and
hostpci indices are preserved. Pass `--renumber` to normalize indices to
canonical order (which costs extra restarts).

**Lifecycle.** Affected VMs are stopped, configs rewritten, and exactly those
that were running before are started again. A VM that was stopped stays stopped.

## Safety

Before stopping anything, `pvepci` checks whether a VM is actually in use. Every
check is a **warning, not a hard error** — `--force` always overrides.

| check | signal |
|---|---|
| `utilization` | device utilization **sampled over a window** (default 30s) |
| `users` | logged-in users, via the guest agent's `get-users` |
| `processes` | watchlisted long-running processes (python, torchrun, jupyter…) |
| `uptime` | recent boot suggests someone is setting up |

**GPU memory is deliberately not a signal.** A server that loads a model into
VRAM at startup (e.g. `llama-server`) pins many GiB on every GPU for its whole
life at 0% utilization with nobody logged in — VRAM residency is a constant
false positive. Utilization must be sampled *over a window* because bursty
inference reads 0% between requests.

### Shutdown flow

1. `wall` announcement into the guest, and a **veto file** is created there.
2. **Grace period** (default 5 min).
3. Safety checks **re-run** — aborts if someone started working during the wait.
4. Graceful `qm shutdown` with a timeout. `pvepci` never hard-stops a VM.

**The veto file hands users the brake.** The announcement tells them to write
into `/tmp/pvepci-veto`; any text there aborts the operation.

```
NOTICE: this VM will be shut down in ~5 min to reassign its PCI devices.
If you are using it, write your objection into /tmp/pvepci-veto and the
shutdown will be cancelled.
```

### Flags

| flag | effect |
|---|---|
| `--dry-run` / `-n` | print the diff, change nothing |
| `--no-confirm` | don't prompt; **still** announces, waits, and honors the veto |
| `--skip-wait` | skip the grace period — users get no chance to veto |
| `--force` | override safety warnings **and** a written veto |
| `--grace SECS` | override the grace period for this run |
| `--renumber` | normalize hostpci indices (causes extra restarts) |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The solver, config, and parsing layers are pure and unit-tested offline — no
host needed. All subprocess calls are confined to `pve.py` and `guest.py`.
