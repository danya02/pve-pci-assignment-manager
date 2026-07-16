"""Solver tests. Pure logic -- no host required."""

from __future__ import annotations

import pytest

from pvepci.config import Config, Request
from pvepci.model import HostPCI, HostState, Mapping, Pool, PowerState, VM
from pvepci.solver import SolveError, parse_inline, solve

GPUS = ["gpu0", "gpu1", "gpu2"]


def make_config(fungible: bool = True) -> Config:
    return Config(pools={"gpu": Pool(name="gpu", members=list(GPUS), fungible=fungible)})


def make_state(*vms: VM) -> HostState:
    return HostState(
        node="iipaserver",
        mappings={
            n: Mapping(name=n, path=f"0000:{i}1:00.0", iommugroup=i) for i, n in enumerate(GPUS)
        },
        vms={vm.vmid: vm for vm in vms},
    )


def vm(vmid: int, running: bool, *mappings: str, start: int = 0) -> VM:
    return VM(
        vmid=vmid,
        name=f"vm{vmid}",
        state=PowerState.RUNNING if running else PowerState.STOPPED,
        hostpci=[HostPCI(index=start + i, mapping=m) for i, m in enumerate(mappings)],
    )


def req(vmid: int, **counts: int) -> Request:
    return Request(vmid=vmid, counts=dict(counts))


class TestOwnership:
    def test_only_running_vms_own_devices(self):
        state = make_state(vm(100, True, "gpu0"), vm(101, False, "gpu0"))
        assert state.owners == {"gpu0": 100}

    def test_stopped_vm_claim_is_not_ownership(self):
        state = make_state(vm(101, False, "gpu0"))
        assert state.owners == {}
        assert state.claimants("gpu0") == [101]


