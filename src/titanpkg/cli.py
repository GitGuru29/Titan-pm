"""Command line interface for titanpkg (Phase 4).

Supports both Arch ``pacman``-style operation flags (``-Syu``, ``-S``, ``-Ss``,
``-Si``, ``-Sl``, ``-Sc``, ``-Scc``, ``-R``, ``-Rns``, ``-Q``, ``-Qu``, ``-Qi``,
``-Ql``, ``-Qo``, ``-Qk``, ``-Qdt``) and word subcommands (``update``,
``upgrade``, ``install``, ``remove``, ``verify``, ``rollback``, etc.).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, RepoConfig, enabled_repos, load_config, load_repos
from .db import Database, load_db
from .errors import (
    ResolutionError,
    SecurityError,
    TitanError,
    UsageError,
)
from .repo import Keyring, MergedCatalog, load_catalog, sync_repo
from .resolver import (
    PlannedInstall,
    TransactionPlan,
    check_reverse_dependencies,
    resolve_autoremove,
    resolve_install,
    resolve_remove,
    resolve_upgrade,
)
from .transaction import execute_transaction, transaction_lock, verify_installed
from .vercmp import vercmp

COMMANDS = (
    "update",
    "upgrade",
    "install",
    "reinstall",
    "remove",
    "autoremove",
    "list",
    "search",
    "info",
    "files",
    "owns",
    "verify",
    "rollback",
    "clean",
    "doctor",
    "key",
    "self-check",
)

PACMAN_HELP_EPILOG = """\
Arch pacman-style operations (tpac / titanpkg):
  sudo tpac -Syu [pkg...]      synchronize repositories and upgrade all subsystems
  sudo tpac -S <pkg...>        install (or reinstall) packages
  sudo tpac -Sy                synchronize repository databases only
  sudo tpac -Su [pkg...]       upgrade installed subsystems
  tpac -Ss <query>             search repositories for matching packages
  tpac -Si <pkg>               display repository package information
  tpac -Sl                     list all available packages in repositories
  sudo tpac -Sc / -Scc         clean old (-Sc) or all (-Scc) cached packages
  sudo tpac -R <pkg...>        remove installed packages
  sudo tpac -Rns <pkg...>      remove packages and orphaned dependencies
  tpac -Q                      list installed packages
  tpac -Qu                     list upgradable packages
  tpac -Qi <pkg>               display installed package information
  tpac -Ql <pkg>               list files owned by an installed package
  tpac -Qo <path>              query which package owns a filesystem path
  tpac -Qk [pkg...]            verify installed files against SHA-256 hashes
  tpac -Qdt                    list orphaned dependency packages

