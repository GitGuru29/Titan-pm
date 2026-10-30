from __future__ import annotations

from pathlib import Path

import pytest

from titanpkg.config import Paths, RepoConfig
from titanpkg.core.models import Dependency, FileEntry, InstalledPackage
from titanpkg.db import load_db, save_db
from titanpkg.errors import DatabaseError, RepositoryError, SecurityError
from titanpkg.repo import (
    RepositoryIndex,
    download_package,
    load_catalog,
    merge_indexes,
    sync_repo,
)
from titanpkg.util import atomic_write_bytes, atomic_write_json, sha256_bytes


def test_database_load_save_and_ownership(tmp_path: Path):
    paths = Paths.from_root(tmp_path)
    db = load_db(paths)
    assert len(db.packages) == 0

    digest = sha256_bytes(b"hwm-binary")
    pkg = InstalledPackage(
        name="titan-hwm",
        version="3.1.0-1",
        arch="x86_64",
        repo="archtitan",
        files=(
            FileEntry(
                path="usr/local/bin/titan-hwm",
                type="file",
                mode="0755",
                sha256=digest,
            ),
        ),
    )
    db.extra_fields["custom_meta"] = {"schema_note": "keep-me"}
    db.put(pkg)
    assert db.owner_of("usr/local/bin/titan-hwm") == "titan-hwm"

    save_db(paths, db)
    assert paths.installed_db_path.exists()
    assert paths.file_owners_path.exists()

    reloaded = load_db(paths)
    assert reloaded.is_installed("titan-hwm")
    assert reloaded.owner_of("usr/local/bin/titan-hwm") == "titan-hwm"
    assert reloaded.extra_fields["custom_meta"] == {"schema_note": "keep-me"}

    conflicting_pkg = InstalledPackage(
        name="other-pkg",
        version="1.0.0-1",
        arch="x86_64",
        repo="archtitan",
        files=(
            FileEntry(
                path="usr/local/bin/titan-hwm",
                type="file",
                mode="0755",
                sha256=digest,
            ),
        ),
    )
    with pytest.raises(DatabaseError):
        reloaded.put(conflicting_pkg)

    # Replacing the old package is allowed
    reloaded.put(conflicting_pkg, replacing=("titan-hwm",))
    assert not reloaded.is_installed("titan-hwm")
    assert reloaded.owner_of("usr/local/bin/titan-hwm") == "other-pkg"


def test_database_supports_mapping_files_and_detects_corruption(tmp_path: Path):
    paths = Paths.from_root(tmp_path)
    digest = sha256_bytes(b"content")
    atomic_write_json(
        paths.installed_db_path,
        {
            "format": 1,
            "generated": "2026-01-01T00:00:00Z",
            "packages": {
                "titan-hwm": {
                    "version": "3.1.0-1",
                    "repo": "archtitan",
                    "reason": "explicit",
                    "installed_at": "2026-01-01T00:00:00Z",
                    "files": {
                        "usr/local/bin/titan-hwm": {
                            "type": "file",
                            "sha256": digest,
                            "mode": "0755",
                            "written": True,
                            "backup": True,
                        }
                    },
                }
            },
        },
    )
    db = load_db(paths)
    pkg = db.get("titan-hwm")
    assert pkg is not None
    assert pkg.backup == ("usr/local/bin/titan-hwm",)
    assert db.owner_of("usr/local/bin/titan-hwm") == "titan-hwm"

    # Write corrupt JSON and confirm load_db refuses it
    atomic_write_bytes(paths.installed_db_path, b"{broken json")
    with pytest.raises(DatabaseError):
        load_db(paths)


def _make_sample_index(
    repo_name: str,
    *,
    generated: str = "2026-05-01T00:00:00Z",
    pkg_bytes: bytes = b"fake-tpkg-payload",
) -> dict:
    digest = sha256_bytes(pkg_bytes)
    return {
        "format": 1,
        "repo": repo_name,
        "generated": generated,
        "packages": {
            "titan-hwm": {
                "latest": "3.1.0-1",
                "versions": {
                    "3.1.0-1": {
                        "file": "x86_64/titan-hwm-3.1.0-1-x86_64.tpkg",
                        "sha256": digest,
                        "size": len(pkg_bytes),
                        "depends": ["titan-ui>=1.0.0"],
                        "provides": ["titan-hwm", "hwm-daemon=3.1"],
                        "description": "Titan Hardware Manager",
                    },
                    "3.2.0-1": {
                        "file": "x86_64/titan-hwm-3.2.0-1-x86_64.tpkg",
                        "sha256": digest,
                        "size": len(pkg_bytes),
                        "depends": ["titan-ui>=1.0.0"],
                        "provides": ["titan-hwm", "hwm-daemon=3.2"],
                        "description": "Titan Hardware Manager",
                    },
                },
            }
        },
    }


