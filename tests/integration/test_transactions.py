from __future__ import annotations

import os
from pathlib import Path

import pytest

from titanpkg.builder.build_package import build_package
from titanpkg.builder.build_repo import build_repo
from titanpkg.cli import main
from titanpkg.config import load_config, load_repos
from titanpkg.db import load_db
from titanpkg.errors import ResolutionError, SecurityError, TransactionError
from titanpkg.repo import load_catalog
from titanpkg.resolver import resolve_install
from titanpkg.transaction import execute_transaction
from titanpkg.util import atomic_write_json, sha256_file


def _create_source_tree(src_root: Path, *, ver_tag: str, default_cfg: str) -> None:
    bin_dir = src_root / "usr" / "local" / "bin"
    cfg_dir = src_root / "etc" / "titan-hwm"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)

    binary = bin_dir / "titan-hwm"
    binary.write_text(f"#!/bin/sh\necho titan-hwm-{ver_tag}\n", encoding="utf-8")
    os.chmod(binary, 0o755)

    link = bin_dir / "titan-hwm-cli"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink("titan-hwm", link)

    cfg = cfg_dir / "config"
    cfg.write_text(default_cfg, encoding="utf-8")
    os.chmod(cfg, 0o644)


def test_install_upgrade_rollback_remove_and_conffiles(tmp_path: Path, monkeypatch):
    """End-to-end test: build v1 & v2 .tpkg -> repo -> install -> upgrade -> rollback -> remove."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    src_v1 = tmp_path / "src_v1"
    src_v2 = tmp_path / "src_v2"
    _create_source_tree(src_v1, ver_tag="1.0.0", default_cfg="profile=balanced\n")
    _create_source_tree(src_v2, ver_tag="2.0.0", default_cfg="profile=performance\n")

    manifest_path = tmp_path / "titan-hwm.json"
    atomic_write_json(
        manifest_path,
        {
            "name": "titan-hwm",
            "version": "1.0.0",
            "release": 1,
            "arch": "x86_64",
            "description": "Titan Hardware Manager",
            "depends": [],
            "provides": ["titan-hwm"],
            "backup": ["etc/titan-hwm/config"],
            "setuid": [],
            "files": [
                "usr/local/bin/titan-hwm",
                "usr/local/bin/titan-hwm-cli",
                "etc/titan-hwm/config",
            ],
        },
    )

    pkgs_dir = tmp_path / "packages"
    pkg_v1_a = build_package(manifest_path, src_v1, pkgs_dir, version="1.0.0", release=1)
    hash_a = sha256_file(pkg_v1_a)
    pkg_v1_b = build_package(manifest_path, src_v1, pkgs_dir, version="1.0.0", release=1)
    assert sha256_file(pkg_v1_b) == hash_a, "Builds must be 100% deterministic"

    build_package(manifest_path, src_v2, pkgs_dir, version="2.0.0", release=1)

    repo_dir = tmp_path / "repo"
    index_path = build_repo(repo_dir, pkgs_dir, arch="x86_64", name="archtitan")
    assert index_path.exists()
    assert (repo_dir / ".nojekyll").exists()
    assert (repo_dir / "SHA256SUMS").exists()

    # Configure sandbox root
    target_root = tmp_path / "target_root"
    etc_dir = target_root / "etc" / "titanpkg"
    repos_d = etc_dir / "repos.d"
    repos_d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        etc_dir / "titanpkg.json",
        {"siglevel": "none", "timeout": 10},
    )
    atomic_write_json(
        repos_d / "archtitan.json",
        {
            "name": "archtitan",
            "server": f"file://{repo_dir}",
            "enabled": True,
            "siglevel": "none",
        },
    )

    # 1. Update repository index
    assert main(["--root", str(target_root), "update"]) == 0

    # 2. Install v1 explicitly
    assert (
        main(["--root", str(target_root), "--noconfirm", "install", "titan-hwm=1.0.0-1"])
        == 0
    )
    installed_bin = target_root / "usr" / "local" / "bin" / "titan-hwm"
    installed_cfg = target_root / "etc" / "titan-hwm" / "config"
    assert "1.0.0" in installed_bin.read_text(encoding="utf-8")
    assert installed_cfg.read_text(encoding="utf-8") == "profile=balanced\n"

    # 3. Modify conffile as user, then upgrade to v2 -> .pacnew must be written
    installed_cfg.write_text("profile=custom-user\n", encoding="utf-8")
    assert main(["--root", str(target_root), "--noconfirm", "upgrade"]) == 0
    assert "2.0.0" in installed_bin.read_text(encoding="utf-8")
    assert installed_cfg.read_text(encoding="utf-8") == "profile=custom-user\n"
    pacnew = target_root / "etc" / "titan-hwm" / "config.pacnew"
    assert pacnew.exists()
    assert pacnew.read_text(encoding="utf-8") == "profile=performance\n"

    # 4. Verify installed package integrity (modified backup is allowed without failing exit code)
    assert main(["--root", str(target_root), "verify"]) == 0

    # 5. Tamper with binary -> verify must return exit code 4 (SecurityError)
    orig_bytes = installed_bin.read_bytes()
    installed_bin.write_bytes(b"corrupted-binary")
    assert main(["--root", str(target_root), "verify"]) == SecurityError.exit_code
    installed_bin.write_bytes(orig_bytes)
    assert main(["--root", str(target_root), "verify"]) == 0

    # 6. Rollback to 1.0.0-1 automatically
    assert main(["--root", str(target_root), "--noconfirm", "rollback", "titan-hwm"]) == 0
    assert "1.0.0" in installed_bin.read_text(encoding="utf-8")

    # 7. Default protected_packages blocks removing titan-hwm
    assert (
        main(["--root", str(target_root), "--noconfirm", "remove", "titan-hwm"])
        == ResolutionError.exit_code
    )
    assert installed_bin.exists()

    # 8. With protected_packages cleared in titanpkg.json, remove saves .pacsave
    atomic_write_json(
        etc_dir / "titanpkg.json",
        {"siglevel": "none", "timeout": 10, "protected_packages": []},
    )
    assert main(["--root", str(target_root), "--noconfirm", "remove", "titan-hwm"]) == 0
    assert not installed_bin.exists()
    pacsave = target_root / "etc" / "titan-hwm" / "config.pacsave"
    assert pacsave.exists()
    assert pacsave.read_text(encoding="utf-8") == "profile=custom-user\n"


def test_atomic_rollback_on_mid_transaction_failure(tmp_path: Path, monkeypatch):
    """Simulated write failure mid-commit must restore every file and installed.json."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    src_v1 = tmp_path / "v1"
    src_v2 = tmp_path / "v2"
    _create_source_tree(src_v1, ver_tag="1.0.0", default_cfg="v1\n")
    _create_source_tree(src_v2, ver_tag="2.0.0", default_cfg="v2\n")

    manifest = tmp_path / "manifest.json"
    atomic_write_json(
        manifest,
        {
            "name": "titan-hwm",
            "version": "1.0.0",
            "release": 1,
            "arch": "x86_64",
            "files": [
                "usr/local/bin/titan-hwm",
                "usr/local/bin/titan-hwm-cli",
                "etc/titan-hwm/config",
            ],
        },
    )
    pkgs_dir = tmp_path / "pkgs"
    build_package(manifest, src_v1, pkgs_dir, version="1.0.0", release=1)
    build_package(manifest, src_v2, pkgs_dir, version="2.0.0", release=1)
    repo_dir = tmp_path / "repo"
    build_repo(repo_dir, pkgs_dir, arch="x86_64", name="archtitan")

    root = tmp_path / "root"
    repos_d = root / "etc" / "titanpkg" / "repos.d"
    repos_d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "etc" / "titanpkg" / "titanpkg.json", {"siglevel": "none"})
    atomic_write_json(
        repos_d / "archtitan.json",
        {"name": "archtitan", "server": f"file://{repo_dir}", "siglevel": "none"},
    )

    assert main(["--root", str(root), "update"]) == 0
    assert main(["--root", str(root), "--noconfirm", "install", "titan-hwm=1.0.0-1"]) == 0

    cfg = load_config(root=root)
    repos = {r.name: r for r in load_repos(cfg.paths, cfg)}
    db = load_db(cfg.paths)
    catalog = load_catalog(list(repos.values()), cfg.paths, offline=True)
    plan = resolve_install(["titan-hwm=2.0.0-1"], catalog, db, root=root)

    with pytest.raises(TransactionError, match="rolled back cleanly"):
        execute_transaction(plan, cfg, db, repos, fail_after_files=1)

    # Verify both on-disk files and installed.json remained at 1.0.0-1
    reloaded_db = load_db(cfg.paths)
    assert reloaded_db.get("titan-hwm").version == "1.0.0-1"
    bin_file = root / "usr" / "local" / "bin" / "titan-hwm"
    assert "1.0.0" in bin_file.read_text(encoding="utf-8")


