# Daily Log Audit

Prompt for a scheduled Claude Code agent (or on-demand run) that summarizes
the last 24 hours of CoPI activity, flags likely bugs and wasteful LLM
behavior, and emails the result to the configured recipients.

---

You are auditing the CoPI agent simulation. Working directory: /home/ubuntu/copi-python.

GOAL
Produce a concise summary of the last 24 hours of activity, flag faults and
wasteful LLM spend, and report roster engagement. Email the summary to the
recipients configured in `AUDIT_RECIPIENTS` (see HOW TO SEND THE EMAIL
below) — do not hardcode addresses here or in the send command.

"Flag" means report. Severity is decided only by the OVERALL STATUS
procedure below, which is the single authority on it. Agent dormancy is
never a fault — see AGENT DORMANCY IS NOT A BUG.

WHAT TO EXAMINE

1. Agent run log — read it live from the container:
     docker logs agent-run --since 24h
   This is the main signal source, and the only one that observes the
   simulation process at all. Do NOT use logs/run_*.log: those are snapshots
   saved at a restart, not a streaming file. Useful filters:
     - grep -E "ERROR|Traceback|empty content|retry|rate.limit"
     - grep -c "=== Turn"   — liveness, see Step 1 of OVERALL STATUS
   Note: this can be hundreds of MB. Stream with shell tools first, only
   Read narrow ranges.

2. Container logs (last 24h) for context:
     docker logs --since 24h copi-python-app-1
     docker logs --since 24h copi-python-worker-1
     docker logs --since 24h copi-python-grantbot-1
   Focus on stack traces, non-2xx HTTP, repeated warnings. `docker compose`
   is broken in this tree ("invalid project"), and that compose service list
   omits the simulation anyway — agent-run is a one-off container, not a
   compose service.

3. Database sanity (optional, only if logs suggest data trouble):
     docker exec copi-python-postgres-1 psql -U copi -d copi -c "
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

5. Human user activity (last 24h) — distinct from agent/LLM activity, this
   is what the real PIs and their delegates did. Run the roll-up below
   against postgres. (docker compose is typically broken in this tree —
   "invalid project"; use `docker exec` against the postgres container
   directly, e.g. `docker exec copi-python-postgres-1 psql -U copi -d copi`.)
   All column names below are verified against the live schema.

     docker exec copi-python-postgres-1 psql -U copi -d copi -c "
     SELECT a AS activity, cnt, latest FROM (
       SELECT 1 ord, 'logins'                a, COUNT(*) cnt, MAX(last_login_at) latest FROM users WHERE last_login_at > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 2, 'new_users (claimed)',      COUNT(*), MAX(claimed_at)   FROM users WHERE claimed_at  > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 3, 'profile_edits',            COUNT(*), MAX(updated_at)   FROM researcher_profiles WHERE updated_at > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 4, 'profile_revisions(human)', COUNT(*), MAX(created_at)   FROM profile_revisions WHERE created_at > NOW() - INTERVAL '24 hours' AND changed_by_user_id IS NOT NULL
       UNION ALL SELECT 5, 'proposal_ratings',         COUNT(*), MAX(reviewed_at)  FROM proposal_reviews WHERE reviewed_at > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 6, 'graph_votes',              COUNT(*), MAX(created_at)   FROM proposal_votes WHERE created_at > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 7, 'email_responses',          COUNT(*), MAX(responded_at) FROM email_notifications WHERE responded_at > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 8, 'delegate_accepts',         COUNT(*), MAX(accepted_at)  FROM delegate_invitations WHERE accepted_at > NOW() - INTERVAL '24 hours'
       UNION ALL SELECT 9, 'waitlist_signups',         COUNT(*), MAX(created_at)   FROM waitlist_signups WHERE created_at > NOW() - INTERVAL '24 hours'
     ) t ORDER BY ord;"

   What each row means / where it comes from:
     - logins              — users.last_login_at (set in src/routers/auth.py on OAuth login)
     - new_users (claimed) — users.claimed_at (a seeded PI claimed their profile via OAuth)
     - profile_edits       — researcher_profiles.updated_at (any profile row touched)
     - profile_revisions   — profile_revisions where changed_by_user_id IS NOT NULL
                             (human edits only; agent/pipeline edits have NULL).
                             Optionally break down by `mechanism` ('web' vs 'slack_dm').
     - proposal_ratings    — proposal_reviews.reviewed_at; the PI's 1–5 rating of a
                             proposal. Optionally group by `rating` and `submitted_via`
                             ('web' vs 'email') to see how they're rating and via which
                             channel.
     - graph_votes         — proposal_votes (lightweight up/down votes on the public
                             collaboration graph; may be anonymous visitors).
     - email_responses     — email_notifications.responded_at (a PI replied to a
                             proposal/review email).
     - delegate_accepts    — delegate_invitations.accepted_at (someone accepted a
                             delegate invite).
     - waitlist_signups    — waitlist_signups.created_at (web UI early-access requests).

   For anything non-zero and interesting (e.g. ratings came in, a profile was
   edited), it's fine to drill into who/what with a targeted follow-up query —
   but keep it brief. If every row is 0, just say "no human user activity in
   the window" in the snapshot.

