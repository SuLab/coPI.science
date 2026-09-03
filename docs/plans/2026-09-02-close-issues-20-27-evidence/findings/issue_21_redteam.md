# Issue #21 — red-team pass (second reviewer)

Tree: `copi-prod` @ 18ba52c, clean. Everything below re-derived by my own grep/sed/python against the current tree;
`git show 8f96f86^:src/services/email_inbound.py` used to read the pre-fix code. No DB/Docker/network.

## 1. Table

| id | first-agent verdict | red-team result | one-line reason | evidence |
|---|---|---|---|---|
| V11-a | ACCURATE | UPHELD | four slots + `_default = TsMinter(WRITER_WEB)` | `src/agent/ids.py:47-50`, `:111` |
| V11-b | ACCURATE (lines drifted) | UPHELD | callers exactly `src/main.py:113`, `src/agent/main.py:54`, `src/agent/grantbot.py:731,764` | grep `set_default_writer_id` |
| V11-c | STILL PRESENT | UPHELD | `grep -c ids src/worker/main.py` = 0 in imports; no `set_default_writer_id` in file | `src/worker/main.py:6-18` |
| V11-d | STILL PRESENT | UPHELD | `email_inbound.py:674`→`pi_inbox.py:120`; `:638`→`private_channels.py:269`; `record_pi_dm` (`pi_inbox.py:139/151`) is called only from `pi_handler.py`, `simulation.py`, `agent_page.py` — not worker | grep `record_pi_dm` |
| V11-e | ACCURATE | UPHELD | `src/config.py:152 enable_inbound_email: bool = False`; gate `src/worker/main.py:162` | sed |
| V11-f | STILL PRESENT | UPHELD | `docs/inbound-email.md:84-90` step 4; `grep -i "writer\|slot"` on the doc → no hits | grep |
| V11-g | ACCURATE | UPHELD | `grep -rln "mint_local_ts\|TsMinter" scripts/` → nothing | grep |
| V11-h | STILL PRESENT | UPHELD | `specs/local-db-conversations.md:66-67` "Three processes"; `remediate_duplicates.py:131 = 99` | sed |
| COR-17 | STILL PRESENT (characterization test) | UPHELD | `worker/main.py:100-111` no rollback; `tests/integration/test_worker.py:760-827` asserts `status == "processing"` and `last_error is None` — i.e. asserts the BUG; flipping it is still owed | sed |
| COR-18a | STILL PRESENT | UPHELD | only writer `worker/main.py:49`; all other `started_at` hits are `SimulationRun.started_at` | grep |
| COR-18b | STILL PRESENT | UPHELD | `"processing"` in src/ only at `worker/main.py:48` and display `admin.py:126` | grep |
| COR-18c | STILL PRESENT, retry immediate | UPHELD | `worker/main.py:127-137`: `asyncio.sleep` only in the `else:` (no-job) branch; after `process_job` returns the loop re-enters `claim_job` with no delay; `claim_job` orders by `enqueued_at` so the re-`pending` job is first | sed 127-137 |
| COR-18d | STILL PRESENT | UPHELD | `worker/main.py:110` | sed |
| COR-18e | STILL PRESENT | UPHELD | enum `models/job.py:23`; worker writes processing/completed/dead/pending only; `tests/unit/test_reachability.py:39-44` documents it | grep |
| COR-18f | STILL PRESENT | QUALIFIED | verdict stands, but a `dead` job does NOT show the spinner — it renders no branch at all (blank body); the spinner is for `processing`-wedged (COR-17) only. Admin mitigation mis-cited (see §3) | template `profile_review.html:28,47,60` |
| COR-19.1 | FIXED (8f96f86) | UPHELD | pre-fix file has no `copy_object`/`_S3_FAILURE_COUNTS`/`MAX_S3_PROCESS_ATTEMPTS`; the test's `fake.copied == [("inbound/poison","failed/poison")]` would fail (pre-fix: `[]`; the `monkeypatch.setattr(inbound, "_S3_FAILURE_COUNTS")` would already AttributeError). Quarantine-path exceptions are caught (`:207-208`), counter not popped → re-attempted next poll. Caveats in §2 | `email_inbound.py:33,48,186-208`; `git show 8f96f86^:…` |
| COR-19.2 | FIXED (8f96f86) | UPHELD | pre-fix has the constant (`:26`) and no `_reply_rate_ok`; test imports the symbol → ImportError pre-fix; enforced at `:241` before the notification lookup. Note: a rate-limited reply returns normally → poller commits + deletes the S3 object (dropped, not deferred) | `email_inbound.py:52-65,241-245` |
| COR-19.3 | STILL PRESENT | UPHELD (re-run) | `_decode_part -> LookupError unknown encoding: unknown-8bit`; `_extract_reply_body` same | python run below |
| COR-19.4 | STILL PRESENT | UPHELD (re-run) | `'3' -> TypeError`; `2.5`/`True` accepted (extra) | python run below |
| COR-19.5 | STILL PRESENT | UPHELD | `:165 MaxKeys=50`, no `ContinuationToken`/`IsTruncated` | grep |
| COR-19.6 | STILL PRESENT | UPHELD | side effects `:335,:352,:359,:605,:638,:655,:706`; sole commit `:179`, delete `:182` | grep |
| COR-32 | STILL PRESENT | QUALIFIED | cites exact (`:612,:628,:662,:682,:699,:704,:719`); caller `:348` unconditional. Wording: the silent-consume path is reachable on the DEFAULT config too — `enable_private_refinement: bool = True` (`config.py:391`) routes into `migrate_public_thread_to_private` inside the same `try`, so a migration failure hits the blanket `except` `:717-719` → False → notification retired, no email. Not legacy-only | awk over 596-722 |
| V4-1 | STILL PRESENT | UPHELD | `grep -c rollback` = 0; excepts/commits at cited lines | grep/sed |
| V4-2 | STILL PRESENT | QUALIFIED (worse) | for `new_proposal` the phantom row is created on EVERY allowlist-suppressed recipient, not just SES failure: row+flush `:995-1004` precedes `_send_html_email` whose allowlist check (`:634-636`) returns False; dedup `:968-976` then skips forever. First agent's "positive note" holds only for `proposal_review` (`:319-326` before row) | sed |
| V4-3 | STILL PRESENT | UPHELD | 0 hits in service; only `models/email_notification.py:42` comment | grep |
| V4-4a | STILL PRESENT | UPHELD | increment `:295`; bail `:247-250` → `_check_engagement_and_downgrade` `:502` returns at `<3`; resets `:517,:523,:594`, `settings.py:133`; only `status="responded"` writer is `:614` (`mark_notification_responded`), always paired with `record_engagement` | grep |
| V4-4b | STILL PRESENT | UPHELD (+ sharpened) | filter `:606-611` on replier id; web `review_proposal` raises 400 "Already reviewed" (`agent_page.py:490-497`) BEFORE `mark_notification_responded` (`:511`) → PI cannot clear its row via web once a delegate reviewed; an email reply would clear it (`_handle_review` returns early but caller still marks responded `:334`) but inbound is off | sed |
| T-1 | ACCURATE | UPHELD | `config.py:152` | |
| T-2 | NOT VERIFIABLE | UPHELD | repo-side evidence all consistent with "unset": `.env.example` has no inbound key (51 lines, 0 hits), local `.env` 0 hits, `docker-compose.prod.yml` passes only `env_file: .env` + `ENVIRONMENT`, `docs/inbound-email.md:41` and `scripts/setup_inbound_email.py:16` both state it is unset in prod | grep |
| T-3 | ACCURATE | UPHELD | `email_notifications.py:192-199`; `get_or_create_pref` callers are only `status_overview` (`:714`), `new_proposal` (`:963`), settings router — never `proposal_review` | grep callers |
| T-4 | ACCURATE | UPHELD | `config.py:158 = 300`; `worker/main.py:141` | |
| T-5 | PARTIALLY ACCURATE | UPHELD | `:60-77` SELECT then INSERT only if None; composite PK `(user_id, category)` confirmed `models/email_notification.py:109-115`. Per-cycle write inventory in §2 | sed |
| T-6 | ACCURATE | UPHELD | `docs/inbound-email.md:84-90` | |
| DoD | STALE | UPHELD | `test_worker.py` 15 tests, d732804 (2026-07-30), NOT an ancestor of `main@b7edcbc` — so the issue was right when filed and is stale now | `git merge-base` |

