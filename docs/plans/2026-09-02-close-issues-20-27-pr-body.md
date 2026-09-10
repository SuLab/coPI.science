# Close issues #20–#27: engine, worker, profiles, clients, web, data, docs, deploy

Implements `docs/plans/2026-09-02-close-issues-20-27.md` (108 tasks, Parts 0/20–27/M/R) on top of `copi-prod` @ 18ba52c.
Every task was implemented by a subagent, reviewed by a second one against the plan text, and the whole branch was then
audited per issue against the issue's own item list (`.superpowers/sdd/2026-09-02-close-issues-20-27/audit-issue-NN.md`,
summarized below). **No file under `prompts/` changed and no inline model-facing string changed** (Decision D33; every
reviewer hashed the prompt strings in the files it touched before and after).

## What

- **#20 engine (Tasks 20.1–20.22, 20.9b, 20.9c):** no phantom rows on failed posts; dead-thread eviction purges the log and tombstones the thread; `✅` finalizes only the most recent `:memo:`; shared `_is_finalize_marker`; idempotent `_close_thread` keyed on `thread_id`; scan cursor from the log high-water mark; Phase-3 activation resets the reply budget; PI-owned proposal clearing with an implicit `rating=-1` `ProposalReview` (readers filter it: badge, review e-mail, dashboard form, digest, `/admin/agents`, discussions panel); durable reopen state (`thread_decisions.reopened_at`, migration 0028); per-agent transport-error isolation in `_poll_pi_dms`; guarded inbound handlers and DMs; LLM-log flush re-queue; Phase-5 replies use the real channel; `visibility` stamped on all writers; shared `src/agent/mentions.py` (case-insensitive tags, real `<@Uxxx>`); mid-turn rate check with headroom slicing; roster re-add restores state from the DB; daily cap bypass for PI/funding/private candidates (actionable only); `has_pi_directive`/`pi_context` cleared only when consumed; `interesting_posts` swap restored in `finally`; web PI-message writer accepts only its own agent's threads.
- **#21 worker + e-mail (21.1–21.13):** `WRITER_WORKER` canonical-id slot; rollback-before-record failure path; `failed` status (no `completed_at`), retry backoff; stale-`processing` reaper (3600 s); inbound e-mail: unknown-charset guard, `int` rating coercion, paginated S3 listing, commit before the SES confirmation, failed instruction post notifies the PI (terminal inside the private-channel migration, retried before it), D6 upsert of the implicit review; sweeps commit per item with re-load, `EmailNotification` created after SES accepts, unanswered review notifications expire (`EMAIL_NOTIFICATION_EXPIRY_DAYS=14`), any responder retires the whole agent's outstanding notifications.
- **#22 profiles (22.1–22.14):** null-safe ORCID parsing; migration 0025 dedups `publications` and adds `uq_publications_user_pmid`; in-run dedup; `itertext()` for PubMed; `_validate_profile`/`apply_synthesis` type-safe gate shared with the four operator scripts; form-presence gating (no more `Form("")` blanking); `synthesis_validated` reset on PI saves; dead `pending_profile` reader removed; `atomic_write.py` at every disk writer + DB-before-disk on the private save; private export falls back to the seed and clears the file when both columns are empty (agent picks the deletion up live); SQL-side atomic `profile_version` and `delegate_slack_ids` updates.
- **#23 external clients (23.1–23.15):** bounded `parse_retry_after`; nested `display_name`; GrantBot hard-fails selection and refuses to post without its own token (DB-first, `.env` last tier, uid re-probed on rotation); dead helpers removed; one shared FOA pattern; apostrophe classes and long-reply protection in the ack detector; `funding_reject_count` reset; case-insensitive bot tags in the funding summarizer; `http_retry.py` for ORCID/grants; NCBI retry + process-wide lock-free pacing cursor + key-sized concurrency; budgets charged only on success; delegate-invite token logging.
- **#24 web races (24.1–24.4):** waitlist and `review_proposal` IntegrityError guards spanning `add`→`commit`; `slack_provisioning` `_async` twins with capped Retry-After; `admin_provisioning` releases the DB connection before every Slack call.
- **#25 data integrity (25.1–25.6):** CASCADE on PCM rows (0026) + `passive_deletes` on 11 relationships; 409 on user-delete IntegrityError; FK/composite indexes (0027) with model parity; `/static` + `/api/health` short-circuit in the badge middleware; `make_engine()` pool defaults for all five engines; stale `running` simulation runs reconciled at startup.
- **#26 docs + roster (26.1–26.13):** posthog `tojson`; spec/README/AGENT.md/CLAUDE.md factual corrections; lab count from `/admin/agents`; one-time `backfill_slack_ts.py` paragraph; live roster sync (rename, token rotation, removal, uid-map flush); numeric-suffix agent-id fallback with 409 on the request race; `backfill_agents.py` + `generate_sparsedata_user.py` parity; public `extract_json`.
- **#27 deploy (27.1–27.14) + M.1–M.3:** `.dockerignore` closes the secret/bloat hole (`backups/`, `.env*`, `profiles`, tests, `.git`); `/api/health` probes the DB (503) with a bounded timeout; `migrate` one-shot service gates app/worker/grantbot; hash-pinned `requirements.lock` with upper caps, gate fails on lock drift; deps-before-src, multi-stage build, non-root UID 10001, bytecode baked; nginx comment/cache cleanup, devel/blackbird rate limits, CSP-Report-Only, 300 s provisioning timeout; resource limits (postgres uncapped, D24); mypy ceiling ratchet; preflight/postflight know the 0025–0028 chain; `run_migration.sh --via-run`; `docs/production-migration.md` §10 routine path.