def test_repo_sync_anti_replay_and_verified_download(tmp_path: Path):
    root_dir = tmp_path / "root"
    repo_srv = tmp_path / "repo"
    (repo_srv / "x86_64").mkdir(parents=True)

    payload = b"deterministic-tpkg-archive-bytes"
    pkg_file = repo_srv / "x86_64" / "titan-hwm-3.2.0-1-x86_64.tpkg"
    pkg_file.write_bytes(payload)

    index_doc = _make_sample_index(
        "archtitan", generated="2026-05-02T00:00:00Z", pkg_bytes=payload
    )
    atomic_write_json(repo_srv / "titanpkg.db.json", index_doc)

    paths = Paths.from_root(root_dir)
    repo_cfg = RepoConfig(
        name="archtitan",
        servers=(f"file://{repo_srv}",),
        siglevel="none",
    )

    idx = sync_repo(repo_cfg, paths)
    # Latest must be computed by vercmp (3.2.0-1 > 3.1.0-1 even if index said 3.1.0-1)
    assert idx.packages["titan-hwm"].latest == "3.2.0-1"

    # Anti-replay check: older timestamp must raise SecurityError
    stale_doc = _make_sample_index(
        "archtitan", generated="2026-01-01T00:00:00Z", pkg_bytes=payload
    )
    atomic_write_json(repo_srv / "titanpkg.db.json", stale_doc)
    with pytest.raises(SecurityError, match="replay/rollback"):
        sync_repo(repo_cfg, paths)

    # Restore valid index and test catalog + package download
    atomic_write_json(repo_srv / "titanpkg.db.json", index_doc)
    catalog = load_catalog([repo_cfg], paths, offline=True)
    provider = catalog.find_provider(Dependency.parse("hwm-daemon>=3.2"))
    assert provider is not None
    assert provider.version == "3.2.0-1"

    cached_pkg = download_package(provider, repo_cfg, paths)
    assert cached_pkg.exists()
    assert cached_pkg.read_bytes() == payload

    # Tamper with on-server package bytes (oversized) -> must fail with size limit SecurityError
    cached_pkg.unlink()
    pkg_file.write_bytes(payload + b"!")
    with pytest.raises(SecurityError, match="exceeded declared size"):
        download_package(provider, repo_cfg, paths)

    # Tamper with on-server package bytes (same size, wrong hash) -> must fail with SHA-256 mismatch
    pkg_file.write_bytes(b"X" * len(payload))
    with pytest.raises(SecurityError, match="SHA-256 mismatch"):
        download_package(provider, repo_cfg, paths)


def test_required_signature_blocks_unsigned_repo(tmp_path: Path):
    root_dir = tmp_path / "root"
    repo_srv = tmp_path / "repo"
    repo_srv.mkdir(parents=True)
    atomic_write_json(repo_srv / "titanpkg.db.json", _make_sample_index("archtitan"))

    paths = Paths.from_root(root_dir)
    repo_cfg = RepoConfig(
        name="archtitan",
        servers=(f"file://{repo_srv}",),
        siglevel="required",
    )
    with pytest.raises(SecurityError, match="signature required"):
        sync_repo(repo_cfg, paths)


def test_merge_indexes_priority_and_mirror_fallback(tmp_path: Path):
    idx1 = RepositoryIndex.from_dict(
        _make_sample_index("testing", pkg_bytes=b"from-testing")
    )
    idx2 = RepositoryIndex.from_dict(
        _make_sample_index("core", pkg_bytes=b"from-core")
    )
    merged = merge_indexes([idx1, idx2])
    # First repo in list wins on duplicate (name, version)
    assert merged.get_latest("titan-hwm").repo == "testing"

    # Mirror fallback test: dead primary mirror -> working backup mirror
    repo_srv = tmp_path / "mirror2"
    repo_srv.mkdir(parents=True)
    atomic_write_json(repo_srv / "titanpkg.db.json", _make_sample_index("archtitan"))
    paths = Paths.from_root(tmp_path / "root")
    repo_cfg = RepoConfig(
        name="archtitan",
        servers=(f"file://{tmp_path / 'nonexistent'}", f"file://{repo_srv}"),
        siglevel="none",
    )
    synced = sync_repo(repo_cfg, paths)
    assert "titan-hwm" in synced.packages

    with pytest.raises(RepositoryError):
        sync_repo(
            RepoConfig(
                name="broken",
                servers=(f"file://{tmp_path / 'missing'}",),
                siglevel="none",
            ),
            paths,
        )
