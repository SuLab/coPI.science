# Red-team audit — MASTER_PLAN.md Part 21 (worker & background jobs), lines 4651–7644

Tree audited: `/home/a/scripps/coPI.science` @ `copi-prod` `18ba52c`, clean, read-only.
Everything below was re-derived by my own `sed`/`grep` against the current tree. Where a test was
DB-free I copied the repo to a scratch worktree
(`.../scratchpad/rt21work`), applied the task's diff there, and ran it with
`/home/a/scripps/coPI.science/.venv-test/bin/python`. Commands and output are quoted inline.

---

## 1. Task → verdict

| Task | Verdict | One line |
|---|---|---|
| 21.1 WRITER_WORKER slot | **OK** | every anchor byte-exact; I applied the diff and ran the two tests — 13 passed; `test_remediate_duplicates.py` + `test_reachability.py` stay green (156 passed) |
| 21.2 rollback before recording failure | **needs-fix (minor)** | mechanism verified empirically; two internal notes contradict each other on rollback-expiry; the new test's `last_error` assertion is a tautology |
| 21.3 `failed`/no `completed_at`/backoff | **needs-fix (major)** | `'failed'` needs **no** migration (verified); but the backoff `asyncio.sleep` blocks the entire worker loop and ignores `_shutdown`, and the enumeration of tests needing the backoff monkeypatch is incomplete |
| 21.4 stale-`processing` reaper | **BLOCKER** | `timedelta` is not imported in `src/worker/main.py` and the task never adds it → `NameError` on the first reap (reproduced) |
| 21.5 unknown MIME charset | **OK** | anchors byte-exact; ran pre-fix (`LookupError`) → post-fix (pass) |
| 21.6 coerce rating to int | **OK (minor)** | ran pre-fix (9 failures) → post-fix (pass); Step-2 wording wrong; the mid-snippet `import` line would trip ruff `E402` if pasted literally |
| 21.7 paginate S3 listing | **OK (minor)** | ran pre-fix (`assert 50 == 120`) → post-fix (pass); `for/else` + `_page` are ruff-clean; the 200-page cap lets one poll process 10 000 emails inline |
| 21.8 commit before SES confirm | **needs-fix (major)** | reorder + test shape are right, but the committed-row cleanup misses the `simulation_runs` row `_world` creates → permanent leak in the session-scoped test DB |
| 21.9 instruction failure raises | **needs-fix (major)** | raising makes the **non-idempotent** `migrate_public_thread_to_private` retry up to 3× (duplicate private Slack channel) and emails the PI once per retry |
| 21.10 per-item rollback in sweeps | **BLOCKER** | `await db.rollback()` in a per-item `except` of a *single-session* sweep discards every **earlier** item's already-emailed rows — the exact data loss V4-1 is about. Fix does not close the finding. |
| 21.11 create row after send | **needs-fix (major)** | focus-area (d) hypothesis **refuted** (the token is minted before the row either way) — but the post-send INSERT can now fail on `uq_email_notification_user_thread_category` *after* the mail is out → mail loop + poisoned sweep session |
| 21.12 expire notifications + ladder | **BLOCKER ×2** | (a) the expiry fall-through re-INSERTs `(user_id, thread_decision_id, 'proposal_review')` into a **live UNIQUE constraint**; (b) the tests take `db_session` but are told to live in `tests/unit/…` (and Files/Step-2/Step-4/commit disagree on the path), and the first one raises `MissingGreenlet` on `user.agent` and then attempts a **real SES send** |
| 21.13 retire by `agent_registry_id` | **needs-fix (major)** | re-scope and all four call sites are correct, but 24.2's `except IntegrityError: await db.rollback()` **undoes** 21.13's retire for the race loser, and reconciliation item 2 mis-describes what 21.13 actually does |

Counts: **3 tasks blocked** (21.4, 21.10, 21.12 — 4 blocker findings), **6 need major fixes**,
**4 OK-with-minors**.

---

## 2. Findings, by severity

### BLOCKER B1 — Task 21.4: `timedelta` is never imported into `src/worker/main.py`

`src/worker/main.py:11` is `from datetime import datetime, timezone`. `timedelta` is **not**
imported, is used nowhere else in the file, and Task 21.4's Step 3 shows no import edit (Task 21.1
is the only task in this part that edits that import block, and it adds only `src.agent.ids`).
`reap_stale_jobs`'s *first* statement uses `timedelta`.

Reproduced in the scratch worktree with the task's exact function body:

```
$ .venv-test/bin/python -c "... asyncio.run(m.reap_stale_jobs(FakeFactory()))"
NameError name 'timedelta' is not defined
```

**Corrected text** — add to Task 21.4 Step 3, *before* the constants block:

```python
# src/worker/main.py — imports, BEFORE (:11):
from datetime import datetime, timezone

# AFTER (reap_stale_jobs computes a stale cutoff; `timedelta` was not imported):
from datetime import datetime, timedelta, timezone
```

Also add `src/worker/main.py:11` to Task 21.4's **Files** list ("imports (:11)").

---

### BLOCKER B2 — Task 21.10: the per-item `rollback()` throws away *earlier* items' rows, i.e. it re-implements V4-1 deterministically

All three sweeps share ONE `AsyncSession` for the whole loop and commit ONCE after it
(`email_notifications.py:216`, `:726`, `:917` — verified). The task's own Background paragraph
states the harm precisely:

> "the loop-final `await db.commit()` then raises OUT of the sweep entirely, discarding … every row
> already written for users EARLIER in the loop whose SES send had already succeeded. Those users
> get a duplicate email next cycle with a fresh reply token"

Adding `await db.rollback()` to the per-item `except` produces **exactly that** outcome, and now
unconditionally: user 1's send succeeds and its `EmailNotification` row + `tracker` mutation are
flushed-but-uncommitted; user 2 raises; the new `rollback()` discards user 1's INSERT along with
user 2's mess; the loop continues and the loop-final commit commits nothing for user 1 → user 1
already got the email and has no row → next cycle re-sends with a fresh token. Pre-fix this only
happened when the *final* commit failed; post-fix it happens on **every** per-item failure.

The task's test cannot detect this — it only asserts `rollback_calls` is non-empty. I ran it and it
does pass with the rollback-only patch (`1 passed`), while the data loss above is untouched.

**Corrected text** — Step 3, `check_and_send_notifications`:

```python
        for user in users:
            try:
                sent = await _process_user_notifications(user, db)
                if sent:
                    sent_count += 1
                # Per-item commit (V4-1): the sweep shares ONE session, so a later
                # user's failure must not be able to discard a row for a user whose
                # email has ALREADY gone out. Commit each item, roll back only the
                # item that failed. (Dossier C.7 per-item poller pattern.)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.error(
                    "Error processing notifications for user %s: %s",
                    user.id,
                    exc,
                    exc_info=True,
                )
```

`check_and_send_status_overviews` needs the `continue`s folded so the commit is not skipped:

