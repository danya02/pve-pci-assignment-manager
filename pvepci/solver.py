"""The assignment solver. Pure logic -- no host access, no side effects.

Two rules drive everything here:

* **Minimize churn.** A VM already holding a valid set is left alone and never
  rebooted. Renumbering hostpci indices to canonical order is opt-in.
* **Running VMs are canonical.** Only a running VM owns a device. A stopped
  VM's hostpci line is a stale claim, and PVE tolerates conflicting claims in
  config -- they only fail at VM start.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config, Request
from .model import HostPCI, HostState


class SolveError(Exception):
    pass


@dataclass
class VMChange:
    vmid: int
    name: str
    was_running: bool
    before: list[HostPCI]
    after: list[HostPCI]

    @property
    def changed(self) -> bool:
        return self._pairs(self.before) != self._pairs(self.after)

    @property
    def devices_changed(self) -> bool:
        """True if the *set* of devices differs, ignoring index renumbering."""
        return {h.mapping for h in self.before} != {h.mapping for h in self.after}

    @property
    def restart(self) -> bool:
        """A running VM must be stopped to have its hostpci config rewritten."""
        return self.was_running and self.changed

    @property
    def added(self) -> list[str]:
        return sorted({h.mapping for h in self.after} - {h.mapping for h in self.before})

    @property
    def removed(self) -> list[str]:
        return sorted({h.mapping for h in self.before} - {h.mapping for h in self.after})

    @staticmethod
    def _pairs(entries: list[HostPCI]) -> list[tuple[int, str]]:
        return sorted((h.index, h.mapping) for h in entries)


@dataclass
class Plan:
    changes: list[VMChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def touched(self) -> list[VMChange]:
        return [c for c in self.changes if c.changed]

    @property
    def restarts(self) -> list[VMChange]:
        return [c for c in self.changes if c.restart]

    @property
    def is_noop(self) -> bool:
        return not self.touched


def _pool_members_of(cfg: Config, pool_name: str) -> list[str]:
    return cfg.pools[pool_name].members


def _allocate(
    cfg: Config,
    state: HostState,
    requests: list[Request],
    warnings: list[str],
) -> dict[int, set[str]]:
    """Decide which devices each requested VM ends up with, per pool."""
    req_vmids = {r.vmid for r in requests}
    result: dict[int, set[str]] = {r.vmid: set() for r in requests}

    for pool_name, pool in cfg.pools.items():
        # Which requests touch this pool at all?
        named: dict[str, int] = {}
        for r in requests:
            for dev in r.devices:
                if dev in pool.members:
                    named[dev] = r.vmid

        wants_count = {
            r.vmid: r.counts.get(pool_name, 0)
            for r in requests
            if pool_name in r.counts
        }
        if not named and not wants_count:
            continue

        # A device held by a *running* VM outside the profile can't be taken
        # without rebooting a VM the profile never mentioned.
        blocked: dict[str, int] = {}
        for dev in pool.members:
            owner = state.owners.get(dev)
            if owner is not None and owner not in req_vmids:
                blocked[dev] = owner

        for dev, vmid in named.items():
            if dev in blocked:
                raise SolveError(
                    f"device {dev!r} is requested for VM {vmid}, but it is held by "
                    f"running VM {blocked[dev]}, which this profile does not mention. "
                    f"Add VM {blocked[dev]} to the profile to release it."
                )

        free = [m for m in pool.members if m not in named and m not in blocked]

        for dev, vmid in named.items():
            result[vmid].add(dev)

        # Keep passes: running VMs first, since keeping a device they already
        # hold is what avoids a reboot. Stopped VMs only save a config write.
        need = {
            vmid: n for vmid, n in wants_count.items() if n > 0
        }
        ordered = sorted(
            need,
            key=lambda v: (not state.vms[v].is_running if v in state.vms else True, v),
        )

        for vmid in ordered:
            vm = state.vms.get(vmid)
            if vm is None:
                continue
            current = [m for m in pool.members if m in set(vm.mappings)]
            keepable = [m for m in current if m in free]
            for dev in keepable[: need[vmid]]:
                result[vmid].add(dev)
                free.remove(dev)
                need[vmid] -= 1

        # Fill pass: whoever still needs devices takes from what's left.
        for vmid in ordered:
            while need[vmid] > 0:
                if not free:
                    raise SolveError(
                        f"pool {pool_name!r} exhausted while satisfying VM {vmid}: "
                        f"{need[vmid]} more device(s) needed. "
                        + (
                            "Held by running VM(s) outside this profile: "
                            + ", ".join(f"{d} -> VM {v}" for d, v in sorted(blocked.items()))
                            if blocked
                            else "Not enough members in the pool."
                        )
                    )
                dev = free.pop(0)
                result[vmid].add(dev)
                need[vmid] -= 1

        # Stale claims that will now conflict in config (legal, fails at start).
        for dev in pool.members:
            target = next((v for v, devs in result.items() if dev in devs), None)
            if target is None:
                continue
            for other in state.vms.values():
                if other.vmid == target or other.vmid in req_vmids:
                    continue
                if dev in other.mappings and not other.is_running:
                    warnings.append(
                        f"VM {other.vmid} ({other.name}, stopped) still claims {dev}, "
                        f"which is now assigned to VM {target}. That VM will fail to "
                        f"start until its config is changed."
                    )

    return result


def _build_after(
    cfg: Config,
    vm_before: list[HostPCI],
    target: set[str],
    touched_pools: set[str],
    renumber: bool,
) -> list[HostPCI]:
    """Lay out the resulting hostpci lines, preserving indices unless renumbering."""
    # Devices in pools this request never mentioned are left exactly as they are.
    untouched = [
        h
        for h in vm_before
        if (p := cfg.pool_of(h.mapping)) is None or p.name not in touched_pools
    ]
    untouched_names = {h.mapping for h in untouched}
    target = target - untouched_names

    if renumber:
        canonical = [m for p in cfg.pools.values() for m in p.members if m in target]
        canonical += sorted(target - set(canonical))
        entries = [h.mapping for h in sorted(untouched, key=lambda h: h.index)] + canonical
        return [HostPCI(index=i, mapping=m) for i, m in enumerate(entries)]

    kept = [h for h in vm_before if h.mapping in target]
    result = list(untouched) + list(kept)
    used = {h.index for h in result}

    for name in sorted(target - {h.mapping for h in kept}):
        idx = 0
        while idx in used:
            idx += 1
        used.add(idx)
        result.append(HostPCI(index=idx, mapping=name))

    return sorted(result, key=lambda h: h.index)


def solve(
    cfg: Config,
    state: HostState,
    requests: list[Request],
    renumber: bool = False,
) -> Plan:
    warnings: list[str] = []

    missing = [r.vmid for r in requests if r.vmid not in state.vms]
    if missing:
        raise SolveError(
            f"VM(s) not found on node {state.node!r}: "
            f"{', '.join(map(str, sorted(missing)))}"
        )

    for name in sorted(cfg.managed_members - set(state.mappings)):
        warnings.append(
            f"pool member {name!r} has no PVE mapping in /cluster/mapping/pci"
        )

    allocation = _allocate(cfg, state, requests, warnings)

    changes: list[VMChange] = []
    for req in sorted(requests, key=lambda r: r.vmid):
        vm = state.vms[req.vmid]
        touched_pools = set(req.counts)
        for dev in req.devices:
            pool = cfg.pool_of(dev)
            if pool is not None:
                touched_pools.add(pool.name)

        after = _build_after(
            cfg, vm.hostpci, allocation[req.vmid], touched_pools, renumber
        )
        changes.append(
            VMChange(
                vmid=vm.vmid,
                name=vm.name,
                was_running=vm.is_running,
                before=sorted(vm.hostpci, key=lambda h: h.index),
                after=after,
            )
        )

    return Plan(changes=changes, warnings=warnings)


def parse_inline(specs: list[str], cfg: Config) -> list[Request]:
    """Parse ad-hoc assignments: '100=gpu:2' or '100=devices:nic-sriov-0,gpu:1'."""
    by_vmid: dict[int, Request] = {}

    for spec in specs:
        if "=" not in spec:
            raise SolveError(
                f"bad assignment {spec!r}; expected VMID=pool:count "
                "(e.g. 100=gpu:2 or 101=devices:gpu0)"
            )
        vmid_raw, body = spec.split("=", 1)
        try:
            vmid = int(vmid_raw.strip())
        except ValueError:
            raise SolveError(f"bad VMID in {spec!r}") from None

        req = by_vmid.setdefault(vmid, Request(vmid=vmid))
        for term in body.split(","):
            term = term.strip()
            if not term:
                continue
            if ":" not in term:
                raise SolveError(
                    f"bad term {term!r} in {spec!r}; expected pool:count or devices:name"
                )
            key, value = (t.strip() for t in term.split(":", 1))
            if key == "devices":
                if value not in cfg.managed_members:
                    raise SolveError(f"device {value!r} is not a member of any pool")
                req.devices.append(value)
                continue
            if key not in cfg.pools:
                raise SolveError(
                    f"unknown pool {key!r} (known: {', '.join(sorted(cfg.pools))})"
                )
            if not cfg.pools[key].fungible:
                raise SolveError(
                    f"pool {key!r} is non-fungible; request its devices by name "
                    f"(e.g. {vmid}=devices:<name>)"
                )
            try:
                count = int(value)
            except ValueError:
                raise SolveError(f"bad count {value!r} in {term!r}") from None
            if count < 0:
                raise SolveError(f"count in {term!r} must be non-negative")
            req.counts[key] = count

    return sorted(by_vmid.values(), key=lambda r: r.vmid)
