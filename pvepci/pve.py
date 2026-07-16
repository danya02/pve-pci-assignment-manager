"""Host interface: the only module that shells out to pvesh/qm.

Keeping every subprocess call behind this boundary is what lets the solver and
the diff logic be tested offline with no host.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess

from .model import HostPCI, HostState, Mapping, PowerState, VM

HOSTPCI_RE = re.compile(r"^hostpci(\d+)$")


class PVEError(RuntimeError):
    pass


class NotOnHostError(PVEError):
    """Raised when the PVE tooling isn't present -- i.e. we're not on a node."""


def _run(argv: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NotOnHostError(
            f"{argv[0]!r} not found -- pvepci must run on a PVE node"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PVEError(f"{' '.join(argv)} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise PVEError(f"{' '.join(argv)} failed ({proc.returncode}): {err}")
    return proc.stdout


def _run_json(argv: list[str], timeout: int = 60):
    out = _run(argv, timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise PVEError(f"{' '.join(argv)} returned non-JSON: {out[:200]!r}") from exc


def check_available() -> None:
    for tool in ("qm", "pvesh"):
        if shutil.which(tool) is None:
            raise NotOnHostError(
                f"{tool!r} not found -- pvepci must run on a PVE node"
            )


def parse_mapping_entry(entry: dict) -> Mapping:
    """Parse one /cluster/mapping/pci element.

    The 'map' field is a list of comma-separated k=v strings, one per node. We
    take the first; multi-node maps would need node filtering, which this host
    (single node) never exercises.
    """
    name = entry["id"]
    raw = entry.get("map") or []
    if not raw:
        return Mapping(name=name, path="", iommugroup=None)

    fields: dict[str, str] = {}
    for part in raw[0].split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()

    group = fields.get("iommugroup")
    return Mapping(
        name=name,
        path=fields.get("path", ""),
        iommugroup=int(group) if group and group.isdigit() else None,
        device_id=fields.get("id"),
        subsystem_id=fields.get("subsystem-id"),
        node=fields.get("node"),
    )


def get_mappings() -> dict[str, Mapping]:
    """Mapped PCI resources, keyed by name.

    pvesh does not return these in sorted order, so we sort for stable output.
    """
    data = _run_json(["pvesh", "get", "/cluster/mapping/pci", "--output-format", "json"])
    out = {}
    for entry in data:
        if entry.get("type", "pci") != "pci":
            continue
        m = parse_mapping_entry(entry)
        out[m.name] = m
    return dict(sorted(out.items()))


def parse_hostpci_value(value: str) -> str | None:
    """Extract the mapping name from a hostpci value.

    Only mapped resources are in scope; a raw PCI address (e.g. '0000:01:00.0')
    returns None so callers can flag it rather than silently manage it.
    """
    for part in value.split(","):
        part = part.strip()
        if part.startswith("mapping="):
            return part.split("=", 1)[1].strip()
    return None


def get_vm_config(vmid: int, node: str) -> dict[str, str]:
    """Read a VM's config as JSON.

    Via pvesh rather than `qm config`, which has no --output-format and would
    need its plain 'key: value' output parsed by hand.
    """
    return _run_json(
        ["pvesh", "get", f"/nodes/{node}/qemu/{vmid}/config", "--output-format", "json"]
    )


def get_node_name() -> str:
    """The local PVE node name, which is the hostname.

    Not /nodes[0] -- that would silently pick a peer in a cluster.
    """
    node = socket.gethostname().split(".")[0]
    known = {n["node"] for n in _run_json(["pvesh", "get", "/nodes", "--output-format", "json"])}
    if node not in known:
        raise PVEError(
            f"local hostname {node!r} is not a known PVE node ({', '.join(sorted(known))})"
        )
    return node


def list_vms() -> list[dict]:
    return _run_json(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])


def _hostpci_from_config(cfg: dict) -> tuple[list[HostPCI], list[tuple[str, str]]]:
    """Return (mapped hostpci entries, unmapped raw entries)."""
    mapped: list[HostPCI] = []
    raw: list[tuple[str, str]] = []
    for key, value in cfg.items():
        m = HOSTPCI_RE.match(key)
        if not m:
            continue
        name = parse_hostpci_value(str(value))
        if name is None:
            raw.append((key, str(value)))
        else:
            mapped.append(HostPCI(index=int(m.group(1)), mapping=name))
    return sorted(mapped, key=lambda h: h.index), sorted(raw)


def read_state() -> tuple[HostState, dict[int, list[tuple[str, str]]]]:
    """Snapshot the host.

    Returns the state plus any raw (unmapped) hostpci entries per VM, which the
    caller should surface as warnings -- the tool refuses to manage those.
    """
    check_available()
    node = get_node_name()
    mappings = get_mappings()
    vms: dict[int, VM] = {}
    raw_by_vm: dict[int, list[tuple[str, str]]] = {}

    for res in list_vms():
        if res.get("type") != "qemu":
            continue
        # PCI mappings are node-local, so VMs elsewhere in a cluster are out of scope.
        if res.get("node") != node:
            continue
        vmid = int(res["vmid"])
        cfg = get_vm_config(vmid, node)
        mapped, raw = _hostpci_from_config(cfg)
        if raw:
            raw_by_vm[vmid] = raw
        state = (
            PowerState.RUNNING
            if res.get("status") == "running"
            else PowerState.STOPPED
        )
        vms[vmid] = VM(
            vmid=vmid,
            name=str(cfg.get("name") or res.get("name") or f"vm{vmid}"),
            state=state,
            hostpci=mapped,
        )

    return (
        HostState(node=node, mappings=mappings, vms=dict(sorted(vms.items()))),
        raw_by_vm,
    )
