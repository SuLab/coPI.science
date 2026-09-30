#!/usr/bin/env bash
# Compiles assets/css/app.css to static/css/app.min.css with a pinned,
# checksum-verified Tailwind v4 standalone binary.
#
# Why this exists rather than just relying on the Dockerfile's `css` stage:
# static/ is gitignored (it's a build artifact, not source), and the prod
# image bakes the compiled CSS in at build time via that stage. But the dev
# compose file bind-mounts the whole repo over /app, which shadows the
# image's static/css/app.min.css with the host's (empty) static/ tree — so
# without running this script on the host, dev and the Playwright test tier
# have no stylesheet at all and fail outright (not merely look unstyled).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

TAILWIND_VERSION=v4.3.3
TAILWIND_SHA256=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
TAILWIND_URL="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64"
BIN=".cache/tailwindcss/tailwindcss-${TAILWIND_VERSION}"

if [ ! -x "$BIN" ]; then
    mkdir -p "$(dirname "$BIN")"
    tmp="$(mktemp)"
    curl -fsSL -o "$tmp" "$TAILWIND_URL"
    echo "${TAILWIND_SHA256}  ${tmp}" | sha256sum -c -
    chmod +x "$tmp"
    mv "$tmp" "$BIN"
fi
# Re-verify on every run: .cache/ is a user-writable, untracked path, so a binary
# that passed once is not evidence about the bytes that are there now.
echo "${TAILWIND_SHA256}  ${BIN}" | sha256sum -c - >/dev/null

mkdir -p static/css
"$BIN" -i assets/css/app.css -o static/css/app.min.css --minify "$@"

size=$(wc -c < static/css/app.min.css)
css_version=$(sha256sum static/css/app.min.css | cut -c1-12)
echo "static/css/app.min.css: ${size} bytes, css_version=${css_version}"
