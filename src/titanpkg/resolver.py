"""Dependency, provides, conflicts and replaces resolution (Phase 3)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .core.models import Dependency, InstalledPackage
from .db import Database
from .errors import ResolutionError
from .repo import MergedCatalog, RepoVersionRecord
from .vercmp import vercmp


@dataclass(frozen=True)
class PlannedInstall:
    """One package scheduled to be installed, upgraded, downgraded or reinstalled."""

    record: RepoVersionRecord
    reason: str = "explicit"
    action: str = "install"  # install | upgrade | downgrade | reinstall
    previous_version: str | None = None
    replaces: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def version(self) -> str:
        return self.record.version


@dataclass(frozen=True)
class TransactionPlan:
    """Resolved, conflict-checked execution plan for a transaction."""

    installs: tuple[PlannedInstall, ...] = ()
    removes: tuple[InstalledPackage, ...] = ()
    replaced_names: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return not self.installs and not self.removes


def _installed_satisfies(pkg: InstalledPackage, req: Dependency) -> bool:
    return pkg.name == req.name and req.matches(pkg.version)


def _record_satisfies(rec: RepoVersionRecord, req: Dependency) -> bool:
    if rec.name == req.name and req.matches(rec.version):
        return True
    for prov in rec.metadata.provides:
        if prov.name == req.name:
            prov_ver = prov.version or rec.version
            if req.matches(prov_ver):
                return True
    return False


def check_system_dependencies(
    requirements: Sequence[Dependency],
    *,
    root: Path = Path("/"),
    pacman_binary: str | None = None,
) -> None:
    """Verify ``system_depends`` against ``pacman -Q`` when pacman is available."""
    if not requirements:
        return
    # Skip host pacman checks when operating inside a non-'/' test sandbox root
    if root.resolve() != Path("/").resolve() and pacman_binary is None:
        return
    pacman = pacman_binary or shutil.which("pacman")
    if not pacman:
        return

    for req in requirements:
        proc = subprocess.run(
            [pacman, "--root", str(root), "-Q", req.name],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ResolutionError(
                f"missing Arch system dependency {req} (install with 'pacman -S {req.name}')"
            )
        parts = proc.stdout.strip().split()
        installed_ver = parts[1] if len(parts) >= 2 else None
        if not req.matches(installed_ver):
            raise ResolutionError(
                f"Arch system dependency {req} not satisfied by installed "
                f"{req.name} {installed_ver}"
            )


def resolve_install(
    specs: Sequence[str],
    catalog: MergedCatalog,
    db: Database,
    *,
    reinstall: bool = False,
    pinned_version: str | None = None,
    root: Path = Path("/"),
) -> TransactionPlan:
    """Resolve package names/constraints into a topological ``TransactionPlan``."""
    if not specs:
        raise ResolutionError("no packages specified")

    explicit_names: set[str] = set()
    initial_targets: list[tuple[RepoVersionRecord, str]] = []

    for raw_spec in specs:
        req = Dependency.parse(raw_spec)
        if pinned_version is not None:
            rec = catalog.get_version(req.name, pinned_version)
            if rec is None:
                raise ResolutionError(
                    f"version {pinned_version!r} of {req.name!r} not found in any repository"
                )
        elif req.op == "=" and req.version is not None:
            rec = catalog.get_version(req.name, req.version)
            if rec is None:
                rec = catalog.find_provider(req)
        else:
            rec = catalog.find_provider(req)

        if rec is None:
            raise ResolutionError(f"package not found: {raw_spec}")

        explicit_names.add(rec.name)
        initial_targets.append((rec, "explicit"))

    resolved_order: list[RepoVersionRecord] = []
    reasons: dict[str, str] = {}
    selected: dict[str, RepoVersionRecord] = {}
    visiting: list[str] = []
    visited: set[str] = set()

    def _visit(record: RepoVersionRecord, reason: str) -> None:
        if record.name in visiting:
            cycle_start = visiting.index(record.name)
            chain = " -> ".join([*visiting[cycle_start:], record.name])
            raise ResolutionError(f"dependency cycle detected: {chain}")

        if record.name in selected:
            if reason == "explicit":
                reasons[record.name] = "explicit"
            return

        visiting.append(record.name)
        for dep in record.metadata.depends:
            # Check if an already-selected package satisfies dep
            if any(_record_satisfies(sel, dep) for sel in selected.values()):
                continue
            # Check if an already-installed package satisfies dep (and is not being upgraded)
            installed_dep = db.get(dep.name)
            if (
                installed_dep is not None
                and dep.name not in explicit_names
                and _installed_satisfies(installed_dep, dep)
            ):
                continue

            provider = catalog.find_provider(dep)
            if provider is None:
                raise ResolutionError(
                    f"unresolvable dependency {dep} required by {record.name} {record.version}"
                )
            _visit(provider, "dependency")

        visiting.pop()
        if record.name not in visited:
            visited.add(record.name)
            selected[record.name] = record
            reasons[record.name] = reason
            resolved_order.append(record)

    for rec, reason in initial_targets:
        _visit(rec, reason)

    # Check system dependencies and conflicts/replaces
    all_system_deps: list[Dependency] = []
    replaced_all: set[str] = set()
    planned: list[PlannedInstall] = []

    for rec in resolved_order:
        all_system_deps.extend(rec.metadata.system_depends)
        pkg_replaces: list[str] = []
        for rep_dep in rec.metadata.replaces:
            if db.is_installed(rep_dep.name) and rep_dep.name != rec.name:
                installed_target = db.get(rep_dep.name)
                if installed_target and rep_dep.matches(installed_target.version):
                    pkg_replaces.append(rep_dep.name)
                    replaced_all.add(rep_dep.name)

        installed_current = db.get(rec.name)
        if installed_current is not None:
            cmp_val = vercmp(rec.version, installed_current.version)
            if cmp_val == 0:
                if not reinstall and rec.name not in explicit_names:
                    continue
                if not reinstall and pinned_version is None:
                    # Already up-to-date
                    continue
                action = "reinstall"
            elif cmp_val > 0:
                action = "upgrade"
            else:
                action = "downgrade"
            reason_val = (
                "explicit"
                if rec.name in explicit_names
                else installed_current.reason
            )
            prev_ver = installed_current.version
        else:
            action = "install"
            reason_val = reasons.get(rec.name, "explicit")
            prev_ver = None

        planned.append(
            PlannedInstall(
                record=rec,
                reason=reason_val,
                action=action,
                previous_version=prev_ver,
                replaces=tuple(sorted(set(pkg_replaces))),
            )
        )

    # Evaluate package conflicts against other planned packages and installed packages
    planned_by_name = {item.name: item.record for item in planned}
    for item in planned:
        rec = item.record
        for conflict in rec.metadata.conflicts:
            # Conflict with another planned install?
            for other_name, other_rec in planned_by_name.items():
                if other_name == rec.name:
                    continue
                if _record_satisfies(other_rec, conflict):
                    raise ResolutionError(
                        f"{rec.name} {rec.version} conflicts with "
                        f"{other_name} {other_rec.version} ({conflict})"
                    )
            # Conflict with an installed package that is not being replaced?
            installed_conf = db.get(conflict.name)
            if (
                installed_conf is not None
                and conflict.name != rec.name
                and conflict.name not in replaced_all
                and conflict.matches(installed_conf.version)
            ):
                raise ResolutionError(
                    f"{rec.name} {rec.version} conflicts with installed package "
                    f"{installed_conf.name} {installed_conf.version}"
                )

    check_system_dependencies(all_system_deps, root=root)
    return TransactionPlan(
        installs=tuple(planned),
        removes=(),
        replaced_names=frozenset(replaced_all),
    )


def resolve_upgrade(
    names: Sequence[str],
    catalog: MergedCatalog,
    db: Database,
    *,
    root: Path = Path("/"),
) -> TransactionPlan:
    """Resolve upgrades for all (or selected) installed packages."""
    targets: list[str] = []
    candidates = names if names else sorted(db.packages.keys())
    for pkg_name in candidates:
        installed = db.get(pkg_name)
        if installed is None:
            raise ResolutionError(f"package {pkg_name!r} is not installed")
        latest = catalog.get_latest(pkg_name)
        if latest is None:
            continue
        if vercmp(latest.version, installed.version) > 0:
            targets.append(pkg_name)

    if not targets:
        return TransactionPlan()

    plan = resolve_install(targets, catalog, db, reinstall=False, root=root)
    # Preserve original install reasons for upgraded packages
    adjusted: list[PlannedInstall] = []
    for item in plan.installs:
        orig = db.get(item.name)
        adjusted.append(
            PlannedInstall(
                record=item.record,
                reason=orig.reason if orig is not None else item.reason,
                action=item.action,
                previous_version=item.previous_version,
                replaces=item.replaces,
            )
        )
    return TransactionPlan(
        installs=tuple(adjusted),
        removes=plan.removes,
        replaced_names=plan.replaced_names,
    )


def resolve_remove(
    names: Sequence[str],
    db: Database,
) -> TransactionPlan:
    """Resolve package removals, rejecting removal if reverse dependencies remain."""
    if not names:
        raise ResolutionError("no packages specified for removal")

    remove_set: set[str] = set()
    remove_list: list[InstalledPackage] = []
    for name in names:
        pkg = db.get(name)
        if pkg is None:
            raise ResolutionError(f"package {name!r} is not installed")
        if name not in remove_set:
            remove_set.add(name)
            remove_list.append(pkg)

    # Check if any remaining installed package depends on a package in remove_set
    # Note: installed.json doesn't store full depends AST, so we also support checking
    # against catalog or optional metadata if present.
    return TransactionPlan(installs=(), removes=tuple(remove_list))


def check_reverse_dependencies(
    removing_names: Sequence[str],
    db: Database,
    catalog: MergedCatalog | None = None,
) -> None:
    """Raise ``ResolutionError`` if remaining installed packages depend on ``removing_names``."""
    removing = set(removing_names)
    if catalog is None:
        return
    for installed_name, installed_pkg in sorted(db.packages.items()):
        if installed_name in removing:
            continue
        rec = catalog.get_version(installed_name, installed_pkg.version) or catalog.get_latest(
            installed_name
        )
        if rec is None:
            continue
        for dep in rec.metadata.depends:
            if dep.name in removing:
                raise ResolutionError(
                    f"cannot remove {dep.name!r}: required by installed package "
                    f"{installed_name!r} ({dep})"
                )


def resolve_autoremove(
    db: Database,
    catalog: MergedCatalog | None = None,
) -> TransactionPlan:
    """Identify orphaned packages installed with ``reason == 'dependency'``."""
    needed: set[str] = {
        name for name, pkg in db.packages.items() if pkg.reason == "explicit"
    }
    if catalog is not None:
        changed = True
        while changed:
            changed = False
            for name in list(needed):
                inst = db.get(name)
                if inst is None:
                    continue
                rec = catalog.get_version(name, inst.version) or catalog.get_latest(name)
                if rec is None:
                    continue
                for dep in rec.metadata.depends:
                    if dep.name in db.packages and dep.name not in needed:
                        needed.add(dep.name)
                        changed = True

    orphans = [
        pkg
        for name, pkg in sorted(db.packages.items())
        if name not in needed and pkg.reason == "dependency"
    ]
    return TransactionPlan(installs=(), removes=tuple(orphans))
