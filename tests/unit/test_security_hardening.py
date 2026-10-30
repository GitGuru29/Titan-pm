from __future__ import annotations

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from titanpkg.builder.build_package import build_package
from titanpkg.cli import main
from titanpkg.config import DEFAULT_ALLOWED_PREFIXES, Paths, RepoConfig
from titanpkg.db import load_db
from titanpkg.errors import ResolutionError, SecurityError, ValidationError
from titanpkg.repo import MergedCatalog, RepositoryIndex, sync_repo
from titanpkg.resolver import resolve_install
from titanpkg.transaction import _place_entry_atomically, inspect_and_stage_tpkg
from titanpkg.util import atomic_write_json, sha256_bytes


def test_insecure_http_repo_rejected_by_default():
    with pytest.raises(ValidationError, match="insecure http://"):
        RepoConfig.from_dict(
            {
                "name": "evil",
                "server": "http://mirror.example.com/repo",
            }
        )

    cfg = RepoConfig.from_dict(
        {
            "name": "localhttp",
            "server": "http://127.0.0.1:8000/repo",
            "allow_insecure_http": True,
        }
    )
    assert cfg.server == "http://127.0.0.1:8000/repo"


def test_fingerprint_pinning_validation():
    valid_fpr = "A" * 40
    cfg = RepoConfig.from_dict(
        {
            "name": "archtitan",
            "server": "https://example.org/repo",
            "trusted_fingerprints": [valid_fpr.lower()],
        }
    )
    assert cfg.trusted_fingerprints == (valid_fpr,)

    with pytest.raises(ValidationError, match="invalid OpenPGP fingerprint"):
        RepoConfig.from_dict(
            {
                "name": "archtitan",
                "server": "https://example.org/repo",
                "trusted_fingerprints": ["not-a-hex-fingerprint"],
            }
        )


def test_anti_freeze_expired_and_stale_index_rejected(tmp_path: Path):
    paths = Paths.from_root(tmp_path / "root")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True)

    digest = sha256_bytes(b"pkg")
    expired_index = {
        "format": 1,
        "repo": "archtitan",
        "generated": "2020-01-01T00:00:00Z",
        "expires": "2020-01-08T00:00:00Z",
        "packages": {
            "titan-sysmon": {
                "latest": "1.0.0-1",
                "versions": {
                    "1.0.0-1": {
                        "file": "x86_64/titan-sysmon-1.0.0-1-x86_64.tpkg",
                        "sha256": digest,
                        "size": 3,
                    }
                },
            }
        },
    }
    atomic_write_json(repo_dir / "titanpkg.db.json", expired_index)

    repo = RepoConfig(
        name="archtitan",
        servers=(f"file://{repo_dir}",),
        siglevel="none",
    )
    with pytest.raises(SecurityError, match="expired"):
        sync_repo(repo, paths)

    # Now test max_index_age_days without explicit 'expires'
    stale_index = dict(expired_index)
    stale_index.pop("expires")
    atomic_write_json(repo_dir / "titanpkg.db.json", stale_index)
    repo_with_ttl = RepoConfig(
        name="archtitan",
        servers=(f"file://{repo_dir}",),
        siglevel="none",
        max_index_age_days=14,
    )
    with pytest.raises(SecurityError, match="max_index_age_days=14"):
        sync_repo(repo_with_ttl, paths)


def test_protected_package_cannot_be_replaced_by_another_package(tmp_path: Path):
    paths = Paths.from_root(tmp_path)
    db = load_db(paths)
    digest = sha256_bytes(b"payload")
    idx = RepositoryIndex.from_dict(
        {
            "format": 1,
            "repo": "archtitan",
            "generated": "2026-05-01T00:00:00Z",
            "packages": {
                "rogue-hwm": {
                    "latest": "9.0.0-1",
                    "versions": {
                        "9.0.0-1": {
                            "file": "x86_64/rogue-hwm-9.0.0-1-x86_64.tpkg",
                            "sha256": digest,
                            "size": 7,
                            "replaces": ["titan-hwm"],
                        }
                    },
                }
            },
        }
    )
    catalog = MergedCatalog(packages=dict(idx.packages))
    with pytest.raises(
        ResolutionError,
        match="cannot replace protected core package 'titan-hwm'",
    ):
        resolve_install(
            ["rogue-hwm"],
            catalog,
            db,
            protected_packages=("titan-hwm", "titanpkg"),
        )


def test_parent_directory_symlink_toctou_blocked(tmp_path: Path):
    install_root = tmp_path / "root"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True)
    (install_root / "usr").mkdir(parents=True)
    # Create a symlink at /usr/local pointing outside
    os.symlink(str(outside_dir), install_root / "usr" / "local")

    staged_file = tmp_path / "staged_bin"
    staged_file.write_bytes(b"binary")
    from titanpkg.core.models import FileEntry

    entry = FileEntry(
        path="usr/local/bin/titan-sysmon",
        type="file",
        mode="0755",
        sha256=sha256_bytes(b"binary"),
    )
    target_path = install_root / "usr" / "local" / "bin" / "titan-sysmon"
    with pytest.raises(
        SecurityError, match="refusing to write through symlinked parent directory"
    ):
        _place_entry_atomically(
            entry,
            staged_file,
            target_path,
            install_root=install_root,
            allowed_prefixes=DEFAULT_ALLOWED_PREFIXES,
        )


