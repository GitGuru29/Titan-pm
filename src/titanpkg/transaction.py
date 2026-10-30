"""Staged, reversible filesystem transactions (Phase 3).

Lifecycle::

    lock -> plan -> preflight -> download -> stage -> backup -> commit -> rollback
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from .config import Config, RepoConfig
from .core.models import FileEntry, InstalledPackage, PackageMetadata
from .db import Database, save_db
from .errors import DatabaseError, SecurityError, TransactionError, ValidationError
from .repo import Keyring, download_package
from .resolver import PlannedInstall, TransactionPlan
from .util import (
    CHUNK_SIZE,
    atomic_write_bytes,
    is_allowed_path,
    mode_from_string,
    mode_to_string,
    now_iso,
    resolve_symlink_target,
    sha256_file,
    validate_allowed_path,
)

MAX_DECOMPRESS_SLACK = 32 * 1024 * 1024  # 32 MiB slack above declared installed_size
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024  # 1 GiB hard ceiling per package


# ---------------------------------------------------------------------------
# Exclusive Transaction Lock
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def transaction_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive non-blocking flock on ``/var/lib/titanpkg/lock``."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[bytes] = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DatabaseError(
                f"cannot acquire transaction lock {lock_path}: another titanpkg process is running"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# ---------------------------------------------------------------------------
# Staged Package Inspection & Extraction
# ---------------------------------------------------------------------------


@dataclass
class StagedPackage:
    """Verified, extracted contents of one ``.tpkg`` inside ``txn/<id>/stage/``."""

    planned: PlannedInstall | None
    metadata: PackageMetadata
    files: tuple[FileEntry, ...]
    payload_dir: Path
    install_script: str | None = None


def inspect_and_stage_tpkg(
    archive_path: Path,
    stage_dir: Path,
    *,
    expected_name: str,
    expected_version: str,
    allowed_prefixes: Sequence[str],
    planned: PlannedInstall | None = None,
) -> StagedPackage:
    """Quarantine-inspect ``archive_path`` and extract payload to ``stage_dir``."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(stage_dir, 0o700)
    payload_dir = stage_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)

    meta_bytes: bytes | None = None
    files_bytes: bytes | None = None
    install_script: str | None = None
    install_script_bytes: bytes | None = None
    extracted_entries: dict[str, FileEntry] = {}
    total_payload_bytes = 0

    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            for member in tar:
                name = member.name.lstrip("./")
                if not name:
                    continue
                if "\x00" in name or name.startswith("/") or ".." in name.split("/"):
                    raise SecurityError(
                        f"unsafe tar entry path in {archive_path.name}: {member.name!r}"
                    )
                if member.islnk():
                    raise SecurityError(
                        f"hardlinks are forbidden in .tpkg archives: {member.name!r}"
                    )
                if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise SecurityError(
                        f"special device/FIFO member forbidden in .tpkg: {member.name!r}"
                    )

                if name == "METADATA.json":
                    if not member.isfile() or member.size > 4 * 1024 * 1024:
                        raise SecurityError("invalid METADATA.json member in archive")
                    fobj = tar.extractfile(member)
                    if fobj is None:
                        raise SecurityError("unreadable METADATA.json in archive")
                    meta_bytes = fobj.read(4 * 1024 * 1024 + 1)
                    continue

                if name == "FILES.json":
                    if not member.isfile() or member.size > 16 * 1024 * 1024:
                        raise SecurityError("invalid FILES.json member in archive")
                    fobj = tar.extractfile(member)
                    if fobj is None:
                        raise SecurityError("unreadable FILES.json in archive")
                    files_bytes = fobj.read(16 * 1024 * 1024 + 1)
                    continue

                if name == "INSTALL":
                    if not member.isfile() or member.size > 1024 * 1024:
                        raise SecurityError("invalid INSTALL scriptlet member")
                    fobj = tar.extractfile(member)
                    if fobj is not None:
                        install_script_bytes = fobj.read(1024 * 1024 + 1)
                        install_script = install_script_bytes.decode("utf-8")
                    continue

                if not name.startswith("payload/"):
                    if member.isdir() and name == "payload":
                        continue
                    raise SecurityError(
                        f"unexpected top-level member in {archive_path.name}: {name!r}"
                    )

                relpath = name[len("payload/") :]
                if not relpath or member.isdir():
                    continue
                validate_allowed_path(relpath, allowed_prefixes)
                dest_path = payload_dir / relpath
                dest_path.parent.mkdir(parents=True, exist_ok=True)

                if member.issym():
                    target = member.linkname
                    resolve_symlink_target(relpath, target, allowed_prefixes)
                    if dest_path.exists() or dest_path.is_symlink():
                        raise SecurityError(f"duplicate tar entry for {relpath!r}")
                    os.symlink(target, dest_path)
                    extracted_entries[relpath] = FileEntry(
                        path=relpath,
                        type="symlink",
                        mode="0777",
                        target=target,
                    )
                elif member.isfile():
                    if dest_path.exists() or dest_path.is_symlink():
                        raise SecurityError(f"duplicate tar entry for {relpath!r}")
                    fobj = tar.extractfile(member)
                    if fobj is None:
                        raise SecurityError(f"cannot read payload member {relpath!r}")
                    digest = hashlib.sha256()
                    written = 0
                    with dest_path.open("wb") as out_handle:
                        while True:
                            chunk = fobj.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            written += len(chunk)
                            total_payload_bytes += len(chunk)
                            if total_payload_bytes > MAX_ARCHIVE_BYTES:
                                raise SecurityError(
                                    f"decompression bomb detected in {archive_path.name}"
                                )
                            digest.update(chunk)
                            out_handle.write(chunk)
                        out_handle.flush()
                    raw_mode = member.mode & 0o7777
                    extracted_entries[relpath] = FileEntry(
                        path=relpath,
                        type="file",
                        mode=mode_to_string(raw_mode),
                        sha256=digest.hexdigest(),
                    )
                else:
                    raise SecurityError(
                        f"unsupported tar entry type for {relpath!r}"
                    )
    except SecurityError:
        raise
    except (tarfile.TarError, OSError, UnicodeDecodeError) as exc:
        raise SecurityError(
            f"corrupt or malformed package archive {archive_path.name}: {exc}"
        ) from exc

    if meta_bytes is None:
        raise SecurityError(f"{archive_path.name} is missing METADATA.json")
    if files_bytes is None:
        raise SecurityError(f"{archive_path.name} is missing FILES.json")

    try:
        metadata = PackageMetadata.from_dict(json.loads(meta_bytes.decode("utf-8")))
        raw_files = json.loads(files_bytes.decode("utf-8"))
        if not isinstance(raw_files, list):
            raise ValidationError("FILES.json must be a JSON array")
        declared_files = tuple(FileEntry.from_dict(item) for item in raw_files)
    except Exception as exc:
        raise SecurityError(
            f"invalid metadata or manifest in {archive_path.name}: {exc}"
        ) from exc

    if metadata.name != expected_name or metadata.version != expected_version:
        raise SecurityError(
            f"package identity mismatch in {archive_path.name}: expected "
            f"{expected_name} {expected_version}, got {metadata.name} {metadata.version}"
        )

    if metadata.script_sha256 is not None:
        if install_script_bytes is None:
            raise SecurityError(
                f"{archive_path.name} declares script_sha256 but is missing INSTALL"
            )
        actual_script_sha = hashlib.sha256(install_script_bytes).hexdigest()
        if actual_script_sha != metadata.script_sha256:
            raise SecurityError(
                f"INSTALL scriptlet SHA-256 mismatch in {archive_path.name}: "
                f"expected {metadata.script_sha256}, got {actual_script_sha}"
            )

    size_ceiling = metadata.installed_size + MAX_DECOMPRESS_SLACK
    if metadata.installed_size > 0 and total_payload_bytes > size_ceiling:
        raise SecurityError(
            f"payload size ({total_payload_bytes} B) exceeds declared "
            f"installed_size ({metadata.installed_size} B) in {archive_path.name}"
        )

    declared_by_path = {entry.path: entry for entry in declared_files}
    if set(declared_by_path.keys()) != set(extracted_entries.keys()):
        missing = sorted(set(declared_by_path.keys()) - set(extracted_entries.keys()))
        extra = sorted(set(extracted_entries.keys()) - set(declared_by_path.keys()))
        raise SecurityError(
            f"payload files in {archive_path.name} do not match FILES.json "
            f"(missing={missing}, extra={extra})"
        )

    setuid_allow = set(metadata.setuid)
    verified_entries: list[FileEntry] = []

    for relpath, declared in sorted(declared_by_path.items()):
        validate_allowed_path(relpath, allowed_prefixes)
        actual = extracted_entries[relpath]
        if actual.type != declared.type:
            raise SecurityError(
                f"file type mismatch for {relpath!r}: expected {declared.type}, got {actual.type}"
            )
        staged_file = payload_dir / relpath
        if declared.type == "symlink":
            resolve_symlink_target(relpath, declared.target or "", allowed_prefixes)
            if actual.target != declared.target:
                raise SecurityError(
                    f"symlink target mismatch for {relpath!r}: "
                    f"expected {declared.target!r}, got {actual.target!r}"
                )
            verified_entries.append(declared)
        else:
            if actual.sha256 != declared.sha256:
                raise SecurityError(
                    f"SHA-256 mismatch for payload file {relpath!r}: "
                    f"expected {declared.sha256}, got {actual.sha256}"
                )
            mode_int = mode_from_string(declared.mode)
            if (
                mode_int & (stat.S_ISUID | stat.S_ISGID)
                and relpath not in setuid_allow
            ):
                mode_int &= ~(stat.S_ISUID | stat.S_ISGID)
            os.chmod(staged_file, mode_int)
            verified_entries.append(
                FileEntry(
                    path=relpath,
                    type="file",
                    mode=mode_to_string(mode_int),
                    sha256=declared.sha256,
                )
            )

    return StagedPackage(
        planned=planned,
        metadata=metadata,
        files=tuple(verified_entries),
        payload_dir=payload_dir,
        install_script=install_script,
    )


