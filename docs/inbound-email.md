# Inbound email (reply-to-review) — architecture and runbook

PIs are emailed collaboration proposals and can answer by replying: a rating
(1–4) files a `ProposalReview`, instructions reopen the proposal for
refinement. This document covers how the pipeline works, why it was dead in
production, and how to bring it up safely.

## Architecture

```
PI hits "reply" ──► DNS MX (reply.copi.science)
                      └─► SES inbound SMTP (us-east-2) — receipt rule
                            └─► S3 s3://copi-inbound-email/inbound/<messageId>
                                  └─► worker poll_inbound_emails (every 60s,
                                      gated on ENABLE_INBOUND_EMAIL)
                                        └─► process_inbound_email:
                                            SES auth verdicts → auto-reply
                                            filter → token lookup → sender
                                            match → LLM classify →
                                            review / instruction / help email
```

Outbound review emails set `Reply-To: review+<token>@reply.copi.science`
(token = 64-char urlsafe secret stored on the `EmailNotification` row). The
worker deletes each S3 object after processing; objects that fail processing
3 times are quarantined under `failed/` for inspection.

## Why it was dead in production (investigated 2026-08-11)

Every layer below the outbound send was missing. In order of the mail's path:

1. **No MX record** on `reply.copi.science` (only an A record to the EC2
   box, which listens on no SMTP port) — PI replies bounced after their mail
   server gave up retrying.
2. **No S3 bucket**: `copi-inbound-email` did not exist in account
   215751090072.
3. **No SES receipt rule** delivering the reply domain to S3 (and the reply
   domain was not verified for receiving).
4. **Instance role** `copi-ec2-ses-role` has send-only SES perms and no S3
   read/delete on the inbound bucket.
5. **`ENABLE_INBOUND_EMAIL` unset** in the prod `.env`, so the worker never
   polled even if 1–4 had existed.

Meanwhile the outbound emails actively told PIs to reply (129 sent by
2026-08-06; outbound was then paused by disabling notification categories in
the DB).

## Code changes on the email-fix branch

- Outbound review/new-proposal/welcome emails only solicit replies (and only
  set `Reply-To` to the reply domain) when `ENABLE_INBOUND_EMAIL=true` —
  outbound email can be re-enabled safely before inbound is provisioned.
- The SEC-5 anti-spoofing gate trusts only the topmost (SES-stamped)
  `Authentication-Results` header; a sender-forged `...pass` header no longer
  overrides SES's fail verdicts.
- HTML-only replies fall back to tag-stripped HTML instead of being silently
  dropped.
- Auto-submitted mail (RFC 3834, e.g. out-of-office) is ignored — no help
  email is sent back, so no mail loops.
- The declared per-token rate limit (10 replies/hour) is enforced.
- A poison message is quarantined to `failed/` after 3 attempts instead of
  being retried every 60 seconds forever.

## Bringing inbound email up

Run each step with **admin** AWS credentials (the instance role cannot do
this — see finding 4):

```bash
# 1. See what's missing:
python scripts/setup_inbound_email.py --check

# 2. Create bucket, bucket policy, receipt rule set/rule; prints DNS + IAM steps:
python scripts/setup_inbound_email.py --provision
```

Then, in this order:

1. Add the printed DNS records at the registrar (Namecheap):
   `reply.copi.science. MX 10 inbound-smtp.us-east-2.amazonaws.com.` plus the
   `_amazonses` TXT verification record if the domain was newly verified.
2. Attach the printed S3 policy to `copi-ec2-ses-role`.
3. Re-run `--check` until all layers are OK.
4. **Prerequisite (land before this step, not after):** the worker must already claim its own
   canonical-id writer slot (`WRITER_WORKER` in `src/agent/ids.py`, claimed in
   `src/worker/main.py:main()`) — otherwise it mints PI-reply messages in the web app's residue
   class and a same-microsecond collision silently drops one of the two messages (the
   `uq_agent_messages_run_ts` conflict handler's resolution; unrecoverable now that the DB is the
   only durable store). Landed as part of #21 V11 — if this line still says "not yet landed" when
   you read it, stop and land it first.

   **Prerequisite (also land before this step):** the poller must survive a poison object and an
   odd-but-legitimate reply, because every failure here happens to a real PI's mail and there is no
   second copy. Four hardening items, all landed as part of #21 V3 — verify each is present before
   you flip the flag:
   - a malformed or unprocessable object is quarantined to `failed/` after
     `MAX_S3_PROCESS_ATTEMPTS` (3) consecutive failures (`src/services/email_inbound.py:33-34`)
     instead of being retried on every poll forever;
   - an unknown MIME charset (`charset=unknown-8bit` and friends) falls back instead of raising
     `LookupError` out of the decode (`_decode_part`, `:490-499`) — otherwise that PI's reply is
     quarantined and lost rather than read;
   - the LLM's `rating` is coerced to `int` and `bool` is rejected (`_coerce_rating`, `:563`), so a
     string rating cannot raise `TypeError` past the range guard;
   - the S3 listing is paginated with a bounded page count (`:193-195`, cap 20 pages = 1,000
     objects per poll), so a backlog cannot starve the tail of the bucket behind the first 50 keys.

   Two behaviours to expect once it is on, neither of which is a bug: a review or instruction is
   committed **before** the SES confirmation is sent, so a send failure never rolls back work the
   PI already did; and a failure *inside* the private-channel migration is terminal — the PI is
   e-mailed and told to use the dashboard rather than the object being retried. See deploy note 21
   in `docs/plans/2026-09-02-close-issues-20-27.md` for the one case that still retries (a full
   Postgres outage) and how to spot the orphan `priv-…` channels it can leave.

   Set `ENABLE_INBOUND_EMAIL=true` in the prod `.env` and recreate BOTH the
   worker (polling + proposal/reminder emails) and the app (the welcome email
   reads the same flag for its reply-vs-dashboard copy — recreating only the
   worker leaves new signups being told the dashboard is the only way in):
   `docker compose -f docker-compose.prod.yml -f docker-compose.override.yml up -d app worker`
   (`up -d` recreates on env change; a bare `docker restart` re-runs the OLD
   environment — `env_file` is resolved at container creation.)
5. End-to-end test: trigger a proposal notification to a test recipient,
   reply with "3 sounds great", and watch
   `docker logs -f copi-python-worker-1` for `Email review created`.
   Confirm the `proposal_reviews` row and the confirmation email.

Only after step 5 passes, re-enable the notification categories that were
turned off in the DB (`email_notification_preferences.enabled`) / user
frequencies as desired.

## Operational notes

- The reply flow degrades safely: with `ENABLE_INBOUND_EMAIL` unset/false the
  worker skips polling AND outbound emails stop soliciting replies.
- Quarantined mail lands in `s3://copi-inbound-email/failed/` — inspect and
  delete manually.
- The rate limiter and quarantine counters are in-memory; a worker restart
  resets them (by design — worst case is one extra processing round).
