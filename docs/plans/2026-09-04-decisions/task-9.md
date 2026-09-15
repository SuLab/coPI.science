# Task 9 — #20 COR-5: persisting the implicit review for an agent with no linked user

**Status: IMPLEMENTED by the Task 9 executor, 2026-09-04.** The plan marks Task 9 **FIX**, not
DECIDE. One judgement call inside the fix was not settled by Task 8's ruling and needed writing
down: *how far the new carrier may be read* (Ruling 1). Task 8 fixed the carrier; it did not say
which agents a per-decision timestamp clears.

## The clause and what shipped

#20 COR-5's `Fix:` is "require the sender be the owning PI; insert a `ProposalReview`." The first
half shipped long ago (`_check_pi_proposal_review`'s `authorized_agent_ids`). The second half never
happened when `AgentRegistry.user_id IS NULL`: `_persist_implicit_proposal_review` logged a WARNING
and returned, so the block was cleared in memory only and `_rebuild_agent_state` re-blocked the
proposal on the next restart.

Per `task-8.md`, the carrier is **`thread_decisions.pi_engaged_at`** (migration `0029`), not a
`ProposalReview` row and not a nullable `proposal_reviews.user_id` — that column is
`ForeignKey("users.id", ondelete="CASCADE")`, so the issue's literal instruction would build a
self-erasing record.

## Ruling 1 — the read is scoped to the population that produces the write

`pi_engaged_at` lives on the `ThreadDecision`, so it is **per-decision**, where a `ProposalReview`
row is **per-agent** — and a decision names two agents (`agent_a`, `agent_b`), each with its own
`ProposalRef` and its own block.

Reading it unconditionally in the rebuild would therefore let one lab's PI clear the *other* lab's
agent, which is precisely the cross-lab unblock COR-5's **first** half exists to stop. That is not
hypothetical: on the disposable production copy (`copi-prodtest-db` / `copi_verify`, read-only,
2026-09-04) **639** of the `outcome='proposal'` decisions pair two agents whose `agents.user_id`
differ.

So both rebuild readers gate on the agent having no linked PI:

* `_rebuild_agent_state` step 3 — `is_reviewed = (td.id, aid) in reviewed_set or
  (td.pi_engaged_at is not None and aid not in linked_pi_agent_ids)`.
* `_rebuild_one_agent_state` — the same predicate, scoped to the one agent being rebuilt.

The reader selects the **linked** agent ids and tests non-membership, rather than selecting the
unlinked ones, so that an `agent_id` with no `agent_registry` row at all is read the same way the
writer reads it (`scalar_one_or_none()` → `None` → "no user").

## Ruling 2 — `_rebuild_one_agent_state` gets the same predicate

COR-5's text names `_rebuild_agent_state` only. `_rebuild_one_agent_state` (the E6(2) roster-flip
rebuild, `1c75d70`) computes the same `reviewed` flag from the same evidence; leaving it out would
mean an inactive→active flip re-blocks exactly the proposal a restart no longer re-blocks. Same
defect, same carrier, different trigger — not a widening of scope.

## Reader audit (plan Step 4)

`grep -rn 'ProposalReview' src/ | grep -v test`. **No `ProposalReview` reader changes behaviour**,
because the fix writes **no** `ProposalReview` row: the user-less population had none before and has
none now. The `-1`-marker filters were checked specifically —
`src/routers/admin.py:864` and `templates`' feeder now read `rating.notin_((-1, 0))` after Task 16
(`9505554`); `src/main.py:182`, `src/routers/agent_page.py:255` and
`src/services/email_notifications.py:178,854` still read `rating != -1`. All of them **exclude** the
engine's implicit marker, so a `pi_engaged_at` timestamp is exactly as invisible to them as the `-1`
row they already drop. The dashboard, the review e-mail, the digest and the admin count therefore
keep showing such a proposal as awaiting review — unchanged, and the reason the operator log line is
kept (downgraded to INFO) rather than deleted.

`tests/integration/test_proposal_review.py:621-666` manufactures its `IntegrityError` from
`proposal_reviews.user_id`'s `NOT NULL`. Ruling (b) would have broken it; ruling (a) does not, and
the file is untouched and green.

## Population

`SELECT count(*) FILTER (WHERE user_id IS NULL), count(*) FROM agents WHERE status='active'` on
`copi_verify` → **0 of 53**, i.e. the defect is latent today. It is one button press from
non-zero (`audit-phase8-functional.md` I3), and Task 25 (`cd0ed78`) closed that button; the carrier
deliberately does not depend on that guard holding.