6. Roster engagement — how much of the roster is waiting on PI review. This
   is REPORTED, never escalated (see AGENT DORMANCY IS NOT A BUG).

     docker exec copi-python-postgres-1 psql -U copi -d copi -c "
     WITH active AS (SELECT agent_id FROM agents WHERE status='active' AND agent_id <> 'schultz'),
     sides AS (
       SELECT td.id, td.thread_id, td.decided_at, s.agent_id
       FROM thread_decisions td
       CROSS JOIN LATERAL (VALUES (td.agent_a),(td.agent_b)) AS s(agent_id)
       WHERE td.outcome='proposal' AND s.agent_id IN (SELECT agent_id FROM active)),
     latest AS (
       SELECT DISTINCT ON (agent_id, thread_id) agent_id, thread_id, id, decided_at
       FROM sides ORDER BY agent_id, thread_id, decided_at DESC NULLS LAST),
     roots AS (
       SELECT DISTINCT ON (message_ts) message_ts, content, posted_at
       FROM agent_messages WHERE message_ts IS NOT NULL ORDER BY message_ts, posted_at),
     blocking AS (
       SELECT l.agent_id, l.decided_at
       FROM latest l LEFT JOIN roots r ON r.message_ts = l.thread_id
       WHERE NOT EXISTS (SELECT 1 FROM proposal_reviews pr
                         WHERE pr.thread_decision_id = l.id AND pr.agent_id = l.agent_id)
         AND NOT COALESCE(r.content LIKE '%:moneybag:%'
                  AND to_timestamp(r.posted_at) > NOW() - INTERVAL '14 days', FALSE)),
     per_agent AS (
       SELECT a.agent_id, COUNT(b.agent_id) AS n
       FROM active a LEFT JOIN blocking b ON b.agent_id = a.agent_id GROUP BY a.agent_id)
     SELECT (SELECT COUNT(*) FROM active)                    AS active_non_exempt,
            COUNT(*) FILTER (WHERE n >= 2)                   AS gated_upper_bound,
            COUNT(*) FILTER (WHERE n = 1)                    AS one_away,
            COUNT(*) FILTER (WHERE n = 0)                    AS free,
            (SELECT COUNT(*) FROM blocking)                  AS backlog_sides,
            (SELECT MIN(decided_at)::date FROM blocking)     AS oldest_unreviewed,
            (SELECT MAX(reviewed_at)::date FROM proposal_reviews
               WHERE submitted_via <> 'auto')                AS last_human_review
     FROM per_agent;"

   Reading it, and the traps:

   - `backlog_sides`, `oldest_unreviewed` and `last_human_review` are exact.
     They are the product signal — report these as the headline numbers.
   - `gated_upper_bound` is an UPPER BOUND on agents gated by unreviewed
     proposals, not a live count. Label it as such. `_rebuild_agent_state`
     runs once at process start, so agents added to the roster later
     (`grep "\[roster\] Added newly-active agent" `) carry no restored
     proposals and are gated only by proposals decided after they joined.
   - Do NOT add a date filter. The rebuild at `simulation.py:4250-4255`
     selects `outcome='proposal'` with no date and no run-id bound.
     `REBUILD_WINDOW_S` (`simulation.py:214`) bounds only MessageLog
     hydration (`:3696`) and has nothing to do with proposals. Scoping this
     query to 14 days understates it roughly 4x and has produced two wrong
     CRITICAL emails and two corrections.
   - `submitted_via <> 'auto'` is the correct "human review" filter.
     `reviewed_by_user_id IS NOT NULL` is NOT: operator-inserted placeholder
     rows carry a reviewer id, and early genuine web reviews do not.
   - The query covers the proposal gate only. The active-thread gate
     (`simulation.py:2049`) reads in-memory state and is invisible to SQL.
   - `active_non_exempt` is the active roster minus the one exempt agent
     (`UNBLOCK_EXEMPT_AGENTS`, `simulation.py:219`), so it reads one lower
     than the roster size. Say which you are quoting.
   - The `COALESCE(..., FALSE)` on the funding filter is load-bearing. A
     proposal whose thread root is missing from `agent_messages` (a `--fresh`
     run wipes messages but keeps proposals, `src/agent/main.py:163-172`)
     leaves the LEFT JOIN columns NULL; without the COALESCE the predicate
     is NULL and the row is silently dropped from the backlog.

