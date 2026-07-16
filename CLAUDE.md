## What this is

`pvepci` — a CLI that moves PCI devices (in practice: 3× Tesla V100 GPUs)
between Proxmox VE VMs in one command. It **runs on the PVE node**, driving
`qm` and `pvesh` locally and reading `/etc/pve`. The SSH access below is for
development only; the tool is not a remote driver.

## Layout

| module | role |
|---|---|
| `pvepci/model.py` | dataclasses; the ownership model lives here |
| `pvepci/config.py` | YAML load + validation (`/etc/pve/pvepci.yaml`) |
| `pvepci/solver.py` | **pure** min-churn assignment logic |
| `pvepci/pve.py` | host interface — `pvesh`/`qm` subprocess calls |
| `pvepci/guest.py` | QEMU guest agent access |
| `pvepci/safety.py` | the "is anyone using this?" checks |
| `pvepci/apply.py` | mutating path: announce → wait → stop → rewrite → start |
| `pvepci/render.py` | terminal output |
| `pvepci/cli.py` | argparse entry point |

**Keep subprocess calls confined to `pve.py` and `guest.py`.** That boundary is
what lets `solver.py`, `config.py`, and the parsers be unit-tested with no host.

## Invariants — do not re-litigate

These were settled with the user. Changing them needs an explicit ask.

1. **Never gate on GPU memory / `--query-compute-apps`.** VM 100 runs
   `llama-server`, which pins ~10 GiB on every GPU at 0% utilization for its
   whole life with nobody logged in. VRAM residency is a constant false
   positive. Verified live.
2. **Utilization must be sampled over a window**, never a single reading —
   bursty inference reads 0% between requests.
3. **Running VMs are canonical.** Only a running VM owns a device. A stopped
   VM's `hostpci` line is a *stale claim*; PVE allows conflicting claims and
   only fails at VM start. Model this, don't "fix" it.
4. **Minimize churn.** A VM already holding a valid set is never rebooted, and
   hostpci indices are preserved. Renumbering is opt-in via `--renumber`.
5. **Restart exactly the VMs that were running before.** A stopped VM whose
   config changed stays stopped.
6. **All safety checks are warnings, not errors.** `--force` always overrides.
7. **Only PVE mapped resources are managed.** Never raw PCI addresses in the UX.
8. **Deliberately excluded** (offered and declined): config backups before
   edit; IOMMU group / vfio-binding validation. Don't add unprompted.

## Host quirks learned the hard way

Each of these caused a real bug; all have regression tests.

- **`qm config` has no `--output-format`.** Use
  `pvesh get /nodes/<node>/qemu/<vmid>/config --output-format json`.
- **`qm guest cmd <vmid> ping` exits 0 and prints nothing.** An empty reply is
  success, not a parse failure — parsing it as JSON made every VM look BUSY.
- **`qm guest exec` omits `out-data` entirely** when the command prints nothing.
- `qm agent` is an alias for `qm guest cmd` and does **not** accept
  `exec`/`exec-status`. Those are `qm guest exec`.
- Guest exec needs **absolute paths** (`/usr/bin/nvidia-smi`); wrap pipelines in
  `/bin/bash -c`.
- `pvesh get /cluster/mapping/pci` does **not** return mappings sorted.
- `/etc/pve` is pmxcfs (FUSE): no hardlinks, ~1 MB/file. Plain create/replace.
- Node name is the hostname — **not** `/nodes[0]`, which picks a peer in a cluster.

## Testing

```bash
.venv/bin/python -m pytest        # 78 tests, no host needed
```

The dev venv exists because Arch's Python lacks `python3-yaml` (the PVE host has
it). Fixtures in `tests/test_pve.py` are verbatim captures from the live host.

## Packaging

Shipped as a `.deb`, built by `.github/workflows/build-deb.yml` inside a
`debian:trixie` container — Debian 13, the same base as PVE 9, so it is built
against the Python the node actually runs. The deb build also runs the unit
suite (`PYBUILD_TEST_PYTEST=1` in `debian/rules`).

- **Bumping the version means editing two files**: `pyproject.toml` and
  `debian/changelog`. CI fails on a mismatch.
- Native package (`3.0 (native)`), so there is no orig tarball to manage.
- Pushing a `v*` tag builds and attaches the deb to a GitHub release.
- `pyproject.toml` is the only build config; that needs
  `pybuild-plugin-pyproject`, without which pybuild falls back to distutils and
  fails since there is no `setup.py`.

There is no local deb build — Arch has no debhelper. Let CI build it.

## The live host

```
ssh -J llm_test2@kron.botik.ru root@192.168.71.113     # node: iipaserver
```

**This is a computer lab's shared production server** — other people run real
workloads on it. PVE 9.2.4, Python 3.13.5, `python3-yaml` present.

- **VM 101 `sample`** is blank and disposable — the safe end-to-end target.
- **VM 100 `migrated-ubuntu`** is the real workload. **Ask before stopping it.**
  Permission granted on 2026-07-16 was situational, not standing.
- **`llama-server` on VM 100 runs as the docker container `qwen`** (image
  `llama-cpp-v100:latest`, `restart=unless-stopped`). It comes back by itself
  ~40s after a VM restart, model loaded and serving on :8080. Verified live
  2026-07-16. An earlier note here claimed it was started by hand and never
  returned; that was wrong — the recovery just takes longer than it was watched.
- **But it only comes back while all three GPUs are present.** The container is
  pinned to devices 0,1,2 (`--tensor-split 32,32,32`), so with a GPU taken away
  it fails at start with `nvidia-container-cli: device error: 2: unknown
  device`, and stays down until the third GPU is returned. Taking a GPU from
  VM 100 therefore has a cost the safety checks cannot see — they report IDLE,
  which is correct, but the container will not survive the move.

Prefer this order when touching the host: `status` → `--dry-run` → VM 101 →
VM 100. Read-only recon costs nothing and has repeatedly caught real bugs that
unit tests could not.

## Conventions

- Flags are named for exactly what they do. `--yes` was split into
  `--no-confirm` (don't prompt) and `--skip-wait` (skip the grace period)
  because one flag doing two jobs silently disabled the veto brake.
- Comments explain constraints the code can't show (host quirks, why a check is
  absent), not what the next line does.
