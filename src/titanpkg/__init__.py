"""titanpkg — a standalone package manager for ArchTitan OS subsystems.

The public entry point is :func:`titanpkg.cli.main`.  The package deliberately
depends only on the Python standard library at runtime.
"""

from __future__ import annotations

from collections.abc import Sequence

__version__ = "0.1.0"
__all__ = ["__version__", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface."""
    from .cli import main as _main

    return _main(argv)
