"""Root-aware paths and configuration loading (Phase 1)."""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core.models import NAME_RE, validate_arch
from .errors import UsageError, ValidationError
from .util import normalize_prefixes

SIGLEVELS = ("required", "optional", "none")

DEFAULT_ALLOWED_PREFIXES = (
    "usr/local/",
    "usr/lib/qt6/qml/ArchTitan/",
    "usr/lib/archtitan/",
    "usr/lib/libTitanUI.so/",
    "usr/include/archtitan/",
    "usr/share/archtitan/",
    "usr/share/applications/",
    "usr/share/icons/hicolor/",
    "etc/archtitan/",
    "etc/titanpkg/",
    "etc/titan-hwm/",
    "etc/titan-sandbox/",
    "etc/skel/",
    "etc/systemd/system/",
    "var/lib/archtitan/",
    "opt/archtitan/",
)

DEFAULT_PROTECTED_PACKAGES = (
    "titan-hwm",
    "titanpkg",
    "archtitan-guard",
    "titan-ui",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "db": "/var/lib/titanpkg",
    "cache": "/var/cache/titanpkg",
    "config_dir": "/etc/titanpkg",
    "repos_dir": "/etc/titanpkg/repos.d",
    "keyring": "/etc/titanpkg/trusted.gpg",
    "siglevel": "required",
    "timeout": 30,
    "arch": "x86_64",
    "allowed_prefixes": list(DEFAULT_ALLOWED_PREFIXES),
    "protected_packages": list(DEFAULT_PROTECTED_PACKAGES),
    "max_index_age_days": 0,
}

CONFIG_KEYS = frozenset(DEFAULT_CONFIG)
_ENV_OVERRIDES = {
    "TITANPKG_DB": "db",
    "TITANPKG_CACHE": "cache",
    "TITANPKG_CONFIG_DIR": "config_dir",
    "TITANPKG_REPOS_DIR": "repos_dir",
    "TITANPKG_KEYRING": "keyring",
    "TITANPKG_SIGLEVEL": "siglevel",
    "TITANPKG_TIMEOUT": "timeout",
    "TITANPKG_ARCH": "arch",
    "TITANPKG_ALLOWED_PREFIXES": "allowed_prefixes",
    "TITANPKG_PROTECTED_PACKAGES": "protected_packages",
    "TITANPKG_MAX_INDEX_AGE_DAYS": "max_index_age_days",
}

FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}([0-9A-Fa-f]{24})?$")


def _normalize_root(root: str | os.PathLike[str]) -> Path:
    path = Path(root).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(str(path)))


