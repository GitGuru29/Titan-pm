"""Repository indexes, isolated GPG keyring, anti-replay sync and verified downloads (Phase 2)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Paths, RepoConfig
from .core.models import (
    SHA256_RE,
    AvailablePackage,
    Dependency,
    PackageMetadata,
    validate_arch,
    validate_name,
    validate_version,
)
from .errors import RepositoryError, SecurityError, ValidationError
from .util import (
    CHUNK_SIZE,
    DEFAULT_TIMEOUT,
    USER_AGENT,
    atomic_write_bytes,
    fetch_bytes,
    read_json,
    safe_relpath,
    sha256_file,
)
from .vercmp import vercmp

MAX_INDEX_BYTES = 32 * 1024 * 1024  # 32 MiB ceiling for titanpkg.db.json
MAX_SIG_BYTES = 64 * 1024  # 64 KiB ceiling for detached .sig files


# ---------------------------------------------------------------------------
# Dedicated GPG Keyring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyInfo:
    """Metadata for a trusted GPG public key in ``/etc/titanpkg/trusted.gpg``."""

    fingerprint: str
    uid: str = ""
    created: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "fingerprint": self.fingerprint,
            "uid": self.uid,
            "created": self.created,
        }


class Keyring:
    """Isolated GPG keyring manager that never touches ``~/.gnupg``."""

    def __init__(self, paths: Paths, gpg_binary: str = "gpg") -> None:
        self.paths = paths
        self.keyring_path = paths.keyring_path
        self.homedir = paths.config_dir / ".gnupg"
        self.gpg_binary = gpg_binary

    def _ensure_gpg(self) -> str:
        resolved = shutil.which(self.gpg_binary)
        if not resolved:
            raise SecurityError(
                f"GPG binary {self.gpg_binary!r} not found in PATH; cannot verify signatures"
            )
        return resolved

    def _prepare_homedir(self) -> None:
        self.keyring_path.parent.mkdir(parents=True, exist_ok=True)
        self.homedir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.homedir, 0o700)

    def _base_cmd(self) -> list[str]:
        gpg = self._ensure_gpg()
        return [
            gpg,
            "--batch",
            "--no-tty",
            "--homedir",
            str(self.homedir),
            "--no-default-keyring",
            "--keyring",
            str(self.keyring_path),
        ]

    def _clean_env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        }

    def has_keys(self) -> bool:
        if not self.keyring_path.exists() or self.keyring_path.stat().st_size == 0:
            return False
        try:
            return len(self.list_keys()) > 0
        except SecurityError:
            return False

    def import_key(self, key_file: Path) -> list[KeyInfo]:
        """Import a public key file into the dedicated keyring."""
        if not key_file.exists() or not key_file.is_file():
            raise SecurityError(f"key file not found: {key_file}")
        self._prepare_homedir()
        cmd = [*self._base_cmd(), "--import", str(key_file)]
        proc = subprocess.run(
            cmd,
            env=self._clean_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise SecurityError(f"failed to import key {key_file}: {detail}")
        if self.keyring_path.exists():
            with contextlib.suppress(OSError):
                os.chmod(self.keyring_path, 0o644)
        return self.list_keys()

    def list_keys(self) -> list[KeyInfo]:
        """Return all public keys stored in ``/etc/titanpkg/trusted.gpg``."""
        if not self.keyring_path.exists() or self.keyring_path.stat().st_size == 0:
            return []
        self._prepare_homedir()
        cmd = [*self._base_cmd(), "--with-colons", "--list-keys"]
        proc = subprocess.run(
            cmd,
            env=self._clean_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise SecurityError(f"failed to list trusted keys: {detail}")
        return _parse_gpg_colon_keys(proc.stdout)

    def verify_file(self, data_path: Path, sig_path: Path) -> str:
        """Verify ``sig_path`` against ``data_path``; return signer fingerprint."""
        if not self.keyring_path.exists() or self.keyring_path.stat().st_size == 0:
            raise SecurityError(
                f"trusted keyring is missing or empty: {self.keyring_path}"
            )
        if not data_path.exists():
            raise SecurityError(f"missing signed data file: {data_path}")
        if not sig_path.exists():
            raise SecurityError(f"missing detached signature: {sig_path}")

        self._prepare_homedir()
        cmd = [
            *self._base_cmd(),
            "--status-fd",
            "1",
            "--verify",
            str(sig_path),
            str(data_path),
        ]
        proc = subprocess.run(
            cmd,
            env=self._clean_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        goodsig = False
        fingerprint = ""
        for line in proc.stdout.splitlines():
            if line.startswith("[GNUPG:] GOODSIG "):
                goodsig = True
            elif line.startswith("[GNUPG:] VALIDSIG "):
                parts = line.split()
                if len(parts) >= 3:
                    fingerprint = parts[2]
        if proc.returncode != 0 or not goodsig or not fingerprint:
            detail = (proc.stderr or proc.stdout).strip()
            raise SecurityError(
                f"GPG signature verification failed for {data_path.name}: {detail}"
            )
        return fingerprint

    def verify_bytes(self, data: bytes, sig_bytes: bytes, *, label: str = "payload") -> str:
        """Verify detached signature bytes against ``data`` before parsing."""
        self._prepare_homedir()
        with tempfile.TemporaryDirectory(
            dir=str(self.homedir), prefix=".verify-"
        ) as tmp_dir:
            tmp_root = Path(tmp_dir)
            os.chmod(tmp_root, 0o700)
            data_path = tmp_root / label
            sig_path = tmp_root / f"{label}.sig"
            data_path.write_bytes(data)
            sig_path.write_bytes(sig_bytes)
            return self.verify_file(data_path, sig_path)


def _parse_gpg_colon_keys(output: str) -> list[KeyInfo]:
    keys: list[KeyInfo] = []
    current_created = ""
    current_fpr = ""
    current_uid = ""
    in_pub = False

    def _flush() -> None:
        nonlocal current_created, current_fpr, current_uid, in_pub
        if in_pub and current_fpr:
            keys.append(
                KeyInfo(
                    fingerprint=current_fpr,
                    uid=current_uid,
                    created=current_created,
                )
            )
        current_created = ""
        current_fpr = ""
        current_uid = ""
        in_pub = False

    for line in output.splitlines():
        fields = line.split(":")
        record_type = fields[0] if fields else ""
        if record_type == "pub":
            _flush()
            in_pub = True
            if len(fields) > 5:
                current_created = fields[5]
        elif record_type == "sub":
            in_pub = False
        elif in_pub and record_type == "fpr" and not current_fpr and len(fields) > 9:
            current_fpr = fields[9]
        elif in_pub and record_type == "uid" and not current_uid and len(fields) > 9:
            current_uid = fields[9]

    _flush()
    return keys


# ---------------------------------------------------------------------------
# Repository Index & Catalog Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoVersionRecord:
    """One version entry inside ``titanpkg.db.json``."""

    package: AvailablePackage
    file_relpath: str

    @property
    def name(self) -> str:
        return self.package.name

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def repo(self) -> str:
        return self.package.repo

    @property
    def metadata(self) -> PackageMetadata:
        return self.package.metadata


@dataclass(frozen=True)
class RepoPackageSeries:
    """All available versions of a single package in a repository or merged catalog."""

    name: str
    latest: str
    versions: Mapping[str, RepoVersionRecord]

    def get_latest(self) -> RepoVersionRecord:
        return self.versions[self.latest]

    def get_version(self, version: str) -> RepoVersionRecord | None:
        return self.versions.get(version)

    def sorted_versions(self) -> list[str]:
        """Return versions sorted oldest-to-newest using ``vercmp``."""
        from functools import cmp_to_key

        return sorted(self.versions.keys(), key=cmp_to_key(vercmp))


@dataclass(frozen=True)
class RepositoryIndex:
    """Parsed contents of a single repository's ``titanpkg.db.json``."""

    repo: str
    generated: str
    packages: Mapping[str, RepoPackageSeries]
    format: int = 1

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        expected_repo: str | None = None,
        default_arch: str = "x86_64",
    ) -> RepositoryIndex:
        if not isinstance(value, Mapping):
            raise ValidationError("repository index must be a JSON object")
        fmt = value.get("format", 1)
        if fmt != 1:
            raise ValidationError(f"unsupported repository index format: {fmt!r}")
        repo_name = value.get("repo") or expected_repo
        if not isinstance(repo_name, str) or not repo_name:
            raise ValidationError("repository index missing 'repo' name")
        validate_name(repo_name, what="repository name")
        if expected_repo is not None and repo_name != expected_repo:
            raise SecurityError(
                f"repository name mismatch: expected {expected_repo!r}, index says {repo_name!r}"
            )
        generated = value.get("generated", "")
        if not isinstance(generated, str):
            raise ValidationError("repository index 'generated' must be a string")

        raw_packages = value.get("packages", {})
        if not isinstance(raw_packages, Mapping):
            raise ValidationError("repository index 'packages' must be an object")

        parsed_packages: dict[str, RepoPackageSeries] = {}
        for pkg_name, pkg_block in raw_packages.items():
            validate_name(pkg_name)
            if not isinstance(pkg_block, Mapping):
                raise ValidationError(
                    f"repository package entry {pkg_name!r} must be an object"
                )
            raw_versions = pkg_block.get("versions", {})
            if not isinstance(raw_versions, Mapping) or not raw_versions:
                raise ValidationError(
                    f"repository package {pkg_name!r} has no versions"
                )
            version_records: dict[str, RepoVersionRecord] = {}
            for ver_str, ver_data in raw_versions.items():
                validate_version(ver_str)
                record = _parse_version_record(
                    repo_name=repo_name,
                    pkg_name=pkg_name,
                    version=ver_str,
                    value=ver_data,
                    default_arch=default_arch,
                )
                version_records[ver_str] = record

            computed_latest = _compute_latest_version(version_records.keys())
            parsed_packages[pkg_name] = RepoPackageSeries(
                name=pkg_name,
                latest=computed_latest,
                versions=version_records,
            )

        return cls(
            format=fmt,
            repo=repo_name,
            generated=generated,
            packages=parsed_packages,
        )

    def to_dict(self) -> dict[str, Any]:
        packages_out: dict[str, Any] = {}
        for pkg_name, series in sorted(self.packages.items()):
            versions_out: dict[str, Any] = {}
            for ver_str in series.sorted_versions():
                rec = series.versions[ver_str]
                meta = rec.metadata
                versions_out[ver_str] = {
                    "file": rec.file_relpath,
                    "sha256": rec.package.sha256,
                    "size": rec.package.size,
                    "arch": meta.arch,
                    "description": meta.description,
                    "url": meta.url,
                    "license": list(meta.license),
                    "depends": [str(d) for d in meta.depends],
                    "provides": [str(d) for d in meta.provides],
                    "conflicts": [str(d) for d in meta.conflicts],
                    "replaces": [str(d) for d in meta.replaces],
                    "system_depends": [str(d) for d in meta.system_depends],
                    "backup": list(meta.backup),
                    "setuid": list(meta.setuid),
                    "installed_size": meta.installed_size,
                    "build_date": meta.build_date,
                    "files_count": meta.files_count,
                }
            packages_out[pkg_name] = {
                "latest": series.latest,
                "versions": versions_out,
            }
        return {
            "format": self.format,
            "repo": self.repo,
            "generated": self.generated,
            "packages": packages_out,
        }


