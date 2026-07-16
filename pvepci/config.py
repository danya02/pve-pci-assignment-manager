"""Config loading and validation.

Schema (as approved):

    pools:
      gpu: { members: [gpu0, gpu1, gpu2], fungible: true }

    profiles:
      all-to-one:
        100: { gpu: 3 }
      split-2-1:
        100: { gpu: 2 }
        101: { gpu: 1 }
      special:
        100: { devices: [nic-sriov-0] }   # non-fungible, named

Everything under `safety` is optional and falls back to Defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .model import Pool

DEFAULT_CONFIG_PATH = Path("/etc/pve/pvepci.yaml")

# Long-running processes that suggest someone is actually working in the guest.
DEFAULT_WATCHLIST = ["python", "python3", "torchrun", "jupyter", "ipython"]


class ConfigError(Exception):
    pass


@dataclass
class SafetyConfig:
    # Utilization sampled over a window -- a single instantaneous reading is
    # not enough, since bursty inference reads 0% between requests.
    util_window_secs: int = 30
    util_interval_secs: int = 2
    util_threshold_pct: int = 10
    # Grace period between the wall announcement and the shutdown.
    grace_secs: int = 300
    shutdown_timeout_secs: int = 180
    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    watchlist_min_age_secs: int = 600
    uptime_recent_activity_secs: int = 900
    veto_file: str = "/tmp/pvepci-veto"


@dataclass
class Request:
    """What a profile asks of one VM: pool counts and/or explicit devices."""

    vmid: int
    counts: dict[str, int] = field(default_factory=dict)
    devices: list[str] = field(default_factory=list)


@dataclass
class Config:
    pools: dict[str, Pool] = field(default_factory=dict)
    profiles: dict[str, list[Request]] = field(default_factory=dict)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    path: Path | None = None

    def pool_of(self, member: str) -> Pool | None:
        for pool in self.pools.values():
            if member in pool.members:
                return pool
        return None

    @property
    def managed_members(self) -> set[str]:
        return {m for p in self.pools.values() for m in p.members}


def _parse_pools(raw: dict) -> dict[str, Pool]:
    pools: dict[str, Pool] = {}
    if not isinstance(raw, dict):
        raise ConfigError("'pools' must be a mapping of pool name -> definition")

    seen: dict[str, str] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"pool {name!r} must be a mapping")
        members = body.get("members")
        if not isinstance(members, list) or not members:
            raise ConfigError(f"pool {name!r} needs a non-empty 'members' list")
        members = [str(m) for m in members]
        for m in members:
            if m in seen:
                raise ConfigError(
                    f"device {m!r} appears in both pool {seen[m]!r} and {name!r}; "
                    "a device may belong to only one pool"
                )
            seen[m] = str(name)
        pools[str(name)] = Pool(
            name=str(name),
            members=members,
            fungible=bool(body.get("fungible", False)),
        )
    return pools


def _parse_request(vmid_raw, body, profile: str, pools: dict[str, Pool]) -> Request:
    try:
        vmid = int(vmid_raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"profile {profile!r}: {vmid_raw!r} is not a valid VMID"
        ) from None

    if not isinstance(body, dict):
        raise ConfigError(f"profile {profile!r}, VM {vmid}: entry must be a mapping")

    req = Request(vmid=vmid)
    all_members = {m for p in pools.values() for m in p.members}

    for key, value in body.items():
        key = str(key)
        if key == "devices":
            if not isinstance(value, list):
                raise ConfigError(
                    f"profile {profile!r}, VM {vmid}: 'devices' must be a list"
                )
            for dev in (str(d) for d in value):
                if dev not in all_members:
                    raise ConfigError(
                        f"profile {profile!r}, VM {vmid}: device {dev!r} is not a "
                        "member of any pool"
                    )
                req.devices.append(dev)
            continue

        if key not in pools:
            raise ConfigError(
                f"profile {profile!r}, VM {vmid}: unknown pool {key!r} "
                f"(known: {', '.join(sorted(pools)) or 'none'})"
            )
        pool = pools[key]
        if not pool.fungible:
            raise ConfigError(
                f"profile {profile!r}, VM {vmid}: pool {key!r} is non-fungible, so "
                "it must be requested by name under 'devices', not by count"
            )
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"profile {profile!r}, VM {vmid}: count for pool {key!r} must be a "
                f"non-negative integer, got {value!r}"
            )
        if value > len(pool.members):
            raise ConfigError(
                f"profile {profile!r}, VM {vmid}: asks for {value} from pool {key!r} "
                f"which has only {len(pool.members)} members"
            )
        req.counts[key] = value

    return req


def _parse_profiles(raw: dict, pools: dict[str, Pool]) -> dict[str, list[Request]]:
    profiles: dict[str, list[Request]] = {}
    if not isinstance(raw, dict):
        raise ConfigError("'profiles' must be a mapping of name -> definition")

    for name, body in raw.items():
        name = str(name)
        if not isinstance(body, dict):
            raise ConfigError(f"profile {name!r} must be a mapping of VMID -> request")
        reqs = [_parse_request(vmid, entry, name, pools) for vmid, entry in body.items()]

        seen_vmids = [r.vmid for r in reqs]
        dupes = {v for v in seen_vmids if seen_vmids.count(v) > 1}
        if dupes:
            raise ConfigError(
                f"profile {name!r}: VM {', '.join(map(str, sorted(dupes)))} listed twice"
            )

        # A named device may only go to one VM within a profile.
        claimed: dict[str, int] = {}
        for r in reqs:
            for dev in r.devices:
                if dev in claimed:
                    raise ConfigError(
                        f"profile {name!r}: device {dev!r} assigned to both VM "
                        f"{claimed[dev]} and VM {r.vmid}"
                    )
                claimed[dev] = r.vmid

        # Total demand per fungible pool must fit.
        for pool_name, pool in pools.items():
            demand = sum(r.counts.get(pool_name, 0) for r in reqs)
            named = sum(1 for d in claimed if d in pool.members)
            if demand + named > len(pool.members):
                raise ConfigError(
                    f"profile {name!r}: requests {demand + named} devices from pool "
                    f"{pool_name!r}, which has only {len(pool.members)}"
                )

        profiles[name] = reqs
    return profiles


def _parse_safety(raw: dict | None) -> SafetyConfig:
    safety = SafetyConfig()
    if raw is None:
        return safety
    if not isinstance(raw, dict):
        raise ConfigError("'safety' must be a mapping")

    known = {f for f in vars(safety)}
    for key, value in raw.items():
        key = str(key)
        if key not in known:
            raise ConfigError(
                f"unknown safety option {key!r} (known: {', '.join(sorted(known))})"
            )
        current = getattr(safety, key)
        if isinstance(current, list):
            if not isinstance(value, list):
                raise ConfigError(f"safety.{key} must be a list")
            setattr(safety, key, [str(v) for v in value])
        elif isinstance(current, int):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"safety.{key} must be an integer, got {value!r}")
            if value < 0:
                raise ConfigError(f"safety.{key} must be non-negative")
            setattr(safety, key, value)
        else:
            setattr(safety, key, str(value))
    return safety


def load(path: Path | None = None) -> Config:
    if path is None:
        env = os.environ.get("PVEPCI_CONFIG")
        path = Path(env) if env else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise ConfigError(
            f"config not found at {path}\n"
            "Create one, or pass --config <path>. See config.example.yaml."
        )

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    unknown = set(raw) - {"pools", "profiles", "safety"}
    if unknown:
        raise ConfigError(
            f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}"
        )

    pools = _parse_pools(raw.get("pools") or {})
    if not pools:
        raise ConfigError(f"{path}: at least one pool must be defined")

    return Config(
        pools=pools,
        profiles=_parse_profiles(raw.get("profiles") or {}, pools),
        safety=_parse_safety(raw.get("safety")),
        path=path,
    )
