"""Build a deterministic .tpkg from a manifest (Phase 5)."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import DEFAULT_ALLOWED_PREFIXES
from ..core.models import FileEntry, PackageMetadata, validate_arch, validate_name
from ..errors import SecurityError, UsageError, ValidationError
from ..util import (
    mode_to_string,
    resolve_symlink_target,
    safe_relpath,
    sha256_file,
    validate_allowed_path,
)


def _deterministic_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise ValidationError(f"invalid SOURCE_DATE_EPOCH: {raw!r}") from exc
    return 0


def _epoch_iso(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _expand_manifest_files(
    source_root: Path, patterns: Sequence[str]
) -> list[tuple[str, Path]]:
    """Expand manifest file paths/globs under ``source_root`` deterministically."""
    if not patterns:
        raise ValidationError("manifest 'files' list cannot be empty")
    collected: dict[str, Path] = {}

    for raw_pattern in patterns:
        cleaned = raw_pattern.strip().lstrip("/")
        if not cleaned:
            raise ValidationError("empty path pattern in manifest 'files'")
        if "\x00" in cleaned or ".." in cleaned.split("/"):
            raise SecurityError(f"unsafe pattern in manifest 'files': {raw_pattern!r}")

        has_glob = any(ch in cleaned for ch in ("*", "?", "["))
        if has_glob:
            matches = sorted(source_root.glob(cleaned))
        else:
            candidate = source_root / cleaned
            matches = [candidate] if (candidate.exists() or candidate.is_symlink()) else []

        if not matches:
            raise UsageError(
                f"no files matched pattern {raw_pattern!r} under {source_root}"
            )

        for match in matches:
            if match.is_dir() and not match.is_symlink():
                for child in sorted(match.rglob("*")):
                    if child.is_dir() and not child.is_symlink():
                        continue
                    rel = child.relative_to(source_root).as_posix()
                    collected[safe_relpath(rel)] = child
            else:
                rel = match.relative_to(source_root).as_posix()
                collected[safe_relpath(rel)] = match

    if not collected:
        raise UsageError(f"manifest matched zero regular files or symlinks in {source_root}")
    return sorted(collected.items(), key=lambda item: item[0])


def _resolve_install_script(manifest_path: Path, raw_install: str | None) -> Path | None:
    if not raw_install:
        return None
    candidates = [
        Path(raw_install),
        manifest_path.parent / raw_install,
        manifest_path.parent.parent.parent / raw_install,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise UsageError(f"install scriptlet not found: {raw_install}")


def _add_bytes_to_tar(
    tar: tarfile.TarFile,
    name: str,
    data: bytes,
    *,
    mode: int,
    mtime: int,
) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.REGTYPE
    info.size = len(data)
    info.mode = mode & 0o7777
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime
    tar.addfile(info, io.BytesIO(data))


def _add_symlink_to_tar(
    tar: tarfile.TarFile,
    name: str,
    linkname: str,
    *,
    mtime: int,
) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = linkname
    info.size = 0
    info.mode = 0o777
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime
    tar.addfile(info)


def build_package(
    manifest_path: Path,
    source_root: Path,
    out_dir: Path,
    *,
    arch: str | None = None,
    version: str | None = None,
    release: str | int | None = None,
    allowed_prefixes: Sequence[str] = DEFAULT_ALLOWED_PREFIXES,
) -> Path:
    """Build a reproducible ``.tpkg`` archive and return its output path."""
    if not manifest_path.exists():
        raise UsageError(f"manifest not found: {manifest_path}")
    if not source_root.exists() or not source_root.is_dir():
        raise UsageError(f"source root directory not found: {source_root}")

    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValidationError(f"invalid manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValidationError(f"manifest must be a JSON object: {manifest_path}")

    pkg_name = validate_name(str(manifest.get("name", "")))
    raw_ver = str(version if version is not None else manifest.get("version", "")).strip()
    if not raw_ver:
        raise ValidationError(f"manifest {manifest_path} missing 'version'")
    raw_rel = release if release is not None else manifest.get("release", 1)
    full_version = raw_ver if "-" in raw_ver and release is None else f"{raw_ver}-{raw_rel}"
    pkg_arch = validate_arch(str(arch or manifest.get("arch", "x86_64")))

    setuid_paths = {safe_relpath(p) for p in manifest.get("setuid", [])}
    backup_paths = [safe_relpath(p) for p in manifest.get("backup", [])]
    for p in setuid_paths | set(backup_paths):
        validate_allowed_path(p, allowed_prefixes)

    staged = _expand_manifest_files(source_root, manifest.get("files", []))
    epoch = _deterministic_epoch()

    file_entries: list[FileEntry] = []
    payload_records: list[tuple[FileEntry, Path]] = []
    installed_size = 0

    for relpath, src_path in staged:
        validate_allowed_path(relpath, allowed_prefixes)
        st = src_path.lstat()
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(src_path)
            resolve_symlink_target(relpath, target, allowed_prefixes)
            entry = FileEntry(
                path=relpath,
                type="symlink",
                mode="0777",
                target=target,
            )
            file_entries.append(entry)
            payload_records.append((entry, src_path))
        elif stat.S_ISREG(st.st_mode):
            raw_mode = stat.S_IMODE(st.st_mode)
            if relpath in setuid_paths:
                if not (raw_mode & (stat.S_ISUID | stat.S_ISGID)):
                    raw_mode |= stat.S_ISUID
            else:
                raw_mode &= ~(stat.S_ISUID | stat.S_ISGID)
            digest = sha256_file(src_path)
            installed_size += st.st_size
            entry = FileEntry(
                path=relpath,
                type="file",
                mode=mode_to_string(raw_mode),
                sha256=digest,
            )
            file_entries.append(entry)
            payload_records.append((entry, src_path))
        else:
            raise SecurityError(
                f"unsupported special file type in package payload: {src_path}"
            )

    install_script = _resolve_install_script(manifest_path, manifest.get("install"))
    script_bytes = install_script.read_bytes() if install_script is not None else None
    script_sha256 = (
        hashlib.sha256(script_bytes).hexdigest() if script_bytes is not None else None
    )

    metadata = PackageMetadata.from_dict(
        {
            "format": 1,
            "name": pkg_name,
            "version": full_version,
            "arch": pkg_arch,
            "description": manifest.get("description", ""),
            "url": manifest.get("url", ""),
            "license": manifest.get("license", []),
            "depends": manifest.get("depends", []),
            "provides": manifest.get("provides", [pkg_name]),
            "conflicts": manifest.get("conflicts", []),
            "replaces": manifest.get("replaces", []),
            "system_depends": manifest.get("system_depends", []),
            "backup": backup_paths,
            "setuid": sorted(setuid_paths),
            "installed_size": installed_size,
            "build_date": _epoch_iso(epoch),
            "files_count": len(file_entries),
            "script_sha256": script_sha256,
        }
    )

    metadata_bytes = (
        json.dumps(metadata.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    files_bytes = (
        json.dumps(
            [entry.to_dict() for entry in file_entries],
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_filename = f"{pkg_name}-{full_version}-{pkg_arch}.tpkg"
    out_path = out_dir / out_filename

    fd, temp_str = tempfile.mkstemp(
        dir=str(out_dir), prefix=f".{out_filename}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as raw_handle:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_handle, mtime=epoch
            ) as gz_handle, tarfile.open(
                fileobj=gz_handle, mode="w|", format=tarfile.USTAR_FORMAT
            ) as tar:
                _add_bytes_to_tar(
                    tar, "METADATA.json", metadata_bytes, mode=0o644, mtime=epoch
                )
                _add_bytes_to_tar(
                    tar, "FILES.json", files_bytes, mode=0o644, mtime=epoch
                )
                if script_bytes is not None:
                    _add_bytes_to_tar(
                        tar,
                        "INSTALL",
                        script_bytes,
                        mode=0o755,
                        mtime=epoch,
                    )
                for entry, src_path in payload_records:
                    member_name = f"payload/{entry.path}"
                    if entry.type == "symlink":
                        _add_symlink_to_tar(
                            tar,
                            member_name,
                            entry.target or "",
                            mtime=epoch,
                        )
                    else:
                        _add_bytes_to_tar(
                            tar,
                            member_name,
                            src_path.read_bytes(),
                            mode=int(entry.mode, 8),
                            mtime=epoch,
                        )
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        os.chmod(temp_str, 0o644)
        os.replace(temp_str, out_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_str)
        raise

    return out_path
