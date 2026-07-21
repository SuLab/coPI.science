#!/usr/bin/env bash
#
# Install the local pre-push hook that runs scripts/ci.sh. There is no server-side CI;
# this hook is how the gate gets enforced. Idempotent — safe to re-run.
#
# Bypass the gate for a single push (use sparingly) with:  git push --no-verify
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_DIR="$REPO_ROOT/.git/hooks"
HOOK="$HOOK_DIR/pre-push"

mkdir -p "$HOOK_DIR"
cat > "$HOOK" <<'EOF'
#!/usr/bin/env bash
# Auto-installed by scripts/install-hooks.sh — runs the local CI gate before every push.
# Bypass once with: git push --no-verify
set -euo pipefail
exec "$(git rev-parse --show-toplevel)/scripts/ci.sh"
EOF
chmod +x "$HOOK"

echo "Installed pre-push hook -> scripts/ci.sh"
echo "Bypass a single push with: git push --no-verify"
