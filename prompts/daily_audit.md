# Daily Log Audit

Prompt for a scheduled Claude Code agent (or on-demand run) that summarizes
the last 24 hours of CoPI activity, flags likely bugs and wasteful LLM
behavior, and emails the result to the configured recipients.

---

You are auditing the CoPI agent simulation. Working directory: /home/ubuntu/copi-python.

GOAL
Produce a concise summary of the last 24 hours of activity and flag any
likely bugs or wasteful LLM behavior. Email the summary to asu@scripps.edu
and malanjary@scripps.edu.

WHAT TO EXAMINE

1. Agent run log — the currently-streaming file at logs/run_*.log
   (pick the most recently modified one in /home/ubuntu/copi-python/logs/).
   This is the main signal source. Only look at the last 24h of lines —
   parse timestamps inline; do NOT load the whole file. Useful filters:
     - grep -E "ERROR|Traceback|empty content|retry|rate.limit"
     - tail with --lines to scope by recency if timestamps are sparse
   Note: this file can be hundreds of MB. Stream with shell tools first,
   only Read narrow ranges.

2. Container logs (last 24h) for context:
     docker compose logs --since 24h app worker grantbot
   Focus on stack traces, non-2xx HTTP, repeated warnings.

3. Database sanity (optional, only if logs suggest data trouble):
     docker compose exec -T postgres psql -U copi -d copi -c "
       SELECT agent_id, COUNT(*) FROM agent_messages
       WHERE created_at > NOW() - INTERVAL '24 hours'
       GROUP BY agent_id ORDER BY 2 DESC;"

4. Uncommitted code changes — check whether the working tree has drifted
   from the last commit:
     git -C /home/ubuntu/copi-python status --short
     git -C /home/ubuntu/copi-python stash list
   If there are modified, staged, or untracked files (ignore the usual
   noise — logs/, data/, profiles/, *.log), note that uncommitted changes
   exist. Do NOT diff or read the contents; just report that they're there.

WHAT COUNTS AS A "BUG" OR WASTE

- Repeated tracebacks or unhandled exceptions
- "empty content" / empty-completion warnings from Claude (see commit
  0a61c57 — these are explicitly logged with agent, phase, tokens, prompt tail)
- Retries on the same prompt > 2 times
- A single agent burning disproportionate LLM calls vs peers
- Agents stuck in a tight loop (same phase repeated > N times in a row)
- Outbound emails that failed to send
- Worker jobs that errored or are stuck pending

OVERALL STATUS

Before composing the email, classify the audit into exactly one status.
This drives the subject-line prefix so the severity is visible in the
inbox without opening the message.

  - OK       → no issues, or only cosmetic noise
  - WARNING  → something looks off and deserves a human glance, but the
               system is still functioning (e.g. elevated empty-completion
               rate, one agent looping, a handful of failed jobs)
  - CRITICAL → something is actively broken or burning money (e.g.
               unhandled exceptions repeating, agent crash loop, runaway
               LLM spend, outbound email pipeline failing, DB errors)

Subject-line prefix mapping (use these exact glyphs):

  OK       → "✅ CoPI daily audit"
  WARNING  → "⚠️ CoPI daily audit"
  CRITICAL → "🚨🚨 CoPI daily audit"

OUTPUT FORMAT

Compose a plain-text email body with these sections:
  1. Headline: the chosen status (OK / WARNING / CRITICAL) plus a
     one-sentence justification
  2. Activity snapshot: total LLM calls (if extractable), per-agent counts,
     proposals created, emails sent
  3. Issues — bulleted, each with: severity (low/med/high), one-line
     description, file:line or log timestamp pointer so a human can dig in
  4. Wasteful-call candidates — agents/phases that look expensive relative
     to output
  5. Uncommitted changes — if the working tree has uncommitted changes
     (beyond the usual logs/data/profiles noise), state that they exist
     with a note to consider committing them. If the tree is clean, say so
     in one line or omit this section.
  6. Recommended next action (or "none")

Keep the whole body under ~400 lines. If there's truly nothing to say,
still send the email with status OK + the activity snapshot — a daily
heartbeat is the point.

HOW TO SEND THE EMAIL

Use the app container's existing SES config (already verified working).
Pass the body on stdin and the status-derived subject prefix via env var
so emoji glyphs survive the shell intact:

  PREFIX="✅ CoPI daily audit"   # or "⚠️ CoPI daily audit" / "🚨🚨 CoPI daily audit"
  BODY="...your composed body..."

  PREFIX="$PREFIX" docker compose exec -T -e PREFIX app python -c "
  import os, sys, datetime, boto3
  from src.config import get_settings
  body = sys.stdin.read()
  subject = os.environ['PREFIX'] + ' — ' + datetime.date.today().isoformat()
  s = get_settings()
  resp = boto3.client('ses', region_name=s.aws_region).send_email(
      Source=s.ses_sender_email,
      Destination={'ToAddresses': ['asu@scripps.edu', 'malanjary@scripps.edu']},
      Message={
          'Subject': {'Data': subject, 'Charset': 'UTF-8'},
          'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}},
      },
  )
  print('MessageId:', resp['MessageId'])
  " <<< "$BODY"

Confirm the SES MessageId in your final response so the run is traceable.
Do not modify any files in the repo. Read-only audit only.
