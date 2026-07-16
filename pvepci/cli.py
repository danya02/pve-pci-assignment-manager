"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, pve, render
from .config import Config, ConfigError
from .solver import Plan, SolveError, parse_inline, solve


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pvepci",
        description="Manage PCI device assignments across Proxmox VE VMs.",
    )
    p.add_argument("--version", action="version", version=f"pvepci {__version__}")
    p.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help=f"config file (default: {config.DEFAULT_CONFIG_PATH})",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show current device assignments and VM power states")

    ap = sub.add_parser(
        "apply",
        help="apply a profile or an ad-hoc layout",
        description=(
            "Apply a layout. Stops affected VMs, rewrites their configs, and "
            "restarts exactly those that were running before."
        ),
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", metavar="NAME", help="a profile from the config")
    group.add_argument(
        "--set",
        dest="assignments",
        nargs="+",
        metavar="VMID=POOL:N",
        help="ad-hoc assignment, e.g. --set 100=gpu:2 101=gpu:1",
    )
    ap.add_argument(
        "-n", "--dry-run", action="store_true", help="show the diff and touch nothing"
    )
    ap.add_argument(
        "--renumber",
        action="store_true",
        help="normalize hostpci indices to canonical order (causes extra restarts)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="proceed despite safety warnings and a user's veto",
    )
    ap.add_argument(
        "--no-confirm",
        action="store_true",
        help="do not prompt for confirmation (still announces and waits)",
    )
    ap.add_argument(
        "--skip-wait",
        action="store_true",
        help="skip the announcement grace period, stopping VMs immediately "
        "(users get no chance to veto)",
    )
    ap.add_argument(
        "--grace",
        type=int,
        metavar="SECS",
        help="override the announcement grace period",
    )

    lp = sub.add_parser("profiles", help="list profiles defined in the config")
    lp.add_argument("--json", action="store_true", help="machine-readable output")

    cp = sub.add_parser("check", help="run the safety checks and report, changing nothing")
    cp.add_argument(
        "vmids", nargs="*", type=int, metavar="VMID", help="VMs to check (default: all running)"
    )

    return p


def _requests(args, cfg: Config):
    if args.profile:
        if args.profile not in cfg.profiles:
            known = ", ".join(sorted(cfg.profiles)) or "none defined"
            raise SolveError(f"unknown profile {args.profile!r} (known: {known})")
        return cfg.profiles[args.profile]
    return parse_inline(args.assignments, cfg)


def _cmd_status(args, cfg: Config | None) -> int:
    state, raw = pve.read_state()
    print(render.render_status(state, raw))
    return 0


def _cmd_profiles(args, cfg: Config) -> int:
    if args.json:
        import json

        print(
            json.dumps(
                {
                    name: {
                        str(r.vmid): {**r.counts, **({"devices": r.devices} if r.devices else {})}
                        for r in reqs
                    }
                    for name, reqs in cfg.profiles.items()
                },
                indent=2,
            )
        )
        return 0

    if not cfg.profiles:
        print(render.dim("no profiles defined in " + str(cfg.path)))
        return 0

    for name, reqs in cfg.profiles.items():
        print(render.bold(name))
        for r in reqs:
            parts = [f"{p}={n}" for p, n in sorted(r.counts.items())]
            parts += [f"devices={','.join(r.devices)}"] if r.devices else []
            print(f"  VM {r.vmid}: {'  '.join(parts)}")
        print()
    return 0


def _cmd_apply(args, cfg: Config) -> int:
    from .apply import execute

    state, raw = pve.read_state()
    for vmid, entries in sorted(raw.items()):
        for key, value in entries:
            print(
                render.warn(
                    f"VM {vmid} has {key}={value} (raw PCI address); pvepci leaves it alone"
                ),
                file=sys.stderr,
            )

    plan: Plan = solve(cfg, state, _requests(args, cfg), renumber=args.renumber)
    print(render.render_plan(plan, renumber=args.renumber))

    if args.dry_run:
        print()
        print(render.dim("dry run -- nothing was changed"))
        return 0

    if plan.is_noop:
        return 0

    return execute(plan, cfg, args)


def _cmd_check(args, cfg: Config) -> int:
    from .safety import check_vm, render_report

    state, _ = pve.read_state()
    vmids = args.vmids or [v.vmid for v in state.vms.values() if v.is_running]
    if not vmids:
        print(render.dim("no running VMs to check"))
        return 0

    reports = []
    for vmid in vmids:
        if vmid not in state.vms:
            print(render.warn(f"VM {vmid} not found"), file=sys.stderr)
            continue
        reports.append(check_vm(state.vms[vmid], cfg.safety))
    print(render_report(reports))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        # `status` is useful even without a config file.
        cfg: Config | None = None
        if args.command != "status" or args.config is not None:
            try:
                cfg = config.load(args.config)
            except ConfigError:
                if args.command == "status":
                    cfg = None
                else:
                    raise

        handlers = {
            "status": _cmd_status,
            "apply": _cmd_apply,
            "profiles": _cmd_profiles,
            "check": _cmd_check,
        }
        return handlers[args.command](args, cfg)

    except (ConfigError, SolveError, pve.PVEError) as exc:
        print(f"{render.red('error:')} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
