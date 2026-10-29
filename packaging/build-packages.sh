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
  python3 "$here/build-package.py" \
    --manifest "$manifest" \
    --source-root "$SOURCE_ROOT" \
    --out "$OUT" \
    --arch "$ARCH"
done

if [[ "$BUILD_REPO" == "1" ]]; then
  mkdir -p "$REPO"
  repo_args=(--repo "$REPO" --packages "$OUT" --arch "$ARCH" --name archtitan)
  [[ -n "$SIGN_KEY" ]] && repo_args+=(--sign-key "$SIGN_KEY")
  [[ "$SIGN_PACKAGES" == "1" ]] && repo_args+=(--sign-packages)
  python3 "$here/build-repo.py" "${repo_args[@]}"
fi
