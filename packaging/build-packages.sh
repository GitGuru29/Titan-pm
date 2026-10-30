#!/usr/bin/env bash
# Build every manifest in dependency order, then regenerate the repository.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

SOURCE_ROOT=""
OUT="$root/dist/packages"
REPO="$root/dist/repo"
ARCH="x86_64"
SIGN_KEY=""
SIGN_PACKAGES=0
BUILD_REPO=1

usage() {
  cat <<EOF
Usage: $0 --source-root PATH [options]

  --source-root PATH   root containing the installed ArchTitan tree
  --out PATH           package output directory (default: dist/packages)
  --repo PATH          repository output directory (default: dist/repo)
  --arch ARCH          package architecture (default: x86_64)
  --sign-key KEYID     sign the repository index (and packages if --sign-packages)
  --sign-packages      sign every .tpkg
  --no-repo            only build packages
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-root) SOURCE_ROOT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    --sign-key) SIGN_KEY="$2"; shift 2 ;;
    --sign-packages) SIGN_PACKAGES=1; shift ;;
    --no-repo) BUILD_REPO=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$SOURCE_ROOT" ]]; then
  echo "error: --source-root is required" >&2
  usage >&2
  exit 2
fi

# Dependency order: shared design system and themes first.
ORDER=(
  titan-ui
  archtitan-themes
  archtitan-tools
  archtitan-guard
  titan-hwm
  titan-sandbox
  titan-sysmon
  titan-media-hud
  titan-quicksettings
  titan-spotlight
  titan-powermenu
  titan-workspace-hud
  titan-hotzones
  titan-menu
  titan-settings
  titanfetch
  titan-browser
  titanpkg
)

mkdir -p "$OUT"
for name in "${ORDER[@]}"; do
  manifest="$here/manifests/$name.json"
  [[ -f "$manifest" ]] || { echo "skip: no manifest for $name"; continue; }
  echo "==> building $name"
  pkg_src="$SOURCE_ROOT"
  tmp_stage=""
  if [[ "$name" == "titanpkg" && ! -f "$SOURCE_ROOT/usr/local/bin/titanpkg" ]]; then
    tmp_stage="$(mktemp -d)"
    mkdir -p "$tmp_stage/usr/local/bin" "$tmp_stage/usr/local/lib/titanpkg" "$tmp_stage/etc/titanpkg/repos.d"
    cp -R "$root/src/titanpkg" "$tmp_stage/usr/local/lib/titanpkg/"
    find "$tmp_stage/usr/local/lib/titanpkg" -name '__pycache__' -type d -prune -exec rm -rf {} +
    cat > "$tmp_stage/usr/local/bin/titanpkg" <<'LAUNCHER'
#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/titanpkg")
from titanpkg.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
LAUNCHER
    chmod 0755 "$tmp_stage/usr/local/bin/titanpkg"
    ln -sf titanpkg "$tmp_stage/usr/local/bin/tpac"
    cp "$root/config/titanpkg.json" "$tmp_stage/etc/titanpkg/titanpkg.json"
    cp "$root/config/repos.d/archtitan.json.example" "$tmp_stage/etc/titanpkg/repos.d/archtitan.json"
    pkg_src="$tmp_stage"
  fi
  python3 "$here/build-package.py" \
    --manifest "$manifest" \
    --source-root "$pkg_src" \
    --out "$OUT" \
    --arch "$ARCH"
  [[ -n "$tmp_stage" ]] && rm -rf "$tmp_stage"
done

if [[ "$BUILD_REPO" == "1" ]]; then
  mkdir -p "$REPO"
  repo_args=(--repo "$REPO" --packages "$OUT" --arch "$ARCH" --name archtitan)
  [[ -n "$SIGN_KEY" ]] && repo_args+=(--sign-key "$SIGN_KEY")
  [[ "$SIGN_PACKAGES" == "1" ]] && repo_args+=(--sign-packages)
  python3 "$here/build-repo.py" "${repo_args[@]}"
fi
