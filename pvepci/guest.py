"""QEMU guest agent access.

Two distinct surfaces, easy to confuse:

* `qm guest cmd <vmid> <cmd>` (aliased as `qm agent`) -- built-in agent verbs
  like get-users. It does *not* accept exec/exec-status.
* `qm guest exec <vmid> --timeout N -- /abs/path args` -- runs a command,
  returning {"exitcode":0,"exited":1,"out-data":"..."}. 'out-data' is absent
  when the command printed nothing.

Commands must use absolute paths; pipelines need wrapping in /bin/bash -c.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .pve import PVEError, _run


class GuestError(PVEError):
    pass


@dataclass
class ExecResult:
    exitcode: int
    out: str

    @property
    def ok(self) -> bool:
        return self.exitcode == 0


def agent_cmd(vmid: int, cmd: str, timeout: int = 20):
    """Run a built-in agent verb.

    Returns None for verbs that succeed silently (e.g. 'ping' prints nothing);
    an empty reply is success, not a parse failure.
    """
    try:
        out = _run(["qm", "guest", "cmd", str(vmid), cmd], timeout=timeout)
    except PVEError as exc:
        raise GuestError(f"VM {vmid}: guest agent '{cmd}' failed: {exc}") from exc

    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise GuestError(f"VM {vmid}: agent '{cmd}' returned non-JSON: {out[:200]!r}") from exc


def exec_cmd(vmid: int, argv: list[str], timeout: int = 30) -> ExecResult:
    """Run argv in the guest. argv[0] must be an absolute path."""
    out = _run(
        ["qm", "guest", "exec", str(vmid), "--timeout", str(timeout), "--"] + argv,
        timeout=timeout + 15,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GuestError(f"VM {vmid}: guest exec returned non-JSON: {out[:200]!r}") from exc

    return ExecResult(
        exitcode=int(data.get("exitcode", -1)),
        # 'out-data' is absent when the command printed nothing.
        out=str(data.get("out-data", "")),
    )


def bash(vmid: int, script: str, timeout: int = 30) -> ExecResult:
    return exec_cmd(vmid, ["/bin/bash", "-c", script], timeout=timeout)


def is_responsive(vmid: int) -> bool:
    """True if the guest agent answers. 'ping' succeeds with no output."""
    try:
        agent_cmd(vmid, "ping", timeout=10)
        return True
    except (GuestError, PVEError):
        return False


def get_users(vmid: int) -> list[dict]:
    data = agent_cmd(vmid, "get-users")
    return data if isinstance(data, list) else []