WHAT COUNTS AS A "BUG" OR WASTE

- Repeated tracebacks or unhandled exceptions
- "empty content" / empty-completion warnings from Claude (see commit
  0a61c57 — these are explicitly logged with agent, phase, tokens, prompt tail)
- Retries on the same prompt > 2 times
- A single agent burning disproportionate LLM *spend* vs peers. Turn
  allocation is not spend: an agent taking many turns without reaching an
  LLM call costs nothing and is not a bug.
- An agent looping: the same phase making repeated LLM calls without
  progress. Turns that end without an LLM call are not a loop — see AGENT
  DORMANCY IS NOT A BUG.
- Outbound emails that failed to send, or an email category that has stopped
  sending while it still has queued work
- Worker jobs that errored or are stuck pending

AGENT DORMANCY IS NOT A BUG

Agents going quiet is designed behaviour and an engagement signal, not a
fault, and not something a system administrator can fix. It must never set
the status. None of the following is a bug, an "issue", or grounds to
escalate — no matter how many agents it affects or how long it has lasted:

- Agents blocked from new posts by unreviewed proposals — `blocked_for_regular`
  (`src/agent/simulation.py:2058`), threshold `unreviewed_proposal_block_count`
  = 2 (`src/config.py:340`), bail-out at `simulation.py:2129`
- Agents at the active-thread threshold (`active_thread_threshold`,
  `simulation.py:2049`)
- Agents at the daily post cap (`daily_post_cap`, `simulation.py:2044`)
- Agents held by the state-change gate (`simulation.py:1020`) — nothing new
  to react to and the spontaneous timer not yet due
- Low or zero posts, threads or proposals in the window, fewer participating
  agents than yesterday, or turns that complete without an LLM call — WHEN a
  gate above accounts for it. Zero output with no gate that explains it is
  NOT dormancy; it is a fault, and belongs in Step 2.
- Zero human PI activity, and an unreviewed-proposal backlog that grows
- A roster-wide version of any of the above