class TestMinChurn:
    def test_vm_already_correct_is_untouched(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"))
        plan = solve(make_config(), state, [req(100, gpu=3)])
        assert plan.is_noop
        assert plan.restarts == []

    def test_shrinking_keeps_existing_devices(self):
        """100 drops to 2 -- it should keep two it already has, not shuffle."""
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"))
        plan = solve(make_config(), state, [req(100, gpu=2)])
        change = plan.changes[0]
        assert [h.mapping for h in change.after] == ["gpu0", "gpu1"]
        assert change.removed == ["gpu2"]
        assert change.added == []
        assert change.restart

    def test_split_frees_device_for_stopped_vm(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"), vm(101, False))
        plan = solve(make_config(), state, [req(100, gpu=2), req(101, gpu=1)])
        by_id = {c.vmid: c for c in plan.changes}
        assert [h.mapping for h in by_id[100].after] == ["gpu0", "gpu1"]
        assert [h.mapping for h in by_id[101].after] == ["gpu2"]

    def test_stopped_vm_is_not_started(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"), vm(101, False))
        plan = solve(make_config(), state, [req(100, gpu=2), req(101, gpu=1)])
        by_id = {c.vmid: c for c in plan.changes}
        assert by_id[101].changed
        assert not by_id[101].restart  # config edit only; stays stopped
        assert [c.vmid for c in plan.restarts] == [100]

    def test_no_churn_when_layout_already_satisfied_across_vms(self):
        state = make_state(vm(100, True, "gpu0"), vm(101, True, "gpu1"))
        plan = solve(make_config(), state, [req(100, gpu=1), req(101, gpu=1)])
        assert plan.is_noop

    def test_running_vms_keep_preferentially_over_stopped(self):
        """101 is stopped and claims gpu0; running 100 must keep gpu0 anyway."""
        state = make_state(vm(100, True, "gpu0"), vm(101, False, "gpu0"))
        plan = solve(make_config(), state, [req(100, gpu=1), req(101, gpu=1)])
        by_id = {c.vmid: c for c in plan.changes}
        assert [h.mapping for h in by_id[100].after] == ["gpu0"]
        assert not by_id[100].restart
        assert [h.mapping for h in by_id[101].after] != ["gpu0"]

    def test_release_all(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"))
        plan = solve(make_config(), state, [req(100, gpu=0)])
        assert plan.changes[0].after == []
        assert plan.changes[0].removed == GPUS


class TestIndices:
    def test_indices_preserved_by_default(self):
        """Dropping gpu0 must not renumber gpu1/gpu2 -- that is extra churn."""
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"))
        plan = solve(make_config(), state, [Request(vmid=100, devices=["gpu1", "gpu2"])])
        after = {h.index: h.mapping for h in plan.changes[0].after}
        assert after == {1: "gpu1", 2: "gpu2"}

    def test_renumber_normalizes(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"))
        plan = solve(
            make_config(), state, [Request(vmid=100, devices=["gpu1", "gpu2"])], renumber=True
        )
        after = {h.index: h.mapping for h in plan.changes[0].after}
        assert after == {0: "gpu1", 1: "gpu2"}

    def test_new_device_fills_lowest_free_index(self):
        state = make_state(vm(100, True, "gpu1", start=1))
        plan = solve(make_config(), state, [req(100, gpu=2)])
        after = {h.index: h.mapping for h in plan.changes[0].after}
        assert after == {1: "gpu1", 0: "gpu0"}

    def test_renumber_alone_triggers_restart_but_not_device_change(self):
        state = make_state(vm(100, True, "gpu1", start=1))
        plan = solve(make_config(), state, [req(100, gpu=1)], renumber=True)
        change = plan.changes[0]
        assert change.changed
        assert not change.devices_changed
        assert change.restart


class TestNonFungible:
    def test_named_device_is_honored(self):
        cfg = make_config(fungible=False)
        state = make_state(vm(100, False))
        plan = solve(cfg, state, [Request(vmid=100, devices=["gpu2"])])
        assert [h.mapping for h in plan.changes[0].after] == ["gpu2"]

    def test_named_device_wins_over_fungible_keep(self):
        state = make_state(vm(100, True, "gpu0"), vm(101, False))
        plan = solve(
            make_config(), state, [req(100, gpu=1), Request(vmid=101, devices=["gpu0"])]
        )
        by_id = {c.vmid: c for c in plan.changes}
        assert [h.mapping for h in by_id[101].after] == ["gpu0"]
        assert "gpu0" not in [h.mapping for h in by_id[100].after]
        assert by_id[100].restart


class TestConflicts:
    def test_device_held_by_unmentioned_running_vm_is_an_error(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"), vm(101, False))
        with pytest.raises(SolveError, match="held by running VM 100"):
            solve(make_config(), state, [Request(vmid=101, devices=["gpu0"])])

    def test_pool_exhausted_reports_blocker(self):
        state = make_state(vm(100, True, "gpu0", "gpu1", "gpu2"), vm(101, False))
        with pytest.raises(SolveError, match="exhausted"):
            solve(make_config(), state, [req(101, gpu=2)])

    def test_unknown_vm(self):
        state = make_state(vm(100, True))
        with pytest.raises(SolveError, match="not found"):
            solve(make_config(), state, [req(999, gpu=1)])

    def test_stale_claim_produces_warning_not_error(self):
        """101 (stopped) claims gpu0; giving gpu0 elsewhere warns, never fails."""
        state = make_state(vm(100, True), vm(101, False, "gpu0"))
        plan = solve(make_config(), state, [req(100, gpu=1)])
        assert any("still claims gpu0" in w for w in plan.warnings)
        assert [h.mapping for h in plan.changes[0].after] == ["gpu0"]

    def test_missing_mapping_warns(self):
        cfg = Config(pools={"gpu": Pool("gpu", ["gpu0", "ghost"], fungible=True)})
        state = make_state(vm(100, True))
        plan = solve(cfg, state, [req(100, gpu=1)])
        assert any("ghost" in w for w in plan.warnings)


class TestOtherPools:
    def test_devices_from_unmentioned_pool_are_left_alone(self):
        cfg = Config(
            pools={
                "gpu": Pool("gpu", list(GPUS), fungible=True),
                "nic": Pool("nic", ["nic0"], fungible=False),
            }
        )
        state = HostState(
            node="n",
            mappings={
                **{n: Mapping(n, "", None) for n in GPUS},
                "nic0": Mapping("nic0", "", None),
            },
            vms={
                100: VM(
                    100,
                    "vm100",
                    PowerState.RUNNING,
                    [HostPCI(0, "nic0"), HostPCI(1, "gpu0")],
                )
            },
        )
        plan = solve(cfg, state, [req(100, gpu=2)])
        after = [h.mapping for h in plan.changes[0].after]
        assert "nic0" in after  # untouched pool preserved
        assert sorted(m for m in after if m.startswith("gpu")) == ["gpu0", "gpu1"]


class TestInlineParsing:
    def test_basic(self):
        cfg = make_config()
        reqs = parse_inline(["100=gpu:2", "101=gpu:1"], cfg)
        assert [(r.vmid, r.counts) for r in reqs] == [(100, {"gpu": 2}), (101, {"gpu": 1})]

    def test_devices(self):
        cfg = make_config()
        reqs = parse_inline(["100=devices:gpu0"], cfg)
        assert reqs[0].devices == ["gpu0"]

    def test_combined_terms(self):
        cfg = Config(
            pools={
                "gpu": Pool("gpu", list(GPUS), fungible=True),
                "nic": Pool("nic", ["nic0"], fungible=False),
            }
        )
        reqs = parse_inline(["100=gpu:1,devices:nic0"], cfg)
        assert reqs[0].counts == {"gpu": 1}
        assert reqs[0].devices == ["nic0"]

    def test_non_fungible_by_count_is_rejected(self):
        cfg = make_config(fungible=False)
        with pytest.raises(SolveError, match="non-fungible"):
            parse_inline(["100=gpu:2"], cfg)

    @pytest.mark.parametrize("bad", ["100", "abc=gpu:1", "100=gpu", "100=nope:1", "100=gpu:x"])
    def test_malformed(self, bad):
        with pytest.raises(SolveError):
            parse_inline([bad], make_config())
