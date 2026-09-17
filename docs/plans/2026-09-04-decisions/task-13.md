# Task 13 — is the reopen recovery arm's re-bind machinery live or dead? (verify-deploy 1a)

Plan: `docs/plans/2026-09-04-close-remaining-gaps.md`, Task 13 residue 1. The plan required
the machinery to be **removed or proved live**, because "correcting the comment while
leaving dead code makes the code unexplained rather than wrongly explained".

## Ruling

**Dead. Removed** — the capture (`refined_channel_id = td.refined_in_channel`), the
assignment in the `except IntegrityError` arm, and the "recovered by re-binding" log
clause. The `ThreadDecision` re-select stays, with `scalar_one_or_none()` and a new
purpose; see "What was kept, and why" below.

## Evidence

Two independent measurements, both against `d7ce1a5` (Task 12's tip), Slack-off migration
path, real Postgres.

**1. The value it restores is already committed, so there is nothing to restore.** Both
migration paths write `refined_in_channel` and then `await db.commit()` before returning:
`src/services/private_channels.py:397,415` (offline, landed by Task 11 `391e545`) and
`:629,644` (Slack-on, landed by `34d3c15`). Nothing runs between the assignment and the
commit that can raise, and a failure of the commit itself leaves the route in the
`except Exception -> HTTPException(500)` arm around the migration call, which never reaches
the write block. So at the moment the arm runs, the captured value and the committed value
are necessarily equal.

**2. The re-bind emitted no SQL.** A `before_cursor_execute` recorder on the test engine,
driving `reopen_proposal` into a real `IntegrityError` on
`uq_proposal_reviews_decision_agent`, captured everything the arm did after its
`ROLLBACK TO SAVEPOINT`:

```
ROLLBACK TO SAVEPOINT sa_savepoint_3
SAVEPOINT sa_savepoint_4
SELECT thread_decisions.… WHERE thread_decisions.id = %s          <- the re-select
SELECT proposal_reviews.… WHERE thread_decision_id = %s AND agent_id = %s
SELECT email_engagement_tracking.…
SELECT email_notifications.…
RELEASE SAVEPOINT sa_savepoint_4
```

No `UPDATE thread_decisions` at all: assigning the identical value SQLAlchemy had just
loaded produces no statement. The machinery was not merely redundant, it was already a
no-op at the wire.

Pinned by
`tests/integration/test_proposal_review.py::test_refined_in_channel_survives_the_lost_race_without_the_arm_re_binding_it`,
which asserts both halves — the channel and `refined_in_channel` survive the rollback, and
the arm emits no `UPDATE thread_decisions` after it. It is an *unreachability* proof, not a
red-then-green test: it passed before the removal too. Its value is that it is armed
against the regression that would make the machinery necessary again — if a migration path
stopped committing, the AgentChannel count goes to 0 and `refined_in_channel` to `None`.

## What was kept, and why

The re-select itself, narrowed to `select(ThreadDecision.id)` and read through
`scalar_one_or_none()`. It is no longer a repair, it is the diagnosis: an `IntegrityError`
out of that block is *usually* the review-uniqueness conflict, but an FK violation from a
concurrently deleted `ThreadDecision` produces one too, and that is the only shape that
explains an empty `winner`. The arm now logs the two cases differently instead of calling
both "unexpected". Deleting the query outright was the first implementation and is
strictly cleaner, but it shifts the fake-session script in
`tests/unit/test_concurrent_write_guards.py` (whose `select_results` list encodes the
call order) by one, and Task 13 does not own that file — see the report's residuals.

## Consequence for the closing comments

- **#24 V5 (i)** — closed: the arm no longer 500s when the decision has vanished.
- **verify-deploy finding 1a** — closed by removal, not by a comment fix.
- Deploy note: none. No behaviour a PI or an operator can observe changes, beyond two log
  lines that now say something true.
