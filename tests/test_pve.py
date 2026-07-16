"""Tests for the pure parsing helpers in the host-interface layer.

Fixtures are verbatim output shapes captured from the live host (PVE 9.2.4).
"""

from __future__ import annotations

import pytest

from pvepci.pve import _hostpci_from_config, parse_hostpci_value, parse_mapping_entry

# Captured from: pvesh get /cluster/mapping/pci --output-format json
LIVE_MAPPING = {
    "digest": "b5a6552d5ad4265acf0e12640960a9f4b33e2f2f",
    "id": "gpu1",
    "map": [
        "id=10de:1db8,iommugroup=13,node=iipaserver,path=0000:81:00.0,subsystem-id=10de:12ab"
    ],
    "type": "pci",
}


class TestMappingParsing:
    def test_parses_live_entry(self):
        m = parse_mapping_entry(LIVE_MAPPING)
        assert m.name == "gpu1"
        assert m.path == "0000:81:00.0"
        assert m.iommugroup == 13
        assert m.device_id == "10de:1db8"
        assert m.subsystem_id == "10de:12ab"
        assert m.node == "iipaserver"

    def test_missing_map_is_tolerated(self):
        m = parse_mapping_entry({"id": "orphan", "map": []})
        assert m.name == "orphan"
        assert m.iommugroup is None


class TestHostPCIParsing:
    def test_mapping_form(self):
        assert parse_hostpci_value("mapping=gpu0") == "gpu0"

    def test_mapping_with_extra_options(self):
        assert parse_hostpci_value("mapping=gpu0,pcie=1,x-vga=1") == "gpu0"

    def test_raw_address_is_not_a_mapping(self):
        assert parse_hostpci_value("0000:01:00.0") is None
        assert parse_hostpci_value("0000:01:00.0,pcie=1") is None


class TestConfigExtraction:
    def test_extracts_vm100_shape(self):
        """VM 100's real config: three mapped GPUs."""
        cfg = {
            "name": "migrated-ubuntu",
            "agent": "1,fstrim_cloned_disks=1",
            "memory": 200000,
            "onboot": 1,
            "hostpci0": "mapping=gpu0",
            "hostpci1": "mapping=gpu1",
            "hostpci2": "mapping=gpu2",
        }
        mapped, raw = _hostpci_from_config(cfg)
        assert [(h.index, h.mapping) for h in mapped] == [
            (0, "gpu0"),
            (1, "gpu1"),
            (2, "gpu2"),
        ]
        assert raw == []

    def test_raw_entries_are_reported_separately(self):
        mapped, raw = _hostpci_from_config(
            {"hostpci0": "mapping=gpu0", "hostpci1": "0000:81:00.0"}
        )
        assert [h.mapping for h in mapped] == ["gpu0"]
        assert raw == [("hostpci1", "0000:81:00.0")]

    def test_ignores_unrelated_keys(self):
        mapped, raw = _hostpci_from_config({"net0": "virtio=AA:BB", "scsi0": "disk"})
        assert mapped == [] and raw == []

    @pytest.mark.parametrize("key", ["hostpci10", "hostpci0"])
    def test_multi_digit_indices(self, key):
        mapped, _ = _hostpci_from_config({key: "mapping=gpu0"})
        assert mapped[0].index == int(key.removeprefix("hostpci"))