All of these bail out via `logger.debug` while the process runs at INFO
(`src/agent/main.py:22-25`), so a dormant roster legitimately produces a log
with nothing in it. Silence in the log is not evidence of a fault.

A blocked roster is success seen from the wrong angle: agents are blocked
*because* they produced proposals, and the backlog grows as the roster grows.
The only action any of this warrants is a human reviewing proposals — never a
restart, a rebuild, a throttle or a code change. Report it in the Roster
engagement section and leave the status alone.

Two qualifications, so this is not read more broadly than it is meant:

- Dormancy is exempt ONLY once liveness is proven (Step 1 below). "Quiet
  because dormant" and "quiet because dead" produce identical silence in
  every source this prompt names, and the simulation has no healthcheck, no
  restart policy and no heartbeat row — this email is its only monitor.
- Dormancy is not literally free: every unreviewed proposal thread is polled
  every 30s (`PROPOSAL_POLL_INTERVAL`, `simulation.py:148`), so the backlog
  costs Slack API budget even at zero LLM spend. Report that; do not escalate
  on it.

Do not record a memory that contradicts this section, and do not treat an
older memory that predates it as authority to escalate dormancy.

OVERALL STATUS

Before composing the email, classify the audit into exactly one status. This
drives the subject-line prefix so severity is visible in the inbox without
opening the message.

Standing-issue rule, applied to every step below: a defect an earlier audit
already reported, that has not materially changed, is STANDING. It goes in
the Standing issues section with its age and does NOT set the status. It
re-raises the status only if it is new this window, has a new symptom, has a
wider blast radius, a materially worse rate, or a fix for it regressed.
"Wider blast radius" means a new class of affected component — not growth in
a count that was already reported, so a larger backlog or more latched users
is NOT a widening. "Materially worse rate" IS a re-raise, though: a per-call
or per-hour rate that has roughly doubled against its own recent baseline is
a changed defect, not the same one bigger. Judge rates as rates — a refusal
rate going 1.2% -> 4.5% re-raises even though "refusals" was already on the
list.

Never standing, however many times it has been reported:
  - anything in Step 1 — a dead process is escalated every day it is dead;
  - any ongoing condition still causing loss: failing sends, failing worker
    jobs, data corruption, DB errors, or spend over the Step 3 thresholds.
    Those re-raise every day they persist.
STANDING is for a defect whose damage is done or bounded — the review-email
latch, for instance, whose ongoing effect is a backlog that grows, which is
itself exempt.

Work these steps in order; take the first status that applies.

Step 1 — Liveness. CRITICAL if any of:
  - any of copi-python-{postgres,app,worker,grantbot,nginx,certbot}-1, or the
    agent-run container, is not running, is restarting, or has exited
    (`docker ps -a`)
  - agent-run is up but `docker logs agent-run --since 1h | grep -c "=== Turn"`
    returns literally 0. Only zero. A merely lower rate is expected as the
    backlog grows — each turn polls every open proposal thread — and is not a
    finding. (Observed 2026-09-16: 1,609 turns/24h, hourly range 57-113.)
  - repeated unhandled exceptions or a crash loop
  - DB connection errors, or Slack auth failures (`invalid_auth`,
    `account_inactive`, `token_revoked`). Grep the FULL log for these, not
    just `--since 24h`: `slack_client.py:454` logs an auth failure once, at
    connect, so on a process that has been up for weeks a dead token is
    invisible in a 24h window — and its only other symptom, an agent that
    silently stops, is exempt as dormancy.
  The dormancy exemption never applies to this step.

