#!/usr/bin/env python3
"""Regenerate (and optionally sign) titanpkg.db.json.

Usage:
    ./packaging/build-repo.py --repo dist/repo --arch x86_64 \
        --packages dist/packages --sign-key KEYID
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--packages", required=True, type=Path)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--name", default="archtitan")
    parser.add_argument("--sign-key", default=None)
    parser.add_argument("--sign-packages", action="store_true")
    args = parser.parse_args()

    try:
        from titanpkg.builder.build_repo import build_repo
    except ImportError:
        print(
            "titanpkg: repository builder is not implemented yet (Phase 5). "
            "See docs/IMPLEMENTATION-PLAN.md",
            file=sys.stderr,
        )
        return 1

    result = build_repo(
        repo_dir=args.repo,
        packages_dir=args.packages,
        arch=args.arch,
        name=args.name,
        sign_key=args.sign_key,
        sign_packages=args.sign_packages,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
