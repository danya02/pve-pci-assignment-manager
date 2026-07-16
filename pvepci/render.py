"""Terminal output. Kept separate so the logic modules stay printer-free."""

from __future__ import annotations

import sys

from .model import HostState
from .solver import Plan


def _use_color() -> bool:
    return sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _use_color() else text


def bold(t: str) -> str:
    return c(t, "1")


def dim(t: str) -> str:
    return c(t, "2")


def green(t: str) -> str:
    return c(t, "32")


def yellow(t: str) -> str:
    return c(t, "33")


def red(t: str) -> str:
    return c(t, "31")


def warn(text: str) -> str:
    return f"{yellow('warning:')} {text}"


def table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return dim("  (none)")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  " + "  ".join(bold(h.ljust(widths[i])) for i, h in enumerate(headers))]
    for row in rows:
        out.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(out).rstrip()


def render_status(state: HostState, raw_by_vm: dict[int, list[tuple[str, str]]]) -> str:
    out: list[str] = [f"{bold('node')}  {state.node}", ""]

    owners = state.owners
    rows = []
    for name, m in state.mappings.items():
        owner = owners.get(name)
        claimants = [v for v in state.claimants(name) if v != owner]
        if owner is not None:
            held = green(f"VM {owner} ({state.vms[owner].name})")
        else:
            held = dim("free")
        stale = (
            dim("claimed by " + ", ".join(f"VM {v}" for v in claimants))
            if claimants
            else ""
        )
        rows.append([name, m.path, str(m.iommugroup or "?"), held, stale])
    out.append(bold("devices"))
    out.append(table(rows, ["MAPPING", "PATH", "GROUP", "HELD BY", "ALSO CLAIMED"]))
    out.append("")

    vm_rows = []
    for vm in state.vms.values():
        state_txt = green("running") if vm.is_running else dim("stopped")
        devices = ", ".join(vm.mappings) if vm.mappings else dim("-")
        if vm.mappings and not vm.is_running:
            devices = dim(devices)
        vm_rows.append([str(vm.vmid), vm.name, state_txt, devices])
    out.append(bold("vms"))
    out.append(table(vm_rows, ["VMID", "NAME", "STATE", "DEVICES"]))

    notes: list[str] = []
    for vmid, raw in sorted(raw_by_vm.items()):
        for key, value in raw:
            notes.append(
                f"VM {vmid} has {key}={value} using a raw PCI address rather than a "
                "mapped resource. pvepci does not manage it."
            )
    for name in sorted(state.unknown_mappings()):
        notes.append(f"a VM references mapping {name!r}, which no longer exists")

    if notes:
        out.append("")
        out.extend(warn(n) for n in notes)

    return "\n".join(out)


def render_plan(plan: Plan, renumber: bool = False) -> str:
    out: list[str] = []

    for w in plan.warnings:
        out.append(warn(w))
    if plan.warnings:
        out.append("")

    if plan.is_noop:
        out.append(green("Already in the requested layout. Nothing to do."))
        return "\n".join(out)

    out.append(bold("planned changes"))
    out.append("")

    for ch in plan.changes:
        if not ch.changed:
            out.append(
                f"  VM {ch.vmid} ({ch.name}) "
                + dim(f"unchanged -- keeps {', '.join(ch.before and [h.mapping for h in ch.before]) or 'nothing'}")
            )
            continue

        header = f"  VM {ch.vmid} ({ch.name})"
        if ch.restart:
            header += "  " + yellow("[stop -> edit -> start]")
        elif ch.was_running:
            header += "  " + yellow("[stop -> edit -> start]")
        else:
            header += "  " + dim("[edit only, stays stopped]")
        out.append(header)

        for h in ch.before:
            if h not in ch.after:
                out.append(f"      {red('-')} {h.key}: mapping={h.mapping}")
        for h in ch.after:
            if h not in ch.before:
                out.append(f"      {green('+')} {h.key}: mapping={h.mapping}")
        out.append("")

    restarts = plan.restarts
    if restarts:
        out.append(
            bold("restarts: ")
            + ", ".join(f"VM {c.vmid} ({c.name})" for c in restarts)
        )
    else:
        out.append(green("no VM restarts required"))

    edits_only = [c for c in plan.touched if not c.restart]
    if edits_only:
        out.append(
            dim(
                "config-only edits (VM stays stopped): "
                + ", ".join(f"VM {c.vmid}" for c in edits_only)
            )
        )

    if not renumber and any(
        c.changed and not c.devices_changed for c in plan.changes
    ):
        out.append(dim("(index changes only; pass --renumber to normalize order)"))

    return "\n".join(out)