```python
        for user in users:
            try:
                pref = await get_or_create_pref(user.id, "status_overview", db)
                if pref.enabled and _is_time_to_send(pref.frequency, pref.last_sent_at):
                    if await _send_status_overview(user, pref, db):
                        sent_count += 1
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.error(
                    "Error sending status overview for user %s: %s",
                    user.id, exc, exc_info=True,
                )
```

`check_and_send_new_proposal_emails` (no `continue` in the try body):

```python
                try:
                    if await _maybe_send_new_proposal(td, agent_id_str, db):
                        sent_count += 1
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    logger.error(
                        "Error sending new-proposal email (proposal %s, agent %s): %s",
                        td.id, agent_id_str, exc, exc_info=True,
                    )
```

Leave the existing loop-final `await db.commit()` in place (harmless empty commit).

**Corrected test** (add to Step 1 — the existing one cannot fail for the right reason):

```python
@pytest.mark.asyncio
async def test_an_earlier_users_committed_row_survives_a_later_users_failure(monkeypatch):
    """V4-1's actual harm: the sweep shares one session, so a rollback triggered by user 2
    must not discard user 1's notification row — user 1's email has already been sent and
    without a durable row the next cycle re-sends it with a fresh token."""
    events: list[str] = []

    class _Sess:
        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

        async def execute(self, *a, **k):
            class _R:
                def scalars(s):
                    return s

                def all(s):
                    return [_U("u1"), _U("u2")]

            return _R()

    class _U:
        def __init__(self, i):
            self.id = i

    seen = []

    async def _per_user(user, db):
        seen.append(user.id)
        if user.id == "u2":
            raise RuntimeError("poisoned flush")
        return True

    monkeypatch.setattr(en, "_process_user_notifications", _per_user)
    sess = _Sess()
    await en.check_and_send_notifications(_FakeSessionFactory(sess))

    assert seen == ["u1", "u2"]
    assert events[:2] == ["commit", "rollback"], (
        "user 1's work was not committed before user 2's failure rolled the shared session "
        f"back — u1 got an email with no durable row. events={events}"
    )
```

---

### BLOCKER B3 — Task 21.12: the expiry fall-through violates a live UNIQUE constraint

`alembic/versions/0016_notification_categories.py:72-76` creates
`uq_email_notification_user_thread_category` on `("user_id", "thread_decision_id", "category")`, and
nothing later drops it (`grep -rn uq_email_notification alembic/versions/` → only 0008/0016). The
model records it as a comment (`src/models/email_notification.py:60-64`), so it is migration-only
but present on every migrated database.

The task marks the outstanding row `"expired"` and then **falls through** to
`_get_unreviewed_proposals_for_user` → `send_proposal_notification`, which INSERTs a *new*
`EmailNotification(user_id, thread_decision_id, category="proposal_review")`.
`_get_unreviewed_proposals_for_user` (`email_notifications.py:129-181`, read in full) filters on
`ProposalReview` existence only — it does **not** exclude proposals that already have a notification
row — and sorts by `decided_at`, so `proposals[0]` is the same `td` the expired row was for. The
INSERT therefore collides on the unique constraint essentially every time.

With Task 21.11 also applied (row created *after* SES accepts), the sequence is:
SES sends the mail → `db.flush()` raises `IntegrityError` → caught by
`send_proposal_notification`'s blanket `except` → returns `False` → `tracker.last_notification_sent_at`
is **not** bumped (`:293-295`) → the next 300-second sweep re-sends → **an email to the PI every five
minutes, forever**, with a reply token that has no row (so replying does nothing:
`email_inbound.py:252-255` "No notification found for token"). The session is also left needing
rollback, so every later user in that sweep fails.

**Corrected text** — make the row write an upsert. Replace Task 21.11's post-send block in
`send_proposal_notification` with:

```python
        # Log the notification only now that SES actually accepted it (V4-2). The write
        # must not be able to un-send mail that already went out:
        # uq_email_notification_user_thread_category (migration 0016) means a PREVIOUS
        # row for this (user, proposal, 'proposal_review') — e.g. one this sweep just
        # marked 'expired' (V4-3) — makes a plain INSERT fail. Reconcile that row.
        result = await db.execute(
            select(EmailNotification).where(
                EmailNotification.user_id == user.id,
                EmailNotification.thread_decision_id == thread_decision.id,
                EmailNotification.category == "proposal_review",
            )
        )
        notification = result.scalar_one_or_none()
        if notification is None:
            db.add(EmailNotification(
                user_id=user.id,
                thread_decision_id=thread_decision.id,
                agent_registry_id=agent.id,
                reply_token=reply_token,
                category="proposal_review",
                status="sent",
            ))
        else:
            notification.reply_token = reply_token
            notification.agent_registry_id = agent.id
            notification.status = "sent"
            notification.response_type = None
            notification.responded_at = None
            notification.sent_at = datetime.now(timezone.utc)
        await db.flush()
        return True
```

and apply the same shape in `_send_new_proposal_email` (category `"new_proposal"`).

This requires `_FakeDb` in `tests/unit/test_email_reply_solicitation.py:34-42` to grow an `execute`
(the four pre-existing tests reach this block on the success path and would otherwise
`AttributeError`):

```python
class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, *a, **k):
        class _R:
            def scalar_one_or_none(self):
                return None
        return _R()
```

Alternative (do **not** silently pick it): a new alembic revision replacing the constraint with a
partial unique index on active rows. That contradicts Part 21's header claim "No migration in this
part", so if the coordinator prefers it, the header and the `0025→0028` chain must be amended.

---

### BLOCKER B4 — Task 21.12: the new tests are DB tests placed under `tests/unit/`, the path is self-contradictory, and the first one cannot run

Three separate problems:

1. **Path contradiction.** Task 21.10 creates `tests/unit/test_email_notification_sweeps.py`
   (Files line, Step 1 header, Step 6 `git add`). Task 21.12's Files line, Step 2/Step 4 commands
   and Step 6 `git add` all say **`tests/integration/test_email_notification_sweeps.py`**, while its
   prose says "add … to the file Task 21.10 created". As written the executor is told to add tests
   to a file that does not exist.
