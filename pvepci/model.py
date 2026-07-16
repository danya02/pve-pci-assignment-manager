"""Core domain types.

The ownership model is the important thing here: a *running* VM's hostpci lines
define who owns a device, because that is what cannot be changed without a
reboot. A stopped VM's hostpci lines are stale claims, not ownership -- PVE
permits conflicting claims in config and only fails at VM start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PowerState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"

    @property
    def is_running(self) -> bool:
        return self is PowerState.RUNNING


@dataclass(frozen=True)
class Mapping:
    """A PVE mapped PCI resource, from /cluster/mapping/pci."""

    name: str
    path: str
    iommugroup: int | None
    device_id: str | None = None
    subsystem_id: str | None = None
    node: str | None = None


@dataclass(frozen=True)
class HostPCI:
    """A single hostpci<N> line on a VM, restricted to mapped resources."""

    index: int
    mapping: str

    @property
    def key(self) -> str:
        return f"hostpci{self.index}"


@dataclass
class VM:
    vmid: int
    name: str
    state: PowerState
    hostpci: list[HostPCI] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state.is_running

    @property
    def mappings(self) -> list[str]:
        """Mapping names this VM declares, in hostpci index order."""
        return [h.mapping for h in sorted(self.hostpci, key=lambda h: h.index)]

    @property
    def owned(self) -> frozenset[str]:
        """Mappings this VM actually owns. Empty unless running."""
        return frozenset(self.mappings) if self.is_running else frozenset()

    def next_free_index(self, used: set[int]) -> int:
        i = 0
        while i in used:
            i += 1
        return i


@dataclass
class Pool:
    """A named group of mappings.

    Fungible pools are requested by count (any N members will do); non-fungible
    pools are requested by explicit member name.
    """

    name: str
    members: list[str]
    fungible: bool = False


@dataclass
class HostState:
    """A point-in-time snapshot of the host."""

    node: str
    mappings: dict[str, Mapping]
    vms: dict[int, VM]

    @property
    def owners(self) -> dict[str, int]:
        """mapping name -> vmid of the running VM that owns it."""
        out: dict[str, int] = {}
        for vm in self.vms.values():
            for name in vm.owned:
                out[name] = vm.vmid
        return out

    def claimants(self, mapping: str) -> list[int]:
        """Every VM declaring this mapping, running or not."""
        return sorted(
            vm.vmid for vm in self.vms.values() if mapping in vm.mappings
        )

    def unknown_mappings(self) -> set[str]:
        """Mappings referenced by a VM but absent from /cluster/mapping/pci."""
        referenced = {m for vm in self.vms.values() for m in vm.mappings}
        return referenced - set(self.mappings)
