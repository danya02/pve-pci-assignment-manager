"""Guest agent tests, against captured real-host output shapes."""

from __future__ import annotations

import pytest

from pvepci import guest
from pvepci.guest import GuestError


class TestAgentCmd:
    def test_silent_verb_is_success_not_failure(self, monkeypatch):
        """`qm guest cmd <vmid> ping` exits 0 and prints nothing.

        Regression: this was parsed as JSON and the resulting error was
        misreported as 'guest agent not responding'.
        """
        monkeypatch.setattr(guest, "_run", lambda *a, **k: "")
        assert guest.agent_cmd(100, "ping") is None
        assert guest.is_responsive(100) is True

    def test_get_users_empty_list(self, monkeypatch):
        """Real VM 100 output: nobody logged in."""
        monkeypatch.setattr(guest, "_run", lambda *a, **k: "[]\n")
        assert guest.get_users(100) == []

    def test_get_users_populated(self, monkeypatch):
        monkeypatch.setattr(
            guest, "_run", lambda *a, **k: '[{"user":"alice","login-time":1}]'
        )
        assert guest.get_users(100)[0]["user"] == "alice"

    def test_non_json_output_raises(self, monkeypatch):
        monkeypatch.setattr(guest, "_run", lambda *a, **k: "not json at all")
        with pytest.raises(GuestError, match="non-JSON"):
            guest.agent_cmd(100, "get-users")

    def test_unresponsive_agent(self, monkeypatch):
        def boom(*a, **k):
            raise guest.PVEError("QEMU guest agent is not running")

        monkeypatch.setattr(guest, "_run", boom)
        assert guest.is_responsive(100) is False


class TestExec:
    def test_parses_exec_result(self, monkeypatch):
        monkeypatch.setattr(
            guest,
            "_run",
            lambda *a, **k: '{"exitcode":0,"exited":1,"out-data":"hello\\n"}',
        )
        res = guest.exec_cmd(100, ["/bin/echo", "hello"])
        assert res.ok and res.out == "hello\n"

    def test_out_data_absent_when_command_prints_nothing(self, monkeypatch):
        """'out-data' is omitted entirely, not empty -- must not KeyError."""
        monkeypatch.setattr(guest, "_run", lambda *a, **k: '{"exitcode":0,"exited":1}')
        res = guest.exec_cmd(100, ["/bin/true"])
        assert res.ok and res.out == ""

    def test_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(guest, "_run", lambda *a, **k: '{"exitcode":1,"exited":1}')
        assert not guest.exec_cmd(100, ["/bin/false"]).ok
