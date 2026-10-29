"""Command line interface.

Phase 0 exposes ``--version`` and ``--help``.  The full command surface is
specified in README.md and implemented in Phase 4.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__

COMMANDS = (
    "update",
    "upgrade",
    "install",
    "reinstall",
    "remove",
    "autoremove",
    "list",
    "search",
    "info",
    "files",
    "verify",
    "rollback",
    "clean",
    "doctor",
    "key",
    "self-check",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="titanpkg",
        description="Standalone package manager for ArchTitan OS subsystems.",
        epilog="Documentation: docs/IMPLEMENTATION-PLAN.md",
    )
    parser.add_argument("--version", action="version", version=f"titanpkg {__version__}")
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=None,
        help="operate on a sandbox root (testing / imaging)",
    )
    parser.add_argument(
        "--noconfirm",
        action="store_true",
        help="assume yes for all prompts",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use cached repository indexes only",
    )
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        help="command to run (see README.md)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    print(
        f"titanpkg: '{args.command}' is not implemented yet "
        "(scaffold phase; see docs/IMPLEMENTATION-PLAN.md)",
        file=sys.stderr,
    )
    return 1
