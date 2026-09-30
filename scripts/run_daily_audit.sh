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