Step 2 — Faults, on direct evidence only — an error, a failed send, a stuck
row. Never inferred from low output:
  - CRITICAL: SES sends failing; an email category with queued work sending
    zero; worker jobs failing; data corruption; DB errors
  - WARNING: elevated empty-completion or refusal rate; JSON parse failures;
    retries > 2; a handful of failed jobs; isolated Slack transport errors —
    but NOT Slack 429 / rate-limit retries, which scale with the unreviewed
    backlog (a 30s poll per open proposal thread) and are therefore a
    dormancy symptom, not a fault
  - Also direct evidence, not inference: a phase that should be firing sitting
    at ~0 calls while others run normally — e.g. `thread_reply` near zero
    against `scan` in the thousands over 7 days — is a broken activation path,
    not dormancy. Check the 7-day phase mix in `llm_call_logs` before
    concluding that a quiet window is merely gated.

Step 3 — Cost. Compute the trailing 7-day median of daily input tokens from
`llm_call_logs` and compare this window against it:
  - CRITICAL above 4x the median (runaway spend)
  - WARNING above 2x, or one agent/phase is a spend outlier vs peers
  - `llm_call_logs` is a FLOOR, not the total: GrantBot writes no rows to it
    (verified 2026-09-16 — 0 grantbot rows in 7 days, and the only phases
    present are the simulation's scan/new_post/prune/thread_reply/memory),
    yet it makes roughly 30 Anthropic calls a day. So a GrantBot-side runaway
    is invisible to this step. Say that the figure excludes GrantBot rather
    than presenting it as total spend, and sanity-check GrantBot's call
    volume from its container log.
  - CRITICAL regardless of the ratio if daily input tokens exceed 15M. The
    ratio alone cannot catch sustained overspend: a step change that persists
    lifts the median above it within about four days, after which the ratio
    reads 1.0 and the alarm clears itself. For scale, the 7-day median has
    run 2.4-4.4M/day; the 2026-09-04 spike was 34M.
  Normal spend on a quiet day is OK. Low output is never a cost finding. The
  no-op share is structurally high and must never be escalated on: grouped by
  phase, `COUNT(*) FILTER (WHERE output_tokens <= 40)` has run ~0% for prune
  and ~86-90% for scan, and no-op calls have accounted for ~68-73% of all
  input tokens. Report it; it is the resting state of this system.

Step 4 — Otherwise OK. A window with no liveness failure, no fault and no
cost anomaly is OK even if the simulation produced nothing at all. Use the
idle glyph below when that is the case, so the regime stays visible without
raising an alarm.

Subject-line prefix mapping (use these exact glyphs):

  OK                → "✅ CoPI daily audit"
  OK, roster idle   → "✅ CoPI daily audit (idle)"
  WARNING           → "⚠️ CoPI daily audit"
  CRITICAL          → "🚨🚨 CoPI daily audit"

"Roster idle" means the window produced no proposals and no meaningful
posting activity. It is still an OK status — the parenthetical exists so a
genuine return to activity is visible at subject level.

OUTPUT FORMAT

Compose a plain-text email body with these sections:
  1. Headline: the chosen status (OK / WARNING / CRITICAL) plus a
     one-sentence justification
  2. Activity snapshot: total LLM calls (if extractable), per-agent counts,
     proposals created, emails sent
  3. User activity (human PIs) — the roll-up from examination step 5:
     logins, new claimed users, profile edits, proposal ratings, graph
     votes, email responses, delegate accepts, waitlist signups. Give the
     counts; call out anything notable (e.g. ratings that came in, a profile
     that was edited). If every row is 0, one line: "no human user activity
     in the window." This section always appears — it's part of the
     heartbeat even on a quiet day.
  4. Roster engagement — the roll-up from examination step 6. ALWAYS
     appears and NEVER sets the status. Lead with the exact numbers:
     proposal-sides awaiting review, the oldest one, and the date of the
     last genuine human review. Then the gated-agent count, explicitly
     labelled an upper bound. One sentence on the trend since the previous
     audit — get the previous figures from `logs/daily-audit.log` (it has no
     per-line timestamps; locate prior runs with
     `grep -n "CoPI daily audit —" logs/daily-audit.log`). Write it for the
     product owner: this is review throughput, not an incident.
  5. Issues — faults found in THIS window only, bulleted, each with:
     severity (low/med/high), one-line description, and a file:line or log
     timestamp pointer so a human can dig in. Dormancy items never appear
     here — they belong in section 4. A "high" item here means a fault that
     Step 1 or Step 2 already escalated; do not use severity to smuggle in
     a finding the status procedure declined to raise.
  6. Standing issues — defects an earlier audit already reported that are
     still unfixed. One line each, with an age ("review-email latch, unfixed
     27 days"). Source the age from where the defect was recorded — your
     project memory directory for durable findings, or the dated subject
     lines in `logs/daily-audit.log`. Do not estimate an age you cannot
     source. These do not set the status; they exist so nothing rots
     invisibly.
  7. Wasteful-call candidates — agents/phases that look expensive relative
     to output. Informational only: this section never sets the status, and
     a high no-op share is the normal resting state of this system. Only the
     Step 3 comparison against the 7-day median can raise a cost alarm.
  8. Uncommitted changes — if the working tree has uncommitted changes
     (beyond the usual logs/data/profiles noise), state that they exist
     with a note to consider committing them. If the tree is clean, say so
     in one line or omit this section.
  9. Recommended next action (or "none"). If the only finding is dormancy,
     the action is "none (PI review throughput)" — never a restart, a
     rebuild or a throttle.

Keep the whole body under ~400 lines. If there's truly nothing to say,
still send the email with status OK + the activity snapshot — a daily
heartbeat is the point.

HOW TO SEND THE EMAIL

Use the app container's existing SES config (already verified working).
The recipient list is NOT hardcoded here — it comes from the
`AUDIT_RECIPIENTS` setting (`src/config.py`, comma-separated; parsed by
`settings.audit_recipient_list`). Override it by setting `AUDIT_RECIPIENTS`
in `.env`; to change it permanently, edit the default in `src/config.py`.

Pass the body on stdin and the status-derived subject prefix via env var
so emoji glyphs survive the shell intact:

  # one of: "✅ CoPI daily audit" / "✅ CoPI daily audit (idle)"
  #          "⚠️ CoPI daily audit"  / "🚨🚨 CoPI daily audit"
  PREFIX="✅ CoPI daily audit (idle)"
  BODY="...your composed body..."

  PREFIX="$PREFIX" docker exec -i -e PREFIX copi-python-app-1 python -c "
  import os, sys, datetime, boto3
  from src.config import get_settings
  body = sys.stdin.read()
  subject = os.environ['PREFIX'] + ' — ' + datetime.date.today().isoformat()
  s = get_settings()
  resp = boto3.client('ses', region_name=s.aws_region).send_email(
      Source=s.ses_sender_email,
      Destination={'ToAddresses': s.audit_recipient_list},
      Message={
          'Subject': {'Data': subject, 'Charset': 'UTF-8'},
          'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}},
      },
  )
  print('MessageId:', resp['MessageId'], 'To:', s.audit_recipient_list)
  " <<< "$BODY"

(`docker compose exec` fails in this tree — "invalid compose project" — so
address the container by name.)

Confirm the SES MessageId and the recipient list in your final response so
the run is traceable.

THE HEARTBEAT (for your awareness — you do NOT write it)

`scripts/run_daily_audit.sh`, the cron wrapper that invoked you, writes
`logs/.audit-heartbeat` after you exit — and only if you exited cleanly AND
this run appended a new `MessageId:` line to `logs/daily-audit.log`. So print
the SES MessageId in your final response, as instructed above: that line is
what proves the send happened.

`scripts/audit_watchdog.sh` (cron, 09:00 UTC) emails an alert if that
heartbeat goes stale. It is the only thing that notices when this audit does
not run at all — which happened silently for 12 days, 2026-08-23 to 09-03,
when the Claude OAuth token expired. Do not write or touch the heartbeat file
yourself; doing so would defeat it.

Do not modify any file in the repo. This audit is read-only.
