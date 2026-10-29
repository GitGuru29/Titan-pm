"""Typed models for packages, files, dependencies and installed state."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationError
from ..util import safe_relpath
from ..vercmp import vercmp

NAME_RE = re.compile(r"^[A-Za-z0-9@_+][A-Za-z0-9@._+-]*$")
ARCH_RE = re.compile(r"^[A-Za-z0-9_]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODE_RE = re.compile(r"^[0-7]{3,4}$")

FILE_TYPES = ("file", "symlink")
REASONS = ("explicit", "dependency")
DEPENDENCY_OPERATORS = (">=", "<=", "=", ">", "<")


def validate_name(value: str, *, what: str = "package name") -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ValidationError(f"invalid {what}: {value!r}")
    return value


def validate_version(value: str, *, what: str = "version") -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"empty {what}")
    if value != value.strip() or any(char.isspace() for char in value):
        raise ValidationError(f"invalid {what}: {value!r}")
    if "\x00" in value or "/" in value:
        raise ValidationError(f"invalid {what}: {value!r}")
    return value


def validate_arch(value: str) -> str:
    if not isinstance(value, str) or not ARCH_RE.match(value):
        raise ValidationError(f"invalid architecture: {value!r}")
    return value


def _string_list(value: Any, *, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{what} must be a string or list of strings")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(f"{what} contains a non-string value")
        result.append(item)
    return tuple(result)


def _parse_dependencies(value: Any, *, what: str) -> tuple[Dependency, ...]:
    return tuple(Dependency.parse(item) for item in _string_list(value, what=what))


@dataclass(frozen=True)
class Dependency:
    """A pacman-style ``name[op version]`` requirement or provision."""

    name: str
    op: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        validate_name(self.name, what="dependency name")
        if self.op is not None and self.op not in DEPENDENCY_OPERATORS:
            raise ValidationError(f"invalid dependency operator: {self.op!r}")
        if self.op is None and self.version is not None:
            raise ValidationError("dependency version requires an operator")
        if self.op is not None:
            if self.version is None:
                raise ValidationError("dependency operator requires a version")
            validate_version(self.version, what="dependency version")

    @classmethod
    def parse(cls, value: str) -> Dependency:
        if not isinstance(value, str):
            raise ValidationError(f"dependency must be a string: {value!r}")
        text = value.strip()
        if not text:
            raise ValidationError("empty dependency")
        for operator in DEPENDENCY_OPERATORS:
            index = text.find(operator)
            if index != -1:
                return cls(
                    name=text[:index].strip(),
                    op=operator,
                    version=text[index + len(operator) :].strip(),
                )
        return cls(name=text)

    @property
    def is_versioned(self) -> bool:
        return self.op is not None

    def matches(self, version: str | None) -> bool:
        """Whether a concrete version satisfies this requirement."""
        if self.op is None:
            return True
        if version is None:
            return False
        comparison = vercmp(version, self.version or "")
        if self.op == ">=":
            return comparison >= 0
        if self.op == "<=":
            return comparison <= 0
        if self.op == ">":
            return comparison > 0
        if self.op == "<":
            return comparison < 0
        return comparison == 0

    def satisfied_by(self, provided: Dependency) -> bool:
        """Whether a provide/conflict entry satisfies this requirement."""
        if self.name != provided.name:
            return False
        if self.op is None:
            return True
        if provided.version is None:
            return False
        return self.matches(provided.version)

    def __str__(self) -> str:
        if self.op is None:
            return self.name
        return f"{self.name}{self.op}{self.version}"


@dataclass(frozen=True)
class FileEntry:
    """One regular file or symlink inside a package payload."""

    path: str
    type: str
    mode: str = "0644"
    sha256: str | None = None
    target: str | None = None

    def __post_init__(self) -> None:
        safe_relpath(self.path)
        if self.type not in FILE_TYPES:
            raise ValidationError(f"invalid file type: {self.type!r}")

        mode = self.mode
        if isinstance(mode, int):
            mode = f"{mode & 0o7777:04o}"
        if not isinstance(mode, str) or not MODE_RE.match(mode):
            raise ValidationError(f"invalid mode for {self.path}: {mode!r}")
        object.__setattr__(self, "mode", mode.zfill(4))

        if self.type == "file":
            if self.target is not None:
                raise ValidationError(f"regular file {self.path} cannot have a target")
            if not self.sha256 or not SHA256_RE.match(self.sha256.lower()):
                raise ValidationError(f"regular file {self.path} needs a SHA-256")
            object.__setattr__(self, "sha256", self.sha256.lower())
        else:
            if self.sha256 is not None:
                raise ValidationError(f"symlink {self.path} cannot have a SHA-256")
            if not self.target:
                raise ValidationError(f"symlink {self.path} needs a target")
            if "\x00" in self.target:
                raise ValidationError(f"NUL byte in symlink target for {self.path}")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FileEntry:
        if not isinstance(value, Mapping):
            raise ValidationError("file entry must be an object")
        unknown = set(value) - {"path", "type", "mode", "sha256", "target"}
        if unknown:
            raise ValidationError(f"unknown file entry fields: {', '.join(sorted(unknown))}")
        try:
            path = value["path"]
            file_type = value["type"]
        except KeyError as exc:
            raise ValidationError(f"missing file entry field: {exc.args[0]}") from exc
        default_mode = "0777" if file_type == "symlink" else "0644"
        return cls(
            path=path,
            type=file_type,
            mode=value.get("mode", default_mode),
            sha256=value.get("sha256"),
            target=value.get("target"),
        )

    def to_dict(self) -> dict[str, Any]:
        if self.type == "symlink":
            return {
                "path": self.path,
                "type": self.type,
                "mode": self.mode,
                "target": self.target,
            }
        return {
            "path": self.path,
            "type": self.type,
            "mode": self.mode,
            "sha256": self.sha256,
        }


METADATA_FIELDS = {
    "format",
    "name",
    "version",
    "arch",
    "description",
    "url",
    "license",
    "depends",
    "provides",
    "conflicts",
    "replaces",
    "system_depends",
    "backup",
    "setuid",
    "installed_size",
    "build_date",
    "files_count",
}


@dataclass(frozen=True)
class PackageMetadata:
    """Contents of a package's ``METADATA.json``."""

    name: str
    version: str
    arch: str
    format: int = 1
    description: str = ""
    url: str = ""
    license: tuple[str, ...] = ()
    depends: tuple[Dependency, ...] = ()
    provides: tuple[Dependency, ...] = ()
    conflicts: tuple[Dependency, ...] = ()
    replaces: tuple[Dependency, ...] = ()
    system_depends: tuple[Dependency, ...] = ()
    backup: tuple[str, ...] = ()
    setuid: tuple[str, ...] = ()
    installed_size: int = 0
    build_date: str = ""
    files_count: int = 0

    def __post_init__(self) -> None:
        validate_name(self.name)
        validate_version(self.version)
        validate_arch(self.arch)
        if self.format != 1:
            raise ValidationError(f"unsupported metadata format: {self.format!r}")
        if not isinstance(self.description, str):
            raise ValidationError("description must be a string")
        if not isinstance(self.url, str):
            raise ValidationError("url must be a string")

        for attribute in (
            "license",
            "depends",
            "provides",
            "conflicts",
            "replaces",
            "system_depends",
            "backup",
            "setuid",
        ):
            value = getattr(self, attribute)
            object.__setattr__(self, attribute, tuple(value))

        for path in self.backup:
            safe_relpath(path)
        for path in self.setuid:
            safe_relpath(path)
        if len(set(self.backup)) != len(self.backup):
            raise ValidationError("duplicate backup path")
        if len(set(self.setuid)) != len(self.setuid):
            raise ValidationError("duplicate setuid path")
        if not isinstance(self.installed_size, int) or self.installed_size < 0:
            raise ValidationError("installed_size must be a non-negative integer")
        if not isinstance(self.files_count, int) or self.files_count < 0:
            raise ValidationError("files_count must be a non-negative integer")
        if not isinstance(self.build_date, str):
            raise ValidationError("build_date must be a string")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PackageMetadata:
        if not isinstance(value, Mapping):
            raise ValidationError("metadata must be an object")
        unknown = set(value) - METADATA_FIELDS
        if unknown:
            raise ValidationError(f"unknown metadata fields: {', '.join(sorted(unknown))}")
        for required in ("name", "version", "arch"):
            if required not in value:
                raise ValidationError(f"missing metadata field: {required}")

        license_value = _string_list(value.get("license"), what="license")
        return cls(
            format=value.get("format", 1),
            name=value["name"],
            version=value["version"],
            arch=value["arch"],
            description=value.get("description", ""),
            url=value.get("url", ""),
            license=license_value,
            depends=_parse_dependencies(value.get("depends"), what="depends"),
            provides=_parse_dependencies(value.get("provides"), what="provides"),
            conflicts=_parse_dependencies(value.get("conflicts"), what="conflicts"),
            replaces=_parse_dependencies(value.get("replaces"), what="replaces"),
            system_depends=_parse_dependencies(
                value.get("system_depends"), what="system_depends"
            ),
            backup=_string_list(value.get("backup"), what="backup"),
            setuid=_string_list(value.get("setuid"), what="setuid"),
            installed_size=value.get("installed_size", 0),
            build_date=value.get("build_date", ""),
            files_count=value.get("files_count", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "name": self.name,
            "version": self.version,
            "arch": self.arch,
            "description": self.description,
            "url": self.url,
            "license": list(self.license),
            "depends": [str(item) for item in self.depends],
            "provides": [str(item) for item in self.provides],
            "conflicts": [str(item) for item in self.conflicts],
            "replaces": [str(item) for item in self.replaces],
            "system_depends": [str(item) for item in self.system_depends],
            "backup": list(self.backup),
            "setuid": list(self.setuid),
            "installed_size": self.installed_size,
            "build_date": self.build_date,
            "files_count": self.files_count,
        }


Metadata = PackageMetadata


@dataclass(frozen=True)
class AvailablePackage:
    """A package advertised by a repository index."""

    metadata: PackageMetadata
    repo: str
    filename: str
    sha256: str
    size: int = 0

    def __post_init__(self) -> None:
        if not self.filename or "/" in self.filename or "\x00" in self.filename:
            raise ValidationError(f"invalid package filename: {self.filename!r}")
        if not SHA256_RE.match(self.sha256.lower()):
            raise ValidationError(f"invalid package SHA-256: {self.sha256!r}")
        if not isinstance(self.size, int) or self.size < 0:
            raise ValidationError("package size must be a non-negative integer")
        object.__setattr__(self, "sha256", self.sha256.lower())

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def version(self) -> str:
        return self.metadata.version


@dataclass(frozen=True)
class InstalledPackage:
    """One row in ``/var/lib/titanpkg/installed.json``."""

    name: str
    version: str
    arch: str
    repo: str
    reason: str = "explicit"
    installed_at: str = ""
    files: tuple[FileEntry, ...] = ()
    backup: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_name(self.name)
        validate_version(self.version)
        validate_arch(self.arch)
        if not isinstance(self.repo, str) or not self.repo:
            raise ValidationError("installed package needs a repo")
        if self.reason not in REASONS:
            raise ValidationError(f"invalid install reason: {self.reason!r}")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "backup", tuple(self.backup))
        paths = [entry.path for entry in self.files]
        if len(set(paths)) != len(paths):
            raise ValidationError(f"duplicate file paths for {self.name}")
        for path in self.backup:
            safe_relpath(path)
            if path not in paths:
                raise ValidationError(f"backup path not in package files: {path}")

    @property
    def files_by_path(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.files}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InstalledPackage:
        if not isinstance(value, Mapping):
            raise ValidationError("installed package must be an object")
        unknown = set(value) - {
            "name",
            "version",
            "arch",
            "repo",
            "reason",
            "installed_at",
            "files",
            "backup",
        }
        if unknown:
            raise ValidationError(
                f"unknown installed package fields: {', '.join(sorted(unknown))}"
            )
        for required in ("name", "version", "arch", "repo"):
            if required not in value:
                raise ValidationError(f"missing installed package field: {required}")
        files_value = value.get("files", [])
        if not isinstance(files_value, (list, tuple)):
            raise ValidationError("installed package files must be a list")
        return cls(
            name=value["name"],
            version=value["version"],
            arch=value["arch"],
            repo=value["repo"],
            reason=value.get("reason", "explicit"),
            installed_at=value.get("installed_at", ""),
            files=tuple(FileEntry.from_dict(item) for item in files_value),
            backup=_string_list(value.get("backup"), what="backup"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "arch": self.arch,
            "repo": self.repo,
            "reason": self.reason,
            "installed_at": self.installed_at,
            "files": [entry.to_dict() for entry in sorted(self.files, key=lambda e: e.path)],
            "backup": list(self.backup),
        }


@dataclass
class InstalledDatabase:
    """The on-disk collection of installed packages."""

    format: int = 1
    generated: str = ""
    packages: dict[str, InstalledPackage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.format != 1:
            raise ValidationError(f"unsupported installed database format: {self.format!r}")
        for name, package in self.packages.items():
            if name != package.name:
                raise ValidationError(f"installed database key {name!r} != package name")
        if not isinstance(self.generated, str):
            raise ValidationError("generated must be a string")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InstalledDatabase:
        if not isinstance(value, Mapping):
            raise ValidationError("installed database must be an object")
        unknown = set(value) - {"format", "generated", "packages"}
        if unknown:
            raise ValidationError(
                f"unknown installed database fields: {', '.join(sorted(unknown))}"
            )
        raw_packages = value.get("packages", {})
        if not isinstance(raw_packages, Mapping):
            raise ValidationError("installed database packages must be an object")
        packages = {
            name: InstalledPackage.from_dict(item)
            for name, item in raw_packages.items()
        }
        return cls(
            format=value.get("format", 1),
            generated=value.get("generated", ""),
            packages=packages,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "generated": self.generated,
            "packages": {
                name: package.to_dict()
                for name, package in sorted(self.packages.items())
            },
        }


def dependency_names(dependencies: Iterable[Dependency]) -> set[str]:
    return {dependency.name for dependency in dependencies}
