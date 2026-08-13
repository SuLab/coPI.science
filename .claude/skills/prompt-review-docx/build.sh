#!/usr/bin/env bash
# Build styled Word review docs from prompt-set markdown.
#
#   .claude/skills/prompt-review-docx/build.sh [input.md ...]
#
# With no arguments it rebuilds every docs/specs/*bot-prompts.md. Output is
# written next to each source as <name>.docx (gitignored). pandoc is fetched to
# a local cache on first run if it is not already installed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="$HERE/.cache"
PANDOC_VER=3.10.2

# --- locate or fetch pandoc (no system install / sudo needed) ---
PANDOC="$(command -v pandoc 2>/dev/null || find "$CACHE" -type f -name pandoc 2>/dev/null | head -1 || true)"
if [ -z "${PANDOC:-}" ] || [ ! -x "$PANDOC" ]; then
  os=$(uname -s); arch=$(uname -m); mkdir -p "$CACHE"
  case "$os-$arch" in
    Linux-x86_64)  asset="pandoc-$PANDOC_VER-linux-amd64.tar.gz" ;;
    Linux-aarch64) asset="pandoc-$PANDOC_VER-linux-arm64.tar.gz" ;;
    Darwin-arm64)  asset="pandoc-$PANDOC_VER-arm64-macOS.zip" ;;
    Darwin-x86_64) asset="pandoc-$PANDOC_VER-x86_64-macOS.zip" ;;
    *) echo "Please install pandoc — no prebuilt binary for $os-$arch." >&2; exit 1 ;;
  esac
  url="https://github.com/jgm/pandoc/releases/download/$PANDOC_VER/$asset"
  echo "fetching pandoc $PANDOC_VER for $os-$arch ..." >&2
  case "$asset" in
    *.tar.gz) curl -fsSL "$url" | tar xz -C "$CACHE" ;;
    *.zip)    curl -fsSL "$url" -o "$CACHE/p.zip" && unzip -oq "$CACHE/p.zip" -d "$CACHE" && rm -f "$CACHE/p.zip" ;;
  esac
  PANDOC="$(find "$CACHE" -type f -name pandoc | head -1)"
fi
[ -x "$PANDOC" ] || { echo "pandoc unavailable" >&2; exit 1; }

# --- inputs ---
if [ "$#" -gt 0 ]; then inputs=("$@"); else inputs=(docs/specs/*bot-prompts.md); fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for md in "${inputs[@]}"; do
  [ -f "$md" ] || { echo "skip (not found): $md" >&2; continue; }
  out="${md%.md}.docx"
  python3 "$HERE/transform.py"   "$md"           "$tmp/t.md"
  # -raw_html keeps literal <slack_message>/<assessment_json> tags verbatim;
  # +fenced_divs enables the ::: promptbody wrapper.
  "$PANDOC" "$tmp/t.md" -f markdown-raw_html+fenced_divs -o "$tmp/raw.docx"
  python3 "$HERE/postprocess.py" "$tmp/raw.docx" "$out"
  echo "built  $out"
done