# ---------------------------------------------------------------------------
# Pacman Ownership Preflight
# ---------------------------------------------------------------------------


def check_pacman_ownership(
    relpaths: Sequence[str],
    *,
    root: Path,
    pacman_binary: str | None = None,
) -> None:
    """Verify none of ``relpaths`` are owned by a pacman package."""
    if not relpaths:
        return
    if root.resolve() != Path("/").resolve() and pacman_binary is None:
        return
    pacman = pacman_binary or shutil.which("pacman")
    if not pacman:
        return

    existing_abs = [
        str(root / rel)
        for rel in relpaths
        if (root / rel).exists() or (root / rel).is_symlink()
    ]
    if not existing_abs:
        return

    for abs_path in existing_abs:
        proc = subprocess.run(
            [pacman, "--root", str(root), "-Qo", abs_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            detail = proc.stdout.strip()
            raise SecurityError(
                f"refusing to overwrite pacman-owned file: {detail}"
            )


# ---------------------------------------------------------------------------
# Scriptlet Execution
# ---------------------------------------------------------------------------


def run_scriptlet(
    script_text: str | None,
    hook_name: str,
    *,
    pkg_name: str,
    pkg_version: str,
    root: Path,
    action: str,
    run_in_non_root: bool = False,
) -> int:
    """Execute ``hook_name`` from ``INSTALL`` inside a sanitized environment."""
    if not script_text or hook_name not in script_text:
        return 0
    if root.resolve() != Path("/").resolve() and not run_in_non_root:
        return 0

    runner = (
        "umask 022\n"
        f"{script_text}\n"
        f"if command -v {hook_name} >/dev/null 2>&1; then\n"
        f"  {hook_name}\n"
        f"fi\n"
    )
    clean_env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C",
        "LC_ALL": "C",
        "TITANPKG_PACKAGE": pkg_name,
        "TITANPKG_VERSION": pkg_version,
        "TITANPKG_ROOT": str(root),
        "TITANPKG_ACTION": action,
    }
    cwd = str(root) if root.exists() else "/"
    try:
        proc = subprocess.run(
            ["/bin/sh", "-e", "-c", runner],
            cwd=cwd,
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
            close_fds=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise TransactionError(
            f"INSTALL scriptlet {hook_name} for {pkg_name} timed out after 60s"
        ) from exc
    return proc.returncode


# ---------------------------------------------------------------------------
# Backup & Rollback State
# ---------------------------------------------------------------------------


@dataclass
class _BackupRecord:
    relpath: str
    existed: bool
    is_symlink: bool = False
    symlink_target: str = ""
    mode: int = 0o644
    backup_file: Path | None = None


@dataclass
class TransactionResult:
    """Summary of a completed transaction."""

    installed: tuple[InstalledPackage, ...] = ()
    removed: tuple[str, ...] = ()
    pacnew_files: tuple[str, ...] = ()
    pacsave_files: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Atomic File Placement & Directory Pruning
# ---------------------------------------------------------------------------


def _verify_no_symlink_ancestors(
    target_path: Path,
    install_root: Path,
    allowed_prefixes: Sequence[str],
) -> None:
    """Reject any ancestor directory inside ``install_root`` that is a symlink (TOCTOU guard)."""
    rel_parent = target_path.relative_to(install_root).parent
    cur = install_root
    for part in rel_parent.parts:
        cur = cur / part
        if cur.is_symlink():
            rel_cur = cur.relative_to(install_root).as_posix()
            raise SecurityError(
                f"refusing to write through symlinked parent directory /{rel_cur}"
            )


def _place_entry_atomically(
    entry: FileEntry,
    staged_source: Path,
    target_path: Path,
    *,
    install_root: Path,
    allowed_prefixes: Sequence[str],
) -> None:
    """Atomically install a regular file or symlink at ``target_path``."""
    _verify_no_symlink_ancestors(target_path, install_root, allowed_prefixes)
    rel_parent = target_path.relative_to(install_root).parent
    cur = install_root
    for part in rel_parent.parts:
        cur = cur / part
        if not cur.exists():
            cur.mkdir(parents=True, exist_ok=True)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if entry.type == "symlink":
        tmp_link = target_path.parent / f".{target_path.name}.titanpkg-{uuid.uuid4().hex[:8]}.tmp"
        with contextlib.suppress(FileNotFoundError):
            tmp_link.unlink()
        os.symlink(entry.target or "", tmp_link)
        os.replace(tmp_link, target_path)
    else:
        mode_int = mode_from_string(entry.mode)
        fd, tmp_str = tempfile.mkstemp(
            dir=str(target_path.parent),
            prefix=f".{target_path.name}.",
            suffix=".titanpkg-tmp",
        )
        try:
            with (
                os.fdopen(fd, "wb") as dst_handle,
                staged_source.open("rb") as src_handle,
            ):
                shutil.copyfileobj(src_handle, dst_handle, length=CHUNK_SIZE)
                dst_handle.flush()
                os.fsync(dst_handle.fileno())
            os.chmod(tmp_str, mode_int)
            os.replace(tmp_str, target_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_str)
            raise


def _prune_empty_parents(
    relpath: str,
    install_root: Path,
    allowed_prefixes: Sequence[str],
) -> None:
    """Remove empty parent directories of ``relpath`` while staying inside ``allowed_prefixes``."""
    parent_rel = Path(relpath).parent.as_posix()
    while parent_rel and parent_rel != ".":
        if not is_allowed_path(parent_rel, allowed_prefixes):
            break
        abs_dir = install_root / parent_rel
        if not abs_dir.exists() or abs_dir.is_symlink() or not abs_dir.is_dir():
            break
        try:
            abs_dir.rmdir()
        except OSError:
            break
        parent_rel = Path(parent_rel).parent.as_posix()


# ---------------------------------------------------------------------------
# Transaction Engine
# ---------------------------------------------------------------------------


def execute_transaction(
    plan: TransactionPlan,
    config: Config,
    db: Database,
    repos_by_name: dict[str, RepoConfig],
    *,
    offline: bool = False,
    run_scripts: bool = False,
    nosave: bool = False,
    keyring: Keyring | None = None,
    fail_after_files: int | None = None,
) -> TransactionResult:
    """Execute ``plan`` atomically with full backup and automatic rollback on failure."""
    if plan.is_empty:
        return TransactionResult()

    paths = config.paths
    kr = keyring or Keyring(paths)

    with transaction_lock(paths.lock_file):
        txn_id = f"{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
        txn_dir = paths.transactions_dir / txn_id
        stage_root = txn_dir / "stage"
        backup_root = txn_dir / "backup"
        txn_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(txn_dir, 0o700)

        # 1. Download & verify all required package archives
        staged_packages: list[StagedPackage] = []
        for item in plan.installs:
            repo_cfg = repos_by_name.get(item.record.repo)
            if repo_cfg is None:
                repo_cfg = RepoConfig(
                    name=item.record.repo,
                    servers=("file:///nonexistent",),
                    siglevel=config.siglevel,
                )
            archive_path = download_package(
                item.record,
                repo_cfg,
                paths,
                timeout=config.timeout,
                offline=offline,
                keyring=kr,
            )
            # 2. Quarantine-inspect & stage each archive
            staged = inspect_and_stage_tpkg(
                archive_path,
                stage_root / item.name,
                expected_name=item.name,
                expected_version=item.version,
                allowed_prefixes=config.allowed_prefixes,
                planned=item,
            )
            staged_packages.append(staged)

        # 3. Preflight: check inter-package file conflicts & pacman ownership
        removing_names = {pkg.name for pkg in plan.removes} | set(plan.replaced_names)
        batch_seen_paths: dict[str, str] = {}
        unowned_target_paths: list[str] = []

        for staged in staged_packages:
            pkg_file_paths = [entry.path for entry in staged.files]
            db_conflicts = db.find_conflicts(
                staged.metadata.name,
                pkg_file_paths,
                replacing=(*staged.planned.replaces, *removing_names),
            )
            if db_conflicts:
                c_path, c_owner = next(iter(db_conflicts.items()))
                raise SecurityError(
                    f"file conflict: {c_path!r} in {staged.metadata.name} "
                    f"is already owned by installed package {c_owner!r}"
                )
            for entry in staged.files:
                prev_batch_owner = batch_seen_paths.get(entry.path)
                if prev_batch_owner is not None and prev_batch_owner != staged.metadata.name:
                    raise SecurityError(
                        f"file conflict in transaction: {entry.path!r} is in both "
                        f"{prev_batch_owner!r} and {staged.metadata.name!r}"
                    )
                batch_seen_paths[entry.path] = staged.metadata.name
                if db.owner_of(entry.path) is None:
                    unowned_target_paths.append(entry.path)

        check_pacman_ownership(unowned_target_paths, root=paths.root)

        # 4. Collect all filesystem paths that may be touched & back them up
        paths_to_backup: set[str] = set()
        for rem_pkg in plan.removes:
            for entry in rem_pkg.files:
                paths_to_backup.add(entry.path)
                paths_to_backup.add(f"{entry.path}.pacsave")
        for rep_name in plan.replaced_names:
            rep_pkg = db.get(rep_name)
            if rep_pkg is not None:
                for entry in rep_pkg.files:
                    paths_to_backup.add(entry.path)
        for staged in staged_packages:
            old_pkg = db.get(staged.metadata.name)
            if old_pkg is not None:
                for entry in old_pkg.files:
                    paths_to_backup.add(entry.path)
            for entry in staged.files:
                paths_to_backup.add(entry.path)
                if entry.path in staged.metadata.backup:
                    paths_to_backup.add(f"{entry.path}.pacnew")

        backups: dict[str, _BackupRecord] = {}
        backup_root.mkdir(parents=True, exist_ok=True)
        for relpath in sorted(paths_to_backup):
            abs_target = paths.map(relpath)
            if abs_target.is_symlink():
                backups[relpath] = _BackupRecord(
                    relpath=relpath,
                    existed=True,
                    is_symlink=True,
                    symlink_target=os.readlink(abs_target),
                )
            elif abs_target.exists() and abs_target.is_file():
                b_file = backup_root / relpath
                b_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_target, b_file)
                st_mode = stat.S_IMODE(abs_target.lstat().st_mode)
                backups[relpath] = _BackupRecord(
                    relpath=relpath,
                    existed=True,
                    is_symlink=False,
                    mode=st_mode,
                    backup_file=b_file,
                )
            else:
                backups[relpath] = _BackupRecord(relpath=relpath, existed=False)

        # Snapshot in-memory DB state before mutation
        db_snapshot = json.loads(json.dumps(db.to_dict()))
        pacnew_created: list[str] = []
        pacsave_created: list[str] = []
        warnings: list[str] = []
        installed_results: list[InstalledPackage] = []
        removed_results: list[str] = []
        files_written_count = 0

        try:
            # 5a. Execute removals first
            for rem_pkg in plan.removes:
                backup_set = set(rem_pkg.backup)
                for entry in sorted(rem_pkg.files, key=lambda e: e.path, reverse=True):
                    abs_target = paths.map(entry.path)
                    if not abs_target.exists() and not abs_target.is_symlink():
                        continue
                    if (
                        not nosave
                        and entry.path in backup_set
                        and entry.type == "file"
                        and abs_target.is_file()
                        and not abs_target.is_symlink()
                    ):
                        disk_sha = sha256_file(abs_target)
                        if disk_sha != entry.sha256:
                            pacsave_rel = f"{entry.path}.pacsave"
                            pacsave_abs = paths.map(pacsave_rel)
                            os.replace(abs_target, pacsave_abs)
                            pacsave_created.append(pacsave_rel)
                            continue
                    abs_target.unlink()
                    _prune_empty_parents(
                        entry.path, paths.root, config.allowed_prefixes
                    )
                db.remove(rem_pkg.name)
                removed_results.append(rem_pkg.name)

            # 5b. Execute installs / upgrades / downgrades / reinstalls
            for staged in staged_packages:
                meta = staged.metadata
                action = staged.planned.action
                pre_hook = (
                    "pre_upgrade"
                    if action in ("upgrade", "downgrade", "reinstall")
                    else "pre_install"
                )
                post_hook = (
                    "post_upgrade"
                    if action in ("upgrade", "downgrade", "reinstall")
                    else "post_install"
                )
                run_scriptlet(
                    staged.install_script,
                    pre_hook,
                    pkg_name=meta.name,
                    pkg_version=meta.version,
                    root=paths.root,
                    action=action,
                    run_in_non_root=run_scripts,
                )

                old_installed = db.get(meta.name)
                old_files_by_path = (
                    old_installed.files_by_path if old_installed is not None else {}
                )
                new_paths = {entry.path for entry in staged.files}
                backup_set = set(meta.backup)

                for setuid_path in meta.setuid:
                    warnings.append(
                        f"preserving declared setuid bit on /{setuid_path} ({meta.name})"
                    )

                for entry in staged.files:
                    if (
                        fail_after_files is not None
                        and files_written_count >= fail_after_files
                    ):
                        raise OSError("simulated mid-transaction write failure")

                    abs_target = paths.map(entry.path)
                    staged_src = staged.payload_dir / entry.path

                    # Conffile (.pacnew) check
                    if (
                        entry.path in backup_set
                        and entry.type == "file"
                        and abs_target.exists()
                        and abs_target.is_file()
                        and not abs_target.is_symlink()
                    ):
                        old_entry = old_files_by_path.get(entry.path)
                        disk_sha = sha256_file(abs_target)
                        baseline_sha = (
                            old_entry.sha256 if old_entry is not None else entry.sha256
                        )
                        if disk_sha != baseline_sha and disk_sha != entry.sha256:
                            pacnew_rel = f"{entry.path}.pacnew"
                            pacnew_abs = paths.map(pacnew_rel)
                            _place_entry_atomically(
                                entry,
                                staged_src,
                                pacnew_abs,
                                install_root=paths.root,
                                allowed_prefixes=config.allowed_prefixes,
                            )
                            pacnew_created.append(pacnew_rel)
                            files_written_count += 1
                            continue

                    _place_entry_atomically(
                        entry,
                        staged_src,
                        abs_target,
                        install_root=paths.root,
                        allowed_prefixes=config.allowed_prefixes,
                    )
                    files_written_count += 1

                # Remove obsolete files from previous version or replaced packages
                obsolete_paths: set[str] = set(old_files_by_path.keys()) - new_paths
                for rep_name in staged.planned.replaces:
                    rep_pkg = db.get(rep_name)
                    if rep_pkg is not None:
                        obsolete_paths |= {e.path for e in rep_pkg.files} - new_paths
                        if rep_name not in removed_results:
                            removed_results.append(rep_name)

                for obs_rel in sorted(obsolete_paths, reverse=True):
                    obs_abs = paths.map(obs_rel)
                    if obs_abs.exists() or obs_abs.is_symlink():
                        obs_abs.unlink()
                        _prune_empty_parents(
                            obs_rel, paths.root, config.allowed_prefixes
                        )

                installed_pkg = InstalledPackage(
                    name=meta.name,
                    version=meta.version,
                    arch=meta.arch,
                    repo=staged.planned.record.repo,
                    reason=staged.planned.reason,
                    installed_at=now_iso(),
                    files=staged.files,
                    backup=meta.backup,
                )
                db.put(installed_pkg, replacing=staged.planned.replaces)
                installed_results.append(installed_pkg)

                rc = run_scriptlet(
                    staged.install_script,
                    post_hook,
                    pkg_name=meta.name,
                    pkg_version=meta.version,
                    root=paths.root,
                    action=action,
                    run_in_non_root=run_scripts,
                )
                if rc != 0:
                    warnings.append(
                        f"scriptlet {post_hook} for {meta.name} exited with status {rc}"
                    )

            # 6. Atomically commit installed.json LAST
            save_db(paths, db)
        except BaseException as exc:
            _rollback_filesystem(
                backups,
                install_root=paths.root,
                allowed_prefixes=config.allowed_prefixes,
            )
            restored = Database.from_dict(db_snapshot, default_arch=config.arch)
            db.packages = restored.packages
            db.generated = restored.generated
            db.extra_fields = restored.extra_fields
            db._rebuild_index()
            if isinstance(exc, (SecurityError, DatabaseError)):
                raise
            raise TransactionError(
                f"transaction failed and was rolled back cleanly: {exc}"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                shutil.rmtree(stage_root, ignore_errors=True)

        return TransactionResult(
            installed=tuple(installed_results),
            removed=tuple(removed_results),
            pacnew_files=tuple(pacnew_created),
            pacsave_files=tuple(pacsave_created),
            warnings=warnings,
        )


def _rollback_filesystem(
    backups: dict[str, _BackupRecord],
    *,
    install_root: Path,
    allowed_prefixes: Sequence[str],
) -> None:
    """Restore every backed-up file and remove newly created paths."""
    for relpath, rec in sorted(backups.items(), key=lambda item: item[0], reverse=True):
        abs_target = install_root / relpath
        with contextlib.suppress(OSError):
            if not rec.existed:
                if abs_target.exists() or abs_target.is_symlink():
                    abs_target.unlink()
                    _prune_empty_parents(relpath, install_root, allowed_prefixes)
            elif rec.is_symlink:
                abs_target.parent.mkdir(parents=True, exist_ok=True)
                if abs_target.exists() or abs_target.is_symlink():
                    abs_target.unlink()
                os.symlink(rec.symlink_target, abs_target)
            elif rec.backup_file is not None and rec.backup_file.exists():
                atomic_write_bytes(
                    abs_target, rec.backup_file.read_bytes(), mode=rec.mode
                )


# ---------------------------------------------------------------------------
# Live System Integrity Audit (titanpkg verify)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationIssue:
    package: str
    path: str
    # missing | type_mismatch | hash_mismatch | target_mismatch | mode_mismatch | modified_backup
    issue: str

    def to_dict(self) -> dict[str, str]:
        return {
            "package": self.package,
            "path": self.path,
            "issue": self.issue,
        }


def verify_installed(
    db: Database,
    config: Config,
    names: Sequence[str] = (),
) -> list[VerificationIssue]:
    """Audit on-disk files against ``installed.json`` SHA-256 hashes, modes and targets."""
    issues: list[VerificationIssue] = []
    targets = names if names else sorted(db.packages.keys())

    for pkg_name in targets:
        pkg = db.get(pkg_name)
        if pkg is None:
            issues.append(
                VerificationIssue(package=pkg_name, path="", issue="not_installed")
            )
            continue
        backup_set = set(pkg.backup)
        for entry in pkg.files:
            abs_path = config.paths.map(entry.path)
            if not abs_path.exists() and not abs_path.is_symlink():
                issues.append(
                    VerificationIssue(
                        package=pkg_name, path=entry.path, issue="missing"
                    )
                )
                continue

            if entry.type == "symlink":
                if not abs_path.is_symlink():
                    issues.append(
                        VerificationIssue(
                            package=pkg_name, path=entry.path, issue="type_mismatch"
                        )
                    )
                    continue
                actual_target = os.readlink(abs_path)
                if actual_target != entry.target:
                    issues.append(
                        VerificationIssue(
                            package=pkg_name, path=entry.path, issue="target_mismatch"
                        )
                    )
            else:
                if abs_path.is_symlink() or not abs_path.is_file():
                    issues.append(
                        VerificationIssue(
                            package=pkg_name, path=entry.path, issue="type_mismatch"
                        )
                    )
                    continue
                actual_sha = sha256_file(abs_path)
                if actual_sha != entry.sha256:
                    kind = (
                        "modified_backup"
                        if entry.path in backup_set
                        else "hash_mismatch"
                    )
                    issues.append(
                        VerificationIssue(
                            package=pkg_name, path=entry.path, issue=kind
                        )
                    )
                actual_mode = mode_to_string(stat.S_IMODE(abs_path.lstat().st_mode))
                if actual_mode != entry.mode:
                    issues.append(
                        VerificationIssue(
                            package=pkg_name, path=entry.path, issue="mode_mismatch"
                        )
                    )

    return issues
