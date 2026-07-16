"""Config loading and validation tests."""

from __future__ import annotations

import pytest

from pvepci import config
from pvepci.config import ConfigError

VALID = """
pools:
  gpu: { members: [gpu0, gpu1, gpu2], fungible: true }

profiles:
  all-to-one:
    100: { gpu: 3 }
  split-2-1:
    100: { gpu: 2 }
    101: { gpu: 1 }
"""


def write(tmp_path, text):
    p = tmp_path / "pvepci.yaml"
    p.write_text(text)
    return p


def load(tmp_path, text):
    return config.load(write(tmp_path, text))


class TestValid:
    def test_parses_approved_schema(self, tmp_path):
        cfg = load(tmp_path, VALID)
        assert cfg.pools["gpu"].members == ["gpu0", "gpu1", "gpu2"]
        assert cfg.pools["gpu"].fungible
        assert {r.vmid: r.counts for r in cfg.profiles["split-2-1"]} == {
            100: {"gpu": 2},
            101: {"gpu": 1},
        }

    def test_vmid_keys_parse_as_ints(self, tmp_path):
        cfg = load(tmp_path, VALID)
        assert all(isinstance(r.vmid, int) for r in cfg.profiles["all-to-one"])

    def test_named_devices(self, tmp_path):
        cfg = load(
            tmp_path,
            """
pools:
  nic: { members: [nic0, nic1], fungible: false }
profiles:
  special:
    100: { devices: [nic0] }
""",
        )
        assert cfg.profiles["special"][0].devices == ["nic0"]

    def test_safety_defaults(self, tmp_path):
        cfg = load(tmp_path, VALID)
        assert cfg.safety.grace_secs == 300
        assert cfg.safety.veto_file == "/tmp/pvepci-veto"
        assert "python" in cfg.safety.watchlist

    def test_safety_override(self, tmp_path):
        cfg = load(tmp_path, VALID + "\nsafety:\n  grace_secs: 60\n  watchlist: [foo]\n")
        assert cfg.safety.grace_secs == 60
        assert cfg.safety.watchlist == ["foo"]
        assert cfg.safety.util_window_secs == 30  # untouched default

    def test_pool_of(self, tmp_path):
        cfg = load(tmp_path, VALID)
        assert cfg.pool_of("gpu1").name == "gpu"
        assert cfg.pool_of("nope") is None


class TestInvalid:
    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            config.load(tmp_path / "nope.yaml")

    def test_bad_yaml(self, tmp_path):
        with pytest.raises(ConfigError, match="invalid YAML"):
            load(tmp_path, "pools: [oops\n")

    def test_no_pools(self, tmp_path):
        with pytest.raises(ConfigError, match="at least one pool"):
            load(tmp_path, "profiles: {}\n")

    def test_unknown_top_level_key(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown top-level"):
            load(tmp_path, VALID + "\nnonsense: 1\n")

    def test_device_in_two_pools(self, tmp_path):
        with pytest.raises(ConfigError, match="only one pool"):
            load(
                tmp_path,
                """
pools:
  a: { members: [gpu0], fungible: true }
  b: { members: [gpu0], fungible: true }
""",
            )

    def test_unknown_pool_in_profile(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown pool"):
            load(tmp_path, VALID + "\n  bad:\n    100: { nope: 1 }\n")

    def test_count_exceeds_pool(self, tmp_path):
        with pytest.raises(ConfigError, match="only 3 members"):
            load(tmp_path, VALID + "\n  bad:\n    100: { gpu: 9 }\n")

    def test_oversubscribed_profile(self, tmp_path):
        with pytest.raises(ConfigError, match="only 3"):
            load(
                tmp_path,
                VALID + "\n  bad:\n    100: { gpu: 2 }\n    101: { gpu: 2 }\n",
            )

    def test_non_fungible_by_count(self, tmp_path):
        with pytest.raises(ConfigError, match="non-fungible"):
            load(
                tmp_path,
                """
pools:
  nic: { members: [nic0, nic1], fungible: false }
profiles:
  bad:
    100: { nic: 1 }
""",
            )

    def test_unknown_device_name(self, tmp_path):
        with pytest.raises(ConfigError, match="not a member of any pool"):
            load(tmp_path, VALID + "\n  bad:\n    100: { devices: [ghost] }\n")

    def test_same_device_to_two_vms(self, tmp_path):
        with pytest.raises(ConfigError, match="assigned to both"):
            load(
                tmp_path,
                VALID + "\n  bad:\n    100: { devices: [gpu0] }\n    101: { devices: [gpu0] }\n",
            )

    def test_negative_count(self, tmp_path):
        with pytest.raises(ConfigError, match="non-negative"):
            load(tmp_path, VALID + "\n  bad:\n    100: { gpu: -1 }\n")

    def test_bool_is_not_a_count(self, tmp_path):
        with pytest.raises(ConfigError, match="non-negative integer"):
            load(tmp_path, VALID + "\n  bad:\n    100: { gpu: true }\n")

    def test_unknown_safety_option(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown safety option"):
            load(tmp_path, VALID + "\nsafety:\n  nope: 1\n")


class TestExampleConfig:
    def test_shipped_example_is_valid(self, tmp_path):
        """The example we tell users to copy must actually load."""
        from pathlib import Path

        example = Path(__file__).parent.parent / "config.example.yaml"
        cfg = config.load(example)
        assert "gpu" in cfg.pools
        assert set(cfg.profiles) == {"all-to-one", "split-2-1", "one-each"}
