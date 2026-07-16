"""Safety checks.

Every check produces a *warning*, never a hard error -- the user can always
override with --force.

Deliberately NOT checked: GPU memory / --query-compute-apps. The known workload
(llama-server) pins ~10 GiB of VRAM on every GPU at 0% utilization for its whole
life, with nobody logged in. VRAM residency here is a constant false positive.
The real signal is device *utilization sampled over a window*, since bursty
inference reads 0% between requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import render
from .config import SafetyConfig
from .guest import GuestError, bash, exec_cmd, get_users, is_responsive
from .model import VM
from .pve import PVEError


@dataclass
class Signal:
    name: str
    triggered: bool
    detail: str
    indeterminate: bool = False

    @property
    def concerning(self) -> bool:
        return self.triggered or self.indeterminate


@dataclass
class VMReport:
    vmid: int
    name: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def unsafe(self) -> bool:
        return any(s.concerning for s in self.signals)

    @property
    def busy(self) -> list[Signal]:
        return [s for s in self.signals if s.triggered]


def _check_utilization(vm: VM, sc: SafetyConfig) -> Signal:
    """Sample device utilization over a window; a single reading is not enough."""
    samples = max(1, sc.util_window_secs // max(1, sc.util_interval_secs))
    script = (
        f"for i in $(seq 1 {samples}); do "
        f"/usr/bin/nvidia-smi --query-gpu=index,utilization.gpu "
        f"--format=csv,noheader,nounits 2>/dev/null; "
        f"sleep {sc.util_interval_secs}; done"
    )
    try:
        res = bash(vm.vmid, script, timeout=sc.util_window_secs + 30)
    except (GuestError, PVEError) as exc:
        return Signal(
            "utilization", False, f"could not sample utilization: {exc}", indeterminate=True
        )

    peaks: dict[str, int] = {}
    for line in res.out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        idx, util = parts[0], int(parts[1])
        peaks[idx] = max(peaks.get(idx, 0), util)

    if not peaks:
        return Signal(
            "utilization",
            False,
            "nvidia-smi returned no readings (not installed, or no GPUs visible)",
            indeterminate=True,
        )

    hot = {i: u for i, u in peaks.items() if u >= sc.util_threshold_pct}
    summary = ", ".join(f"gpu{i}={u}%" for i, u in sorted(peaks.items()))
    if hot:
        return Signal(
            "utilization",
            True,
            f"peak over {sc.util_window_secs}s: {summary} "
            f"(threshold {sc.util_threshold_pct}%)",
        )
    return Signal(
        "utilization", False, f"idle -- peak over {sc.util_window_secs}s: {summary}"
    )


def _check_users(vm: VM, sc: SafetyConfig) -> Signal:
    try:
        users = get_users(vm.vmid)
    except (GuestError, PVEError) as exc:
        return Signal("users", False, f"could not list users: {exc}", indeterminate=True)

    if not users:
        return Signal("users", False, "nobody logged in")
    names = ", ".join(sorted({str(u.get("user", "?")) for u in users}))
    return Signal("users", True, f"logged in: {names}")


def _check_watchlist(vm: VM, sc: SafetyConfig) -> Signal:
    if not sc.watchlist:
        return Signal("processes", False, "watchlist empty")
    try:
        res = exec_cmd(vm.vmid, ["/bin/ps", "-eo", "etimes=,comm=,args="], timeout=30)
    except (GuestError, PVEError) as exc:
        return Signal("processes", False, f"could not list processes: {exc}", indeterminate=True)

    hits: list[str] = []
    for line in res.out.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\s+(\S+)\s*(.*)$", line)
        if not m:
            continue
        age, comm, argv = int(m.group(1)), m.group(2), m.group(3)
        if age < sc.watchlist_min_age_secs:
            continue
        if not any(w.lower() in comm.lower() for w in sc.watchlist):
            continue
        hits.append(f"{comm} (pid age {age // 60}m): {argv[:60]}")

    if hits:
        return Signal(
            "processes",
            True,
            f"{len(hits)} watchlisted process(es) older than "
            f"{sc.watchlist_min_age_secs // 60}m: " + "; ".join(hits[:3]),
        )
    return Signal("processes", False, "no long-running watchlisted processes")


def _check_uptime(vm: VM, sc: SafetyConfig) -> Signal:
    try:
        res = exec_cmd(vm.vmid, ["/bin/cat", "/proc/uptime"], timeout=20)
    except (GuestError, PVEError) as exc:
        return Signal("uptime", False, f"could not read uptime: {exc}", indeterminate=True)

    try:
        uptime = float(res.out.split()[0])
    except (IndexError, ValueError):
        return Signal("uptime", False, "could not parse /proc/uptime", indeterminate=True)

    hours = uptime / 3600
    if uptime < sc.uptime_recent_activity_secs:
        return Signal(
            "uptime",
            True,
            f"booted {int(uptime // 60)}m ago -- someone may be setting it up",
        )
    return Signal("uptime", False, f"up {hours:.1f}h")


def check_vm(vm: VM, sc: SafetyConfig) -> VMReport:
    report = VMReport(vmid=vm.vmid, name=vm.name)

    if not vm.is_running:
        report.signals.append(Signal("power", False, "stopped -- no checks needed"))
        return report

    if not is_responsive(vm.vmid):
        report.signals.append(
            Signal(
                "agent",
                False,
                "guest agent not responding -- cannot verify whether it is in use",
                indeterminate=True,
            )
        )
        return report

    report.signals.append(_check_users(vm, sc))
    report.signals.append(_check_watchlist(vm, sc))
    report.signals.append(_check_uptime(vm, sc))
    # Sampled last: it is the slowest check, and the strongest signal.
    report.signals.append(_check_utilization(vm, sc))
    return report


def render_report(reports: list[VMReport]) -> str:
    out: list[str] = []
    for r in reports:
        status = render.yellow("BUSY") if r.unsafe else render.green("IDLE")
        out.append(f"{render.bold(f'VM {r.vmid} ({r.name})')}  {status}")
        for s in r.signals:
            if s.triggered:
                marker = render.yellow("!")
            elif s.indeterminate:
                marker = render.yellow("?")
            else:
                marker = render.green("ok")
            out.append(f"  {marker:>3}  {s.name:<12} {s.detail}")
        out.append("")
    return "\n".join(out).rstrip()
