#!/usr/bin/env bash
# Cron wrapper for the daily audit (prompts/daily_audit.md), 07:00 UTC.
#
# Exists to own the heartbeat. The audit is an LLM run, so "the audit will write
# a file when it succeeds" is a request, not a guarantee: it can forget, or write
# it before sending. Here the decision is mechanical — the heartbeat lands only
# if claude exited 0 AND this run appended a new "MessageId:" line to the audit
# log. scripts/audit_watchdog.sh alerts when that heartbeat goes stale.
set -uo pipefail
export PATH="/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin"

REPO="${AUDIT_REPO:-/home/ubuntu/copi-python}"
PROMPT="$REPO/prompts/daily_audit.md"
LOG="$REPO/logs/daily-audit.log"
HEARTBEAT="$REPO/logs/.audit-heartbeat"
CLAUDE="${AUDIT_CLAUDE_BIN:-/home/ubuntu/.local/bin/claude}"

cd "$REPO" || { echo "$(date -u +%FT%TZ) FATAL: cannot cd to $REPO" >&2; exit 1; }
mkdir -p "$REPO/logs"

# Prefer the API key over the interactive OAuth login: /logout (2026-09-16) and
# token expiry (2026-08-23..09-03) have both silenced the audit before. The CLI
# honors ANTHROPIC_API_KEY and it does not expire. Read from .env, never committed.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -f "$REPO/.env" ]]; then
    ANTHROPIC_API_KEY=$(grep -E '^ANTHROPIC_API_KEY=' "$REPO/.env" | tail -n1 | cut -d= -f2- | tr -d "\"' \r")
    [[ -n "$ANTHROPIC_API_KEY" ]] && export ANTHROPIC_API_KEY
fi
# The API org is HIPAA-regulated without Zero Data Retention; the CLI otherwise
# sends the context_management beta and gets 400. Verified 2026-09-17: with this
# unset the call fails, with it set opus[1m] at xhigh answers normally.
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

# Count MessageId lines before the run so we can tell this run's send from any
# earlier one. A failed run that logs nothing leaves the count unchanged.
before=$(grep -c 'MessageId' "$LOG" 2>/dev/null || echo 0)

"$CLAUDE" -p --permission-mode auto < "$PROMPT" >> "$LOG" 2>&1
rc=$?

after=$(grep -c 'MessageId' "$LOG" 2>/dev/null || echo 0)

if [[ $rc -eq 0 && $after -gt $before ]]; then
    printf '%s rc=0 messageid_lines=%s\n' "$(date -u +%FT%TZ)" "$((after - before))" > "$HEARTBEAT"
    echo "$(date -u +%FT%TZ) audit OK — heartbeat written ($((after - before)) MessageId line(s))"
    exit 0
fi

# No heartbeat: the watchdog will notice within a day. Say why here too.
if [[ $rc -ne 0 ]]; then
    echo "$(date -u +%FT%TZ) audit FAILED rc=$rc — heartbeat NOT written" >&2
else
    echo "$(date -u +%FT%TZ) audit exited 0 but sent nothing (no new MessageId) — heartbeat NOT written" >&2
fi
exit 1
