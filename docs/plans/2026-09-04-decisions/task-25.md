# Task 25 — #25 D1: a PI can delete their own account and orphan a live agent

**Status: RULED BY BRANCH OWNER 2026-09-04.** Escalated by
`docs/plans/2026-09-04-close-remaining-gaps.md` Task 25; ruled before the task started. The
implementer implements this ruling and fills in `## Evidence`. It does **not** re-decide it.

## Ruling

**Chosen: option (a) — refuse the account deletion while the user owns an active agent**, and render
an explanatory page telling them to deactivate or transfer the agent first. **Self-service deletion
stays available for everyone else**; this is a guard on one precondition, not a withdrawal of the
feature.

Options considered and rejected:

- **(b) deactivate the owned agents in the same transaction as the delete.** Rejected: it preserves
  self-service at the cost of silently taking agents off Slack — a destructive side effect the user
  did not ask for and cannot undo, buried in a confirm dialog about their own account.
- **(c) state it and ship it, with a follow-up issue.** Rejected: this is the most destructive
  newly-reachable path the branch creates. `phase8` I3 drove `POST /profile/delete-account`
  (`src/routers/profile.py:259`, confirm page at `:246`) to a **302** leaving
  `status=active, user_id IS NULL`, and on the production copy deleting one PI cascades **63 proposal
  reviews and 153 publications** via `ProposalReview.user_id`'s `ondelete="CASCADE"`
  (`src/models/agent_registry.py:82-84`). `0026` on this branch removed the `CheckViolation` that had
  been an accidental guardrail, so the branch made this reachable. Shipping it as a known
  data-destroying path is not acceptable when the guard is a precondition check.

The guard is also the counter-evidence Task 8 leans on: it is what keeps "0 of 53 active agents with
`AgentRegistry.user_id IS NULL`" from being one button press away from false.

## Evidence

*To be filled by Task 25's implementer.* What must be here:

- The **re-measured** cascade on the production copy (`copi-prodtest-db`, `127.0.0.1:55434`) for at
  least two users — one with an active agent, one without — with row counts. Do not inherit phase8's
  63/153 figures; they are quoted above only as the reason this escalated.
- The failing test for the refusal path and its exact pre-fix failure message, then the pass.
- The `passive_deletes=True` regression test required by plan Step 5 (#25 P2): nothing in `tests/`
  currently notices it being removed again (`verify-web-data-docs` #25 note 2, OPEN).

## Consequence a closing comment must state

`#25`'s closing comment must say that self-service account deletion is now **refused** while the user
owns an active agent, name the page the user sees, and state the remedy (deactivate or transfer the
agent first) — a behaviour change to a user-facing route, not only a data-integrity fix. It must also
record that `#25` P2's `passive_deletes=True` is now pinned by a test, since the DoD clause
("each PR ships a test that covers its defect line and fails against the pre-fix code") was
previously unmet for P2.