## 2. Detail — QUALIFIED rows and NEW claims

### COR-19.1 FIXED — upheld, with three caveats the first agent did not state
Pre-fix (`git show 8f96f86^:src/services/email_inbound.py`): only `list_objects_v2` (:93) and the success-path `delete_object` (:110); no
copy, no counter. Current `:186-208`: counter++ on any exception; at >= `MAX_S3_PROCESS_ATTEMPTS` (3) `copy_object` → `failed/<key minus
prefix>` then `delete_object`, then pop. Test `test_poison_email_is_quarantined_after_repeated_failures` (`hardening.py:259-276`) would fail
pre-fix on the `fake.copied` assertion (and earlier on the missing module attribute). Caveats:
1. If `copy_object`/`delete_object` themselves throw, the inner `except Exception` (`:207-208`) only logs; the counter is NOT popped, so the
   next poll re-attempts quarantine. If the failure is permanent (IAM lacks `s3:PutObject` on `failed/*`) the object is retried every poll
   forever — pre-fix behaviour returns. `scripts/setup_inbound_email.py:168-171` does grant PutObject on `failed/*`, so this is a runbook
   prerequisite, not a code bug.
2. The counter cannot distinguish poison from transient: a DB outage of 3 × `inbound_poll_interval` (3 × 60 s) quarantines every object in
   the bucket, i.e. legitimate replies are lost to `failed/` (recoverable only by hand). Same mechanism the first agent noted for COR-19.3.
