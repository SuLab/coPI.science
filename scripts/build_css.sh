#!/usr/bin/env bash
#
# Build static/css/app.css with the pinned Tailwind standalone CLI (spec §6.1, D4).
#
#   scripts/build_css.sh          rebuild static/css/app.css in place
#   scripts/build_css.sh --check  build to a temporary file; exit 1 if it differs from
#                                 the committed static/css/app.css (scripts/ci.sh runs this)
#
# The CLI is one self-contained binary, so the host needs no Node. It is downloaded once
# to .tools/ (gitignored) and refused unless its sha256 matches TAILWIND_SHA256; the
# check runs on every invocation, not only after a download. Only the linux-x64 asset is
# pinned, which is what the host and the CI machine run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TAILWIND_VERSION="v3.4.19"
TAILWIND_ASSET="tailwindcss-linux-x64"
TAILWIND_SHA256="4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e"
TAILWIND_URL="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${TAILWIND_ASSET}"
TOOLS_DIR="$REPO_ROOT/.tools"
CLI="$TOOLS_DIR/tailwindcss-${TAILWIND_VERSION}-linux-x64"
OUT="static/css/app.css"

mode="build"
case "${1:-}" in
  "") ;;
  --check) mode="check" ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
  echo "ERROR: only the ${TAILWIND_ASSET} asset is pinned; this is $(uname -s)-$(uname -m)." >&2
  exit 1
fi

cleanup_files=()
cleanup() { rm -f "${cleanup_files[@]}"; }
trap cleanup EXIT

verify() { printf '%s  %s\n' "$TAILWIND_SHA256" "$1" | sha256sum --check --status; }

if [ ! -f "$CLI" ] || ! verify "$CLI"; then
  mkdir -p "$TOOLS_DIR"
  download="$(mktemp "$TOOLS_DIR/.download.XXXXXX")"
  cleanup_files+=("$download")
  echo "    downloading Tailwind ${TAILWIND_VERSION} (${TAILWIND_ASSET})"
  curl -fsSL --retry 3 -o "$download" "$TAILWIND_URL"
  if ! verify "$download"; then
    echo "ERROR: ${TAILWIND_ASSET} ${TAILWIND_VERSION} does not match TAILWIND_SHA256; refusing to run it." >&2
    exit 1
  fi
  chmod +x "$download"
  mv "$download" "$CLI"
fi

build_to() {
  local target="$1" log
  log="$(mktemp)"
  cleanup_files+=("$log")
  if ! "$CLI" --config tailwind.config.js --input static/css/input.css \
      --output "$target" --minify >"$log" 2>&1; then
    cat "$log" >&2
    echo "ERROR: the Tailwind build failed." >&2
    exit 1
  fi
}

if [ "$mode" = "build" ]; then
  build_to "$OUT"
  echo "    wrote $OUT"
  exit 0
fi

fresh="$(mktemp)"
cleanup_files+=("$fresh")
build_to "$fresh"
if ! cmp -s "$fresh" "$OUT"; then
  echo "ERROR: $OUT is stale: a template, script or src/ string changed the set of" >&2
  echo "utility classes. Run scripts/build_css.sh and commit $OUT with that change." >&2
  exit 1
fi
echo "    $OUT matches a fresh build"
