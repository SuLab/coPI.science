# #25 Data layer: undeletable PI users, cascade performance, missing indexes, pool settings (5 PRs)
state=OPEN created=2026-07-30T15:53:09Z updated=2026-08-11T23:54:39Z labels=['area:data', 'verified-2026-07-30']

## BODY

Five verified items across the models, the delete path, and the DB engine config. **D1 is HIGH** and live-reproduced; the rest are trivial-to-small quick wins that belong in the same migration and the same review.

Originally verified at `origin/main` @ `b7edcbc` (2026-07-30). **Re-verified 2026-08-11 against the open PR-stack tip (`issue-29-authorship-grounding` @ `b1d54da` = main + #30/#31/#32): all five items are still present — the stack changes none of these files, and migrations 0022–0024 add none of the needed constraints/indexes (0022 actually adds three more unindexed FKs, folded into P1 below).**

## Priority (triage 2026-08-11)

**D1 is Tier 1** — user deletion is also a compliance obligation, and the stack now ships a characterization test *asserting the bug* (`tests/integration/test_db_contract.py:268-281` expects the constraint violation), which should be inverted into the regression test when fixing. **P1 + P2 + P3 batch as one Tier-3 migration/config PR; D2 stays cosmetic.**

**Suggested order:** D1 + P2 together (same cascade semantics), then P1 (fold its indexes into the same migration), then P3, then D2.

### PR D1 — Make PI users deletable *(HIGH, small — live-reproduced)*
`private_channel_members` has CHECK `(agent_id IS NULL) != (user_id IS NULL)` (`agent_activity.py:252-256`) while `user_id` is `ondelete="SET NULL"` (`:280-284`); PI member rows are user_id-only (`private_channels.py:365-368`, `:582-586`). `User` has **no** `PrivateChannelMember` relationship, so the ORM never cascades those rows — the DB-side `SET NULL` fires and violates the CHECK. Both delete paths are bare deletes (`admin.py:210`, `profile.py:246`): constraint violation on the admin path; uncaught 500 on self-delete. Any PI who was ever a private-channel member is undeletable. *Fix:* delete PCM rows on user delete (ORM cascade or DB `ondelete="CASCADE"`), or relax the constraint; invert the stack's characterization test into the regression test.

### PR P2 — `passive_deletes=True` on cascaded children *(small)*
Zero `passive_deletes` anywhere in `src/`; **11** relationships now use `cascade="all, delete-orphan"` (`user.py:52-64` ×4, `agent_registry.py:54/59`, `agent_activity.py:58/61/64/168`, `cohort.py:54`) with no DB-side delete, so a user delete SELECTs every child row into memory first. *Fix:* `passive_deletes=True` + confirm DB `ondelete` on the children. **Do this with D1** — same delete path.

### PR D2 — Reconcile stale `running` simulation runs on startup *(LOW, trivial)*
`run.status = "stopped"` is written only in the `finally` (`agent/main.py:291`, the tree's sole writer of that value). A crash / OOM-kill / `docker kill` leaves the row `running`; `--fresh` (`:163-190`) inserts a new run without reconciling prior ones; resume (`:194-215`) repairs only the single latest run. Cosmetic. *Fix:* reconcile stale `running` runs at startup.

### PR P1 — Composite + FK indexes and a `/static` middleware guard *(trivial — measured)*
- `thread_decisions` has **no `__table_args__` at all** (`agent_activity.py:209-241`); its only index is `ix_thread_decisions_run_id`. The badge middleware runs per-request, per-agent `COUNT`s on `outcome` + `(agent_a|agent_b)` (`main.py:80-94`) and needs `(agent_a, outcome)` + `(agent_b, outcome)` — measured ~129× speedup at the original audit. (The `ProposalReview` half of the middleware *is* served by `ix_proposal_reviews_agent_id`.) `AgentBadgeMiddleware.dispatch` (`main.py:28-31`) also has **no `/static` path guard**, and `nginx.conf` has no `location /static/` block, so asset requests carrying a session cookie reach uvicorn and run the queries.
- **18 unindexed `ondelete` FK targets** — the original 15, re-confirmed by cross-checking every `create_index`/constraint in `alembic/versions/`:
  `access_allowlist.added_by_user_id`, `agent_delegates.invitation_id`, `agent_delegates.user_id` (~209×), `agents.approved_by`, `delegate_invitations.accepted_by_user_id`, `delegate_invitations.invited_by_user_id`, `email_notifications.agent_registry_id`, `email_notifications.thread_decision_id`, `private_channel_members.added_by_user_id`, `private_channel_members.user_id`, `profile_revisions.changed_by_user_id`, `proposal_reviews.delegate_user_id`, `proposal_reviews.reviewed_by_user_id`, `proposal_reviews.user_id`, `slack_app_provisions.agent_registry_id` —
  **plus three introduced by migration 0022:** `cohorts.created_by`, `cohort_memberships.added_by`, `cohort_audit_events.actor_id` (all `SET NULL`, `cohort.py:43-45/:78-80/:139-141`).

*Fix:* one migration adding the composites + all 18 FK indexes; a `/static` short-circuit in the middleware. **Do not** add `llm_call_logs(channel,phase)` — measured, no benefit.

### PR P3 — Add `pool_pre_ping` / `pool_recycle` *(trivial)*
`database.py:17-22` sets `pool_size=5, max_overflow=10` with **no** `pool_pre_ping`/`pool_recycle`/`pool_timeout` (file untouched by the stack) → stale-connection 500s after a DB restart, and a 16th concurrent checkout blocks 30 s. Also reduces the trigger for the flush-loss path fixed by PR #19's H1. *Fix:* add `pool_pre_ping=True` + a `pool_recycle`.

**Definition of done:** D1 ships a test that deletes a PI who is a private-channel member (currently pinned as a violation by `test_db_contract.py:268-281`). P1 ships an `alembic upgrade head` check in CI (the offline gate's migration round-trip now covers this — see issue #27 I1).


