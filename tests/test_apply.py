"""Tests for the mutating path's logic, with the host calls stubbed."""

from __future__ import annotations

from pvepci import apply as apply_mod
from pvepci.apply import _read_veto, _rewrite
from pvepci.config import SafetyConfig
from pvepci.model import HostPCI
from pvepci.solver import VMChange


def change(vmid=101, before=(), after=(), running=False) -> VMChange:
    return VMChange(
        vmid=vmid,
        name=f"vm{vmid}",
        was_running=running,
        before=[HostPCI(i, m) for i, m in before],
        after=[HostPCI(i, m) for i, m in after],
    )


class TestRewrite:
    def _capture(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(apply_mod, "_run", lambda argv, **k: calls.append(argv) or "")
        return calls

    def test_deletes_before_sets(self, monkeypatch):
        """A device must never appear at two indices mid-flight."""
        calls = self._capture(monkeypatch)
        _rewrite(change(before=[(0, "gpu0"), (1, "gpu1")], after=[(0, "gpu1")]))
        assert calls == [
            ["qm", "set", "101", "--delete", "hostpci1"],
            ["qm", "set", "101", "--hostpci0", "mapping=gpu1"],
        ]

    def test_unchanged_index_is_not_rewritten(self, monkeypatch):
        calls = self._capture(monkeypatch)
        _rewrite(change(before=[(0, "gpu0"), (1, "gpu1")], after=[(0, "gpu0")]))
        assert calls == [["qm", "set", "101", "--delete", "hostpci1"]]

    def test_pure_addition(self, monkeypatch):
        calls = self._capture(monkeypatch)
        _rewrite(change(before=[], after=[(0, "gpu2")]))
        assert calls == [["qm", "set", "101", "--hostpci0", "mapping=gpu2"]]

    def test_full_release(self, monkeypatch):
        calls = self._capture(monkeypatch)
        _rewrite(change(before=[(0, "gpu0")], after=[]))
        assert calls == [["qm", "set", "101", "--delete", "hostpci0"]]


class TestVeto:
    def test_header_only_is_not_a_veto(self, monkeypatch):
        monkeypatch.setattr(
            apply_mod, "bash", lambda *a, **k: FakeRes("\n".join(apply_mod.VETO_HEADER))
        )
        assert _read_veto(100, SafetyConfig()) is None

    def test_user_text_is_a_veto(self, monkeypatch):
        body = "\n".join(apply_mod.VETO_HEADER) + "\nalice: mid-run, please wait\n"
        monkeypatch.setattr(apply_mod, "bash", lambda *a, **k: FakeRes(body))
        assert _read_veto(100, SafetyConfig()) == "alice: mid-run, please wait"

    def test_blank_lines_ignored(self, monkeypatch):
        monkeypatch.setattr(apply_mod, "bash", lambda *a, **k: FakeRes("#c\n\n   \n"))
        assert _read_veto(100, SafetyConfig()) is None

    def test_missing_file_is_not_a_veto(self, monkeypatch):
        monkeypatch.setattr(apply_mod, "bash", lambda *a, **k: FakeRes(""))
        assert _read_veto(100, SafetyConfig()) is None

    def test_unreadable_veto_does_not_crash(self, monkeypatch):
        def boom(*a, **k):
            raise apply_mod.PVEError("agent gone")

        monkeypatch.setattr(apply_mod, "bash", boom)
        assert _read_veto(100, SafetyConfig()) is None


class FakeRes:
    def __init__(self, out: str):
        self.out = out
        self.exitcode = 0

    @property
    def ok(self) -> bool:
        return True
