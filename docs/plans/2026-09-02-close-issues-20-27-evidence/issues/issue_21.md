# #21 Worker & background jobs: id collisions, session rollback, email robustness (4 PRs)
state=OPEN created=2026-07-30T15:53:03Z updated=2026-08-11T23:54:35Z labels=['area:worker', 'verified-2026-07-30']

## BODY

Verified defects in the `worker` container: the job loop, the inbound-email poller, and the notification sweeps.

Originally verified at `origin/main` @ `b7edcbc` (2026-07-30). **Re-verified 2026-08-11 against the open PR-stack tip (`issue-29-authorship-grounding` @ `b1d54da` = main + #30/#31/#32); line numbers below refer to that tree. PR #31 (email-fix) fixed parts of V3 — marked below.**

## Priority (triage 2026-08-11)

- **Live today:** V2 (a wedged job leaves a new signup on the "Building Your Profile" spinner permanently) and V4's no-rollback hazard (all three sweeps still run every 300 s and still *write* — `get_or_create_pref` inserts on a composite-PK table each cycle — even with sending quieted). V4's phantom-sent + dead-ladder halves may also be live: the 2026-08-06 prod quieting disabled `email_notification_preferences` rows, but the `proposal_review` sweep is gated on **`User`** columns instead (`check_and_send_notifications:195-199`, deliberate per `models/email_notification.py:99-105`) — verify prod user frequencies before assuming it is dormant.
- **Latent, armed by the email bring-up:** V11, V3-remainder and COR-32 are unreachable today (`enable_inbound_email` defaults `False` and is unset in prod) and **all three arm simultaneously at step 4 of `docs/inbound-email.md`** ("flag on + worker recreate"). They must land before `ENABLE_INBOUND_EMAIL` flips, and the runbook should list them as prerequisites (relevant to PR #31).

**Suggested order: V2 → V4 → V11 → V3.**

### PR V11 — Claim a canonical-id writer slot in the worker process *(trivial; prerequisite for enabling inbound email)*
Still unfixed. `src/agent/ids.py:47-50` defines four slots (`WRITER_ENGINE=0`, `WRITER_WEB=1`, `WRITER_GRANTBOT=2`, `WRITER_ENGINE_AUX=3`); the module default is `TsMinter(WRITER_WEB)` (`ids.py:111`). `set_default_writer_id` is called in `src/main.py:113` (WEB), `src/agent/main.py:54` (ENGINE_AUX) and `src/agent/grantbot.py:724/757` (GRANTBOT) — but `src/worker/main.py` has no import of `src.agent.ids` at all.

The worker mints on two paths, both inside the inbound-email handler: `email_inbound.py:626` → `record_pi_message` → `pi_inbox.py:120/151` `mint_local_ts()`, and `email_inbound.py:590` `migrate_public_thread_to_private` → `private_channels.py:269`. So `worker` and `app` share residue class 1 — the exact cross-process collision R1 exists to prevent, whose documented resolution is the `uq_agent_messages_run_ts` conflict handler *dropping* one message, unrecoverable now that the DB is the only durable store.

Both paths are only reachable when `enable_inbound_email=True` (`config.py:152`, default `False`; `worker/main.py:162`) — latent today, armed at runbook step 4, which mentions no writer slot.

*(The backfill scripts are **not** affected: they use Slack's `ts` and never mint.)*

*Fix:* add a `WRITER_WORKER` id and claim it at `src/worker/main.py` entry; correct the "Three processes" count at `specs/local-db-conversations.md:66-67` — now wrong three ways over (four slots defined, five with the worker, six counting `REMEDIATION_WRITER_SLOT = 99` in `scripts/migrate/remediate_duplicates.py:131`).

### PR V2 — Worker session rollback + stale-job reaper *(small; live)*
- **COR-17** — `worker/main.py:100-111` `except Exception: … await db.commit()` with no `await db.rollback()` first. `run_profile_pipeline` shares the session and only `flush()`es, so any flush-time DB error poisons the transaction; the failure-path commit then raises, escapes `process_job` into the outer handler (`:170`), and nothing persists — the row keeps `status='processing'`, `last_error=None`. *Fix:* `await db.rollback()` before the failure-path commit.
- **COR-18** — no reaper for orphaned `processing` rows (`started_at` is written at `:49` and read nowhere in the tree); failed jobs re-queue with no backoff (all 3 attempts burn in ~15 s at `worker_poll_interval=5`); `completed_at` is set on failure (`:110`); the enum's `'failed'` value is never written. Impact: `onboarding.py:79-88` self-heals only when `job is None`, so a wedged job = a permanent onboarding spinner with no retry. *Fix:* a stale-`processing` reaper + exponential backoff.

### PR V3 — Inbound-email poison-pill & instruction-drop hardening *(small; prerequisite for enabling inbound email)*
- **COR-19** — status after PR #31:

  | sub-claim | state |
  |---|---|
  | poison objects retried forever | **fixed in #31** — quarantined to `failed/` after `MAX_S3_PROCESS_ATTEMPTS=3` (`email_inbound.py:167-181`, test-pinned) |
  | `MAX_REPLIES_PER_TOKEN_PER_HOUR` never enforced | **fixed in #31** |
  | `charset=unknown-8bit` → `LookupError` | root cause intact (`:343-346`; codec *lookup* fails before `errors="replace"` applies). Net effect changed: a legitimate PI reply with an odd charset is now **silently lost** into `failed/` after ~3 minutes instead of looping |
  | string rating → `TypeError` | still present (`:288` `rating < 1` on a str; `classify_reply` returns raw `json.loads` output at `:468` with no coercion) → the reply is quarantined and lost rather than filed |
  | `list_objects_v2(MaxKeys=50)` unpaginated | still present (`:139`); starvation is now bounded by the quarantine, but lexicographic head-of-list order still governs which 50 keys a cycle sees |

  New in re-verification: side effects inside `process_inbound_email` (SES confirmation `:301`, Slack post `:658`, private-channel migration `:590`) happen **before** `db.commit()` (`:153`) — a commit failure rolls back the DB idempotency guards but not the sent mail / Slack post, so a retry duplicates them. *Fix:* guard decode + coerce the rating; paginate; order side effects after commit (or make them idempotent).
- **COR-32 (remainder)** — still present. When `_handle_instruction` can't resolve a bot token/channel it returns `False` (`:648-656`, and the blanket `except` at `:669-671`), yet the caller (`:306-319`) still calls `mark_notification_responded` and the poller deletes the S3 object → the PI instruction is silently consumed: notification retired (a resend hits the `status != "sent"` bail at `:230`), no post, no email. The inactive-agent (`:557-563`) and private-origin (`:607-613`) paths *do* notify the PI — this one doesn't. *Fix:* on a failed post, don't mark-responded and don't delete the object.

### PR V4 — Email-notification transactional safety *(medium; partially live today)*
All four sub-claims still present; two are worse than originally filed:
1. **No rollback** — `rollback` appears zero times in `email_notifications.py`. The three per-item `except` blocks (`:208-214`, `:715-719`, `:906-910`) log and fall through to the sweep-final `commit` (`:216/:720/:911`). One failure poisons the session, and the sweep-level rollback then **discards rows for users whose SES send already succeeded → those users get a duplicate email next cycle with a fresh reply token.**
2. **Phantom-sent** — the row is created `status="sent"` and flushed **before** the SES send (`:323-332` vs the send at `:475-489`; identical shape in `_send_new_proposal_email`, `:962-998`). On SES failure nothing clears the row → that user never receives another `proposal_review` email; the `new_proposal` email is permanently lost.
3. **`"expired"` never written** — sole occurrence is the model comment; phantom rows are immortal and reply tokens never expire.
4. **Downgrade ladder dead** — `consecutive_missed` has one increment site (`:295`), unreachable once an unanswered `sent` row exists (bail at `:240-250`), and every retirement path resets it, so it oscillates 0→1→0; `MISSED_THRESHOLD=3` is unreachable and `:499-523` is dead code. Additionally: a *delegate* reply marks only the delegate's rows (`user_id ==` at `:600-606`), leaving the PI's row `sent` forever → the PI is silently blocked.

*Fix:* `db.rollback()` per item; commit-after-send (or a claim row); write an expiry status; retire the PI's row on delegate response.

**Definition of done:** each PR ships a test that fails against the pre-fix code. This issue takes `worker/main.py` from 0 % coverage. V11 and V3 additionally add their prerequisites to `docs/inbound-email.md`'s bring-up checklist.


