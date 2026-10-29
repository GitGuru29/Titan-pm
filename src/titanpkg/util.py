"""Small, dependency-free primitives shared by the manager.

Everything here is intentionally side-effect free unless the function name says
otherwise (``atomic_write_*``, ``download_file``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .errors import RepositoryError, SecurityError

USER_AGENT = f"titanpkg/{__version__}"
DEFAULT_TIMEOUT = 30
CHUNK_SIZE = 1024 * 1024


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_path(path: Path) -> str:
    """Hash a regular file, or the link text for a symlink."""
    if path.is_symlink():
        return sha256_bytes(b"symlink:" + os.readlink(path).encode("utf-8"))
    return sha256_file(path)


# ---------------------------------------------------------------------------
# Atomic writes / JSON
# ---------------------------------------------------------------------------


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` atomically (same-directory temp + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def atomic_write_json(path: Path, value: Any, mode: int = 0o644) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"), mode=mode)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_relpath(value: str) -> str:
    """Validate and normalize a package-relative POSIX path.

    Rejects absolute paths, NUL bytes, empty segments, ``.`` and ``..``.
    """
    if not isinstance(value, str) or not value:
        raise SecurityError("empty path")
    if "\x00" in value:
        raise SecurityError(f"NUL byte in path: {value!r}")
    if value.startswith("/"):
        raise SecurityError(f"absolute path not allowed: {value}")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise SecurityError(f"unsafe path component in {value!r}")
        if part.endswith("\\"):
            raise SecurityError(f"suspicious path component in {value!r}")
    return "/".join(parts)


def normalize_prefix(prefix: str) -> str:
    """Return a relative prefix with exactly one trailing slash."""
    cleaned = prefix.strip()
    if not cleaned:
        raise SecurityError("empty allowed prefix")
    cleaned = cleaned.replace("\\", "/").lstrip("/").rstrip("/")
    if not cleaned:
        raise SecurityError(f"invalid allowed prefix: {prefix!r}")
    for part in cleaned.split("/"):
        if part in ("", ".", ".."):
            raise SecurityError(f"invalid allowed prefix: {prefix!r}")
    return cleaned + "/"


def normalize_prefixes(prefixes: Iterable[str]) -> tuple[str, ...]:
    return tuple(normalize_prefix(prefix) for prefix in prefixes)


def is_allowed_path(relpath: str, prefixes: Sequence[str]) -> bool:
    """True when ``relpath`` is inside one of the normalized prefixes."""
    relpath = safe_relpath(relpath)
    return any(relpath == prefix.rstrip("/") or relpath.startswith(prefix) for prefix in prefixes)


def validate_allowed_path(relpath: str, prefixes: Sequence[str]) -> str:
    relpath = safe_relpath(relpath)
    if not is_allowed_path(relpath, prefixes):
        raise SecurityError(f"path outside allowed prefixes: {relpath}")
    return relpath


def resolve_symlink_target(
    link_relpath: str, target: str, prefixes: Sequence[str]
) -> str:
    """Validate a symlink target and return it unchanged when safe.

    Absolute targets are interpreted relative to the install root and must be
    inside an allowed prefix.  Relative targets must resolve inside one too.
    """
    link_relpath = safe_relpath(link_relpath)
    if not target:
        raise SecurityError(f"empty symlink target for {link_relpath}")
    if "\x00" in target:
        raise SecurityError(f"NUL byte in symlink target for {link_relpath}")

    if target.startswith("/"):
        resolved_rel = safe_relpath(target.lstrip("/"))
        if not is_allowed_path(resolved_rel, prefixes):
            raise SecurityError(
                f"symlink {link_relpath} escapes allowed prefixes: {target}"
            )
        return target

    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(link_relpath), target)
    )
    if resolved in ("", ".", "..") or resolved.startswith("../"):
        raise SecurityError(
            f"symlink {link_relpath} escapes the install root: {target}"
        )
    if not is_allowed_path(resolved, prefixes):
        raise SecurityError(
            f"symlink {link_relpath} escapes allowed prefixes: {target}"
        )
    return target


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https", "file"):
        raise RepositoryError(f"unsupported URL scheme: {parsed.scheme or '<none>'}")
    return url


def fetch_bytes(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_size: int | None = None,
) -> bytes:
    _check_url(url)
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if max_size is None:
                return response.read()
            data = response.read(max_size + 1)
            if len(data) > max_size:
                raise RepositoryError(f"response too large: {url}")
            return data
    except urllib.error.HTTPError as exc:
        raise RepositoryError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RepositoryError(f"cannot fetch {url}: {exc.reason}") from exc
    except OSError as exc:
        raise RepositoryError(f"cannot fetch {url}: {exc}") from exc


def download_file(
    url: str,
    destination: Path,
    timeout: float = DEFAULT_TIMEOUT,
    expected_sha256: str | None = None,
) -> str:
    """Stream ``url`` to ``destination`` atomically; return its SHA-256."""
    _check_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".part"
    )
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            os.fdopen(descriptor, "wb") as handle,
        ):
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise RepositoryError(
                f"checksum mismatch for {url}: expected {expected_sha256}, got {actual}"
            )
        os.replace(temporary, destination)
        return actual
    except urllib.error.HTTPError as exc:
        raise RepositoryError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RepositoryError(f"cannot fetch {url}: {exc.reason}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def mode_to_string(mode: int) -> str:
    return f"{mode & 0o7777:04o}"


def mode_from_string(value: str) -> int:
    try:
        return int(value, 8) & 0o7777
    except (TypeError, ValueError) as exc:
        raise SecurityError(f"invalid file mode: {value!r}") from exc


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