3. `_S3_FAILURE_COUNTS` is in-memory (documented at `:47`); a worker restart resets the count.

### COR-19.2 FIXED — upheld
`_reply_rate_ok` (`:52-65`) is a sliding window; called at `:241` after auth-results and auto-submitted gates, before the notification
lookup. Tests `hardening.py:195-211` exercise the window directly. Pre-fix code has the constant but no function → the test module's
`from ... import _reply_rate_ok` fails at import. Nuance: a rate-limited reply is `return`ed normally, so the poller commits and deletes
the S3 object — the 11th reply in an hour is dropped, not deferred.

### COR-18c NEW claim (retry immediate) — reproduced by reading
```
127  while not _shutdown:
130          job = await claim_job(db)
132      if job:
134          await process_job(...)
135      else:
137          await asyncio.sleep(settings.worker_poll_interval)
```
No sleep after `process_job`. `process_job` re-sets `status="pending"` (`:108`) and commits; the next iteration's `claim_job` (ordered by
`enqueued_at`) picks it straight back up. Three attempts run back-to-back; the issue's "~15 s" is an overstatement of the delay.

### T-5 NEW claim (`get_or_create_pref` SELECTs first) — confirmed; per-300 s-cycle write inventory
- `check_and_send_notifications` (proposal_review): only users with `frequency != 'off'` AND not paused AND email set. Per user:
  `EmailEngagementTracker` INSERT on first sight (`:230-233`); if due and no outstanding `sent` row and unreviewed proposals exist:
  allowlist-suppressed → `tracker.last_notification_sent_at` UPDATE (`:267`); else `EmailNotification` INSERT (`:331-340`) + tracker UPDATE
  (`:294-295`). If quieting was done by setting `User.email_notification_frequency='off'` this sweep writes nothing; if it was done via
  pref rows only, it is unaffected (T-3).
- `check_and_send_status_overviews`: ALL users with email. `get_or_create_pref` INSERT on first sight only; disabled pref → no writes.
  Enabled+due → `pref.last_sent_at` UPDATE on success.
- `check_and_send_new_proposal_emails`: proposals in last 7 days × 2 agents × recipients. `get_or_create_pref` INSERT on first sight;
  disabled → skip; enabled + no existing row → `EmailNotification` INSERT (`:995-1004`) regardless of send outcome (see V4-2).
So with prefs disabled, steady-state per-cycle writes are: tracker inserts for newly eligible proposal_review users, pref inserts for
newly created users, and nothing else. The issue's "inserts on a composite-PK table each cycle" is wrong as stated.

