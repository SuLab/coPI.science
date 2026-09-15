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

### 1. The cascade, re-measured 2026-09-04

On the disposable production copy only (`copi-prodtest-db`, `127.0.0.1:55434`, db `copi_verify`,
`alembic_version = 0028`). Each `DELETE FROM users WHERE id = …` ran inside an explicit transaction
that was **rolled back**, with a full before/after row count of every table in `public`, so the copy
is left byte-identical for the other tasks reading it. Only tables whose count moved are listed.

| user | owned agent | rows deleted |
|---|---|---|
| Andrew I. Su | `su`, **active** | publications **153**, proposal_reviews **63**, jobs 7, delegate_invitations 4, private_channel_members 3, email_notification_preferences 2, email_notifications 1, email_engagement_tracking 1, researcher_profiles 1, users 1 |
| Matthew Holcomb | none (he is a *delegate* of `forli`) | publications 31, agent_delegates 1, jobs 1, researcher_profiles 1, users 1 |
| Michael Williams | `williams`, **pending** (token already provisioned) | jobs 1, researcher_profiles 1, users 1 |
| Danielle Grotjahn | `grotjahn`, **inactive** | proposal_reviews 24, publications 19, jobs 3, email_notification_preferences 2, email_notifications 2, researcher_profiles 1, email_engagement_tracking 1, users 1 |
| William Dion | none (delegate of the **active** `wiseman`) | publications 12, email_notification_preferences 2, email_notifications 1, jobs 1, researcher_profiles 1, agent_delegates 1, email_engagement_tracking 1, users 1 |

**phase8's 63 proposal reviews / 153 publications for Andrew I. Su is confirmed exactly.** The
cascade path is `proposal_reviews.user_id` / `publications.user_id`, both `ON DELETE CASCADE` in the
real schema (`confdeltype = 'c'`); `agents.user_id` is `ON DELETE SET NULL`, so the same statement
leaves `agent_id='su', status='active', user_id IS NULL` — the orphan, reproduced.

Roster context: 144 users — 53 own an `active` agent, 71 an `inactive` one, 3 a `pending` one, 17 own
none. So the guard blocks 56 of 144 and leaves self-service deletion intact for the other 88.

### 2. "Owns an active agent", defined

**An `agents` row whose `user_id` is the caller (the column is `UNIQUE`, so at most one) and whose
`status` is in `{'active', 'pending'}`.** Pinned case by case in
`tests/integration/test_onboarding_flow.py::test_delete_account_is_refused_only_while_the_owned_agent_could_go_live`.

- **`active` blocks.** On the simulation roster (`src/agent/main.py:110`,
  `src/agent/simulation.py:4221`) and posting to Slack in the PI's name.
- **`pending` blocks.** `admin_update_agent` (`src/routers/admin.py:952-956`) promotes a pending row
  straight to `active` and never re-reads `user_id`, so an orphaned pending row becomes an orphaned
  *live* agent with no owner-consented step in between. 2 of the 3 pending rows on the copy already
  carry a `slack_bot_token`, so the Slack app exists before the promotion. The support-burden
  objection does not distinguish the two: there is no self-service deactivate route on `/agent`
  either, so an `active` owner and a `pending` owner both need the same admin conversation.
- **`inactive` does not block** — and must not. "Deactivate the agent first" is the remedy the
  refusal page offers; a guard that also blocked `inactive` would make its own remedy unreachable and
  turn the refusal into a permanent one.
- **`suspended` does not block.** Same off-roster state, reached by `admin_reject_agent`; returning
  it to `active` is an explicit admin dropdown change on a row the admin is looking at.
- **A delegation never blocks.** `agent_delegates.user_id` is `ondelete="CASCADE"`, so deleting a
  delegate removes the delegation row and leaves the agent's owner untouched — measured above on
  William Dion, whose delete costs `wiseman` nothing.

### 3. Red first, then green

`POST /profile/delete-account`, user owning an `active` agent, against pre-fix `src/`:

```
AssertionError: status='active': the delete was not refused (302)
assert 302 == 409
```

and with the same test's expectation inverted, the pre-fix route is confirmed to return
`302 → /login?deleted=1`, delete the user row, and leave the agents row alive with `user_id IS NULL`
— the audit's claim, re-derived by test rather than inherited. `status='pending'` failed identically.
The confirm page failed on `the page does not name the agent that blocks the delete`. After the fix:
`tests/integration/test_db_contract.py tests/integration/test_onboarding_flow.py` → **108 passed**;
`tests/integration/ -k 'profile or delete or contract'` → **141 passed, 5 skipped**.

### 4. #25 P2's `passive_deletes=True` is now pinned (plan Step 5)

Three tests in `tests/integration/test_db_contract.py`, section 7:

- `test_p2_every_delete_orphan_relationship_sets_passive_deletes` — walks `Base.registry.mappers`
  and asserts every `delete-orphan` relationship sets the flag, with the exact set of 11 names as the
  control so an empty walk cannot pass vacuously.
- `test_p2_each_passive_delete_child_fk_really_cascades_in_the_schema` — the other half of P2's
  *Fix:* clause ("confirm DB `ondelete` on the children"): each of the 11 child FKs is
  `confdeltype='c'` in the real migrated schema, so `passive_deletes` hands the work to something
  that will actually do it.
- `test_p2_a_user_delete_leaves_the_publications_to_the_database` — behavioural: a user delete emits
  no SQL against `publications` at all, yet the rows are gone afterwards.

Red proved against `git show 32c4ca3~1:src/models/user.py` overlaid on a clean `git archive HEAD`
export in a scratch tree (`src/` never mutated):

```
AssertionError: delete-orphan cascade without passive_deletes=True:
  ['User.delegated_agents', 'User.jobs', 'User.profile', 'User.publications']

AssertionError: the user delete read or wrote publications itself, so passive_deletes=True is no
  longer in force on User.publications: ['SELECT publications.id … WHERE $1::UUID =
  publications.user_id', 'DELETE FROM publications WHERE publications.id = $1::UUID']
```

### 5. Ratchets

Measured on clean `git archive` exports with `ci.sh`'s exact commands: `ruff check src` **251 → 250**
(the new handler uses `Annotated[…, Depends(…)]`, which costs no B008 where the file's other five
handlers each cost two); `mypy src --ignore-missing-imports` **145 → 145**. D33: an AST walk of every
string constant in `src/routers/profile.py` before and after differs only by two docstrings, the
literals `"active"` / `"pending"`, and a second use of the existing template name — no model-facing
string.

## Consequence a closing comment must state

`#25`'s closing comment must say that self-service account deletion is now **refused** while the user
owns an active agent, name the page the user sees, and state the remedy (deactivate or transfer the
agent first) — a behaviour change to a user-facing route, not only a data-integrity fix. It must also
record that `#25` P2's `passive_deletes=True` is now pinned by a test, since the DoD clause
("each PR ships a test that covers its defect line and fails against the pre-fix code") was
previously unmet for P2.