def test_install_scriptlet_sha256_pinned_and_tamper_detected(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    src_root = tmp_path / "src"
    bin_dir = src_root / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "titan-sysmon"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(exe, 0o755)

    script_file = tmp_path / "titan-sysmon.install"
    script_file.write_text("post_install() {\n  true\n}\n", encoding="utf-8")

    manifest = tmp_path / "manifest.json"
    atomic_write_json(
        manifest,
        {
            "name": "titan-sysmon",
            "version": "1.0.0",
            "release": 1,
            "arch": "x86_64",
            "files": ["usr/local/bin/titan-sysmon"],
            "install": str(script_file),
        },
    )
    out_dir = tmp_path / "pkgs"
    tpkg_path = build_package(manifest, src_root, out_dir)

    # Verify clean package stages and has script_sha256
    staged = inspect_and_stage_tpkg(
        tpkg_path,
        tmp_path / "stage_ok",
        expected_name="titan-sysmon",
        expected_version="1.0.0-1",
        allowed_prefixes=DEFAULT_ALLOWED_PREFIXES,
    )
    assert staged.metadata.script_sha256 is not None

    # Tamper with INSTALL inside the .tpkg without updating METADATA.json
    tampered_path = tmp_path / "tampered.tpkg"
    with (
        gzip.open(tpkg_path, "rb") as gz_in,
        tarfile.open(fileobj=gz_in, mode="r|*") as tar_in,
        gzip.open(tampered_path, "wb") as gz_out,
        tarfile.open(fileobj=gz_out, mode="w|") as tar_out,
    ):
        for member in tar_in:
            fobj = tar_in.extractfile(member)
            data = fobj.read() if fobj is not None else b""
            if member.name == "INSTALL":
                data = b"post_install() {\n   evil_command\n}\n"
            info = tarfile.TarInfo(name=member.name)
            info.size = len(data)
            info.mode = member.mode
            tar_out.addfile(info, io.BytesIO(data))

    with pytest.raises(SecurityError, match="INSTALL scriptlet SHA-256 mismatch"):
        inspect_and_stage_tpkg(
            tampered_path,
            tmp_path / "stage_bad",
            expected_name="titan-sysmon",
            expected_version="1.0.0-1",
            allowed_prefixes=DEFAULT_ALLOWED_PREFIXES,
        )


def test_non_root_rejected_for_system_root_mutations(monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    monkeypatch.delenv("TITANPKG_ALLOW_NON_ROOT", raising=False)
    rc = main(["--root", "/", "-Syu"])
    assert rc == SecurityError.exit_code
    err = capsys.readouterr().err
    assert "unless you are root (use 'sudo tpac ...')" in err


def test_hash_chained_audit_log_and_tamper_detection(tmp_path: Path, capsys):
    from titanpkg.transaction import append_audit_log, read_and_verify_audit_log

    root = tmp_path / "root"
    paths = Paths.from_root(root)
    log_path = paths.log_file

    append_audit_log(
        log_path,
        tx_id="tx-001",
        events=[("installed", "titan-hwm", "3.1.0-1")],
        timestamp="2026-05-01T00:00:00Z",
    )
    append_audit_log(
        log_path,
        tx_id="tx-002",
        events=[("upgraded", "titan-hwm", "3.1.0-1 -> 3.2.0-1")],
        timestamp="2026-05-02T00:00:00Z",
    )

    entries, errors = read_and_verify_audit_log(log_path)
    assert len(entries) == 2
    assert errors == []

    # Verify via CLI (-Qh and history --verify)
    assert main(["--root", str(root), "-Qh"]) == 0
    out = capsys.readouterr().out
    assert "installed titan-hwm (3.1.0-1)" in out
    assert "upgraded titan-hwm (3.1.0-1 -> 3.2.0-1)" in out

    assert main(["--root", str(root), "history", "--verify"]) == 0
    assert "audit log hash chain verified (2 entries intact)" in capsys.readouterr().out

    # Tamper with line 1 in /var/log/titanpkg.log (change version without recomputing chain)
    content = log_path.read_text(encoding="utf-8")
    log_path.write_text(
        content.replace("(3.1.0-1)", "(1.0.0-1)", 1), encoding="utf-8"
    )

    assert main(["--root", str(root), "history", "--verify"]) == SecurityError.exit_code
    out_tampered = capsys.readouterr().out
    assert "TAMPER ERROR: line 1" in out_tampered