### COR-18f QUALIFIED
`templates/onboarding/profile_review.html`: `:28 if pending/processing/none` → spinner; `:47 elif 'failed'` → retry form; `:60 elif profile`
→ profile. A `dead` job with no profile matches none → blank body (no spinner, no retry). A `processing`-wedged job (COR-17) → spinner.
Verdict (no self-service retry) stands; the first agent's "dead job leaves the PI on the spinner" is wrong.

### COR-32 QUALIFIED (wording/reachability)
Exact `return False` sites: `:612` (inactive, emailed), `:628` (already acted on), `:662` (private origin, emailed), `:682` (no run),
`:699` (no token), `:704` (no channel), `:719` (blanket except). `config.py:391 enable_private_refinement: bool = True` means the default
path is `migrate_public_thread_to_private` (`:638`) inside the same `try`; any failure there lands in `:717-719`. The issue's "can't resolve
a bot token/channel" framing (legacy path) understates reach: the default path silently consumes the instruction too.

### V4-2 QUALIFIED (worse for new_proposal)
`_send_new_proposal_email` `:995-1004` creates `status="sent"` + `flush()`, then `_send_html_email` (`:624`) checks
`is_allowed_recipient` at `:634-636` and returns False. `outbound_email_allowlist` defaults `""` (allow all, `config.py:150`,
`email.py:91-93`), so this only bites when the allowlist is set — but when it is, every suppressed recipient gets a permanent phantom row
that the dedup (`:968-976`) honours even after the allowlist is widened.

### V4-4b sharpened
`agent_page.py:490-497` raises `HTTPException(400, "Already reviewed")` before `:511 mark_notification_responded`. So after a delegate
reviews, the PI has no web path to retire their `sent` row. The email path would (caller marks responded unconditionally `:334`) but
inbound is off.

### Reproductions (run with `.venv-test/bin/python`)
```
_decode_part -> LookupError unknown encoding: unknown-8bit
_extract_reply_body -> LookupError unknown encoding: unknown-8bit
'3' -> TypeError '<' not supported between instances of 'str' and 'int'
'abc' -> TypeError '<' not supported between instances of 'str' and 'int'
3 -> accepted   None -> unparseable   0 -> unparseable   5 -> unparseable   2.5 -> accepted   True -> accepted
```

### Reachability
`enable_inbound_email` default False (`config.py:152`); the only consumer is `worker/main.py:162`; `poll_inbound_emails` /
`process_inbound_email` have no other callers in src/ or scripts/. `.env.example` (51 lines) has no inbound key; local `.env` none;
`docker-compose.prod.yml` sets only `ENVIRONMENT` explicitly. Later commits on the three files since b1d54da: `364bee3` (fail closed on
null-email users; help email reply-able), `f94d2a8` (merge #31), `0e2ed84` (model bump) — none touch the mechanisms above.

## 3. Mis-cites / wording errors in the first-agent report
- COR-18f mitigation "admin can enqueue a fresh generate_profile job (`src/routers/admin.py:1108-1114`)": `:1109` is inside the admin
  *impersonation* flow that creates a brand-new user; the re-enqueue-for-existing-user sites are `:1217` and `:1283` (approve/allow
  routes) and fire only when the user has no profile yet. There is no admin "re-run profile" action for a wedged job.
- COR-18f "a `dead` job leaves the PI on the spinner": renders no branch (blank), see §2.
- COR-19.1 report cites quarantine at `:186-209`; the block is `:186-208` (trivial).
- Everything else I checked (ids.py, grantbot, pi_inbox, private_channels, email_inbound `:335/:352/:359/:605/:638/:655/:706`,
  email_notifications `:208-214/:721-725/:912-916/:216/:726/:917/:331-340/:481-495/:502/:517/:523/:594/:606-611`, settings `:133`,
  agent_page `:511/:714`, template `:28/:47`) points at the quoted code.

## 4. Counts
33 rows: **29 upheld**, **0 overturned**, **4 qualified** (COR-18f, COR-32, V4-2, V4-4b — all verdicts stand; evidence/wording/severity
adjusted), **0 unverifiable** beyond T-2 (already marked so by the first agent; upheld as unverifiable).
