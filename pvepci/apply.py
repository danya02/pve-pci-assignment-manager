"""The mutating path: announce, wait, re-check, stop, rewrite, restore.

Lifecycle rule: restart exactly those VMs that were running before, and nothing
else. A VM that was stopped stays stopped, even if its config changed.
"""

from __future__ import annotations

import shlex
import sys
import time

from . import render
from .config import Config, SafetyConfig
from .guest import GuestError, bash, is_responsive
from .pve import PVEError, _run
from .safety import check_vm, render_report
from .solver import Plan, VMChange

VETO_HEADER = [
    "# pvepci is about to STOP this VM to reassign its PCI devices.",
    "# If you are using it and need it left alone, write a line below",
    "# saying so (your name and why). Any text here cancels the operation.",
    "#",
]


class AbortError(Exception):
    pass


def _announce(vmid: int, sc: SafetyConfig, grace_secs: int) -> None:
    mins = max(1, grace_secs // 60)
    msg = (
        f"NOTICE: this VM will be shut down in ~{mins} min to reassign its PCI "
        f"devices. If you are using it, write your objection into {sc.veto_file} "
        f"and the shutdown will be cancelled."
    )
    try:
        bash(vmid, f"/usr/bin/wall {shlex.quote(msg)}", timeout=20)
    except (GuestError, PVEError) as exc:
        print(render.warn(f"VM {vmid}: could not send wall announcement: {exc}"), file=sys.stderr)

    header = "\n".join(VETO_HEADER)
    script = (
        f"/usr/bin/printf '%s\\n' {shlex.quote(header)} > {shlex.quote(sc.veto_file)} && "
        f"/bin/chmod 666 {shlex.quote(sc.veto_file)}"
    )
    try:
        res = bash(vmid, script, timeout=20)
        if not res.ok:
            print(
                render.warn(f"VM {vmid}: could not create veto file {sc.veto_file}"),
                file=sys.stderr,
            )
    except (GuestError, PVEError) as exc:
        print(render.warn(f"VM {vmid}: could not create veto file: {exc}"), file=sys.stderr)


def _read_veto(vmid: int, sc: SafetyConfig) -> str | None:
    """Return the objection text if a user wrote one, else None."""
    try:
        res = bash(
            vmid,
            f"/bin/cat {shlex.quote(sc.veto_file)} 2>/dev/null || true",
            timeout=20,
        )
    except (GuestError, PVEError) as exc:
        print(render.warn(f"VM {vmid}: could not read veto file: {exc}"), file=sys.stderr)
        return None

    lines = [
        ln.strip()
        for ln in res.out.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return "\n".join(lines) if lines else None


def _cleanup_veto(vmid: int, sc: SafetyConfig) -> None:
    try:
        bash(vmid, f"/bin/rm -f {shlex.quote(sc.veto_file)}", timeout=20)
    except (GuestError, PVEError):
        pass


def _wait(grace_secs: int) -> None:
    deadline = time.monotonic() + grace_secs
    while True:
        left = int(deadline - time.monotonic())
        if left <= 0:
            break
        if sys.stderr.isatty():
            print(
                f"\r  waiting {left // 60:02d}:{left % 60:02d} for the grace period "
                "(Ctrl-C to abort)  ",
                end="",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(min(5, max(1, left)))
    if sys.stderr.isatty():
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr, flush=True)


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _stop_vm(vmid: int, timeout: int) -> None:
    print(f"  stopping VM {vmid} (graceful, timeout {timeout}s)...")
    try:
        _run(["qm", "shutdown", str(vmid), "--timeout", str(timeout)], timeout=timeout + 30)
    except PVEError as exc:
        raise AbortError(
            f"VM {vmid} did not shut down gracefully: {exc}\n"
            "Refusing to hard-stop it. Investigate, then retry."
        ) from exc


def _start_vm(vmid: int) -> None:
    print(f"  starting VM {vmid}...")
    _run(["qm", "start", str(vmid)], timeout=180)


def _rewrite(change: VMChange) -> None:
    before = {h.index: h.mapping for h in change.before}
    after = {h.index: h.mapping for h in change.after}

    # Deletes first, so a device never appears at two indices mid-flight.
    for idx in sorted(set(before) - set(after)):
        _run(["qm", "set", str(change.vmid), "--delete", f"hostpci{idx}"], timeout=60)

    for idx in sorted(after):
        if before.get(idx) == after[idx]:
            continue
        _run(
            ["qm", "set", str(change.vmid), f"--hostpci{idx}", f"mapping={after[idx]}"],
            timeout=60,
        )


def execute(plan: Plan, cfg: Config, args) -> int:
    sc = cfg.safety
    grace = args.grace if args.grace is not None else sc.grace_secs
    to_stop = plan.restarts

    print()
    if not to_stop:
        print(render.green("no running VMs are affected; applying config edits only"))
    else:
        print(render.bold("safety checks"))
        print()
        reports = [check_vm_by_change(c, sc) for c in to_stop]
        print(render_report(reports))
        print()

        busy = [r for r in reports if r.unsafe]
        if busy and not args.force:
            names = ", ".join(f"VM {r.vmid}" for r in busy)
            print(render.yellow(f"{names} may be in use (see above)."))
            if not _confirm("Continue anyway?"):
                print("aborted -- re-run with --force to override", file=sys.stderr)
                return 2
        elif not args.no_confirm and not _confirm(
            f"Stop and restart {', '.join(f'VM {c.vmid}' for c in to_stop)}?"
        ):
            print("aborted", file=sys.stderr)
            return 2

    try:
        if to_stop and not args.skip_wait and grace > 0:
            print()
            print(render.bold(f"announcing shutdown, {grace // 60}m grace period"))
            for c in to_stop:
                if is_responsive(c.vmid):
                    _announce(c.vmid, sc, grace)
            _wait(grace)

            print(render.bold("re-checking after the wait"))
            print()
            for c in to_stop:
                if not is_responsive(c.vmid):
                    continue
                objection = _read_veto(c.vmid, sc)
                if objection and not args.force:
                    raise AbortError(
                        f"VM {c.vmid}: a user objected in {sc.veto_file}:\n  "
                        + objection.replace("\n", "\n  ")
                        + "\n\nAborting. Use --force to override."
                    )
                if objection:
                    print(render.warn(f"VM {c.vmid}: overriding objection: {objection}"))

            recheck = [check_vm_by_change(c, sc) for c in to_stop]
            print(render_report(recheck))
            print()
            newly_busy = [r for r in recheck if r.busy]
            if newly_busy and not args.force:
                raise AbortError(
                    ", ".join(f"VM {r.vmid}" for r in newly_busy)
                    + " became busy during the grace period. Aborting.\n"
                    "Use --force to override."
                )

        print(render.bold("applying"))
        for c in to_stop:
            _stop_vm(c.vmid, sc.shutdown_timeout_secs)

        for c in plan.touched:
            print(f"  rewriting VM {c.vmid} config...")
            _rewrite(c)

        # Restart exactly what was running before -- nothing else.
        for c in to_stop:
            _start_vm(c.vmid)

    except AbortError as exc:
        print(f"\n{render.red('error:')} {exc}", file=sys.stderr)
        return 2
    finally:
        for c in to_stop:
            if is_responsive(c.vmid):
                _cleanup_veto(c.vmid, sc)

    print()
    print(render.green("done."))
    return 0


def check_vm_by_change(change: VMChange, sc: SafetyConfig):
    from .model import PowerState, VM

    vm = VM(
        vmid=change.vmid,
        name=change.name,
        state=PowerState.RUNNING if change.was_running else PowerState.STOPPED,
        hostpci=change.before,
    )
    return check_vm(vm, sc)
