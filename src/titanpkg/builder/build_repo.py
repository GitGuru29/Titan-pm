"""Regenerate and sign titanpkg.db.json (Phase 5)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from ..core.models import AvailablePackage, PackageMetadata, validate_arch, validate_name
from ..errors import SecurityError, UsageError, ValidationError
from ..repo import RepoPackageSeries, RepositoryIndex, RepoVersionRecord
from ..util import atomic_write_bytes, atomic_write_json, now_iso, sha256_file
from ..vercmp import vercmp


def read_tpkg_metadata(package_path: Path) -> PackageMetadata:
    """Safely extract and validate ``METADATA.json`` from a ``.tpkg`` archive."""
    if not package_path.exists() or not package_path.is_file():
        raise UsageError(f"package archive not found: {package_path}")
    try:
        with tarfile.open(package_path, mode="r:gz") as tar:
            for member in tar:
                if member.name == "METADATA.json":
                    if not member.isfile() or member.size > 4 * 1024 * 1024:
                        raise SecurityError(
                            f"invalid METADATA.json member in {package_path.name}"
                        )
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        break
                    raw = json.loads(extracted.read().decode("utf-8"))
                    return PackageMetadata.from_dict(raw)
    except (tarfile.TarError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"failed to read METADATA.json from {package_path}: {exc}"
        ) from exc
    raise ValidationError(f"package archive {package_path} is missing METADATA.json")


def _gpg_detach_sign(target_path: Path, sign_key: str) -> Path:
    gpg = shutil.which("gpg")
    if not gpg:
        raise SecurityError("gpg binary not found in PATH; cannot sign repository")
    sig_path = Path(f"{target_path}.sig")
    cmd = [
        gpg,
        "--batch",
        "--yes",
        "--detach-sign",
        "--local-user",
        sign_key,
        "--output",
        str(sig_path),
        str(target_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SecurityError(f"failed to sign {target_path.name}: {detail}")
    return sig_path


def build_repo(
    repo_dir: Path,
    packages_dir: Path,
    *,
    arch: str = "x86_64",
    name: str = "archtitan",
    sign_key: str | None = None,
    sign_packages: bool = False,
    generated: str | None = None,
) -> Path:
    """Copy packages into ``repo/<arch>/``, build ``titanpkg.db.json`` and sign."""
    validate_name(name, what="repository name")
    validate_arch(arch)
    if not packages_dir.exists() or not packages_dir.is_dir():
        raise UsageError(f"packages directory not found: {packages_dir}")

    arch_dir = repo_dir / arch
    arch_dir.mkdir(parents=True, exist_ok=True)

    for src_pkg in sorted(packages_dir.glob("*.tpkg")):
        dst_pkg = arch_dir / src_pkg.name
        if src_pkg.resolve() != dst_pkg.resolve():
            shutil.copy2(src_pkg, dst_pkg)
        src_sig = Path(f"{src_pkg}.sig")
        if src_sig.exists() and src_sig.resolve() != Path(f"{dst_pkg}.sig").resolve():
            shutil.copy2(src_sig, Path(f"{dst_pkg}.sig"))

    tpkg_files = sorted(arch_dir.glob("*.tpkg"))
    if not tpkg_files:
        raise UsageError(f"no .tpkg files found in {packages_dir} or {arch_dir}")

    by_pkg: dict[str, dict[str, RepoVersionRecord]] = {}
    checksum_lines: list[str] = []

    for pkg_file in tpkg_files:
        meta = read_tpkg_metadata(pkg_file)
        if meta.arch != arch:
            continue
        digest = sha256_file(pkg_file)
        size = pkg_file.stat().st_size
        relpath = f"{arch}/{pkg_file.name}"
        checksum_lines.append(f"{digest}  {relpath}")

        if sign_key and sign_packages:
            _gpg_detach_sign(pkg_file, sign_key)

        available = AvailablePackage(
            metadata=meta,
            repo=name,
            filename=pkg_file.name,
            sha256=digest,
            size=size,
        )
        record = RepoVersionRecord(package=available, file_relpath=relpath)
        by_pkg.setdefault(meta.name, {})[meta.version] = record

    series_map: dict[str, RepoPackageSeries] = {}
    for pkg_name, versions_map in sorted(by_pkg.items()):
        latest_ver = next(iter(versions_map))
        for candidate in versions_map:
            if vercmp(candidate, latest_ver) > 0:
                latest_ver = candidate
        series_map[pkg_name] = RepoPackageSeries(
            name=pkg_name,
            latest=latest_ver,
            versions=versions_map,
        )

    index = RepositoryIndex(
        format=1,
        repo=name,
        generated=generated or now_iso(),
        packages=series_map,
    )

    index_path = repo_dir / "titanpkg.db.json"
    atomic_write_json(index_path, index.to_dict(), mode=0o644)

    index_sha = sha256_file(index_path)
    checksum_lines.insert(0, f"{index_sha}  titanpkg.db.json")
    atomic_write_bytes(
        repo_dir / "SHA256SUMS",
        ("\n".join(checksum_lines) + "\n").encode("utf-8"),
        mode=0o644,
    )
    atomic_write_bytes(repo_dir / ".nojekyll", b"", mode=0o644)

    if sign_key:
        _gpg_detach_sign(index_path, sign_key)

    return index_path
