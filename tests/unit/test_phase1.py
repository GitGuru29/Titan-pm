from __future__ import annotations

import json
from pathlib import Path

import pytest

from titanpkg.config import DEFAULT_ALLOWED_PREFIXES, Paths, load_config, load_repos
from titanpkg.core.models import (
    Dependency,
    FileEntry,
    InstalledDatabase,
    InstalledPackage,
    PackageMetadata,
)
from titanpkg.errors import SecurityError, ValidationError
from titanpkg.util import (
    atomic_write_json,
    is_allowed_path,
    read_json,
    resolve_symlink_target,
    safe_relpath,
    sha256_bytes,
    validate_allowed_path,
)


def test_dependency_parsing_and_matching():
    dep_plain = Dependency.parse("titan-ui")
    assert dep_plain.name == "titan-ui"
    assert not dep_plain.is_versioned
    assert dep_plain.matches("1.0.0-1")

    dep_ge = Dependency.parse("titan-ui>=1.0.0")
    assert dep_ge.name == "titan-ui"
    assert dep_ge.op == ">="
    assert dep_ge.version == "1.0.0"
    assert dep_ge.matches("1.0.0-1")
    assert dep_ge.matches("2.0.0")
    assert not dep_ge.matches("0.9.0")

    dep_eq = Dependency.parse("titan-hwm=3.1.0-1")
    assert dep_eq.matches("3.1.0-1")
    assert not dep_eq.matches("3.1.0-2")

    with pytest.raises(ValidationError):
        Dependency.parse("")
    with pytest.raises(ValidationError):
        Dependency.parse("../evil>=1.0")


def test_safe_relpath_and_prefix_jail():
    assert safe_relpath("usr/local/bin/titan-hwm") == "usr/local/bin/titan-hwm"

    for bad in (
        "",
        "/etc/passwd",
        "../etc/shadow",
        "usr/../../etc/passwd",
        "usr/local/\x00bin",
        "a//b",
    ):
        with pytest.raises(SecurityError):
            safe_relpath(bad)

    for allowed in (
        "usr/local/bin/titan-hwm",
        "usr/lib/qt6/qml/ArchTitan/TitanUI/qmldir",
        "usr/lib/libTitanUI.so",
        "usr/lib/libTitanUI.so.1.0",
        "etc/titan-hwm/config",
        "etc/systemd/system/titan-hwm.service",
        "usr/share/applications/titanbrowser.desktop",
    ):
        assert is_allowed_path(allowed, DEFAULT_ALLOWED_PREFIXES)
        validate_allowed_path(allowed, DEFAULT_ALLOWED_PREFIXES)

    for forbidden in ("etc/passwd", "etc/shadow", "usr/bin/pacman", "boot/vmlinuz-linux"):
        assert not is_allowed_path(forbidden, DEFAULT_ALLOWED_PREFIXES)
        with pytest.raises(SecurityError):
            validate_allowed_path(forbidden, DEFAULT_ALLOWED_PREFIXES)


def test_symlink_confinement():
    assert (
        resolve_symlink_target(
            "usr/local/bin/titan-browser", "titanbrowser", DEFAULT_ALLOWED_PREFIXES
        )
        == "titanbrowser"
    )
    assert (
        resolve_symlink_target(
            "usr/local/bin/titan-settings",
            "/usr/local/bin/archtitan-settings",
            DEFAULT_ALLOWED_PREFIXES,
        )
        == "/usr/local/bin/archtitan-settings"
    )

    with pytest.raises(SecurityError):
        resolve_symlink_target(
            "usr/local/bin/evil", "../../../etc/shadow", DEFAULT_ALLOWED_PREFIXES
        )
    with pytest.raises(SecurityError):
        resolve_symlink_target(
            "usr/local/bin/evil", "/etc/shadow", DEFAULT_ALLOWED_PREFIXES
        )


def test_models_roundtrip():
    digest = sha256_bytes(b"titan-binary")
    entry_file = FileEntry(path="usr/local/bin/titan-hwm", type="file", mode="0755", sha256=digest)
    entry_link = FileEntry(
        path="usr/local/bin/titan-browser", type="symlink", target="titanbrowser"
    )

    meta = PackageMetadata.from_dict(
        {
            "name": "titan-hwm",
            "version": "3.1.0-1",
            "arch": "x86_64",
            "description": "Titan Hardware Manager",
            "depends": ["titan-ui>=1.0.0"],
            "provides": ["titan-hwm"],
            "backup": ["etc/titan-hwm/config"],
            "setuid": [],
            "installed_size": 1024,
            "files_count": 2,
        }
    )
    assert meta.to_dict()["name"] == "titan-hwm"

    installed_pkg = InstalledPackage(
        name="titan-hwm",
        version="3.1.0-1",
        arch="x86_64",
        repo="archtitan",
        files=(entry_file, entry_link),
        backup=("usr/local/bin/titan-hwm",),
    )
    db = InstalledDatabase(generated="2026-01-01T00:00:00Z", packages={"titan-hwm": installed_pkg})
    restored = InstalledDatabase.from_dict(db.to_dict())
    assert "titan-hwm" in restored.packages
    assert restored.packages["titan-hwm"].version == "3.1.0-1"


def test_config_and_repos_loading(tmp_path: Path):
    paths = Paths.from_root(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.repos_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_json(
        paths.default_config_file,
        {
            "siglevel": "optional",
            "timeout": 15,
        },
    )
    atomic_write_json(
        paths.repos_dir / "10-archtitan.json",
        {
            "name": "archtitan",
            "server": "https://example.org/repo/",
            "enabled": True,
        },
    )

    cfg = load_config(root=tmp_path)
    assert cfg.siglevel == "optional"
    assert cfg.timeout == 15.0
    assert cfg.paths.root == tmp_path.resolve()

    repos = load_repos(cfg.paths, cfg)
    assert len(repos) == 1
    assert repos[0].name == "archtitan"
    assert repos[0].server == "https://example.org/repo"
    assert repos[0].siglevel == "optional"

    raw = read_json(paths.default_config_file)
    assert raw["timeout"] == 15


def test_all_shipped_manifests_pass_allowed_prefixes():
    manifests_dir = Path(__file__).resolve().parents[2] / "packaging" / "manifests"
    for manifest_path in sorted(manifests_dir.glob("*.json")):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for pattern in data.get("files", []):
            sample_path = (
                pattern.replace("**", "sample")
                .replace(".so*", ".so.1")
                .replace("*", "sample")
            )
            assert is_allowed_path(sample_path, DEFAULT_ALLOWED_PREFIXES), (
                f"{manifest_path.name}: {pattern} not covered by DEFAULT_ALLOWED_PREFIXES"
            )
