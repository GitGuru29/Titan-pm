#!/usr/bin/env python3
"""Build one .tpkg from a manifest.

Usage:
    ./packaging/build-package.py --manifest packaging/manifests/titan-hwm.json \
        --source-root /path/to/airootfs --out dist/packages
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--version", default=None, help="override manifest version")
    parser.add_argument("--release", default=None, help="override manifest release")
    args = parser.parse_args()

    try:
        from titanpkg.builder.build_package import build_package
    except ImportError:
        print(
            "titanpkg: package builder is not implemented yet (Phase 5). "
            "See docs/IMPLEMENTATION-PLAN.md",
            file=sys.stderr,
        )
        return 1

    result = build_package(
        manifest_path=args.manifest,
        source_root=args.source_root,
        out_dir=args.out,
        arch=args.arch,
        version=args.version,
        release=args.release,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