Documentation: docs/IMPLEMENTATION-PLAN.md
"""


def _add_shared_flags(parser: argparse.ArgumentParser, *, is_sub: bool = False) -> None:
    default_val = argparse.SUPPRESS if is_sub else None
    default_bool = argparse.SUPPRESS if is_sub else False
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=default_val,
        help="operate on a sandbox root (testing / imaging)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=default_val,
        help="override path to titanpkg.json",
    )
    parser.add_argument(
        "--noconfirm",
        "-y",
        action="store_true",
        default=default_bool,
        help="assume yes for all prompts",
    )
    parser.add_argument(
        "--needed",
        action="store_true",
        default=default_bool,
        help="do not reinstall up-to-date targets",
    )
    parser.add_argument(
        "--dry-run",
        "-p",
        "--print",
        dest="dry_run",
        action="store_true",
        default=default_bool,
        help="print the resolved plan and exit without mutating",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=default_bool,
        help="use cached repository indexes and packages only",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=default_bool,
        help="emit machine-readable JSON output",
    )
    parser.add_argument(
        "--run-scripts",
        action="store_true",
        default=default_bool,
        help="run INSTALL scriptlets even when --root is not /",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=argparse.SUPPRESS if is_sub else 0,
        help="increase output verbosity",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="titanpkg",
        description="Standalone package manager for ArchTitan OS subsystems.",
        epilog=PACMAN_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"titanpkg {__version__}"
    )
    _add_shared_flags(parser, is_sub=False)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_update = subparsers.add_parser(
        "update", help="refresh repository indexes (alias: -Sy)"
    )
    _add_shared_flags(p_update, is_sub=True)

    p_upgrade = subparsers.add_parser(
        "upgrade", help="upgrade all or selected installed packages (alias: -Su / -Syu)"
    )
    _add_shared_flags(p_upgrade, is_sub=True)
    p_upgrade.add_argument(
        "--sync",
        action="store_true",
        default=False,
        help="synchronize repository databases before upgrading (-Syu)",
    )
    p_upgrade.add_argument("packages", nargs="*", help="optional package names")

    p_install = subparsers.add_parser(
        "install", help="install packages and their dependencies (alias: -S)"
    )
    _add_shared_flags(p_install, is_sub=True)
    p_install.add_argument(
        "--sync",
        action="store_true",
        default=False,
        help="synchronize repository databases first (-Sy <pkg>)",
    )
    p_install.add_argument(
        "--sysupgrade",
        action="store_true",
        default=False,
        help="upgrade installed packages in the same transaction (-Syu <pkg>)",
    )
    p_install.add_argument("packages", nargs="+", help="package names or specs")

    p_reinstall = subparsers.add_parser(
        "reinstall", help="reinstall currently installed packages"
    )
    _add_shared_flags(p_reinstall, is_sub=True)
    p_reinstall.add_argument("packages", nargs="+", help="package names")

    p_remove = subparsers.add_parser(
        "remove", help="remove installed packages (alias: -R / -Rns)"
    )
    _add_shared_flags(p_remove, is_sub=True)
    p_remove.add_argument(
        "--cascade-orphans",
        action="store_true",
        default=False,
        help="also remove orphaned dependencies (-Rs / -Rns)",
    )
    p_remove.add_argument(
        "--nosave",
        action="store_true",
        default=False,
        help="do not create .pacsave files (-Rn / -Rns)",
    )
    p_remove.add_argument("packages", nargs="+", help="package names")

    p_autoremove = subparsers.add_parser(
        "autoremove", help="remove orphaned dependency packages"
    )
    _add_shared_flags(p_autoremove, is_sub=True)

    p_list = subparsers.add_parser("list", help="list packages (alias: -Q / -Sl / -Qu)")
    _add_shared_flags(p_list, is_sub=True)
    list_group = p_list.add_mutually_exclusive_group()
    list_group.add_argument("--installed", action="store_true")
    list_group.add_argument("--available", action="store_true")
    list_group.add_argument("--upgradable", action="store_true")
    list_group.add_argument("--orphans", action="store_true")

    p_search = subparsers.add_parser(
        "search", help="search package names/descriptions (alias: -Ss)"
    )
    _add_shared_flags(p_search, is_sub=True)
    p_search.add_argument("term", help="search query")

    p_info = subparsers.add_parser(
        "info", help="show package metadata and versions (alias: -Si / -Qi)"
    )
    _add_shared_flags(p_info, is_sub=True)
    p_info.add_argument("package", help="package name")

    p_files = subparsers.add_parser(
        "files", help="list package file manifest (alias: -Ql)"
    )
    _add_shared_flags(p_files, is_sub=True)
    p_files.add_argument("package", help="installed package name")

    p_owns = subparsers.add_parser(
        "owns", help="query which package owns a path (alias: -Qo)"
    )
    _add_shared_flags(p_owns, is_sub=True)
    p_owns.add_argument("path", help="filesystem path to query")

    p_verify = subparsers.add_parser(
        "verify", help="verify installed files against recorded SHA-256 hashes (alias: -Qk)"
    )
    _add_shared_flags(p_verify, is_sub=True)
    p_verify.add_argument("packages", nargs="*", help="optional package names")

    p_rollback = subparsers.add_parser(
        "rollback", help="roll back a package to a previous version"
    )
    _add_shared_flags(p_rollback, is_sub=True)
    p_rollback.add_argument("package", help="package name")
    p_rollback.add_argument("target_version", nargs="?", default=None, help="version")

    p_clean = subparsers.add_parser(
        "clean", help="prune cached packages and txn logs (alias: -Sc / -Scc)"
    )
    _add_shared_flags(p_clean, is_sub=True)
    p_clean.add_argument("--all", dest="clean_all", action="store_true")

    p_doctor = subparsers.add_parser(
        "doctor", help="run config, db, repo and keyring security diagnostics"
    )
    _add_shared_flags(p_doctor, is_sub=True)

    p_key = subparsers.add_parser("key", help="manage trusted repository GPG keys")
    _add_shared_flags(p_key, is_sub=True)
    key_sub = p_key.add_subparsers(dest="key_action", metavar="<import|list>")
    p_key_import = key_sub.add_parser("import", help="import a trusted GPG key file")
    _add_shared_flags(p_key_import, is_sub=True)
    p_key_import.add_argument("key_file", type=Path, help="path to public key file")
    p_key_list = key_sub.add_parser("list", help="list trusted GPG keys")
    _add_shared_flags(p_key_list, is_sub=True)

    p_self = subparsers.add_parser(
        "self-check", help="verify titanpkg's own modules and integrity"
    )
    _add_shared_flags(p_self, is_sub=True)

    return parser


def _translate_pacman_argv(argv: Sequence[str]) -> list[str]:
    """Translate pacman-style flags (``-Syu``, ``-S``, ``-Rns``, ``-Q*``) into subcommand argv."""
    tokens = list(argv)
    if not tokens:
        return tokens

    # If the user already passed a known subcommand or --help/--version, keep as-is
    for tok in tokens:
        if tok in COMMANDS or tok in ("-h", "--help", "--version", "-V"):
            return tokens

    op: str | None = None
    modifiers: list[str] = []
    passthrough: list[str] = []
    positionals: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            positionals.extend(tokens[i + 1 :])
            break
        if tok in ("--root", "--config"):
            passthrough.append(tok)
            if i + 1 < len(tokens):
                passthrough.append(tokens[i + 1])
                i += 2
                continue
        if tok.startswith("--root=") or tok.startswith("--config="):
            passthrough.append(tok)
            i += 1
            continue
        if tok.startswith("--"):
            if tok == "--sync":
                op = op or "S"
            elif tok == "--remove":
                op = op or "R"
            elif tok == "--query":
                op = op or "Q"
            elif tok in ("--refresh",):
                modifiers.append("y")
            elif tok in ("--sysupgrade",):
                modifiers.append("u")
            else:
                passthrough.append(tok)
            i += 1
            continue

        if tok.startswith("-") and len(tok) > 1:
            chars = tok[1:]
            # Check if this short flag group contains S, R, Q or pacman modifiers
            if any(c in "SRQ" for c in chars) or (
                op is not None and all(c in "yusilckodtnp" for c in chars)
            ):
                for c in chars:
                    if c in "SRQ":
                        if op is not None and op != c:
                            raise UsageError(
                                f"only one operation (-{op} or -{c}) may be used at a time"
                            )
                        op = c
                    elif c == "p":
                        passthrough.append("--dry-run")
                    else:
                        modifiers.append(c)
                i += 1
                continue
            passthrough.append(tok)
            i += 1
            continue

        positionals.append(tok)
        i += 1

    if op is None:
        return tokens

    mod_set = set(modifiers)

    if op == "S":
        unknown = mod_set - {"y", "u", "s", "i", "l", "c"}
        if unknown:
            raise UsageError(f"invalid option for -S: -{''.join(sorted(unknown))}")

        if "c" in mod_set:
            c_count = modifiers.count("c")
            cmd = ["clean", *passthrough]
            if c_count >= 2:
                cmd.append("--all")
            return cmd

        if "s" in mod_set:
            query = " ".join(positionals) if positionals else ""
            return ["search", *passthrough, query]

        if "i" in mod_set:
            if not positionals:
                raise UsageError("'-Si' requires a package name")
            return ["info", *passthrough, positionals[0]]

        if "l" in mod_set:
            return ["list", "--available", *passthrough]

        has_y = "y" in mod_set
        has_u = "u" in mod_set

        if has_y and not has_u and not positionals:
            return ["update", *passthrough]

        if has_u and not positionals:
            cmd = ["upgrade", *passthrough]
            if has_y:
                cmd.append("--sync")
            return cmd

        if positionals:
            cmd = ["install", *passthrough]
            if has_y:
                cmd.append("--sync")
            if has_u:
                cmd.append("--sysupgrade")
            cmd.extend(positionals)
            return cmd

        raise UsageError("no targets specified for -S (use -Syu, -Sy, -Su, or -S <pkg>)")

    if op == "R":
        unknown = mod_set - {"s", "n", "u", "c"}
        if unknown:
            raise UsageError(f"invalid option for -R: -{''.join(sorted(unknown))}")
        if not positionals:
            raise UsageError("no targets specified for -R")
        cmd = ["remove", *passthrough]
        if "s" in mod_set or "u" in mod_set:
            cmd.append("--cascade-orphans")
        if "n" in mod_set:
            cmd.append("--nosave")
        cmd.extend(positionals)
        return cmd

    if op == "Q":
        unknown = mod_set - {"u", "i", "l", "o", "k", "d", "t"}
        if unknown:
            raise UsageError(f"invalid option for -Q: -{''.join(sorted(unknown))}")

        if "k" in mod_set:
            return ["verify", *passthrough, *positionals]
        if "o" in mod_set:
            if not positionals:
                raise UsageError("'-Qo' requires a file path")
            return ["owns", *passthrough, positionals[0]]
        if "l" in mod_set:
            if not positionals:
                raise UsageError("'-Ql' requires a package name")
            return ["files", *passthrough, positionals[0]]
        if "i" in mod_set:
            if not positionals:
                raise UsageError("'-Qi' requires a package name")
            return ["info", *passthrough, positionals[0]]
        if "u" in mod_set:
            return ["list", "--upgradable", *passthrough]
        if "d" in mod_set or "t" in mod_set:
            return ["list", "--orphans", *passthrough]
        return ["list", "--installed", *passthrough]

    return tokens


def _emit(data: Any, *, json_mode: bool, human_lines: Sequence[str] = ()) -> None:
    if json_mode:
        print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        for line in human_lines:
            print(line)


def _confirm_plan(plan: TransactionPlan, *, noconfirm: bool, json_mode: bool) -> bool:
    if noconfirm or json_mode:
        return True
    if not sys.stdin.isatty():
        return True
    print("Planned operations:")
    for item in plan.installs:
        if item.previous_version:
            print(f"  {item.action:9} {item.name} {item.previous_version} -> {item.version}")
        else:
            print(f"  {item.action:9} {item.name} {item.version}")
    for rem in plan.removes:
        print(f"  remove    {rem.name} {rem.version}")
    reply = input(":: Proceed with installation? [Y/n] ").strip().lower()
    return reply in ("", "y", "yes")


def _plan_to_dict(plan: TransactionPlan) -> dict[str, Any]:
    return {
        "installs": [
            {
                "name": item.name,
                "version": item.version,
                "previous_version": item.previous_version,
                "action": item.action,
                "reason": item.reason,
                "repo": item.record.repo,
                "replaces": list(item.replaces),
            }
            for item in plan.installs
        ],
        "removes": [
            {"name": rem.name, "version": rem.version} for rem in plan.removes
        ],
    }


def _sync_all_repos(
    repos: Sequence[RepoConfig],
    config: Config,
    *,
    offline: bool,
    json_mode: bool,
    emit_output: bool = True,
) -> list[dict[str, Any]]:
    paths = config.paths
    if offline:
        raise UsageError("cannot synchronize repository databases with --offline")
    if not repos:
        raise UsageError(f"no enabled repositories configured in {paths.repos_dir}")

    synced_summary: list[dict[str, Any]] = []
    lines: list[str] = [":: Synchronizing Titan package databases..."]
    for repo in repos:
        idx = sync_repo(repo, paths, arch=config.arch, timeout=config.timeout)
        synced_summary.append(
            {
                "repo": repo.name,
                "generated": idx.generated,
                "packages": len(idx.packages),
            }
        )
        lines.append(f" {repo.name} is up to date ({len(idx.packages)} packages)")

    if emit_output:
        _emit(
            {"repositories": synced_summary},
            json_mode=json_mode,
            human_lines=lines,
        )
    elif not json_mode:
        for line in lines:
            print(line)
    return synced_summary


def _merge_plans(first: TransactionPlan, second: TransactionPlan) -> TransactionPlan:
    by_name: dict[str, PlannedInstall] = {}
    order: list[str] = []
    for item in (*first.installs, *second.installs):
        if item.name not in by_name:
            order.append(item.name)
        by_name[item.name] = item

    removes_by_name = {rem.name: rem for rem in (*first.removes, *second.removes)}
    return TransactionPlan(
        installs=tuple(by_name[name] for name in order),
        removes=tuple(removes_by_name.values()),
        replaced_names=first.replaced_names | second.replaced_names,
    )


def _run_plan(
    plan: TransactionPlan,
    config: Config,
    args: argparse.Namespace,
    repos_by_name: dict[str, Any],
    *,
    synced_repos: list[dict[str, Any]] | None = None,
) -> int:
    db = load_db(config.paths, default_arch=config.arch)
    if plan.is_empty:
        payload: dict[str, Any] = {"status": "noop", "plan": _plan_to_dict(plan)}
        if synced_repos is not None:
            payload["repositories"] = synced_repos
        _emit(
            payload,
            json_mode=args.json_output,
            human_lines=(" there is nothing to do",),
        )
        return 0

    if args.dry_run:
        lines = ["Dry-run transaction plan:"]
        for item in plan.installs:
            lines.append(f"  {item.action}: {item.name} {item.version} ({item.record.repo})")
        for rem in plan.removes:
            lines.append(f"  remove: {rem.name} {rem.version}")
        payload = {"status": "dry_run", "plan": _plan_to_dict(plan)}
        if synced_repos is not None:
            payload["repositories"] = synced_repos
        _emit(
            payload,
            json_mode=args.json_output,
            human_lines=lines,
        )
        return 0

    if not _confirm_plan(
        plan, noconfirm=args.noconfirm, json_mode=args.json_output
    ):
        print("Transaction aborted by user.", file=sys.stderr)
        return 1

    result = execute_transaction(
        plan,
        config,
        db,
        repos_by_name,
        offline=args.offline,
        run_scripts=args.run_scripts,
        nosave=getattr(args, "nosave", False),
    )
    lines = []
    for pkg in result.installed:
        lines.append(f"installed {pkg.name} ({pkg.version})")
    for name in result.removed:
        lines.append(f"removed {name}")
    for pn in result.pacnew_files:
        lines.append(f"warning: config written as /{pn}")
    for ps in result.pacsave_files:
        lines.append(f"warning: modified config saved as /{ps}")
    for warn in result.warnings:
        lines.append(f"warning: {warn}")

    payload = {
        "status": "committed",
        "installed": [p.to_dict() for p in result.installed],
        "removed": list(result.removed),
        "pacnew": list(result.pacnew_files),
        "pacsave": list(result.pacsave_files),
        "warnings": result.warnings,
    }
    if synced_repos is not None:
        payload["repositories"] = synced_repos
    _emit(
        payload,
        json_mode=args.json_output,
        human_lines=lines,
    )
    return 0


def _dispatch(args: argparse.Namespace, config: Config) -> int:
    cmd = args.command
    paths = config.paths
    repos = enabled_repos(load_repos(paths, config))
    repos_by_name = {r.name: r for r in repos}

    if cmd == "update":
        _sync_all_repos(
            repos,
            config,
            offline=args.offline,
            json_mode=args.json_output,
            emit_output=True,
        )
        return 0

    if cmd in ("install", "reinstall"):
        synced_info: list[dict[str, Any]] | None = None
        if getattr(args, "sync", False):
            synced_info = _sync_all_repos(
                repos,
                config,
                offline=args.offline,
                json_mode=args.json_output,
                emit_output=False,
            )
        db = load_db(paths, default_arch=config.arch)
        catalog = load_catalog(
            repos, paths, arch=config.arch, offline=args.offline, timeout=config.timeout
        )
        upg_plan = TransactionPlan()
        if getattr(args, "sysupgrade", False):
            if not args.json_output:
                print(":: Starting full Titan subsystem upgrade...")
            upg_plan = resolve_upgrade([], catalog, db, root=paths.root)

        reinstall_flag = (cmd == "reinstall") or not getattr(args, "needed", False)
        inst_plan = resolve_install(
            args.packages,
            catalog,
            db,
            reinstall=reinstall_flag,
            root=paths.root,
        )
        plan = _merge_plans(upg_plan, inst_plan)
        return _run_plan(
            plan, config, args, repos_by_name, synced_repos=synced_info
        )

    if cmd == "upgrade":
        synced_info = None
        if getattr(args, "sync", False):
            synced_info = _sync_all_repos(
                repos,
                config,
                offline=args.offline,
                json_mode=args.json_output,
                emit_output=False,
            )
        if not args.json_output:
            print(":: Starting full Titan subsystem upgrade...")
        db = load_db(paths, default_arch=config.arch)
        catalog = load_catalog(
            repos, paths, arch=config.arch, offline=args.offline, timeout=config.timeout
        )
        plan = resolve_upgrade(
            args.packages, catalog, db, root=paths.root
        )
        return _run_plan(
            plan, config, args, repos_by_name, synced_repos=synced_info
        )

    if cmd == "remove":
        db = load_db(paths, default_arch=config.arch)
        catalog: MergedCatalog | None = None
        with contextlib.suppress(Exception):
            catalog = load_catalog(
                repos, paths, arch=config.arch, offline=True, timeout=config.timeout
            )
        check_reverse_dependencies(args.packages, db, catalog)
        plan = resolve_remove(args.packages, db)
        if getattr(args, "cascade_orphans", False):
            # Simulate removing the requested packages first, then find new orphans
            simulated_db = Database.from_dict(
                db.to_dict(), default_arch=config.arch
            )
            for name in args.packages:
                simulated_db.remove(name)
            orphan_plan = resolve_autoremove(simulated_db, catalog)
            plan = _merge_plans(plan, orphan_plan)
        return _run_plan(plan, config, args, repos_by_name)

    if cmd == "autoremove":
        db = load_db(paths, default_arch=config.arch)
        catalog = None
        with contextlib.suppress(Exception):
            catalog = load_catalog(
                repos, paths, arch=config.arch, offline=True, timeout=config.timeout
            )
        plan = resolve_autoremove(db, catalog)
        return _run_plan(plan, config, args, repos_by_name)

    if cmd == "rollback":
        db = load_db(paths, default_arch=config.arch)
        installed_pkg = db.get(args.package)
        catalog = load_catalog(
            repos, paths, arch=config.arch, offline=args.offline, timeout=config.timeout
        )
        target_ver = args.target_version
        if target_ver is None:
            if installed_pkg is None:
                raise ResolutionError(
                    f"package {args.package!r} is not installed; specify a version"
                )
            series = catalog.get_series(args.package)
            if series is None:
                raise ResolutionError(f"package {args.package!r} not found in catalog")
            older = [
                v
                for v in series.sorted_versions()
                if vercmp(v, installed_pkg.version) < 0
            ]
            if not older:
                raise ResolutionError(
                    f"no older version of {args.package!r} available in catalog"
                )
            target_ver = older[-1]

        rec = catalog.get_version(args.package, target_ver)
        if rec is None:
            raise ResolutionError(
                f"version {target_ver!r} of {args.package!r} not found in catalog"
            )
        plan = TransactionPlan(
            installs=(
                PlannedInstall(
                    record=rec,
                    reason=installed_pkg.reason if installed_pkg else "explicit",
                    action="downgrade",
                    previous_version=installed_pkg.version if installed_pkg else None,
                ),
            )
        )
        return _run_plan(plan, config, args, repos_by_name)

    if cmd == "list":
        db = load_db(paths, default_arch=config.arch)
        if getattr(args, "orphans", False):
            catalog = None
            with contextlib.suppress(Exception):
                catalog = load_catalog(
                    repos,
                    paths,
                    arch=config.arch,
                    offline=True,
                    timeout=config.timeout,
                )
            orphan_plan = resolve_autoremove(db, catalog)
            items = [pkg.to_dict() for pkg in orphan_plan.removes]
            lines = [f"{pkg.name} {pkg.version}" for pkg in orphan_plan.removes]
            _emit(items, json_mode=args.json_output, human_lines=lines)
            return 0

        if args.available or args.upgradable:
            catalog = load_catalog(
                repos,
                paths,
                arch=config.arch,
                offline=args.offline,
                timeout=config.timeout,
            )
            if args.available:
                items = []
                lines = []
                for name in sorted(catalog.packages):
                    latest = catalog.packages[name].get_latest()
                    inst = db.get(name)
                    items.append(
                        {
                            "name": name,
                            "version": latest.version,
                            "repo": latest.repo,
                            "installed": inst.version if inst else None,
                        }
                    )
                    tag = f" [installed: {inst.version}]" if inst else ""
                    lines.append(f"{latest.repo} {name} {latest.version}{tag}")
                _emit(items, json_mode=args.json_output, human_lines=lines)
                return 0

            # --upgradable
            items = []
            lines = []
            for name, inst in sorted(db.packages.items()):
                latest = catalog.get_latest(name)
                if latest and vercmp(latest.version, inst.version) > 0:
                    items.append(
                        {
                            "name": name,
                            "installed": inst.version,
                            "available": latest.version,
                            "repo": latest.repo,
                        }
                    )
                    lines.append(f"{name} {inst.version} -> {latest.version}")
            _emit(items, json_mode=args.json_output, human_lines=lines)
            return 0

        # Default: --installed
        items = [pkg.to_dict() for _, pkg in sorted(db.packages.items())]
        lines = [
            f"{pkg.name} {pkg.version}"
            for _, pkg in sorted(db.packages.items())
        ]
        _emit(items, json_mode=args.json_output, human_lines=lines)
        return 0

    if cmd == "search":
        catalog = load_catalog(
            repos, paths, arch=config.arch, offline=args.offline, timeout=config.timeout
        )
        matches = catalog.search(args.term)
        items = [
            {
                "name": m.name,
                "version": m.version,
                "repo": m.repo,
                "description": m.metadata.description,
            }
            for m in matches
        ]
        lines = [
            f"{m.repo}/{m.name} {m.version}\n    {m.metadata.description}"
            for m in matches
        ]
        _emit(items, json_mode=args.json_output, human_lines=lines)
        return 0

    if cmd == "info":
        db = load_db(paths, default_arch=config.arch)
        inst = db.get(args.package)
        rec = None
        series_versions: list[str] = []
        with contextlib.suppress(Exception):
            catalog = load_catalog(
                repos,
                paths,
                arch=config.arch,
                offline=args.offline,
                timeout=config.timeout,
            )
            series = catalog.get_series(args.package)
            if series is not None:
                rec = series.get_latest()
                series_versions = series.sorted_versions()

        if inst is None and rec is None:
            raise ResolutionError(f"package {args.package!r} not found")

        payload: dict[str, Any] = {
            "name": args.package,
            "installed": inst.to_dict() if inst else None,
            "available": rec.metadata.to_dict() if rec else None,
            "versions": series_versions,
        }
        lines = [f"Name           : {args.package}"]
        if inst:
            lines.append(f"Installed      : {inst.version} ({inst.reason})")
        if rec:
            lines.append(f"Latest         : {rec.version} [{rec.repo}]")
            lines.append(f"Description    : {rec.metadata.description}")
            lines.append(f"Available Vers : {', '.join(series_versions)}")
        _emit(payload, json_mode=args.json_output, human_lines=lines)
        return 0

    if cmd == "files":
        db = load_db(paths, default_arch=config.arch)
        inst = db.get(args.package)
        if inst is None:
            raise ResolutionError(f"package {args.package!r} is not installed")
        entries = [e.to_dict() for e in inst.files]
        lines = [f"{inst.name} /{e.path}" for e in inst.files]
        _emit(entries, json_mode=args.json_output, human_lines=lines)
        return 0

    if cmd == "owns":
        db = load_db(paths, default_arch=config.arch)
        raw_query = args.path.strip().lstrip("/")
        owner_name = db.owner_of(raw_query)
        if owner_name is None:
            raise ResolutionError(f"no Titan package owns /{raw_query}")
        owner_pkg = db.get(owner_name)
        ver = owner_pkg.version if owner_pkg else ""
        _emit(
            {"path": f"/{raw_query}", "package": owner_name, "version": ver},
            json_mode=args.json_output,
            human_lines=[f"/{raw_query} is owned by {owner_name} {ver}"],
        )
        return 0

    if cmd == "verify":
        db = load_db(paths, default_arch=config.arch)
        issues = verify_installed(db, config, args.packages)
        hard_issues = [i for i in issues if i.issue != "modified_backup"]
        lines = [f"{i.package}: /{i.path} ({i.issue})" for i in issues]
        if not lines:
            lines = [":: all verified files match recorded SHA-256 hashes and modes"]
        _emit(
            {
                "ok": len(hard_issues) == 0,
                "issues": [i.to_dict() for i in issues],
            },
            json_mode=args.json_output,
            human_lines=lines,
        )
        return 0 if not hard_issues else SecurityError.exit_code

    if cmd == "clean":
        with transaction_lock(paths.lock_file):
            db = load_db(paths, default_arch=config.arch)
            keep_filenames = {
                f"{p.name}-{p.version}-{p.arch}.tpkg" for p in db.packages.values()
            }
            removed_files: list[str] = []
            if paths.package_cache_dir.exists():
                for item in sorted(paths.package_cache_dir.iterdir()):
                    should_rm = (
                        args.clean_all
                        or item.name.rstrip(".sig") not in keep_filenames
                    )
                    if should_rm and (item.is_file() or item.is_symlink()):
                        item.unlink()
                        removed_files.append(item.name)
            if paths.transactions_dir.exists():
                for txn_sub in sorted(paths.transactions_dir.iterdir()):
                    if txn_sub.is_dir():
                        shutil.rmtree(txn_sub, ignore_errors=True)
                        removed_files.append(f"transactions/{txn_sub.name}")
            if args.clean_all and paths.index_cache_dir.exists():
                for idx_file in sorted(paths.index_cache_dir.iterdir()):
                    if idx_file.is_file():
                        idx_file.unlink()
                        removed_files.append(f"index/{idx_file.name}")
        _emit(
            {"removed": removed_files},
            json_mode=args.json_output,
            human_lines=[f"cleaned {len(removed_files)} cached item(s)"],
        )
        return 0

    if cmd == "key":
        kr = Keyring(paths)
        if args.key_action == "import":
            keys = kr.import_key(args.key_file)
            _emit(
                {"keys": [k.to_dict() for k in keys]},
                json_mode=args.json_output,
                human_lines=[f"imported key; {len(keys)} trusted key(s) in keyring"],
            )
            return 0
        if args.key_action == "list":
            keys = kr.list_keys()
            lines = [f"{k.fingerprint}  {k.uid}" for k in keys] or [
                "no trusted keys in keyring"
            ]
            _emit(
                {"keys": [k.to_dict() for k in keys]},
                json_mode=args.json_output,
                human_lines=lines,
            )
            return 0
        raise UsageError("usage: titanpkg key <import|list>")

    if cmd == "doctor":
        db = load_db(paths, default_arch=config.arch)
        kr = Keyring(paths)
        warnings: list[str] = []
        if config.siglevel != "required":
            warnings.append(f"global siglevel is {config.siglevel!r} (recommended: 'required')")
        has_keys = kr.has_keys()
        if not has_keys:
            warnings.append(f"no trusted GPG keys present in {paths.keyring_path}")
        for r in repos:
            if r.siglevel != "required":
                warnings.append(f"repo {r.name!r} has siglevel={r.siglevel!r}")
        verify_issues = verify_installed(db, config)
        hard_verify = [i for i in verify_issues if i.issue != "modified_backup"]
        if hard_verify:
            warnings.append(f"{len(hard_verify)} installed file(s) failed integrity verification")

        report = {
            "ok": len(hard_verify) == 0,
            "root": str(paths.root),
            "siglevel": config.siglevel,
            "installed_packages": len(db.packages),
            "enabled_repos": [r.name for r in repos],
            "has_trusted_keys": has_keys,
            "warnings": warnings,
        }
        lines = [
            f"Root               : {paths.root}",
            f"Signature policy   : {config.siglevel}",
            f"Installed packages : {len(db.packages)}",
            f"Enabled repos      : {', '.join(r.name for r in repos) or 'none'}",
            f"Trusted GPG keys   : {'present' if has_keys else 'missing'}",
        ]
        for w in warnings:
            lines.append(f"WARNING: {w}")
        _emit(report, json_mode=args.json_output, human_lines=lines)
        return 0 if not hard_verify else SecurityError.exit_code

    if cmd == "self-check":
        import py_compile

        pkg_root = Path(__file__).resolve().parent
        py_files = sorted(pkg_root.rglob("*.py"))
        checked: list[str] = []
        for py_file in py_files:
            py_compile.compile(str(py_file), doraise=True)
            checked.append(py_file.relative_to(pkg_root).as_posix())
        _emit(
            {"ok": True, "version": __version__, "modules_checked": checked},
            json_mode=args.json_output,
            human_lines=[
                f"titanpkg {__version__}: self-check passed ({len(checked)} modules verified)"
            ],
        )
        return 0

    raise UsageError(f"unknown command: {cmd}")


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    try:
        translated = _translate_pacman_argv(raw_argv)
    except TitanError as exc:
        if "--json" in raw_argv:
            print(
                json.dumps(
                    {
                        "error": exc.__class__.__name__,
                        "message": str(exc),
                        "exit_code": exc.exit_code,
                    },
                    indent=2,
                )
            )
        else:
            print(f"titanpkg: error: {exc}", file=sys.stderr)
        return exc.exit_code

    args = parser.parse_args(translated)

    if args.command is None:
        parser.print_help()
        return 0

    try:
        config = load_config(root=args.root, config_file=args.config)
        return _dispatch(args, config)
    except TitanError as exc:
        if getattr(args, "json_output", False):
            print(
                json.dumps(
                    {
                        "error": exc.__class__.__name__,
                        "message": str(exc),
                        "exit_code": exc.exit_code,
                    },
                    indent=2,
                )
            )
        else:
            print(f"titanpkg: error: {exc}", file=sys.stderr)
        return exc.exit_code