## What (2026-09-08 audit fix wave — `docs/plans/2026-09-08-audit-fixes.md`)

An adversarial audit of this branch (five sonnet passes confirmed by opus, live copi-test runs) found
defects the closure paperwork had missed. Root causes and fixes, each with a red-first test:

- **RC-1 (#20 COR-5, HIGH).** The DB/web PI path authorized by thread membership. `agent_messages.sender_user_id`
  (migration `0030`) is stamped by every writer; review clearing, `pi_context`, `has_pi_directive`, the reopen target
  and the `@Bot` route are gated on the agents the sender owns or is a delegate for. NULL-sender rows get no side effect.
- **RC-2 (HIGH).** PI messages/DMs written while `agent-run` was down lost every side effect. `pi_inbound_state='pending'`
  is stamped on insert; `pending`/`ingested` rows are recovered independently of the cursor with a bounded attempt count;
  `pi_dm_messages.handled_at` (`0030`, backfilled) replaces the in-memory seen-set.
- **RC-3 (#23 V7).** Slack rate-limit retry is a 180 s wait budget with an 8-attempt ceiling.
- **RC-4 (#21 V4-3).** Reply tokens expire at the consumer (14 days from the latest send), every resend rotates the token,
  a superseded token gets a bounce to a known user only.
- **RC-5 (#27 I5).** Enforcing CSP subset (`frame-ancestors/base-uri/object-src`) + Report-Only with `report-uri
  /api/csp-report` (sanitised, size-capped, own nginx rate zone); json-file log rotation in the override file.
- **RC-6 (#27 I2).** `scripts/redeploy.sh` stops app/worker, runs migrate, checks its exit code by container label, then
  restarts and reloads nginx; runbooks point at it.
- **RC-7.** PI standing instructions persist DB-first; a failed disk write no longer clobbers the DB or lies in the ack.
- **RC-8 (#25 D1).** Admin user delete refuses to orphan an active/pending agent (owner + users-row locks).
- **RC-9.** Parked threads are evicted after `PARKED_THREAD_MAX_TURNS`; `post_failure_count` re-derived on rebuild for
  Slack-rooted threads.
- **RC-10 (#27 I2).** `/api/health`'s retry is bounded by the remaining deadline.
- **RC-11 (#22).** ORCID present-but-null containers no longer raise.
- **RC-13.** `COPI_PROFILES_DIR` setting, lazily resolved; live-tier preflight check 6 refuses an unwritable or
  nonexistent profiles dir.
- **RC-14.** `migrate_public_thread_to_private`'s Slack calls run via `asyncio.to_thread`.
- **RC-15.** A new post whose channel the model omitted or that the engine does not know is refused instead of being
  defaulted to `#general` (found by the real-LLM live run).

## What NOT (deliberately not planned — reasons in each Part's coverage matrix in the plan doc)

- **#20:** COR-1a, COR-10(2), COR-9c (already fixed / agreed no-op / hygiene-only); E6(3) cosmetic; in-process `_dead_thread_ids` tombstone rather than a durable eviction marker; restart residual on a capped thread's late joiner (20.8); D25 single-loss of PI triggers on a handler failure (logged at ERROR).
- **#21:** V11-g (accurate; backfill scripts never mint); D23 `_handle_instruction`'s remaining pre-commit side effects shared with the web reopen route.
- **#22:** V1-15f (D27 follow-up: backfill `evidence_pub_count`), V1-16e/f/g (closed as a side effect of 0025 + in-run dedup), V6-22b/22c (documented design), V6-24c/24e (D28 follow-up: pipeline export after the worker's commit)/24f; D8 `pending_profile` column kept, dropped in a later migration.
- **#23:** V7a/b/c, COR-26e, V10a already fixed; COR-26b'/b'' (covered by hard-fail selection / not reproducible); COR-28d cross-ref; COR-29d context; D11 NSF-style FOA numbers out of scope.
- **#24:** P1 single uvicorn worker (architectural premise), P2 general 120 s timeout (handled per-location by 27.12); D29 true-concurrency fixture for the V5 tests (fake-session tests stand in).
- **#25:** D21 the 10 alembic-only indexes / 3 placeholder unique constraints stay documented in `__table_args__` comments, ORM parity in a follow-up PR; D30 `/static` from nginx is a follow-up.
- **#26:** A1, A2, A5, C3, C5 (by design / already fixed / owned by #21); D31 consolidate `simulation.py`'s private JSON extractor onto `extract_json`; D32 AGENT.md gets a superseding Decisions-Log entry.
- **#27:** I1-g no GitHub Actions workflow (D17: CI stays local — `scripts/ci.sh` run by the pre-push hook is the whole gate); D18 CSP stays Report-Only; D20 300 s provisioning timeout on org1 only; D24 postgres limits in a separate window.

## Residual risks and known gaps

Sixty items, produced by the eight Phase 7 audits and then re-verified against the code after the fix wave landed
(three were dropped as fully fixed, ten narrowed). Everything here is deliberate: nothing in this list is an unknown.

### Behaviour changes an operator will notice

- A PI channel post containing the raw `<@USUBOT1>` Slack uid form now reserves the thread to that agent, exactly like the literal `@SuBot` text, and `@SUBOT`-style casing now matches too (#20).
- The engine's implicit `rating = -1` review row is now upgraded in place (rating/comment/user ids/`submitted_via`/`reviewed_at`) on the first explicit web or e-mail action instead of a new row being inserted; reader filters for this exist in `main.py`, `email_notifications.py` (×2), `agent_page.py`, and `admin.py` (×2) (#20).
- `EMAIL_NOTIFICATION_EXPIRY_DAYS` now closes the reply window at 14 days and marks the notification `'expired'`; a resend now reuses the original notification's `reply_token` instead of rotating it (71b8db3, #21 I2), so a reply addressed to the superseded reminder still resolves instead of being dropped silently (#21).
- A pipeline run no longer clobbers a disk-only hand-edited `profiles/private/{agent_id}.md`: before Step 9b, `run_profile_pipeline` now adopts the file into `profile.private_profile_md` whenever both `private_profile_md` and `private_profile_seed` are empty (`profile_pipeline.py:522-527`, b3cf228, #22 COR-23). The narrower residual: once the DB already holds any private-profile content (a live md, or a previously generated seed), `export_private_profile` (`profile_export.py:155-174`) unconditionally rewrites disk from that DB content on every subsequent pipeline run, so a hand-edit made directly to the exported file after that point is still silently overwritten. Agent self-edits are safe: `pi_handler.py:146` persists them to the DB first (#22).
- Two overlapping pipeline runs for the same user now fail the second job on a unique-constraint violation instead of silently double-inserting; it self-heals on retry at the cost of one extra NCBI pass (#22).
- `/api/health` now fails closed and `docker-compose.prod.yml:217-219` makes nginx `depends_on: app: condition: service_healthy`, so a degraded Postgres now blocks an nginx (re)start where it previously would not (#25).
- Deleting a user now hard-deletes their `private_channel_members` rows instead of nulling `user_id`, even though that table has a `removed_at` soft-removal column used for this exact pattern elsewhere — the audit trail of which PI was in which private channel is destroyed rather than preserved (#25).

### Accepted residual risks

- A thread's Phase-3-activated reply budget does not survive an engine restart consistently: `_rebuild_agent_state`/`_rebuild_one_agent_state` give a still-open **reopened** thread a fresh `max_thread_messages` budget on every restart (`offset = msg_count`, `simulation.py:4940`/`:5279`) — bounded only by how often operators restart — while a thread that was already capped before the restart but never reopened rebuilds with `offset = 0`, so a late-joiner reply on it is closed as `timeout` a second time: one spurious `ThreadDecision` plus one spurious PI DM per restart per such thread (#20).
- Once a PI's own agent participates in a thread, that PI's next message clears the pending-proposal block for every participant agent in the thread, including agents from another lab (#20).
- The implicit `rating = -1` review row is memory-only when the acting agent's `AgentRegistry.user_id` is NULL, so it does not persist across a restart; `_persist_implicit_proposal_review` now logs a WARNING naming the thread_decision instead of DEBUG so this is at least visible (424e5f5, #20 COR-5).
- A transient failure inside `_handle_pi_inbound_entry` loses that row's PI triggers (review-clear, reopen, `pi_context`, tag routing); it is logged at ERROR and never retried, to avoid double-sending a non-idempotent DM (#20).
- When the target post is missing from the log, a reply falls back to the LLM's self-declared channel rather than a verified `PostRef.channel`; no reachable leak was found but the safer field isn't used (#20).
- The engine's activity cursor is data-dependent: a future-dated external `posted_at` can pin it ahead of wall time, and a purge can move it backwards (#20).
- `_handle_instruction`'s side effects (private-channel migration, legacy Slack post, inactive-agent/private-origin e-mails) still run before `process_inbound_email`'s commit, because the migration helper is shared with the web `/reopen` route; a commit failure re-runs them on the next poll. The code now says so explicitly (`email_inbound.py:442-445`, a comment added alongside 415d44f) rather than leaving it implicit (#21).
- Retry backoff sleeps inline inside `process_job`, head-of-line blocking the queue; the reaper, notification, and inbound checks can be deferred by up to ~15 s per failing job at the shipped defaults (#21).
- In-memory worker state (`_S3_FAILURE_COUNTS`, `_HELP_EMAILS_SENT`, `_RECENT_REPLY_TIMES`, `_INSTRUCTION_FAILURE_EMAILS_SENT`) resets on every worker restart, granting a fresh round of S3 processing attempts and a fresh failure e-mail (#21).
- `JOB_STALE_PROCESSING_THRESHOLD_SECONDS` was raised 900 s to 3600 s (`src/worker/main.py:49`) to absorb NCBI retry amplification (one `_ncbi_get` can take ~4 attempts x 60 s timeout + 3.5 s backoff, roughly 243 s, multiplied across the per-DOI loop); a job that still exceeds one hour in `processing` is re-queued and can duplicate synthesis spend (#21, #23).
- Two truly concurrent reopens can still both call `migrate_public_thread_to_private` and each mint a private channel: the idempotency check (`already_reviewed`, `agent_page.py:726`) runs before the migration, not around it — pre-existing race, widened by the D6 upsert. 4062eeb (#24 V5) closes the adjacent case: a single request that loses the write race no longer 500s or re-migrates on its own retry, and `reopen_proposal`'s `IntegrityError` guard now restores `refined_in_channel` — but that guard cannot stop two independently concurrent requests from each reaching the migration call before either commits (#21, #24).
- `0025`'s duplicate-publication DELETE is irreversible and `downgrade()` restores nothing; the keeper is now chosen deterministically (`created_at ASC, id ASC`) and every nullable column is COALESCE-merged from the doomed rows into the keeper before they're deleted, instead of picking a keeper by random UUID and losing the loser's data (581307e, #22 COR-16).
- The pipeline's disk export of a profile can still land ahead of its DB commit if the enclosing job transaction rolls back after the export runs (#22).
- The enforced profile word-count gate is 100-350 words while the generation prompt asks for 150-250 (#22).
- `atomic_write_text` can leave a hidden `.{name}.*.tmp` file behind if the process dies between `mkstemp` and `os.replace`; harmless since nothing in `src/` enumerates `profiles/` (#22).
- Any funding reply of 10 words or more is now presumed substantive, so a marker-free, question-free social pleasantry under 200 characters at exactly 10+ words is no longer suppressed as ack-only; pinned by `tests/unit/test_funding_rules.py:133-144` (#23).
- NCBI request pacing (`_pace_ncbi`) is a per-process module-global, so `app`, `worker`, `agent`, and any host script each get their own 2.9/8.3 req/s allowance — an aggregate of up to roughly 3x NCBI's rate ceiling from one host IP; run any bulk backfill in a single process. Retries now re-enter this same pacing gate (ec9853a, #23 COR-29) instead of bypassing it, which tightens compliance per-process but does not change the per-process-global limitation itself (#23).
- The D6 upsert is unserialized: two simultaneous first-time explicit reviews over the same `rating = -1` marker both take the UPDATE path (no `SELECT ... FOR UPDATE`, no constraint to violate); under READ COMMITTED the second blocks then overwrites, and both users see a 302 success even though only one rating survives. This is explicitly accepted in the code itself (`agent_page.py:582-585`: "no serialization on this path ... accepted by the D6 ruling") (#24).
- The reopen path's "leave the real review alone" branch still commits the channel migration, so a proposal can end up with a real `rating` value and `refined_in_channel` set plus a private channel and handover posts already created; look for the log line "gained a review ... leaving it alone" (`agent_page.py:917-921`) (#24).
- The waitlist race loser's supplementary fields are dropped — the concurrent insert path is not equivalent to the sequential upsert path (#24).
- A saturated web connection pool alone can flip `/api/health` to unhealthy: `make_engine` leaves `pool_size=5, max_overflow=10` (15 total) while the health probe's own timeout is 5 s, so 16+ concurrent checkouts trip the nginx health dependency without any actual Postgres degradation (#25).
- `--fresh` run against a live `agent-run` container also flips that live run's `SimulationRun` row to `stopped`/`ended_at`; harmless in the documented resume flow (nothing in `src/` reads that status, and the live process's own `finally` writes the same values) (#25).

### Known gaps (follow-ups, not in this PR)

- A durable eviction marker for closed/dead threads is an explicit follow-up: `_closed_thread_ids`/`_dead_thread_ids` are in-process only, and a restart re-hydrates a genuinely dead thread that must be re-evicted once from `ThreadDecision` rows (#20).
- `<@U...|label>` and `<!subteam^...>` Slack mention forms are still unhandled by thread-reservation matching (`src/agent/mentions.py`'s `_MENTION_RE` only matches bare `<@Uxxx>`) (#20).
- The terminal-failure handler for a private-channel-migration failure (email reopen, `_handle_instruction`) now rolls back first and notifies from pre-captured plain values, so it can no longer commit the migration's partial `AgentChannel`/`PrivateChannelMember`/handover-message writes alongside no `ProposalReview` row and an unset `refined_in_channel` (415d44f, #21 C1). The failure mode shifted rather than closed: on any terminal failure (a Slack API error mid-migration, or the database itself going unreachable) the already-created Slack channel is now rolled back to a total DB orphan instead — no `AgentChannel` row, `refined_in_channel` still unset, no `ProposalReview` — so the engine no longer posts into an untracked channel, but a subsequent retry (a dashboard reopen, or an S3 redelivery if the outage spanned multiple poll cycles, bounded by `MAX_S3_PROCESS_ATTEMPTS`=3) mints a brand-new `priv-…` channel rather than adopting the orphan (`create_private_channel` appends a UTC timestamp per attempt). Detection and cleanup: list `priv-*` channels with no matching `thread_decisions.refined_in_channel` row (#21).
- SES calls in the worker (`_send_simple_email`, `_send_paused_email`, the sweep senders) and S3 listing/get/delete all block the event loop with synchronous `boto3` calls — pre-existing (#21).
- `V11`/`V3`/`COR-32` stay latent until `ENABLE_INBOUND_EMAIL=true`; `docs/inbound-email.md:84-90` documents the V11 writer-slot prerequisite but not the V3 one, and the `s3:PutObject` grant on `failed/*` that the quarantine path needs is only implicit via `scripts/setup_inbound_email.py:168-171`. `specs/local-db-conversations.md` also still omits `REMEDIATION_WRITER_SLOT = 99` from its documented writer slots, a deliberate partial per the plan (#21).
- Publication titles/abstracts already stored in the DB before this fix remain truncated; the `itertext()` fix (2c1d504) only applies to newly-inserted rows. This fix wave narrows the gap: the pipeline's existing-row branch now refreshes `title`/`abstract`/`journal`/`year` from a fresh PubMed record whenever the incoming value is non-empty (35cc010, #22 V1-pm1), so a truncated row self-heals the next time its owning PI's pipeline runs and that PMID is still in the fetch list. A manual backfill is still needed only for rows belonging to a PI whose pipeline never runs again before #29's authorship guard needs to trust existing rows (#22).
- Pre-0023 "legacy" profiles remain unprotected by the `lost_evidence` gate (#22).
- `pending_profile`/`pending_profile_created_at` remain on the table with no writer (#22).
- The provisioning 504 risk from nginx's own timeout is closed: `nginx/nginx.conf:160-171` now gives the `/admin/agents/{id}/slack/provision` route a 300 s `proxy_read_timeout`/`proxy_send_timeout`, comfortably above the ~220 s worst case (4x30 s capped sleep + 5x20 s round trips). The other half is unchanged: `start_provisioning`'s idempotency step only deletes the prior `SlackAppProvision` bridge row (`admin_provisioning.py:190-194`) — it runs a fresh `create_app_async` unconditionally before that delete, so each retry (a stuck first attempt, a double-click) still creates another Slack app in the workspace (#24).
- There is no global `IntegrityError` -> 4xx handler; `reopen_proposal` now has an explicit guard too (4062eeb, #24 V5), joining vote/PI-message/waitlist/review — five endpoints total — so any other SELECT-then-INSERT path can still 500 on a lost race (#24).
- `ProposalReview`'s unique constraint is migration-only (`alembic/versions/0004_add_agent_registry_and_proposal_reviews.py`); a future test fixture built with `metadata.create_all` would silently lack it, so the concurrency guards would look untested while effectively dead (#24).
- There is no static guard against a future blocking `httpx` call on an async route — `tests/unit/test_slack_boundary.py` only polices `slack_sdk` imports, which is exactly why the raw-`httpx` provisioning path escaped detection until commit `fa143a6`'s audit (#24).
- NSF-style FOA numbers (`NSF 25-543`) remain unmatched by `FOA_NUMBER_RE` even though `BIOMEDICAL_AGENCIES` still lists `"NSF"` (`src/services/grants.py:20`); agency-code-less numbers like `OTA-24-001` are also unmatched; both are pinned by `tests/unit/test_foa_pattern.py:22-24` (#23).
- GrantBot's preflight only checks that a token is `xoxb-`-shaped, not that it authenticates; a dead-but-shaped token produces a "quiet day" — `_ensure_channel_membership` swallows its own failure, every post attempt errors and releases its claim, and `_mark_run_complete()` marks the day done — which an operator will misread as healthy; asserting via `auth.test` is a follow-up (#23).
- The nginx `location /static/` block and the compose volume it needs remain an open decision, carried forward rather than added in this PR (#25).
- 25 `compare_metadata` diffs remain between the ORM models and the schema (10 pre-existing alembic-only indexes, 3 placeholder unique constraints not yet backfilled into the models); `alembic revision --autogenerate` is still unsafe on this schema, so every future migration must be hand-written (#25).
- The issue's original "16th concurrent checkout blocks 30 s" symptom is unchanged: `make_engine` makes `pool_timeout=30` explicit but does not raise `pool_size`/`max_overflow`; raising either is a separate sizing decision (#25).
- `HEALTH_PROBE_TIMEOUT_SECONDS = 5.0` bounds the `db.execute` only, not the `async with` session teardown, so a probe can still exceed 5 s if teardown blocks (#27).
- `docker compose config` renders every service except `agent`, which sits behind `profiles: [agent]`, so R.3's render check does not cover the agent service's own resource limits or logging driver (#27).
- Neither creator queries `bot_name` for collisions and `agents.bot_name` has no unique constraint; the derivation is now digit-aware and consistent across the web, backfill and sparsedata paths (0a16f72, #26 I1), but the structural guarantee is absent (#26).
- Pre-existing web-vs-script bot-name divergence on mixed-case and punctuated surnames persists by declared scope: `"Ann McDonald"` gives `AMcDonaldBot` on the web (`agent_page.py`'s `display = last_name`) and `AMcdonaldBot` from the scripts (`backfill_agents.py`'s `last_alpha.capitalize()`); likewise `"peng WU"` and `"O'Brien"` (#26).
- Two of `test_prompt_hygiene.py`'s assertions are green against the base branch, so they are forward-looking guards rather than regression proofs (#26).

### Deploy-window facts

- This PR's engine-process changes require rebuilding, not just restarting, the prod agent image (`--profile agent build agent`), followed by a graceful `docker stop -t 30 agent-run` before the next run (#20).
- Migration 0028 is additive/nullable with an `if_exists` downgrade and needs no data migration (#20).
- The stale-processing reaper is safe only with a single `worker` replica; scaling it requires either a `started_at` heartbeat inside `process_job` or a threshold set above the worst-case job duration across all replicas (#21).
- Tasks that change baked-in agent/grantbot/funding-rules code (23.3, 23.5, 23.8, 23.10, 23.12) require `--profile agent build agent` plus `up -d --build app worker`, not a restart, per CLAUDE.md (#23).
- GrantBot needs a dedicated Slack bot token in prod before this branch ships, or its funding posts stop entirely (previously they were silently mis-attributed to `su`); token precedence is `AgentRegistry.slack_bot_token` for `agent_id='grantbot'` -> `Settings.get_slack_tokens()` (no `grantbot` key, never fires) -> `SLACK_BOT_TOKEN_GRANTBOT` in `.env` -> refuse; preflight must confirm at least one holds a real `xoxb-` value, not `xoxb-placeholder...`. A token set in `.env` requires recreating the `grantbot`/`agent` containers (`Settings` is `lru_cache`d); a token set in the DB is picked up by the next daily GrantBot run with no restart, and by a running `agent-run` within one `ROSTER_POLL_INTERVAL` — but only if the DB value differs from the last probe attempt and passes `is_valid_token`; clearing the DB token does not un-map the old uid without a restart (#23).
- Preflight now counts and dumps the duplicate `publications` rows automatically (`check_publication_duplicates`, check 12, 13844d9, #22 COR-16) instead of requiring an operator to run the `GROUP BY user_id, pmid HAVING count(*) > 1` query by hand before `0025 --apply`; confirm the check's PASS/WARN output (with the `\copy ... dup_publications.csv` remediation, if WARN) appears in the preflight run before proceeding, since the DELETE is still irreversible (#22).
- `0025`'s `create_unique_constraint` takes an ACCESS EXCLUSIVE lock for the whole index build inside the single-transaction alembic env (`lock_timeout=10s`); `worker` is the only writer of `publications`, so stop it before `--apply` (or just retry — the DELETE step is idempotent, verified) (#22).
- The full 0025-0028 migration chain runs as one transaction: every lock in it is held until the final COMMIT. Writes are blocked on `users`, `publications`, `private_channel_members`, and the 13 tables 0027 indexes; reads are blocked on `publications`, `private_channel_members`, and `thread_decisions`. Measured cost for 0026-0028 alone is about 2 s against 300k `thread_decisions` rows; the real cost of the chain is 0025 (#25).
- The 20 extra lock acquisitions in the 0025-0028 chain raise the odds of an `ALEMBIC_LOCK_TIMEOUT_MS` abort if any writer is still live; stop `app`, `worker`, `grantbot`, and `agent-run` before `--apply`. An abort is safe (full rollback) but costs the deploy window (#25).

Process-level deferrals the per-task reviews recorded that are not code risks: `_NCBI_SEMAPHORES` are loop-bound at
first contended acquire and are reset per test (a per-loop map in `src` is a follow-up); the notification sweeps'
`sent_count` is incremented before the per-item commit (pre-existing) and their re-load filters on the primary key
rather than the bulk predicate; a comment at `agent_page.py` still says "THIS responder's" where the behaviour is now
agent-scoped; `tests/integration/test_worker.py` keeps the pre-atomic read-modify-write pattern in one mock; and a
monthly reminder subscriber's unanswered notification always expires (14 days) before their 29-day interval elapses,
so three consecutive misses take about three months rather than three cycles.

## Verification

**Final gate, 2026-09-10 at `72caa26` (after the audit fix waves):** `./scripts/ci.sh` → single head
`0030`, round trip clean, ruff src 248/260, mypy 139/150, **3111 passed / 120 skipped**, branch
coverage **80.84 %**, `CI passed` in 10:14. Live copi-test tier at the same commit: 53/53 non-LLM and
8/8 real-LLM (see `docs/plans/2026-09-04-decisions/task-35.md`). The block below is the earlier
measurement kept for history.


`MIGCHECK_PORT=55433 LOCK_SMOKE=1 ./scripts/ci.sh` at `c049ec7` — **exit 0**, 7:46:

```
==> alembic (single head, no duplicate revision ids)      single head: 0028 (head)
==> alembic round trip (throwaway postgres:15)            clean (upgrade head -> downgrade 0018 -> upgrade head)
==> ruff (test-suite lint)                                All checks passed!
==> ruff (src/ ratchet, ceiling 260)                      251 findings
==> lockfile freshness (matches pyproject.toml)           current (resolved with cpython-3.11)
==> lock smoke test (opt-in: LOCK_SMOKE=1)                PASS  requirements.lock installs on Python 3.11 and
                                                          src.main/src.worker.main/src.agent.main import cleanly
==> mypy (src/ ratchet, ceiling 150)                      145 findings
==> pytest (full suite + branch coverage)                 2534 passed, 120 skipped, 20 snapshots, 78.55 % branch
==> CI passed.
```

`scripts/ci.sh` run by the `pre-push` hook is the whole gate — there is no server-side CI, by decision D17. Two things
about the gate itself changed here because the audit showed they were not true before: deleting either ratchet's
`exit 1` used to leave every one of its tests green, so both now have a behavioural test that runs the real script and
asserts it fails; and nothing had ever executed the dependency set production installs (the test venv resolves fresh
from `pyproject.toml` on 3.12, the image installs `requirements.lock` on 3.11), so `LOCK_SMOKE=1` installs the lock on
3.11 and imports all three entrypoints. The mypy ceiling was set at exactly the then-current count with zero slack on an
unpinned tool; mypy is now capped to one minor version and the ceiling carries documented slack.

Every task has a failing-before/passing-after test cited in its review report; the Phase 4 and Phase 5/6 gates passed at 7b7a29f and a20ac4a (2442 passed / 120 skipped / 77.88 % branch coverage at a20ac4a).

## Rollout (prod)

Part R of the plan doc is the ordered runbook for the agent on the prod host, with R.12's 21 deploy notes. Prod-side actions
it requires: verify/decide the GrantBot token (D10); `sudo mkdir -p data && sudo chown -R 10001:10001 profiles/ data/` and
**never `prompts/`** (it is git-tracked, so chowning it breaks the next `git pull`); render the merged compose
(`docker-compose.prod.yml` + `docker-compose.override.yml`) and expect the `migrate` one-shot to gate app/worker/grantbot
(fail-closed); dump the publication duplicates 0025 will merge-and-delete; run `preflight.py` →
`run_migration.sh --via-run` → `postflight.py` for 0024→0028; run the one-time `backfill_slack_ts.py` repair if this
workspace has never had it (R.6b); rebuild the agent image and restart `agent-run` gracefully; `nginx -s reload`;
cherry-pick the new `.dockerignore` to blackbird and rebuild with `--no-cache` (D3); rotate the credentials that were baked
into earlier images (D4); watch OOM/`dmesg` for the first hour; postgres limits in a separate window (D24).

Four runbook defects the Phase 7 audit found by building the image and running the real tooling against it, all fixed here:
the preflight snapshot was written as UID 10001 into a directory the runbook deliberately never chowns, so R.6's **first**
command would have failed with the app already stopped; the image-content check inspected the running container's image
rather than the one just built, so the check proving the secret-leak hole is closed could not pass; the log-driver check
printed "0" whether or not the required override was present; and R.8 claimed a step re-runs `migrate` when it does not.
The chain runs in ONE transaction, so its locks are additive — R.6 now carries the measured per-revision lock table
(0026→0028 = 2.1 s at 300k `thread_decisions`; the real cost is 0025's unique-index build on `publications`).

## Process notes for the reviewer

- Commit sequence is the review trail — **do not squash-merge**.
- Three commits carry another task's files, swept in by a plain `git commit` race in the shared checkout before the
  `sdd-commit` helper (flock + `git commit --only`) became mandatory: 29bee9c (subject: 21.10; also carries the 20.19
  fix-round `simulation.py` + `test_simulation_logic.py`), 5040516 (subject: 23.6; also 23.3's `grantbot.py` +
  `test_grantbot_selection.py` and 20.19 test minors), eda8603 (subject: 21.6; also 23.7/23.8's `funding_rules.py` +
  `test_funding_rules.py`). Content is correct and was reviewed per task with path-filtered packages; only each commit's
  subject is narrower than its file list. History was deliberately not rewritten (the branch owner may still choose to).
- The last 40 commits are a Phase 7 fix wave: eight adversarial auditors (one per issue, plus the migration tooling and the
  runbook) re-checked the whole branch item by item against the issue text and found 4 Critical and ~20 Important defects
  that the per-task reviews had missed, all in code this branch introduced. Every one is either fixed here with a
  failing-before test or listed above as a stated residual. The audits are in the ledger directory as `audit-issue-NN.md`.
- `.superpowers/sdd/2026-09-02-close-issues-20-27/` holds the ledger (`progress.md`), every brief, review and audit; it is
  git-ignored and not part of this PR.

Closes #20, #22, #23, #24, #25

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_015Nnc7rWXX4MbGSnGNEthzx