2. **Global-constraint violation.** The plan's own constraint is "unit tests must not touch a DB; DB
   tests go under `tests/integration/` and use the `db_session` / `engine` fixtures". Dossier B.3
   says the same: "unit tests never take `db_session`/`client`/`engine`; integration tests do."
   21.12's tests take `db_session`. The task's justification ("mixed unit+integration in one file is
   fine here … see conftest.py's marker list, B.3 of the dossier") is **not what B.3 says** — B.3
   documents marker *placement*, not tier mixing. Putting these in `tests/unit/` breaks the
   offline/no-Docker property of that directory.
3. **The first test errors before its assertion.** `_process_user_notifications` falls through to
   `_get_unreviewed_proposals_for_user`, whose first statement is `if user.agent:`
   (`email_notifications.py:136-137`). The real sweep eager-loads that relationship
   (`selectinload(User.agent)`, `:194`); a `factories.make_user` object does not have it loaded, and
   `User.agent` is a plain lazy relationship (`src/models/user.py:60-62`, no `lazy=`). I confirmed
   with a minimal same-shaped model that the attribute access **emits a SELECT** after flush:

   ```
   accessing u.agent ...
   SQL emitted during attribute access: ['SELECT a.id AS a_id, a.user_id AS a_user_id ']
   ```

   On an `AsyncSession` that is `sqlalchemy.exc.MissingGreenlet`. Then, once that is fixed, the
   fall-through reaches `send_proposal_notification` → `is_allowed_recipient` passes (default
   `outbound_email_allowlist=""`) → `boto3.client("ses", …).send_raw_email(...)`. The new file has
   no `no_outbound_side_effects`-style autouse guard and the test patches neither `boto3.client` nor
   the allowlist: **it attempts a real SES call** (swallowed by the function's blanket `except`, so
   it fails silently and slowly).

**Corrected text** — Task 21.12 Step 1 becomes a NEW file
`tests/integration/test_email_notification_sweeps_expiry.py` (keeping 21.10's unit file
DB-free), with the user re-fetched eagerly and SES blocked:

```python
"""V4-3/V4-4a: an unanswered proposal_review reminder expires instead of being immortal."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import src.services.email_notifications as en
from src.config import get_settings
from src.models import EmailEngagementTracker, EmailNotification, User
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_ses(monkeypatch):
    """Nothing in this file may reach SES."""
    def _boom(*a, **k):
        raise AssertionError(f"boto3.client() called: {a!r} {k!r}")
    monkeypatch.setattr("boto3.client", _boom)


async def _eager(db_session, user_id) -> User:
    """Re-fetch with User.agent loaded — the sweep uses selectinload, and a lazy
    load on an AsyncSession raises MissingGreenlet."""
    return (await db_session.execute(
        select(User).options(selectinload(User.agent)).where(User.id == user_id)
    )).scalar_one()


async def test_an_outstanding_notification_past_the_expiry_window_is_marked_expired(db_session):
    user = await factories.make_user(db_session, email="pi.expiry@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    old_sent_at = datetime.now(timezone.utc) - timedelta(
        days=get_settings().email_notification_expiry_days + 1
    )
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=f"tok-{uuid.uuid4().hex}", category="proposal_review",
        status="sent", sent_at=old_sent_at,
    )
    db_session.add(notification)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    await db_session.flush()
    notif_id = notification.id

    # The proposal is already reviewed, so the fall-through finds nothing to send and
    # cannot reach SES — this test is about the expiry, not the re-send.
    await factories.make_proposal_review(db_session, thread_decision=td, agent_id=agent.agent_id) \
        if hasattr(factories, "make_proposal_review") else None

    await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    await db_session.refresh(notification)
    assert notification.status == "expired", (
        "an unanswered proposal_review notification past the expiry window is still 'sent' — "
        "it is immortal, and it permanently blocks a new reminder from ever going out"
    )
    assert notif_id  # keep the id referenced for the failure message
```

(If `tests/factories.py` has no `make_proposal_review` — verify; it does not at `18ba52c` — insert
the row directly with `ProposalReview(thread_decision_id=td.id, agent_id=agent.agent_id,
user_id=user.id, rating=3, submitted_via="web")` so `_get_unreviewed_proposals_for_user` returns
`[]`.) The control test needs the same `_eager()` treatment even though it returns early.

Add a **third** test that pins B3 so it cannot regress:

```python
async def test_expiring_then_resending_does_not_violate_the_uniqueness_constraint(db_session, monkeypatch):
    """B3: after expiry the sweep falls through and sends again for the SAME proposal —
    uq_email_notification_user_thread_category means the row must be reconciled, not
    re-INSERTed, or the mail goes out and the bookkeeping INSERT fails behind it."""
    ...  # patch boto3.client with a recorder that succeeds; assert send happened AND
         # exactly one email_notifications row exists for (user, td, 'proposal_review')
         # with status 'sent' and a NEW reply_token.
```

---

### MAJOR M1 — Task 21.11: the post-send INSERT can fail after the mail is out (independent of 21.12)

Same constraint as B3, reachable even without 21.12: a `"responded"` row for a proposal that is
still *unreviewed* is producible today. `process_inbound_email:338-352` calls
`mark_notification_responded` **unconditionally**, including when `_handle_instruction` returns
`False` for the inactive-agent (`:605-612`) and already-private-origin (`:655-662`) paths, neither of
which writes a `ProposalReview`. That leaves `(user, td, 'proposal_review')` `responded` with `td`
still unreviewed → next sweep sees no outstanding `sent` row → re-sends → INSERT collides.
Pre-21.11 the flush happened *before* the send, so this failed silently with no mail; post-21.11 the
mail is out first. The corrected upsert in B3 closes this too.

Also, `send_proposal_notification` returning `False` after a successful SES call means
`tracker.last_notification_sent_at` is never advanced — recommend adding to Task 21.11 Step 3:

```python
    except Exception as exc:
        logger.error("Failed to send proposal notification to %s: %s", user.email, exc)
        return False
```
→ keep as-is, but with the upsert above the only remaining `False`-after-send case is a genuine DB
outage, which the per-item commit from B2 will surface as a rollback rather than a mail loop.

**Focus-area (d) verdict: NOT a blocker.** The reply token is minted with
`secrets.token_urlsafe(48)` at `email_notifications.py:328` (and `:993`) — *before* the row and
before `build_reply_address(reply_token)` is used to build the `Reply-To` header. The row only
*logs* the token; it is not the token's source. I applied Task 21.11's diff in the scratch tree and
all four pre-existing solicitation tests, which assert
`msg["Reply-To"].startswith("review+")`, stay green:

```
$ .venv-test/bin/python -m pytest tests/unit/test_email_reply_solicitation.py tests/unit/test_email_templates.py -q
18 passed in 0.78s
```
and the three new tests fail pre-fix / pass post-fix (output quoted in §4).

---

### MAJOR M2 — Task 21.9: raising makes a non-idempotent migration retry, and emails the PI up to 3×

`src/services/private_channels.py:415-419` documents itself: *"Raises on unrecoverable Slack or DB
failures. Partial failures (e.g., the other PI's DM fails) are logged but do not abort the
migration."* It creates a Slack private channel at `:465` before most of its DB work. So an
exception after channel creation leaves a real Slack channel behind.

`_handle_instruction`'s only replay guard is the `ProposalReview` existence check (`:616-628`), and
the review row is written **after** the `try` (`:723-735`). Converting the blanket
`except Exception … return False` (`:717-719`) into `raise InstructionApplyFailed` means the S3
object is **not** deleted, so `poll_inbound_emails` re-runs `process_inbound_email` on the next poll
— the guard still finds no review, and `migrate_public_thread_to_private` runs again, minting a
second `priv-…` channel and re-posting the handover. Up to `MAX_S3_PROCESS_ATTEMPTS = 3` times, then
quarantine. `_notify_instruction_failure` also fires on each attempt → **3 identical emails**, not
the one the task's test asserts.

**Corrected text** — add to Task 21.9 Step 3, immediately after `agent = agent_result.scalar_one()`:

```python
    # Retry safety (COR-32): raising instead of returning False means the S3 object is
    # kept and process_inbound_email re-runs. migrate_public_thread_to_private is NOT
    # idempotent (it creates a Slack channel before most of its DB work, and raises on
    # partial failure — see private_channels.py:415-419), and the ProposalReview guard
    # below only catches a COMPLETED reopen. refined_in_channel is set by the migration,
    # so it is the marker for "a previous attempt already got that far".
    if td.refined_in_channel:
        logger.info(
            "Proposal %s already migrated to %s by an earlier attempt — not re-migrating",
            td.thread_id, td.refined_in_channel,
        )
        return False
```

and cap the PI notification the same way `_HELP_EMAILS_SENT` caps the help email:

```python
# module level, alongside _HELP_EMAILS_SENT (:46):
# notification id -> instruction-failure emails sent (in-memory, like the rate limiter).
_INSTRUCTION_FAILURE_EMAILS_SENT: dict[str, int] = {}


def _notify_instruction_failure(user: User, agent: AgentRegistry, notification: EmailNotification) -> None:
    """PI-facing explanation for a retryable _handle_instruction failure (COR-32).

    Capped at one email per notification: the raise below keeps the S3 object, so this
    site is re-entered on every poll until the object is quarantined.
    """
    key = str(notification.id)
    if _INSTRUCTION_FAILURE_EMAILS_SENT.get(key):
        return
    _INSTRUCTION_FAILURE_EMAILS_SENT[key] = 1
    _send_simple_email(
        user.email,
        f"Couldn't apply your {agent.bot_name} instruction",
        "We ran into a problem applying your instruction to this proposal. "
        "We'll retry automatically; if you don't hear back soon, please try "
        "again from your dashboard at copi.science.",
    )
```

(all four call sites become `_notify_instruction_failure(user, agent, notification)`; `notification`
is a parameter of `_handle_instruction` and in scope at every one of them). Task 21.9's test then
also needs `monkeypatch.setattr(inbound, "_INSTRUCTION_FAILURE_EMAILS_SENT", {})` and a second
assertion that a *second* `process_inbound_email` call sends no further email.

---

### MAJOR M3 — Task 21.8: the test's cleanup leaks a committed `simulation_runs` row

The test commits real rows through a raw `async_sessionmaker(engine, …)` (correctly — the defect
needs a real commit) and cleans up in `finally`. `_world()`
(`tests/integration/test_email_inbound_reply_paths.py:67-89`) calls
`factories.make_thread_decision(db_session, …)`, which (`tests/factories.py:137-140`) creates a
`SimulationRun` when no run is passed. The cleanup deletes `proposal_reviews`,
`email_notifications`, `thread_decisions`, `agents`, `users` — **not `simulation_runs`**, and
`thread_decisions.simulation_run_id` is `ondelete="CASCADE"` in the other direction, so the run
survives every run of this test, permanently, in the session-scoped test database.
`src/services/pi_inbox.py:26-30 get_latest_run_id` and
`src/services/private_channels.py:200-215 _latest_simulation_run_id` both resolve
"the latest run" by `ORDER BY started_at DESC LIMIT 1`, so a leaked row is exactly the shape that
makes another suite flaky.

**Corrected text** — Task 21.8 Step 1, in the `finally` block, capture the run id and delete it.
Add before the `try:`:

```python
        td_run_id = td.simulation_run_id
```
and in the cleanup, after the `thread_decisions` delete:

```python
            await cleanup_db.execute(
                text("DELETE FROM simulation_runs WHERE id = :r"), {"r": td_run_id}
            )
```

Also add a leak guard so a future `_world` change cannot re-open the hole:

```python
        async with factory() as check_db:
            assert await check_db.scalar(
                text("SELECT count(*) FROM simulation_runs WHERE id = :r"), {"r": td_run_id}
            ) == 0, "this test committed a simulation_runs row it did not clean up"
```

---

### MAJOR M4 — Task 21.10 ships a failing test for only 1 of the 3 sites it changes

Global constraint: "Every behaviour change ships a test that fails on the pre-fix code." Task 21.10
adds `await db.rollback()` at three sites and one test, covering
`check_and_send_notifications` only. `check_and_send_status_overviews` and
`check_and_send_new_proposal_emails` get no failing test, so deleting either rollback later is
invisible. Add two more tests to the same (unit) file, monkeypatching
`en._send_status_overview` / `en._maybe_send_new_proposal` to raise, with `_FakeSession.execute`
returning one `User` / one `ThreadDecision` respectively — the same shape as the existing test, and
DB-free (verified: the existing shape runs and passes).

---

### MAJOR M5 — Task 21.13 composed onto 24.2: the `IntegrityError` handler undoes the retire

24.2's `AFTER` body is

```python
    try:
        db.add(review)
        ...
        await mark_notification_responded(current_user.id, thread_decision_id, "review", db)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Already reviewed") from None
```

21.13 changes the argument to `agent.id` (correct), but the race **loser** takes the
`except IntegrityError` arm, whose `rollback()` discards the retire that just happened inside the
try. So the loser's own notification stays `"sent"` — the very state V4-4b exists to close, in the
one path where two people review simultaneously. 21.13's new SELECT-guard branch does not help:
the loser passed that SELECT (that is why it lost the *constraint* race, not the SELECT race).

**Corrected text** — the composed `except` arm:

```python
    except IntegrityError:
        await db.rollback()
        # Someone else (the PI or another delegate) won the race. Their review is the
        # decision for this agent, so still retire THIS responder's outstanding
        # notification (V4-4b) before bouncing them — the rollback above threw away the
        # retire that ran inside the try.
        await record_engagement(current_user.id, db)
        await mark_notification_responded(agent.id, thread_decision_id, "review", db)
        await db.commit()
        raise HTTPException(status_code=400, detail="Already reviewed") from None
```

This requires hoisting 24.2's local import above the `try` (it is currently *inside* it):

```python
    from src.services.email_notifications import (
        mark_notification_responded,
        record_engagement,
    )
    try:
        db.add(review)
        await record_engagement(current_user.id, db)
        await mark_notification_responded(agent.id, thread_decision_id, "review", db)
        await db.commit()
    except IntegrityError:
        ...
```

Note for the executor: 24.2's test `_ReviewRaceSession(raise_at=4)` still holds — hoisting the
import changes no `db.execute` count, and the new handler's two extra `execute` calls happen on
calls 5–6, past the raise point. But the fake's `execute` will be re-entered after the raise, so
`_ReviewRaceSession.execute` must be taught to keep serving results after `raise_at`; add to
Task 24.2's fake:

```python
    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == self._raise_at:
            raise self._raise_exc
        if not self._select_results:
            return _FakeResult(None)      # post-guard cleanup selects
        return _FakeResult(self._select_results.pop(0))
```

---

### MAJOR M6 — reconciliation items 2 and 7 mis-describe Part 21

Line 58 (item 2): *"then by 21.13 (retire notifications by `agent_registry_id`; **move**
`mark_notification_responded` before the "Already reviewed" 400) … 21.13's **relocated**
`mark_notification_responded` call sits **before the SELECT guard**, outside the try."*

Task 21.13 does not move or relocate anything. It **keeps** the success-path call inside 24.2's try
and **adds a second** call *inside* the `if existing.scalar_one_or_none():` branch (i.e. **after**
the SELECT), followed by `await db.commit()` and then the 400. An executor following the
reconciliation would produce different code from the task.

**Corrected reconciliation item 2** (replace the second sentence):

> 21.13 is applied ON TOP of 24.2's guarded body. The guard's `try:` must still start at
> `db.add(review)` and end after `await db.commit()`, and 24.2's local
> `from src.services.email_notifications import …` must be **hoisted above** the `try` so the
> `except IntegrityError` arm can use it. 21.13 does NOT move the success-path
> `mark_notification_responded`; it (a) re-points its first argument to `agent.id`, (b) adds a
> second `record_engagement` + `mark_notification_responded` + `commit` **inside** the
> `if existing.scalar_one_or_none():` branch, before the 400, and (c) adds the same pair to 24.2's
> `except IntegrityError` arm after its `rollback()` (see Part 21 finding M5). The executor of 21.13
> re-reads the function first and re-runs `tests/unit/test_concurrent_write_guards.py`.

Line 71 (item 7) lists ``reopen_proposal`` (21.8, 21.13)``. **Task 21.8 does not touch
`src/routers/agent_page.py` at all** (its Files list is `src/services/email_inbound.py` only). Fix
to `` `reopen_proposal` (21.13) ``.

---

### MAJOR M7 — Task 21.3's backoff sleeps inside `process_job`, blocking the whole worker loop and SIGTERM

`asyncio.sleep(delay)` in `process_job`'s except branch stalls `run_worker` entirely: no
`claim_job` for any *other* pending job, no notification sweep, no inbound-email poll for the
duration. With the proposed `JOB_RETRY_BACKOFF_BASE_SECONDS = 5.0` and the default
`max_attempts = 3` that is `5 + 10 = 15 s` per failing job; `JOB_RETRY_BACKOFF_CAP_SECONDS = 300`
means a job with `max_attempts > 7` can stall the worker for five minutes. The sleep is also not
`_shutdown`-aware: `docker-compose.prod.yml`'s `worker` service sets **no** `stop_grace_period`
(verified, lines 59-82), so Docker's 10 s default SIGKILLs the container mid-sleep on
`up -d --build worker`. (No data is lost — the commit precedes the sleep — but the shutdown is a
hard kill.)

Two acceptable corrections; pick one and state it in the task:

**(a) Cheap, keeps the sleep, bounds the harm.** Chunk it and honour the shutdown flag:

```python
                delay = min(
                    JOB_RETRY_BACKOFF_CAP_SECONDS,
                    JOB_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, job.attempts - 1)),
                )
                logger.info(
                    "Job %s will retry in %.1fs (attempt %d/%d)",
                    job.id, delay, job.attempts, job.max_attempts,
                )
                # Sleep in slices so SIGTERM is honoured: this sleep blocks the whole
                # run_worker loop, and the worker service has no stop_grace_period, so
                # Docker's 10s default would otherwise SIGKILL us mid-backoff.
                waited = 0.0
                while waited < delay and not _shutdown:
                    step = min(1.0, delay - waited)
                    await asyncio.sleep(step)
                    waited += step
```
(`max(0, job.attempts - 1)` also removes the `2 ** -1 = 0.5` case that
`test_an_unknown_job_type_is_rejected_loudly_by_the_dispatcher` hits by calling `process_job` with
`attempts = 0`.)

**(b) Better, still no migration.** Do not sleep at all; make `claim_job` skip a job whose last
attempt is too recent, so the loop stays free:

```python
async def claim_job(db: AsyncSession) -> Job | None:
    """Atomically claim the next pending job whose retry backoff has elapsed."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Job)
        .where(
            Job.status == "pending",
            Job.attempts < Job.max_attempts,
            # Exponential backoff between attempts of the SAME job (COR-18c) without
            # blocking the loop: started_at is when the previous attempt was claimed.
            or_(
                Job.attempts == 0,
                Job.started_at.is_(None),
                Job.started_at
                < now
                - func.make_interval(
                    secs=func.least(
                        JOB_RETRY_BACKOFF_CAP_SECONDS,
                        JOB_RETRY_BACKOFF_BASE_SECONDS * func.pow(2, Job.attempts - 1),
                    )
                ),
            ),
        )
        .order_by(Job.enqueued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
```
(needs `or_`, `func` added to the `sqlalchemy` import.) If (b) is chosen, Task 21.3's two new tests
must be rewritten to assert `claim_job` returns `None` immediately after a failure and returns the
job once `started_at` is backdated — which is a *better* test than the wall-clock timing one.

---

## 3. Minor findings

1. **21.2 vs 21.3 contradict each other on rollback semantics.** 21.2's Step-3 note: *"`await
   db.rollback()` expires its attributes … so the very next attribute access … triggers a fresh
   SELECT"*. 21.3's Step-3 note: *"reading an attribute right after `rollback()`/`commit()` on an
   `expire_on_commit=False` session … does NOT trigger a re-fetch"*. Both are individually true (of
   `rollback` and of `commit` respectively) but read as a contradiction. I verified the composite
   behaviour is correct with a minimal model: after `rollback()` the modified `last_error` survives
   (it is excluded from `state.expired_attributes.intersection(self.unmodified)`) and
   `job.attempts` re-reads `1` from the DB; the flushed-but-uncommitted partial write is discarded
   (`P rows: 1`), confirming 21.2's `leaked == 1 → leaked == 0` inversion. Reword 21.3's note to
   "…right after `commit()`".
2. **21.2's new test contains a tautological assertion.**
   `assert row.last_error and "researcher_profiles" in row.last_error.lower() or row.last_error`
   parses as `(A and B) or C`; with `C` truthy it can never fail on the substring. Replace with
   `assert row.last_error, f"the failure reason never reached the row: {row.last_error!r}"`.
3. **21.3 leaves two in-repo notes asserting the opposite of what it ships.**
   `src/services/profile_pipeline.py:347-351` says *"templates/onboarding/profile_review.html keys
   its 'Try Again' control on job_status == 'failed', which src/worker/main.py never assigns (it
   only ever writes 'pending' or 'dead'), so a dead job falls through to that template's
   `elif profile` branch"*; and `tests/unit/test_reachability.py:38-49` records
   `POST /onboarding/retry` as a *"Known false negative … unreachable at runtime"*. Both become
   false. Add both files to Task 21.3's Files list with the corrected prose (docstring/comment
   only — I confirmed by running `tests/unit/test_reachability.py` (156 passed with 21.1 applied)
   that nothing *asserts* it; `KNOWN_*` sets are empty).
4. **21.3's list of tests needing the backoff monkeypatch is incomplete.** It names three
   (`:381`, `:485`, `:526`). Also reaching the `pending` branch, and therefore also sleeping:
   `test_a_crash_after_partial_work_leaves_a_retryable_job` (:709, 1 sleep),
   Task 21.2's own replacement test (2 sleeps),
   `test_monthly_refresh_for_a_missing_user_fails_loudly` (:898, asserts `pending` at :919),
   `test_a_job_with_no_user_at_all_fails_loudly` (:930), and
   `test_an_unknown_job_type_is_rejected_loudly_by_the_dispatcher` (:1030). At the proposed 5 s base
   that is ~40 s of pure sleep added to the suite. No test times out (there is no timeout plugin in
   `pyproject.toml`), so this is slowness, not breakage.
5. **21.3's `grep -n '"dead"' tests/integration/test_worker.py` post-check is right** — the three
   hits are :410, :515, :590 and the plan flips all three. `'failed'` also needs **no** migration:
   `alembic/versions/0001_initial.py:137-148` creates `job_status_enum` with
   `"pending","processing","completed","failed","dead"`, and no later revision touches the `jobs`
   table (`grep -rn '"jobs"' alembic/versions/` → only 0001). Both admin templates already handle
   `'failed'` (`templates/admin/jobs.html:15,36,69`, `templates/admin/user_detail.html:155`) and
   both render `completed_at` as `… if job.completed_at else '—'` (`jobs.html:88`,
   `user_detail.html:160`), so dropping the failure-path `completed_at` is safe. `admin.py:126`'s
   `j.status in ("pending","processing")` and `onboarding.py:89`'s `job.status` need no change.
   Part 21's header claim "No migration in this part" is **correct** for `job_status_enum` and for
   `email_notifications.status` (a plain `String(20)`, `models/email_notification.py:40-42`,
   migration 0008 `sa.String(20)`), so `'expired'` needs no migration either — but see B3: the
   *unique constraint*, not the column type, is what 21.12 breaks.
6. **21.4 reaper / `claim_job` lock interplay is safe** (focus area (b)): `claim_job` takes
   `FOR UPDATE SKIP LOCKED` only on `status = 'pending'` rows and commits immediately (`:48-51`);
   the reaper only reads/writes `status = 'processing'` rows. Disjoint predicates, no deadlock, no
   blocking. `started_at` semantics are also right: `claim_job` re-stamps it on **every** claim, so
   the stale window restarts per attempt. Three residual nits: (a) the reaper's per-item SELECT has
   no `with_for_update(skip_locked=True)`, so two worker replicas would both reap a row (idempotent
   writes, but `reaped` double-counts) — the prod compose runs one worker, so this is latent;
   (b) `Job.started_at < threshold` silently skips a `processing` row with `started_at IS NULL`
   (unreachable via `claim_job`, but a hand-INSERTed row would be immortal — consider
   `or_(Job.started_at.is_(None), Job.started_at < threshold)`); (c) the reaper overwrites
   `job.last_error` with its own message, destroying the real error from the crashed attempt.
7. **21.4's `run_worker` AFTER block mis-places a comment.** It leaves
   `# Email notification check (throttled)` attached to `now = asyncio.get_event_loop().time()` and
   then inserts the whole reaper block between that comment and its `if`. Move the comment down to
   sit directly above `if now - last_notification_check >= …`.
8. **21.4's Step-5 claim is right but under-guarded.** `reap_stale_jobs` has no `payload->>'tag'`
   filter, and `test_run_worker_loop_survives_a_crashing_job` drives the *real* loop against the
   shared test DB. Its existing pre-flight only checks `foreign_pending_jobs() == 0`. Add a
   `foreign_processing_jobs()` twin, or filter the reaper's batch SELECT in the test via a
   monkeypatched threshold. Low risk (every other suite rolls back, and `wk.sweep()` cleans this
   file's rows), so a note suffices.
9. **21.6's Step-2 expectation is wrong.** `TestCoerceRating` does **not** fail to collect; the
   class body has no module-level reference to `_coerce_rating`, so the tests collect and fail with
   `AttributeError: module 'src.services.email_inbound' has no attribute '_coerce_rating'` at call
   time. Observed output in §4.
10. **21.6 and 21.12 show `import` lines inside a snippet that would land mid-file.** `tests/` must
    be ruff-clean (zero findings, `scripts/ci.sh` step 3), and `E402` is in the selected rule set
    (`pyproject.toml:67`). 21.6's line is annotated `# already imported at the top of this file`
    (it is — `test_email_inbound_hardening.py:15`), so it is only confusing; 21.12's six-line import
    block is not annotated at all. Both should say explicitly "merge into the module's existing
    top-level import block".
11. **21.7's 200-page cap changes the per-poll work ceiling by 200×.** Pre-fix a poll processed at
    most 50 objects; post-fix up to 10 000, each with its own DB session and (for a real reply) an
    LLM classification, all inline in `run_worker`. Recommend `_MAX_S3_LIST_PAGES = 20`
    (1 000 objects/poll) plus an explicit note that the poll blocks the job queue while it runs.
    The rest of 21.7 is clean: `for … else` is used correctly (the warning fires only when all pages
    are consumed without a `break`), and `_page` is underscore-prefixed so `B007` does not fire —
    confirmed, `ruff check src/` stays at exactly **254** findings with 21.1/21.5/21.6/21.7/21.10/
    21.11 all applied (baseline 254).
12. **21.8's "already responded" backstop is at :256-258, not :254-255** (cited twice). The guard
    text is `if notification.status != "sent": logger.info(…); return` — the reasoning is right, the
    line numbers are not.
13. **21.8's claim about the existing tests is correct.** `tests/conftest.py:85-101` builds
    `db_session` with `join_transaction_mode="create_savepoint"`, so the added
    `await db.commit()` is a savepoint release inside the outer transaction that teardown rolls
    back — all existing `test_email_inbound_reply_paths.py` tests are unaffected, as claimed.
14. **21.12's `email_notification_expiry_days` plumbing is sound** (focus area (e)):
    `Settings` uses `SettingsConfigDict(env_file=".env", extra="ignore")` with no `env_prefix`
    (`src/config.py:99`), so `EMAIL_NOTIFICATION_EXPIRY_DAYS` maps automatically; the only consumer
    is `_process_user_notifications`; `timedelta` and `get_settings` are already imported
    (`email_notifications.py:5,12`). Two nits: the claim "`grep -n "expir" src/config.py`" returns
    zero is not literally true (one comment hit at `:468`, no setting — the substance holds), and
    the model comment it cites is at `src/models/email_notification.py:42`, not `:41`.
15. **21.12 introduces silent drops of late replies.** Once a row is `expired`,
    `email_inbound.py:256-258` returns early, so a PI replying to a 15-day-old email gets nothing —
    not even the help email (`_send_help_email` is only reached from the unparseable branch, which
    is downstream of that early return). Worth one sentence in the task, or an `expired`-specific
    branch that sends the "reply window closed, use the dashboard" note.
16. **21.13's re-scope is complete** (focus area (f)). `mark_notification_responded` has exactly
    four callers, all in the task's file list: `email_inbound.py:334`, `:348`,
    `agent_page.py:511`, `:714` (`grep -rn mark_notification_responded src/ tests/ scripts/` —
    the only other hit is a comment in `tests/integration/test_proposal_review.py:681`).
    `notification.agent_registry_id` exists and is `NOT NULL`
    (`models/email_notification.py:29-32`); `agent` is in scope at both `agent_page.py` sites
    (`:473`, and `:585` uses `agent.agent_id`). The delegate case is genuinely covered: the
    delegate path is reachable (`src/dependencies.py:137-145` returns `(agent, False)` for a row in
    `agent_delegates`), and `review_proposal` writes `user_id=agent.user_id` /
    `delegate_user_id=current_user.id`, so `agent.id` is the only identifier shared by the PI's and
    the delegate's notification rows. `AgentDelegate(agent_registry_id=…, user_id=…)` fills every
    NOT NULL column (`notify_proposals` has a Python default of `True`,
    `src/models/delegate.py:82-84`), and the test's fixture signature
    `(client, db_session, lab, proposal)` is valid — `proposal` pulls `llm` transitively
    (`tests/integration/test_proposal_review.py:263-264`).
17. **21.3's "no other part touches this file" is wrong.** Task 25.5 edits `src/worker/main.py`
    (`run_worker`'s engine construction and the `create_async_engine` import) — MASTER_PLAN.md
    :15377, :15469. Reconciliation item 5 already sequences 25.5 after Part 21, so this is a
    wording fix only. Conversely, Part 21's header claim that another part explicitly cedes
    `specs/local-db-conversations.md:66-67` **checks out** (MASTER_PLAN.md :16361, :17274).
18. **V11-h is only partly closed.** The finding calls the spec's count "wrong three ways over
    (4 slots / 5 with worker / 6 with slot 99)". 21.1's replacement prose says "Five writer-slot
    claims exist today (`src/agent/ids.py`)" — accurate for `ids.py`, but it never mentions
    `REMEDIATION_WRITER_SLOT = 99` (`scripts/migrate/remediate_duplicates.py:131`), and the task
    explicitly tells the executor not to add it. Acceptable as scoped, but the coverage matrix
    should say so rather than marking V11-h fully done.
19. **21.1's `monkeypatch.setattr(worker_main.signal, "signal", …)` patches the stdlib module
    globally** for the duration of the test (`worker_main.signal` *is* the `signal` module).
    `monkeypatch` restores it, and the test is synchronous, so this is safe — but
    `monkeypatch.setattr(worker_main, "signal", types.SimpleNamespace(signal=lambda *a, **k: None,
    SIGTERM=15, SIGINT=2))` would be strictly better hygiene.

---

## 4. Test-validity evidence (commands actually run)

Scratch worktree: `rsync -a --exclude .git --exclude backups --exclude .venv-test` of the repo, the
task diffs applied with a Python patch script, run with the repo's `.venv-test`.

**21.1 — post-fix**
```
$ .venv-test/bin/python -m pytest tests/unit/test_worker_writer_slot.py tests/unit/test_ids.py -q
13 passed in 1.13s
$ .venv-test/bin/python -m pytest tests/unit/test_reachability.py tests/unit/test_remediate_duplicates.py -q
156 passed in 3.23s
```
(Pre-fix both new tests fail at import/collection on `WRITER_WORKER`, as the plan says.)

**21.5 / 21.6 / 21.7 — post-fix then pre-fix**
```
$ .venv-test/bin/python -m pytest tests/unit/test_email_inbound_hardening.py -q      # fixed src
27 passed in 1.39s

$ # same tests, original src/services/email_inbound.py restored:
FAILED …::test_unknown_declared_charset_falls_back_instead_of_raising
FAILED …::TestCoerceRating::test_accepts_a_numeric_string        (8 more TestCoerceRating rows)
FAILED …::test_classify_reply_coerces_a_string_rating_to_int
FAILED …::test_list_objects_v2_is_paginated_across_multiple_pages
   AssertionError: only 50/120 objects were processed — the listing stopped after the first page
   assert 50 == 120
11 failed, 1 passed
```
The one pre-fix pass is `test_a_garbled_but_known_charset_still_decodes_lossily`, which is the
task's declared control — correct by design.

**21.10 / 21.11 — post-fix then pre-fix**
```
$ .venv-test/bin/python -m pytest tests/unit/test_email_notification_sweeps.py \
    tests/unit/test_email_reply_solicitation.py tests/unit/test_email_templates.py -q   # fixed src
18 passed in 0.78s

$ # same tests, original src/services/email_notifications.py restored:
FAILED …test_email_notification_sweeps.py::test_check_and_send_notifications_rolls_back_after_a_per_user_failure
FAILED …test_email_reply_solicitation.py::test_review_reminder_does_not_log_a_notification_when_ses_fails
FAILED …test_email_reply_solicitation.py::test_new_proposal_alert_does_not_log_a_notification_when_ses_fails
FAILED …test_email_reply_solicitation.py::test_new_proposal_alert_does_not_log_a_notification_when_allowlist_suppressed
   assert [<EmailNotification user=… category=new_proposal status=sent>] == []
4 failed, 6 passed
```
All four new DB-free tests fail pre-fix and pass post-fix. **All new `tests/unit/` tests in this part
are genuinely DB-free** (focus area (g)): 21.1 fakes `set_default_writer_id`/`signal`/`asyncio.run`;
21.10 fakes the session factory with `__aenter__`/`execute`/`commit`/`rollback`; 21.11 reuses the
existing `_FakeDb` (`tests/unit/test_email_reply_solicitation.py:34-42`) and its `.added` list. The
only exception is 21.12 — see B4.

**Lint parity**
```
$ ./.venv-test/bin/python -m ruff check src/            # pristine tree
Found 254 errors.
$ (scratch tree, 21.1+21.5+21.6+21.7+21.10+21.11 applied)
Found 254 errors.
$ (scratch tree) ruff check tests/unit tests/integration
All checks passed!
```
Net-zero on the `SRC_LINT_MAX=260` ratchet, and the new test code is ruff-clean.

**21.2's mechanism (minimal same-shaped model, sync SQLite)**
```
caught: IntegrityError
after rollback, reading job.attempts -> 1
post-commit in-memory attempts: 1 status: pending
DB row: pending 1 '(sqlite3.IntegrityError) UNIQUE constraint failed: p.user_i…'
P rows: 1
```
Confirms all three of 21.2's claims: the rollback-then-write sequence works, `last_error` survives
the refresh, and the partial write is discarded (so `leaked == 1 → leaked == 0` is the right
inversion).

**B4's `MissingGreenlet` root cause (minimal same-shaped model)**
```
accessing u.agent ...
SQL emitted during attribute access: ['SELECT a.id AS a_id, a.user_id AS a_user_id ']
```

**B1**
```
$ .venv-test/bin/python -c "... asyncio.run(m.reap_stale_jobs(FakeFactory()))"
NameError name 'timedelta' is not defined
```

**Assertions the plan says to invert — verified byte-for-byte on the current tree**

`tests/integration/test_worker.py:410-413`:
```python
    assert state.status == "dead", (
        f"a job that failed max_attempts times is {state.status!r}, not 'dead' — "
        "nothing will ever move it out of the queue's way"
    )
```
`:515`: `assert (await wk.job_state(j_bad)).status == "dead"`
`:589-591`:
```python
    bad = await wk.job_state(j_bad)
    assert bad.status == "dead" and bad.attempts == 2, (
        f"the crashing job ended {bad.status!r} after {bad.attempts} attempts"
    )
```
`:753-756`:
```python
    # Characterization of the partial write described above.
    assert leaked == 1, (
        "the partial profile write was rolled back — good, but the docstring and the "
        "T5 report describing the except-branch commit are now out of date"
    )
```
`:794-799` (the test 21.2 replaces):
```python
    assert state.status == "processing", (
        f"the job is {state.status!r}; if the error handler now survives a database "
        "error this test should assert 'pending' and the bug report is stale"
    )
    assert (await wk.job(jid)).last_error is None, (
        "the failure reason reached the row after all — the handler's commit succeeded"
    )
```
`_one_round_expecting_escape` at `:817-827` has exactly one caller (`:793`), so deleting it after
the replacement is correct. All four inversions are quoted accurately in the plan.

---

## 5. Anchor mismatches (all verified against `18ba52c`)

| Task | Plan says | Actually |
|---|---|---|
| 21.1 | `remediate_duplicates.py` comment `:125-129` | `:125-130` (6 lines; `REMEDIATION_WRITER_SLOT` at `:131`) |
| 21.1 | "stdlib imports `:1-7`" | `:3-7` (`:1` is the docstring, `:2` blank) |
| 21.2 | `test_a_database_error_…_orphans…` `:760-810` | `:760-813` |
| 21.2 | `_one_round_expecting_escape` `:817-826` | `:817-827` |
| 21.3 | T5.2 `"dead"` assertion `:410-412` | `:410-413` |
| 21.3 | T5.3 `"dead"` assertion `:512` | `:515` |
| 21.3 | T5.3 test `:485-522` | `:485-523` |
| 21.3 | `test_run_worker_loop…` `:530-591`, `SimpleNamespace` `:559-565` | `:526-591`, `:561-568` |
| 21.5 | `_decode_part` `:386-389` | ✓ exact, byte-for-byte |
| 21.6 | `classify_reply` `:449-521`, tail `:514-518`, guard `:320-322` | `:449-520`; tail `:511-516`; guard at `:322` |
| 21.7 | `poll_inbound_emails` `:149-211`, BEFORE `:158-168`, `_FakeS3` `:217-234` | `:150-215`; BEFORE ✓ exact; `_FakeS3` `:217-243` |
| 21.8 | `process_inbound_email` `:219-360`; guard `:254-255` (×2); review `:319-336`; instruction `:338-353` | `:219-367`; guard `:256-258`; review ✓; instruction ✓ |
| 21.9 | `_handle_instruction` `:572-735`; sites `:680-682`, `:698-699`, `:703-704`, `:717-719` | ✓ all exact |
| 21.10 | sweep 1 try `:203-213`; sweep 2 `:713-720`; sweep 3 `:907-916` | `:204-214`; `:713-725`; `:909-916` (BEFORE quotes all match text) |
| 21.11 | `send_proposal_notification` `:308-341`, BEFORE `:325-341`, SES `:481-495`; `_send_new_proposal_email` `:951-1004`, BEFORE `:990-1004`, tail `:1066-1075` | `:308-495`; BEFORE starts `:328`; SES ✓ exact; function at `:984`; BEFORE starts `:992`; tail ✓ exact |
| 21.12 | `src/config.py:157-159`; `_process_user_notifications` `:221-278`; outstanding `:240-250`; increment `:294-295`; model comment `:41` | config ✓ exact; function `:221-297`; outstanding ✓ exact; increment ✓; comment at `:42` |
| 21.13 | `mark_notification_responded` `:598-615`; `review_proposal` guard `:486-497`, success `:509-511`; `reopen_proposal` `:583-590`, success `:712-714` | `:598-616`; guard `:487-494`, success ✓ exact; `reopen` block `:588-594`, success ✓ exact |
| docs | `docs/inbound-email.md:84-90`; `specs/local-db-conversations.md:66-67` | ✓ both exact, byte-for-byte |

Every quoted **BEFORE code block** matches the tree modulo whitespace; the mismatches above are all
line-range labels, which the plan's own global constraint ("always relocate by the named symbol")
covers. The two substantive citation errors are 21.8's `:254-255` guard (off by 2, cited twice as
the safety argument for the whole task) and 21.11's `_send_new_proposal_email` "`:951-1004`" in the
Files line (the function starts at `:984`).

---

## 6. Coverage check (focus area (a) and matrix)

Every STILL-PRESENT id in `findings/issue_21.md` / `issue_21_redteam.md` maps to a task, and the two
`FIXED` rows (COR-19.1, COR-19.2) are correctly excluded. I re-verified the four QUALIFIED rows land
where the matrix says:

* **COR-18f** — 21.3's "no other code change" claim is right. `templates/onboarding/profile_review.html:47-57`
  is the only `/onboarding/retry` control, `src/routers/onboarding.py:317-336` enqueues
  unconditionally, and `onboarding.py:79`'s self-heal is gated on `job is None`, so a `'failed'` job
  now renders the retry form instead of a blank body. **All `Job.status` readers in `src/` and
  `templates/`**: `worker/main.py:39,48,95,105,108`, `routers/onboarding.py:89`,
  `routers/admin.py:126,251,260`, `templates/admin/jobs.html:12-16,36,69,74-75`,
  `templates/admin/user_detail.html:155-156`, `templates/onboarding/profile_review.html:28,47`.
  Only `worker/main.py` needs changing; every other reader already handles `'failed'`. No migration.
* **COR-32** — 21.9 covers the default-config reachability the red-team sharpened (the
  `enable_private_refinement = True` path at `src/config.py:391` → `email_inbound.py:632-638` inside
  the same `try`), but see M2 for the retry-idempotency hole it opens.
* **V4-2** — 21.11 covers both forms; `_send_html_email`'s allowlist check is at `:636-640`, before
  the `boto3.client` call at `:646`, so the reorder does close the "created before the allowlist
  check" form. Verified by the third new test.
* **V4-4b** — 21.13 covers it; see M5 for the one path (the concurrent-race loser) it still misses
  once composed with 24.2.

The one coverage gap is bookkeeping, not defect coverage: the matrix marks **V11-h** done while the
`REMEDIATION_WRITER_SLOT = 99` half of the count is deliberately left alone (minor #18).