def _compute_latest_version(versions: Sequence[str] | Any) -> str:
    iterator = iter(versions)
    best = next(iterator)
    for candidate in iterator:
        if vercmp(candidate, best) > 0:
            best = candidate
    return best


def _parse_version_record(
    *,
    repo_name: str,
    pkg_name: str,
    version: str,
    value: Any,
    default_arch: str,
) -> RepoVersionRecord:
    if not isinstance(value, Mapping):
        raise ValidationError(
            f"version record {pkg_name} {version} must be an object"
        )
    raw_file = value.get("file")
    if not isinstance(raw_file, str) or not raw_file:
        raise ValidationError(f"version record {pkg_name} {version} missing 'file'")
    file_relpath = safe_relpath(raw_file)
    if not file_relpath.endswith(".tpkg"):
        raise ValidationError(
            f"package file for {pkg_name} {version} must end with .tpkg: {file_relpath!r}"
        )
    filename = file_relpath.rsplit("/", 1)[-1]

    raw_sha = value.get("sha256", "")
    if not isinstance(raw_sha, str) or not SHA256_RE.match(raw_sha.lower()):
        raise ValidationError(
            f"invalid sha256 for {pkg_name} {version}: {raw_sha!r}"
        )
    size = value.get("size", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValidationError(
            f"invalid size for {pkg_name} {version}: {size!r}"
        )

    arch = validate_arch(str(value.get("arch", default_arch)))
    metadata = PackageMetadata.from_dict(
        {
            "format": 1,
            "name": pkg_name,
            "version": version,
            "arch": arch,
            "description": value.get("description", ""),
            "url": value.get("url", ""),
            "license": value.get("license", []),
            "depends": value.get("depends", []),
            "provides": value.get("provides", [pkg_name]),
            "conflicts": value.get("conflicts", []),
            "replaces": value.get("replaces", []),
            "system_depends": value.get("system_depends", []),
            "backup": value.get("backup", []),
            "setuid": value.get("setuid", []),
            "installed_size": value.get("installed_size", 0),
            "build_date": value.get("build_date", ""),
            "files_count": value.get("files_count", 0),
        }
    )
    available = AvailablePackage(
        metadata=metadata,
        repo=repo_name,
        filename=filename,
        sha256=raw_sha.lower(),
        size=size,
    )
    return RepoVersionRecord(package=available, file_relpath=file_relpath)


@dataclass
class MergedCatalog:
    """Merged view across all enabled repositories in priority order."""

    packages: dict[str, RepoPackageSeries] = field(default_factory=dict)
    repos_by_name: dict[str, RepoConfig] = field(default_factory=dict)

    def get_series(self, name: str) -> RepoPackageSeries | None:
        return self.packages.get(name)

    def get_latest(self, name: str) -> RepoVersionRecord | None:
        series = self.packages.get(name)
        return series.get_latest() if series is not None else None

    def get_version(self, name: str, version: str) -> RepoVersionRecord | None:
        series = self.packages.get(name)
        return series.get_version(version) if series is not None else None

    def find_provider(self, requirement: Dependency) -> RepoVersionRecord | None:
        """Find the highest-priority package satisfying ``requirement``."""
        direct = self.packages.get(requirement.name)
        if direct is not None:
            latest_rec = direct.get_latest()
            if requirement.matches(latest_rec.version):
                return latest_rec
            for ver_str in reversed(direct.sorted_versions()):
                if requirement.matches(ver_str):
                    return direct.versions[ver_str]

        for series in self.packages.values():
            candidate = series.get_latest()
            for provided in candidate.metadata.provides:
                if provided.name == requirement.name:
                    prov_ver = provided.version or candidate.version
                    if requirement.matches(prov_ver):
                        return candidate
        return None

    def search(self, term: str) -> list[RepoVersionRecord]:
        needle = term.lower().strip()
        results: list[RepoVersionRecord] = []
        for pkg_name in sorted(self.packages):
            latest_rec = self.packages[pkg_name].get_latest()
            if (
                needle in pkg_name.lower()
                or needle in latest_rec.metadata.description.lower()
            ):
                results.append(latest_rec)
        return results


def merge_indexes(
    indexes: Sequence[RepositoryIndex],
    repos: Sequence[RepoConfig] = (),
) -> MergedCatalog:
    """Merge multiple repository indexes.

    First repository in order wins on duplicate ``(name, version)``;
    ``latest`` is recomputed across all merged versions with ``vercmp``.
    """
    by_pkg: dict[str, dict[str, RepoVersionRecord]] = {}
    for index in indexes:
        for pkg_name, series in index.packages.items():
            pkg_versions = by_pkg.setdefault(pkg_name, {})
            for ver_str, record in series.versions.items():
                pkg_versions.setdefault(ver_str, record)

    merged_packages: dict[str, RepoPackageSeries] = {}
    for pkg_name, versions_map in sorted(by_pkg.items()):
        latest = _compute_latest_version(versions_map.keys())
        merged_packages[pkg_name] = RepoPackageSeries(
            name=pkg_name,
            latest=latest,
            versions=versions_map,
        )

    repos_map = {repo.name: repo for repo in repos}
    return MergedCatalog(packages=merged_packages, repos_by_name=repos_map)


# ---------------------------------------------------------------------------
# Index Sync, Caching & Anti-Replay Verification
# ---------------------------------------------------------------------------


def index_cache_path(paths: Paths, repo_name: str) -> Path:
    validate_name(repo_name, what="repository name")
    return paths.index_cache_dir / f"index-{repo_name}.json"


def index_sig_cache_path(paths: Paths, repo_name: str) -> Path:
    validate_name(repo_name, what="repository name")
    return paths.index_cache_dir / f"index-{repo_name}.json.sig"


def _verify_index_signature(
    *,
    repo: RepoConfig,
    keyring: Keyring,
    server: str,
    index_bytes: bytes,
    timeout: float,
) -> bytes | None:
    """Verify ``titanpkg.db.json.sig`` according to ``repo.siglevel``."""
    if repo.siglevel == "none":
        return None

    sig_url = f"{server.rstrip('/')}/titanpkg.db.json.sig"
    sig_bytes: bytes | None = None
    try:
        sig_bytes = fetch_bytes(sig_url, timeout=timeout, max_size=MAX_SIG_BYTES)
    except RepositoryError as exc:
        if repo.siglevel == "required":
            raise SecurityError(
                f"signature required for repository {repo.name!r}, "
                f"but failed to fetch {sig_url}: {exc}"
            ) from exc
        return None

    if repo.siglevel == "optional" and not keyring.has_keys():
        return sig_bytes

    keyring.verify_bytes(
        index_bytes, sig_bytes, label=f"index-{repo.name}.json"
    )
    return sig_bytes


def _check_anti_replay(
    repo_name: str, cached_generated: str, incoming_generated: str
) -> None:
    """Reject a newly fetched index if its ISO timestamp goes backwards."""
    if not cached_generated or not incoming_generated:
        return
    if incoming_generated < cached_generated:
        raise SecurityError(
            f"replay/rollback attack detected for repository {repo_name!r}: "
            f"incoming index timestamp {incoming_generated!r} is older than "
            f"cached timestamp {cached_generated!r}"
        )


def sync_repo(
    repo: RepoConfig,
    paths: Paths,
    *,
    arch: str = "x86_64",
    timeout: float = DEFAULT_TIMEOUT,
    keyring: Keyring | None = None,
    allow_rollback: bool = False,
) -> RepositoryIndex:
    """Fetch, verify, anti-replay check and cache ``titanpkg.db.json`` for ``repo``."""
    kr = keyring or Keyring(paths)
    cache_file = index_cache_path(paths, repo.name)
    cached_generated = ""
    if cache_file.exists():
        with contextlib.suppress(Exception):
            cached_raw = read_json(cache_file)
            if isinstance(cached_raw, Mapping):
                cached_generated = str(cached_raw.get("generated", ""))

    last_error: Exception | None = None
    for server in repo.servers:
        index_url = f"{server.rstrip('/')}/titanpkg.db.json"
        try:
            raw_bytes = fetch_bytes(
                index_url, timeout=timeout, max_size=MAX_INDEX_BYTES
            )
            sig_bytes = _verify_index_signature(
                repo=repo,
                keyring=kr,
                server=server,
                index_bytes=raw_bytes,
                timeout=timeout,
            )
            try:
                decoded = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(
                    f"invalid JSON in repository index {index_url}: {exc}"
                ) from exc

            index = RepositoryIndex.from_dict(
                decoded, expected_repo=repo.name, default_arch=arch
            )
            if not allow_rollback:
                _check_anti_replay(repo.name, cached_generated, index.generated)

            atomic_write_bytes(cache_file, raw_bytes, mode=0o644)
            if sig_bytes is not None:
                atomic_write_bytes(
                    index_sig_cache_path(paths, repo.name), sig_bytes, mode=0o644
                )
            return index
        except SecurityError:
            raise
        except (RepositoryError, ValidationError) as exc:
            last_error = exc
            continue

    raise RepositoryError(
        f"failed to synchronize repository {repo.name!r}: {last_error}"
    )


def load_cached_repo(
    repo: RepoConfig,
    paths: Paths,
    *,
    arch: str = "x86_64",
) -> RepositoryIndex:
    """Load a previously cached repository index from ``/var/cache/titanpkg/index/``."""
    cache_file = index_cache_path(paths, repo.name)
    if not cache_file.exists():
        raise RepositoryError(
            f"no cached index for repository {repo.name!r} ({cache_file}); "
            "run 'titanpkg update' first"
        )
    try:
        raw = read_json(cache_file)
    except Exception as exc:
        raise RepositoryError(
            f"corrupt cached index for {repo.name!r}: {exc}"
        ) from exc
    return RepositoryIndex.from_dict(
        raw, expected_repo=repo.name, default_arch=arch
    )


def load_catalog(
    repos: Sequence[RepoConfig],
    paths: Paths,
    *,
    arch: str = "x86_64",
    offline: bool = False,
    refresh: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    keyring: Keyring | None = None,
) -> MergedCatalog:
    """Load or synchronize all enabled repositories and return a ``MergedCatalog``."""
    enabled = [repo for repo in repos if repo.enabled]
    indexes: list[RepositoryIndex] = []
    for repo in enabled:
        cache_file = index_cache_path(paths, repo.name)
        if offline:
            indexes.append(load_cached_repo(repo, paths, arch=arch))
        elif refresh or not cache_file.exists():
            indexes.append(
                sync_repo(
                    repo,
                    paths,
                    arch=arch,
                    timeout=timeout,
                    keyring=keyring,
                )
            )
        else:
            indexes.append(load_cached_repo(repo, paths, arch=arch))
    return merge_indexes(indexes, enabled)


# ---------------------------------------------------------------------------
# Verified Package Download
# ---------------------------------------------------------------------------


def _stream_bounded_download(
    url: str,
    temp_path: Path,
    *,
    timeout: float,
    expected_sha256: str,
    expected_size: int,
) -> None:
    """Stream ``url`` to ``temp_path`` enforcing byte size and SHA-256."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            temp_path.open("wb") as handle,
        ):
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if expected_size > 0 and total > expected_size:
                    raise SecurityError(
                        f"download for {url} exceeded declared size "
                        f"({total} > {expected_size} bytes)"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except urllib.error.HTTPError as exc:
        raise RepositoryError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RepositoryError(f"cannot fetch {url}: {exc.reason}") from exc
    except OSError as exc:
        raise RepositoryError(f"cannot fetch {url}: {exc}") from exc

    if expected_size > 0 and total != expected_size:
        raise SecurityError(
            f"size mismatch for {url}: expected {expected_size} bytes, got {total}"
        )
    actual_sha = digest.hexdigest()
    if actual_sha != expected_sha256.lower():
        raise SecurityError(
            f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual_sha}"
        )


def download_package(
    record: RepoVersionRecord,
    repo: RepoConfig,
    paths: Paths,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    offline: bool = False,
    keyring: Keyring | None = None,
) -> Path:
    """Return a verified local cache path for ``record``, downloading if needed."""
    kr = keyring or Keyring(paths)
    pkg = record.package
    cache_dir = paths.package_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / pkg.filename
    sig_destination = cache_dir / f"{pkg.filename}.sig"

    if destination.exists():
        actual_size = destination.stat().st_size
        if (pkg.size == 0 or actual_size == pkg.size) and (
            sha256_file(destination) == pkg.sha256
        ):
            if repo.siglevel == "required":
                if sig_destination.exists():
                    kr.verify_file(destination, sig_destination)
                    return destination
            elif repo.siglevel == "optional":
                if sig_destination.exists() and kr.has_keys():
                    kr.verify_file(destination, sig_destination)
                return destination
            else:
                return destination
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()

    if offline:
        raise RepositoryError(
            f"package {pkg.filename} is not in cache and --offline is set"
        )

    rel_url = record.file_relpath.lstrip("/")
    last_error: Exception | None = None
    for server in repo.servers:
        pkg_url = f"{server.rstrip('/')}/{rel_url}"
        sig_url = f"{pkg_url}.sig"
        fd, temp_str = tempfile.mkstemp(
            dir=str(cache_dir), prefix=f".{pkg.filename}.", suffix=".part"
        )
        os.close(fd)
        temp_path = Path(temp_str)
        try:
            _stream_bounded_download(
                pkg_url,
                temp_path,
                timeout=timeout,
                expected_sha256=pkg.sha256,
                expected_size=pkg.size,
            )

            if repo.siglevel != "none":
                sig_bytes: bytes | None = None
                try:
                    sig_bytes = fetch_bytes(
                        sig_url, timeout=timeout, max_size=MAX_SIG_BYTES
                    )
                except RepositoryError as exc:
                    if repo.siglevel == "required":
                        raise SecurityError(
                            f"missing required package signature {sig_url}: {exc}"
                        ) from exc

                if sig_bytes is not None:
                    atomic_write_bytes(sig_destination, sig_bytes, mode=0o644)
                    if repo.siglevel == "required" or kr.has_keys():
                        kr.verify_file(temp_path, sig_destination)

            os.chmod(temp_path, 0o644)
            os.replace(temp_path, destination)
            return destination
        except SecurityError:
            with contextlib.suppress(FileNotFoundError):
                sig_destination.unlink()
            raise
        except RepositoryError as exc:
            last_error = exc
            continue
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()

    raise RepositoryError(
        f"failed to download package {pkg.filename}: {last_error}"
    )