def _normalize_logical_path(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{what} must be a non-empty path")
    if "\x00" in value:
        raise ValidationError(f"{what} contains a NUL byte")
    normalized = posixpath.normpath(value)
    if normalized in ("", ".") or normalized == "/":
        raise ValidationError(f"{what} cannot be the root directory")
    if normalized == ".." or normalized.startswith("../"):
        raise ValidationError(f"{what} escapes the root: {value!r}")
    return normalized


def _map_into_root(root: Path, logical: str) -> Path:
    relative = logical.lstrip("/")
    result = root
    for part in relative.split("/"):
        if part in ("", ".", ".."):
            raise ValidationError(f"invalid path component in {logical!r}")
        result = result / part
    return result


@dataclass(frozen=True)
class Paths:
    """All manager locations mapped below a (possibly non-``/``) root."""

    root: Path
    db_dir: Path
    cache_dir: Path
    config_dir: Path
    repos_dir: Path
    keyring_path: Path

    @classmethod
    def from_root(
        cls,
        root: str | os.PathLike[str] = "/",
        *,
        db: str = "/var/lib/titanpkg",
        cache: str = "/var/cache/titanpkg",
        config_dir: str = "/etc/titanpkg",
        repos_dir: str = "/etc/titanpkg/repos.d",
        keyring: str = "/etc/titanpkg/trusted.gpg",
    ) -> Paths:
        root_path = _normalize_root(root)
        logical = {
            "database": _normalize_logical_path(db, what="database path"),
            "cache": _normalize_logical_path(cache, what="cache path"),
            "config": _normalize_logical_path(config_dir, what="config path"),
            "repositories": _normalize_logical_path(repos_dir, what="repositories path"),
            "keyring": _normalize_logical_path(keyring, what="keyring path"),
        }
        return cls(
            root=root_path,
            db_dir=_map_into_root(root_path, logical["database"]),
            cache_dir=_map_into_root(root_path, logical["cache"]),
            config_dir=_map_into_root(root_path, logical["config"]),
            repos_dir=_map_into_root(root_path, logical["repositories"]),
            keyring_path=_map_into_root(root_path, logical["keyring"]),
        )

    def map(self, logical: str | os.PathLike[str]) -> Path:
        """Map a root-absolute or root-relative logical path below ``root``."""
        value = os.fspath(logical)
        normalized = _normalize_logical_path(value, what="path")
        return _map_into_root(self.root, normalized)

    @property
    def lock_file(self) -> Path:
        return self.db_dir / "lock"

    @property
    def installed_db_path(self) -> Path:
        return self.db_dir / "installed.json"

    @property
    def file_owners_path(self) -> Path:
        return self.db_dir / "files.json"

    @property
    def transactions_dir(self) -> Path:
        return self.db_dir / "transactions"

    @property
    def package_cache_dir(self) -> Path:
        return self.cache_dir / "packages"

    @property
    def index_cache_dir(self) -> Path:
        return self.cache_dir / "index"

    @property
    def default_config_file(self) -> Path:
        return self.config_dir / "titanpkg.json"


@dataclass(frozen=True)
class Config:
    paths: Paths
    siglevel: str = "required"
    timeout: float = 30.0
    allowed_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_PREFIXES
    protected_packages: tuple[str, ...] = DEFAULT_PROTECTED_PACKAGES
    max_index_age_days: int = 0
    arch: str = "x86_64"
    source: Path | None = None

    def __post_init__(self) -> None:
        if self.siglevel not in SIGLEVELS:
            raise ValidationError(f"invalid siglevel: {self.siglevel!r}")
        if self.timeout <= 0:
            raise ValidationError("timeout must be greater than zero")
        if not self.allowed_prefixes:
            raise ValidationError("allowed_prefixes cannot be empty")
        for pkg_name in self.protected_packages:
            if not isinstance(pkg_name, str) or not NAME_RE.match(pkg_name):
                raise ValidationError(f"invalid protected package name: {pkg_name!r}")
        if isinstance(self.max_index_age_days, bool) or not isinstance(
            self.max_index_age_days, int
        ) or self.max_index_age_days < 0:
            raise ValidationError("max_index_age_days must be a non-negative integer")
        validate_arch(self.arch)


def _read_json_object(path: Path, *, what: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as exc:
        raise UsageError(f"cannot read {what} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON in {what} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{what} must be a JSON object: {path}")
    return value


def _coerce_prefixes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValidationError("allowed_prefixes must be a list of strings")
    try:
        normalized = normalize_prefixes(value)
    except Exception as exc:
        raise ValidationError(str(exc)) from exc
    return tuple(dict.fromkeys(normalized))


def _coerce_protected_packages(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValidationError("protected_packages must be a list of package names")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not NAME_RE.match(item):
            raise ValidationError(f"invalid protected package name: {item!r}")
        if item not in out:
            out.append(item)
    return tuple(out)


def _coerce_max_age_days(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("max_index_age_days must be an integer")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid max_index_age_days: {value!r}") from exc
    if days < 0:
        raise ValidationError("max_index_age_days must be >= 0")
    return days


def _coerce_fingerprints(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValidationError("trusted_fingerprints must be a list of hex fingerprints")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("fingerprint must be a string")
        cleaned = item.replace(" ", "").upper()
        if not FINGERPRINT_RE.match(cleaned):
            raise ValidationError(
                f"invalid OpenPGP fingerprint (expected 40 or 64 hex chars): {item!r}"
            )
        if cleaned not in out:
            out.append(cleaned)
    return tuple(out)


def _coerce_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValidationError("timeout must be a number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid timeout: {value!r}") from exc
    if timeout <= 0:
        raise ValidationError("timeout must be greater than zero")
    return timeout


def load_config(
    root: str | os.PathLike[str] | None = None,
    config_file: str | os.PathLike[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Load defaults, then the config file, environment and explicit overrides."""
    env = os.environ if environ is None else environ
    root_value = root if root is not None else env.get("TITANPKG_ROOT", "/")

    explicit_config = config_file is not None or env.get("TITANPKG_CONFIG")
    if config_file is not None:
        config_path = Path(config_file).expanduser()
    elif env.get("TITANPKG_CONFIG"):
        config_path = Path(env["TITANPKG_CONFIG"]).expanduser()
    else:
        config_path = Paths.from_root(root_value).default_config_file
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path

    values = dict(DEFAULT_CONFIG)
    source: Path | None = None
    if config_path.exists():
        loaded = _read_json_object(config_path, what="config")
        unknown = set(loaded) - CONFIG_KEYS
        if unknown:
            raise ValidationError(
                f"unknown config keys: {', '.join(sorted(unknown))}"
            )
        values.update(loaded)
        source = config_path
    elif explicit_config:
        raise UsageError(f"config file not found: {config_path}")

    for env_name, key in _ENV_OVERRIDES.items():
        if env_name in env:
            values[key] = env[env_name]
    if overrides:
        unknown = set(overrides) - CONFIG_KEYS
        if unknown:
            raise ValidationError(
                f"unknown config overrides: {', '.join(sorted(unknown))}"
            )
        values.update(overrides)

    siglevel = str(values["siglevel"]).lower()
    if siglevel not in SIGLEVELS:
        raise ValidationError(f"invalid siglevel: {values['siglevel']!r}")

    paths = Paths.from_root(
        root_value,
        db=values["db"],
        cache=values["cache"],
        config_dir=values["config_dir"],
        repos_dir=values["repos_dir"],
        keyring=values["keyring"],
    )
    return Config(
        paths=paths,
        siglevel=siglevel,
        timeout=_coerce_timeout(values["timeout"]),
        allowed_prefixes=_coerce_prefixes(values["allowed_prefixes"]),
        protected_packages=_coerce_protected_packages(values["protected_packages"]),
        max_index_age_days=_coerce_max_age_days(values["max_index_age_days"]),
        arch=validate_arch(str(values["arch"])),
        source=source,
    )


REPO_KEYS = frozenset(
    {
        "name",
        "server",
        "servers",
        "enabled",
        "siglevel",
        "trusted_fingerprints",
        "max_index_age_days",
        "allow_insecure_http",
    }
)


def _validate_server(value: Any, *, allow_insecure_http: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("repository server must be a non-empty URL")
    if "\x00" in value:
        raise ValidationError("repository server contains a NUL byte")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https", "file"):
        raise ValidationError(f"unsupported repository scheme: {parsed.scheme or '<none>'}")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ValidationError(
            f"insecure http:// repository URL rejected ({value!r}); "
            "use https:// or set allow_insecure_http=true"
        )
    if parsed.scheme in ("http", "https") and not parsed.netloc:
        raise ValidationError(f"repository URL has no host: {value!r}")
    if parsed.scheme == "file" and not parsed.path:
        raise ValidationError(f"repository file URL has no path: {value!r}")
    if parsed.scheme == "file" and parsed.netloc not in ("", "localhost"):
        raise ValidationError(f"remote file URLs are not supported: {value!r}")
    return value.rstrip("/") if value.rstrip("/") else value


@dataclass(frozen=True)
class RepoConfig:
    name: str
    servers: tuple[str, ...]
    enabled: bool = True
    siglevel: str = "required"
    trusted_fingerprints: tuple[str, ...] = ()
    max_index_age_days: int = 0
    allow_insecure_http: bool = False
    source: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise ValidationError(f"invalid repository name: {self.name!r}")
        if not self.servers:
            raise ValidationError(f"repository {self.name!r} has no servers")
        if self.siglevel not in SIGLEVELS:
            raise ValidationError(f"invalid siglevel for {self.name!r}: {self.siglevel!r}")
        object.__setattr__(
            self,
            "trusted_fingerprints",
            _coerce_fingerprints(list(self.trusted_fingerprints)),
        )
        if self.max_index_age_days < 0:
            raise ValidationError("max_index_age_days must be >= 0")
        object.__setattr__(
            self,
            "servers",
            tuple(
                dict.fromkeys(
                    _validate_server(s, allow_insecure_http=self.allow_insecure_http)
                    for s in self.servers
                )
            ),
        )

    @property
    def server(self) -> str:
        return self.servers[0]

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        default_siglevel: str = "required",
        default_max_age_days: int = 0,
        source: Path | None = None,
    ) -> RepoConfig:
        if not isinstance(value, Mapping):
            raise ValidationError("repository config must be an object")
        unknown = set(value) - REPO_KEYS
        if unknown:
            raise ValidationError(
                f"unknown repository keys: {', '.join(sorted(unknown))}"
            )
        if "name" not in value:
            raise ValidationError("repository config needs a name")

        servers: list[str] = []
        if "server" in value:
            servers.append(value["server"])
        raw_servers = value.get("servers", [])
        if isinstance(raw_servers, str):
            raw_servers = [raw_servers]
        if not isinstance(raw_servers, (list, tuple)):
            raise ValidationError("repository servers must be a list")
        servers.extend(raw_servers)

        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValidationError("repository enabled must be boolean")

        allow_http = value.get("allow_insecure_http", False)
        if not isinstance(allow_http, bool):
            raise ValidationError("repository allow_insecure_http must be boolean")

        return cls(
            name=value["name"],
            servers=tuple(servers),
            enabled=enabled,
            siglevel=str(value.get("siglevel", default_siglevel)).lower(),
            trusted_fingerprints=_coerce_fingerprints(
                value.get("trusted_fingerprints")
            ),
            max_index_age_days=_coerce_max_age_days(
                value.get("max_index_age_days", default_max_age_days)
            ),
            allow_insecure_http=allow_http,
            source=source,
        )


def load_repos(
    paths: Paths, config: Config | None = None
) -> tuple[RepoConfig, ...]:
    """Load ``repos.d/*.json`` in lexicographic order."""
    directory = paths.repos_dir
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise UsageError(f"repository directory is not a directory: {directory}")
    default_siglevel = config.siglevel if config is not None else "required"
    default_max_age = config.max_index_age_days if config is not None else 0
    repos: list[RepoConfig] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        if path.name.startswith("."):
            continue
        repo = RepoConfig.from_dict(
            _read_json_object(path, what="repository config"),
            default_siglevel=default_siglevel,
            default_max_age_days=default_max_age,
            source=path,
        )
        if repo.name in seen:
            raise ValidationError(f"duplicate repository name: {repo.name}")
        seen.add(repo.name)
        repos.append(repo)
    return tuple(repos)


def enabled_repos(repos: Sequence[RepoConfig]) -> tuple[RepoConfig, ...]:
    return tuple(repo for repo in repos if repo.enabled)
