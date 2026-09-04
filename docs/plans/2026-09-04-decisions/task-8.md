# Task 8 — #20 COR-5: the carrier for the implicit PI review

**Status: RULED BY BRANCH OWNER 2026-09-04.** Escalated by
`docs/plans/2026-09-04-close-remaining-gaps.md` Task 8; ruled before the task started. The
implementer implements this ruling and fills in `## Evidence`. It does **not** re-decide it.

## Ruling

**Chosen: option (a) — the carrier is `thread_decisions.pi_engaged_at`, a nullable `timestamptz`.**
This is `audit-over-implementation.md`'s own recommendation (*Candidate A point 2*), which v1 adopted
against without engaging.

**`0029` carries exactly two things:** this column and Task 7's handled-marker. Nothing else.
Nullable, no server default, no backfill — absent means "unknown", which every reader must treat as
today's behaviour.

**Explicitly rejected: (b) making `proposal_reviews.user_id` nullable.** That column is
`ForeignKey("users.id", ondelete="CASCADE")` (`src/models/agent_registry.py:82-84`), so deleting a PI
would destroy the engine's block-clearing markers for every proposal that PI engaged with, and those
proposals re-block after the next restart. It also breaks
`tests/integration/test_proposal_review.py:621-666`, which manufactures its `IntegrityError` from
that very `NOT NULL` constraint. Matching the issue's literal wording is not worth a destructive
carrier.

**Explicitly rejected: (c) carve COR-5's second half out of `Closes #20`.** The reassurance it rests
on — "0 of 53 active agents with `AgentRegistry.user_id IS NULL`" — is one button press away from
false (`audit-phase8-functional.md` I3: `POST /profile/delete-account`, `src/routers/profile.py:259`,
leaves `status=active, user_id IS NULL`), and `0026` on this branch removed the `CheckViolation` that
had been an accidental guardrail. Task 25 closes that path, but the carrier must not depend on it.

Correct the two v1 errors before writing anything: `src/models/thread_decision.py` **does not
exist** — `ThreadDecision` is `src/models/agent_activity.py:213` and `ProposalReview` is
`src/models/agent_registry.py:70`. v1 swapped them and invented a filename.

## Evidence

*To be filled by Task 8's implementer.* What must be here:

- `tests/integration/test_migration_0029.py`'s pre-fix failure (plan Step 5: `0029` does not exist),
  and the pass after, including the `downgrade 0028` round trip.
- `alembic heads` → exactly one head, `0029`.
- The production-copy chain run (plan Step 10): **what schema state you started from** on
  `copi-prodtest-db` (`127.0.0.1:55434`) — v1's `pristine_0024.dump` does not exist in this repo —
  plus wall time and exit codes for `preflight.py --snapshot …`, `alembic upgrade head`,
  `postflight.py --snapshot …`, and an explicit list of what postflight did **not** check
  (`audit-phase8-migration.md` C1: a concurrent deleter inside a duplicate group; M9: no `--snapshot`
  ⇒ WARN and exit 0).
- **0029's lock row for the operator table** (plan Step 11), measured, with a sentence saying what
  your figure is a measurement *of*. `audit-phase8-migration.md` **N4** records the published
  "worst-case lock window 0.3 s" as not a measurement of this chain, so do not inherit it. Task 33
  Step 11 applies your row to Part R.6 of `docs/plans/2026-09-02-close-issues-20-27.md`; do not edit
  that file yourself.

## Consequence a closing comment must state

`#20`'s closing comment must state the **substitution** against the issue's literal `Fix:` wording.
The clause reads "require the sender be the owning PI; insert a `ProposalReview`". The first half
shipped as written. The second half is implemented as a nullable `thread_decisions.pi_engaged_at`
marker **instead of** a `ProposalReview` row, because `proposal_reviews.user_id` is
`ondelete="CASCADE"` to `users` and a PI deletion would therefore erase the engine's block-clearing
markers — the issue's literal instruction would have built a self-erasing record. The behaviour the
issue asks for (`_rebuild_agent_state` does not re-block the proposal after a restart) is delivered;
the row shape is not the one the issue names.
