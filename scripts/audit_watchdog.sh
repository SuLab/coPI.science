#!/usr/bin/env bash
# Dead-man's switch for the daily audit (prompts/daily_audit.md, cron 07:00 UTC).
#
# scripts/run_daily_audit.sh writes logs/.audit-heartbeat only when the audit
# both exited cleanly and actually sent (a new "MessageId:" line). If that file
# goes stale, either the audit did not run or it ran and sent nothing. Both are
# invisible: a missing audit mail looks exactly like a quiet day, and since
# dormancy no longer raises an alarm, most days are quiet by design.
#
# Not hypothetical. logs/daily-audit.log holds 12 consecutive days of
# "OAuth session expired" (2026-08-23..09-03) that nobody noticed. The log kept
# growing throughout, so its mtime would NOT have caught it — only a heartbeat
# written on success does.
#
# Shares nothing with the audit but that file: no Claude, no OAuth, no LLM.
# Mail goes through the app container's SES config. That is a shared dependency,
# so every failure to send is ALSO written to syslog (tag copi-audit-watchdog),
# because this script's own stdout goes to a log nobody reads.
set -uo pipefail

# cron gives user crontabs a minimal PATH; name the dirs the tools live in.
export PATH="/usr/local/bin:/usr/bin:/bin"

REPO="${AUDIT_WATCHDOG_REPO:-/home/ubuntu/copi-python}"
HEARTBEAT="$REPO/logs/.audit-heartbeat"
AUDIT_LOG="$REPO/logs/daily-audit.log"
CONTAINER="${AUDIT_WATCHDOG_CONTAINER:-copi-python-app-1}"
LOCK="${AUDIT_WATCHDOG_LOCK:-/tmp/copi-audit-watchdog.lock}"
# Set to a single address to test without mailing the real recipient list.
OVERRIDE_TO="${AUDIT_WATCHDOG_TO:-}"

# 24h, not 26h: the audit finishes ~07:06 and this runs 09:00, so one missed
# audit puts the heartbeat at ~25.9h. A 26h limit would have called that OK and
# waited another full day before saying anything.
MAX_AGE_HOURS="${AUDIT_HEARTBEAT_MAX_AGE_HOURS:-24}"
[[ "$MAX_AGE_HOURS" =~ ^[0-9]+$ ]] || MAX_AGE_HOURS=24

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# Second channel. Alerting depends on the app container and SES; syslog does not.
note() { logger -t copi-audit-watchdog -- "$1" 2>/dev/null || true; }

# One at a time: a double cron fire must not double-send.
# Brace-group the redirect: `exec 9>file 2>/dev/null` would send the SHELL's
# stderr to /dev/null for the rest of the run, swallowing every failure message
# the cron log is supposed to capture.
{ exec 9>"$LOCK"; } 2>/dev/null || true
flock -n 9 2>/dev/null || { echo "$(stamp) another watchdog run holds the lock; exiting"; exit 0; }

now=$(date -u +%s)

if [[ -f "$HEARTBEAT" ]]; then
    last=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo "")
    if [[ -z "$last" ]]; then
        age_label="unreadable"
        detail="Heartbeat file $HEARTBEAT exists but could not be stat'd."
    elif (( last > now + 3600 )); then
        # Clock skew or a bad write. Never let a future timestamp read as fresh
        # forever — that would disable the switch silently.
        age_label="future-dated"
        detail="Heartbeat $HEARTBEAT is dated in the future ($(date -u -d "@$last" +%FT%TZ 2>/dev/null)).
Treating as stale: a future timestamp would otherwise look fresh indefinitely."
    else
        age_h=$(( (now - last) / 3600 ))
        if (( age_h < MAX_AGE_HOURS )); then
            echo "$(stamp) OK: audit heartbeat is ${age_h}h old (limit ${MAX_AGE_HOURS}h)"
            exit 0
        fi
        age_label="${age_h}h"
        detail="Last successful audit send was ${age_h}h ago (limit ${MAX_AGE_HOURS}h).
Heartbeat contents: $(head -c 300 "$HEARTBEAT" 2>/dev/null)"
    fi
else
    age_label="never"
    detail="No heartbeat file at $HEARTBEAT.
Either the audit has not completed a successful send since the watchdog was
installed, or something removed the file."
fi

subject="🚨 CoPI daily audit MISSING — no successful send (${age_label})"
body="The daily audit has not reported a successful send.

${detail}

This alert comes from scripts/audit_watchdog.sh on the host, not from the audit
itself, so it still fires when the audit cannot start at all — expired Claude
OAuth token, cron not running, or the audit sending nothing.

What to check, in order:
  1. crontab -l                      — is the 07:00 UTC audit entry still there?
  2. tail -40 ${AUDIT_LOG}
     Repeated 'OAuth session expired' means the Claude credential needs renewing.
  3. claude -p 'ping' in ${REPO}     — confirms the CLI can authenticate.
  4. docker ps                       — app container must be up for the audit to send.
  5. tail -20 ${REPO}/logs/audit-watchdog.log and
     journalctl -t copi-audit-watchdog --since '3 days ago'

Last 25 lines of ${AUDIT_LOG}:
$(tail -n 25 "$AUDIT_LOG" 2>/dev/null || echo '(audit log unreadable)')"

fail() {
    echo "$(stamp) ALERT NOT SENT ($1): audit heartbeat ${age_label} stale" >&2
    note "ALERT NOT SENT ($1): audit heartbeat ${age_label} stale"
    exit 1
}

# `docker inspect` succeeds on a STOPPED container, so absence of an error here
# does not mean the container can run anything — check State.Running explicitly.
running=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo "missing")
case "$running" in
    true)  ;;
    false) fail "container $CONTAINER exists but is not running" ;;
    *)     fail "container $CONTAINER not found, or docker unavailable" ;;
esac

if printf '%s' "$body" | docker exec -i \
        -e WD_SUBJECT="$subject" -e WD_TO="$OVERRIDE_TO" "$CONTAINER" python -c '
import os, sys, boto3
from src.config import get_settings
s = get_settings()
to = [a.strip() for a in os.environ["WD_TO"].split(",") if a.strip()] or s.audit_recipient_list
resp = boto3.client("ses", region_name=s.aws_region).send_email(
    Source=s.ses_sender_email,
    Destination={"ToAddresses": to},
    Message={"Subject": {"Data": os.environ["WD_SUBJECT"], "Charset": "UTF-8"},
             "Body": {"Text": {"Data": sys.stdin.read(), "Charset": "UTF-8"}}},
)
print("MessageId:", resp["MessageId"], "To:", to)
'; then
    echo "$(stamp) ALERT SENT: audit heartbeat ${age_label} stale"
    note "ALERT SENT: audit heartbeat ${age_label} stale"
    exit 0
else
    fail "SES send via $CONTAINER failed"
fi