def test_dependency_cycle_detection(tmp_path: Path):
    from titanpkg.repo import RepositoryIndex, merge_indexes
    from titanpkg.util import sha256_bytes

    dummy_sha = sha256_bytes(b"x")
    idx = RepositoryIndex.from_dict(
        {
            "format": 1,
            "repo": "archtitan",
            "generated": "2026-01-01T00:00:00Z",
            "packages": {
                "pkg-a": {
                    "latest": "1.0-1",
                    "versions": {
                        "1.0-1": {
                            "file": "x86_64/pkg-a-1.0-1-x86_64.tpkg",
                            "sha256": dummy_sha,
                            "size": 1,
                            "depends": ["pkg-b"],
                        }
                    },
                },
                "pkg-b": {
                    "latest": "1.0-1",
                    "versions": {
                        "1.0-1": {
                            "file": "x86_64/pkg-b-1.0-1-x86_64.tpkg",
                            "sha256": dummy_sha,
                            "size": 1,
                            "depends": ["pkg-a"],
                        }
                    },
                },
            },
        }
    )
    catalog = merge_indexes([idx])
    cfg = load_config(root=tmp_path)
    db = load_db(cfg.paths)
    with pytest.raises(ResolutionError, match="dependency cycle detected"):
        resolve_install(["pkg-a"], catalog, db)
