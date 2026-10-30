"""Installed database, atomic save and path ownership index (Phase 2).

Database location: ``/var/lib/titanpkg/installed.json``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Paths
from .core.models import (
    FileEntry,
    InstalledDatabase,
    InstalledPackage,
    validate_arch,
    validate_name,
    validate_version,
)
from .errors import DatabaseError, ValidationError
from .util import atomic_write_json, now_iso, read_json, safe_relpath


def _parse_files_field(
    raw_files: Any, *, pkg_name: str
) -> tuple[ tuple[FileEntry, ...], tuple[str, ...] ]:
    """Accept either list-of-FileEntry dicts or the path-keyed mapping format."""
    if isinstance(raw_files, (list, tuple)):
        entries = tuple(FileEntry.from_dict(item) for item in raw_files)
        return entries, ()

    if isinstance(raw_files, Mapping):
        entries_list: list[FileEntry] = []
        inferred_backups: list[str] = []
        for path, meta in raw_files.items():
            safe_relpath(path)
            if not isinstance(meta, Mapping):
                raise ValidationError(
                    f"invalid file record for {path!r} in package {pkg_name!r}"
                )
            file_type = meta.get("type", "file")
            entry = FileEntry(
                path=path,
                type=file_type,
                mode=meta.get("mode", "0777" if file_type == "symlink" else "0644"),
                sha256=meta.get("sha256"),
                target=meta.get("target"),
            )
            entries_list.append(entry)
            if meta.get("backup") is True:
                inferred_backups.append(path)
        return tuple(entries_list), tuple(inferred_backups)

    raise ValidationError(f"installed package files for {pkg_name!r} must be a list or object")


def _parse_installed_package(
    key_name: str, value: Any, *, default_arch: str = "x86_64"
) -> InstalledPackage:
    if not isinstance(value, Mapping):
        raise ValidationError(f"installed package {key_name!r} must be an object")

    name = value.get("name", key_name)
    if name != key_name:
        raise ValidationError(
            f"installed database key {key_name!r} does not match package name {name!r}"
        )
    validate_name(name)

    if "version" not in value or "repo" not in value:
        raise ValidationError(
            f"installed package {name!r} missing required version or repo field"
        )
    version = validate_version(value["version"])
    arch = validate_arch(value.get("arch", default_arch))
    repo = value["repo"]
    reason = value.get("reason", "explicit")
    installed_at = value.get("installed_at", "")

    entries, inferred_backups = _parse_files_field(
        value.get("files", ()), pkg_name=name
    )
    raw_backup = value.get("backup")
    if raw_backup is None:
        backups = inferred_backups
    elif isinstance(raw_backup, (list, tuple)):
        backups = tuple(str(item) for item in raw_backup)
    else:
        raise ValidationError(f"backup list for {name!r} must be an array")

    return InstalledPackage(
        name=name,
        version=version,
        arch=arch,
        repo=repo,
        reason=reason,
        installed_at=installed_at,
        files=entries,
        backup=backups,
    )


@dataclass
class Database:
    """In-memory representation of ``installed.json`` with reverse path index."""

    format: int = 1
    generated: str = ""
    packages: dict[str, InstalledPackage] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict)
    _owners: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.format != 1:
            raise DatabaseError(f"unsupported installed database format: {self.format!r}")
        if not isinstance(self.generated, str):
            raise DatabaseError("installed database 'generated' field must be a string")
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        owners: dict[str, str] = {}
        for pkg_name, pkg in sorted(self.packages.items()):
            if pkg_name != pkg.name:
                raise DatabaseError(
                    f"database key {pkg_name!r} does not match package name {pkg.name!r}"
                )
            for entry in pkg.files:
                existing = owners.get(entry.path)
                if existing is not None and existing != pkg_name:
                    raise DatabaseError(
                        f"corrupt database: path {entry.path!r} claimed by both "
                        f"{existing!r} and {pkg_name!r}"
                    )
                owners[entry.path] = pkg_name
        self._owners = owners

    @property
    def owners(self) -> Mapping[str, str]:
        """Read-only view mapping ``relpath -> package_name``."""
        return self._owners

    def get(self, name: str) -> InstalledPackage | None:
        return self.packages.get(name)

    def is_installed(self, name: str) -> bool:
        return name in self.packages

    def owner_of(self, relpath: str) -> str | None:
        normalized = safe_relpath(relpath)
        return self._owners.get(normalized)

    def find_conflicts(
        self,
        package_name: str,
        file_paths: Iterable[str],
        *,
        replacing: Iterable[str] = (),
    ) -> dict[str, str]:
        """Return ``{path: owner}`` for paths owned by other non-replaced packages."""
        ignored = {package_name, *replacing}
        conflicts: dict[str, str] = {}
        for raw_path in file_paths:
            normalized = safe_relpath(raw_path)
            owner = self._owners.get(normalized)
            if owner is not None and owner not in ignored:
                conflicts[normalized] = owner
        return conflicts

    def put(
        self,
        package: InstalledPackage,
        *,
        replacing: Iterable[str] = (),
    ) -> None:
        """Insert or replace a package after checking for file ownership collisions."""
        conflicts = self.find_conflicts(
            package.name,
            (entry.path for entry in package.files),
            replacing=replacing,
        )
        if conflicts:
            sample_path, owner = next(iter(conflicts.items()))
            raise DatabaseError(
                f"file conflict: {sample_path!r} is already owned by {owner!r}"
            )
        for old_name in replacing:
            if old_name != package.name:
                self.packages.pop(old_name, None)
        self.packages[package.name] = package
        self._rebuild_index()

    def remove(self, name: str) -> InstalledPackage | None:
        removed = self.packages.pop(name, None)
        if removed is not None:
            self._rebuild_index()
        return removed

    def to_installed_database(self) -> InstalledDatabase:
        return InstalledDatabase(
            format=self.format,
            generated=self.generated,
            packages=dict(self.packages),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extra_fields)
        payload["format"] = self.format
        payload["generated"] = self.generated or now_iso()
        payload["packages"] = {
            name: pkg.to_dict() for name, pkg in sorted(self.packages.items())
        }
        return payload

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, default_arch: str = "x86_64"
    ) -> Database:
        if not isinstance(value, Mapping):
            raise DatabaseError("installed database must be a JSON object")
        fmt = value.get("format", 1)
        if fmt != 1:
            raise DatabaseError(f"unsupported installed database format: {fmt!r}")
        generated = value.get("generated", "")
        if not isinstance(generated, str):
            raise DatabaseError("installed database 'generated' field must be a string")

        raw_packages = value.get("packages", {})
        if not isinstance(raw_packages, Mapping):
            raise DatabaseError("installed database 'packages' field must be an object")

        try:
            packages = {
                name: _parse_installed_package(
                    name, pkg_data, default_arch=default_arch
                )
                for name, pkg_data in raw_packages.items()
            }
        except ValidationError as exc:
            raise DatabaseError(f"corrupt installed database: {exc}") from exc

        extra = {
            k: v
            for k, v in value.items()
            if k not in ("format", "generated", "packages")
        }
        return cls(
            format=fmt,
            generated=generated,
            packages=packages,
            extra_fields=extra,
        )


def load_db(paths: Paths, *, default_arch: str = "x86_64") -> Database:
    """Load ``/var/lib/titanpkg/installed.json`` or return an empty database."""
    db_path: Path = paths.installed_db_path
    if not db_path.exists():
        return Database(format=1, generated="")
    if not db_path.is_file():
        raise DatabaseError(f"installed database path is not a regular file: {db_path}")
    try:
        raw = read_json(db_path)
    except Exception as exc:
        raise DatabaseError(
            f"failed to parse installed database {db_path}: {exc}"
        ) from exc
    return Database.from_dict(raw, default_arch=default_arch)


def save_db(paths: Paths, db: Database) -> None:
    """Atomically write ``installed.json`` and ``files.json`` ownership index."""
    if paths.installed_db_path.exists() and not paths.installed_db_path.is_file():
        raise DatabaseError(
            f"refusing to overwrite non-file database path: {paths.installed_db_path}"
        )
    db.generated = now_iso()
    db._rebuild_index()
    atomic_write_json(paths.installed_db_path, db.to_dict(), mode=0o644)
    atomic_write_json(paths.file_owners_path, dict(sorted(db.owners.items())), mode=0o644)
