"""Arch-compatible version comparison.

Planned for Phase 1 (see docs/IMPLEMENTATION-PLAN.md).  The contract is::

    vercmp("1.0-1", "1.0-2")  -> -1
    vercmp("1:1.0", "2.0")    ->  1
    vercmp("1.0~rc1", "1.0")  -> -1
"""

from __future__ import annotations


def vercmp(a: str, b: str) -> int:
    """Return -1, 0 or 1 comparing two ``epoch:pkgver-pkgrel`` strings."""
    # TODO(phase-1): implement libalpm-compatible epoch/version/pkgrel splitting
    # and alternating numeric/alpha segment comparison.  Add the pactest cases.
    raise NotImplementedError("vercmp is implemented in Phase 1")
