from __future__ import annotations

import os
from pathlib import Path

from titanpkg.builder.build_package import build_package
from titanpkg.builder.build_repo import build_repo
from titanpkg.cli import main
from titanpkg.util import atomic_write_json


def _make_tree(src_root: Path, *, ver: str, cfg_text: str) -> None:
    bin_dir = src_root / "usr" / "local" / "bin"
    cfg_dir = src_root / "etc" / "titan-hwm"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)

    exe = bin_dir / "titan-hwm"
    exe.write_text(f"#!/bin/sh\necho {ver}\n", encoding="utf-8")
    os.chmod(exe, 0o755)

    cfg = cfg_dir / "config"
    cfg.write_text(cfg_text, encoding="utf-8")
    os.chmod(cfg, 0o644)


def test_pacman_style_flags_end_to_end(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    src_v1 = tmp_path / "v1"
    src_v2 = tmp_path / "v2"
    _make_tree(src_v1, ver="1.0.0", cfg_text="mode=v1\n")
    _make_tree(src_v2, ver="2.0.0", cfg_text="mode=v2\n")

    manifest = tmp_path / "titan-hwm.json"
    atomic_write_json(
        manifest,
        {
            "name": "titan-hwm",
            "version": "1.0.0",
            "release": 1,
            "arch": "x86_64",
            "description": "Titan Hardware Manager",
            "backup": ["etc/titan-hwm/config"],
            "files": [
                "usr/local/bin/titan-hwm",
                "etc/titan-hwm/config",
            ],
        },
    )

    pkgs_dir = tmp_path / "pkgs"
    build_package(manifest, src_v1, pkgs_dir, version="1.0.0", release=1)
    repo_dir = tmp_path / "repo"
    build_repo(
        repo_dir,
        pkgs_dir,
        arch="x86_64",
        name="archtitan",
        generated="2026-05-01T00:00:00Z",
    )

    root = tmp_path / "root"
    repos_d = root / "etc" / "titanpkg" / "repos.d"
    repos_d.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "etc" / "titanpkg" / "titanpkg.json", {"siglevel": "none"})
    atomic_write_json(
        repos_d / "archtitan.json",
        {"name": "archtitan", "server": f"file://{repo_dir}", "siglevel": "none"},
    )

    # 1. Sync with -Sy and install v1 with -S
    assert main(["--root", str(root), "-Sy"]) == 0
    capsys.readouterr()

    assert main(["--root", str(root), "--noconfirm", "-S", "titan-hwm"]) == 0
    capsys.readouterr()

    bin_path = root / "usr" / "local" / "bin" / "titan-hwm"
    cfg_path = root / "etc" / "titan-hwm" / "config"
    assert "1.0.0" in bin_path.read_text(encoding="utf-8")

    # 2. Query installed (-Q, -Qi, -Ql, -Qo, -Qk)
    assert main(["--root", str(root), "-Q"]) == 0
    assert "titan-hwm 1.0.0-1" in capsys.readouterr().out

    assert main(["--root", str(root), "-Qi", "titan-hwm"]) == 0
    assert "titan-hwm" in capsys.readouterr().out

    assert main(["--root", str(root), "-Ql", "titan-hwm"]) == 0
    assert "/usr/local/bin/titan-hwm" in capsys.readouterr().out

    assert main(["--root", str(root), "-Qo", "/usr/local/bin/titan-hwm"]) == 0
    assert "is owned by titan-hwm 1.0.0-1" in capsys.readouterr().out

    assert main(["--root", str(root), "-Qk"]) == 0
    capsys.readouterr()

    # 3. Publish v2 into the repo, then run a single 'titanpkg -Syu'
    build_package(manifest, src_v2, pkgs_dir, version="2.0.0", release=1)
    build_repo(
        repo_dir,
        pkgs_dir,
        arch="x86_64",
        name="archtitan",
        generated="2026-05-02T00:00:00Z",
    )

    assert main(["--root", str(root), "--noconfirm", "-Syu"]) == 0
    out = capsys.readouterr().out
    assert "Synchronizing Titan package databases" in out
    assert "Starting full Titan subsystem upgrade" in out
    assert "2.0.0" in bin_path.read_text(encoding="utf-8")

    # 4. Query search (-Ss), repo info (-Si), repo list (-Sl), cache clean (-Sc)
    assert main(["--root", str(root), "-Ss", "Hardware"]) == 0
    assert "archtitan/titan-hwm 2.0.0-1" in capsys.readouterr().out

    assert main(["--root", str(root), "-Si", "titan-hwm"]) == 0
    assert "2.0.0-1" in capsys.readouterr().out

    assert main(["--root", str(root), "-Sl"]) == 0
    assert "archtitan titan-hwm 2.0.0-1" in capsys.readouterr().out

    assert main(["--root", str(root), "-Sc"]) == 0
    capsys.readouterr()

    # 5. Attempting to remove protected core package 'titan-hwm' is blocked by default
    cfg_path.write_text("user-edited=1\n", encoding="utf-8")
    assert main(["--root", str(root), "--noconfirm", "-Rns", "titan-hwm"]) == 3
    err = capsys.readouterr().err
    assert "cannot remove protected core package 'titan-hwm'" in err
    assert bin_path.exists()

    # 6. If admin explicitly clears protected_packages in titanpkg.json, -Rns removes it cleanly
    atomic_write_json(
        root / "etc" / "titanpkg" / "titanpkg.json",
        {"siglevel": "none", "protected_packages": []},
    )
    assert main(["--root", str(root), "--noconfirm", "-Rns", "titan-hwm"]) == 0
    assert not bin_path.exists()
    assert not (root / "etc" / "titan-hwm" / "config.pacsave").exists()

