# Issue #25 — red-team pass (second reviewer)

Tree: `/home/a/scripps/coPI.science` @ `copi-prod` 18ba52c, 2026-09-02. Read-only. Own greps/reads; independent
FK-index enumerator written from scratch (`scratchpad/25/rt_fk.py`, not derived from the first agent's script).

## 1. Summary table

| id | first-agent verdict | red-team | reason | evidence |
|---|---|---|---|---|
| D1.1 | STILL PRESENT | UPHELD | CHECK `:253-256`, `user_id` FK `SET NULL` `:280-284` | `src/models/agent_activity.py` |
| D1.2 | STILL PRESENT | UPHELD | both `db.add(PrivateChannelMember(... user_id=creator_pi_user.id, role="pi"))` sites, no `agent_id` | `src/services/private_channels.py:365-368,582-587` |
| D1.3 | STILL PRESENT | UPHELD + closed the indirect-cascade question | `User.agent` relationship (`user.py:59-61`) has **no** `cascade`; `agents.user_id` is `SET NULL` (`agent_registry.py:22`); `AgentRegistry` has only `delegates`/`invitations` relationships (`:53-59`), none to `AgentChannel`; `AgentChannel.agent_id` is a plain string, no FK (`agent_activity.py:138-170`: only FK is `simulation_runs.id`). So no ORM path User→AgentRegistry→AgentChannel→PCM exists even for the PI's own agent; every `role="pi"` PCM row survives to the DB `SET NULL` | grep |
| D1.4 | STILL PRESENT | UPHELD | `admin.py:229-230` `db.delete(user); commit()`; `profile.py:246-247` same; no try/except; `IntegrityError` only at `public.py:1085`, `agent_page.py:1018,1022`; 0 `exception_handler` in `src/`; **no pre-clean of PCM rows anywhere** (`grep -rn PrivateChannelMember src/ \| grep -i delete` → only the model's cascade line) | grep |
| D1.5 | STILL PRESENT (asserts bug) | UPHELD | quoted below; raw-SQL delete inside `pytest.raises(IntegrityError)` + constraint-name assert | `tests/integration/test_db_contract.py:268-281` |
| P2.1 | STILL PRESENT | UPHELD | `grep -rn passive_deletes src/` → 0 | — |
| P2.2 | STILL PRESENT (11) | UPHELD | `grep -rn delete-orphan src/models/ \| wc -l` → 11 at the cited lines | — |
| P2.3 | NOT REPRODUCIBLE as worded | **UPHELD (verified model + migration for all 11)** | see §2 table; no `create_foreign_key`/FK-altering migration exists | alembic grep |
| D2.1 | STILL PRESENT | UPHELD | `"stopped"` written only at `agent/main.py:291`; enum also has never-written `"completed"` (`agent_activity.py:48`) | grep |
| D2.2 | STILL PRESENT | UPHELD | `--fresh` (`:168-177`) truncates `agent_messages`/`agent_channels`/`pi_dm_messages` and inserts a new `status="running"` row; never touches prior `SimulationRun` rows; resume `:196-197` `order_by(started_at.desc()).limit(1)` then `:203 status="running"` | sed |
| P1.1 | STILL PRESENT | UPHELD | `__table_args__` in `agent_activity.py` only at `:110,252,340` (messages/PCM/pi_dm) — none for `ThreadDecision` (`:209-240`); only index `0003:50 ix_thread_decisions_run_id` | grep |
| P1.2 | STILL PRESENT | UPHELD | `for aid in agent_ids:` `main.py:80`; `outcome=="proposal"` + `agent_a\|agent_b` `:83-84` | sed |
| P1.3 | CONFIRMED | UPHELD | `ProposalReview.agent_id == aid` `:90`; `ix_proposal_reviews_agent_id` (0004) | — |
| P1.4 | STILL PRESENT | UPHELD | only gate `if user_id_str:` `:32`; no `startswith`/`url.path` in `main.py`; badge mw added `:122`, Session mw `:125` (outer) → badge runs with session; `/static` mounted `:136` inside | grep |
| P1.5 | STILL PRESENT | UPHELD | `location.*static` → only `/ingest/static/` `:154`, `/_next/static/` `:167` | nginx grep |
| P1.6 | STILL PRESENT (18 exact) | **UPHELD — independently reproduced** | my enumerator: 37 FK cols (all with `ondelete`), 18 unindexed, `issue-not-now: []`, `now-not-issue: []` | `rt_fk.py` output §2 |
| P3.1 | STILL PRESENT | UPHELD | `database.py:17-22` exactly `echo=False, pool_size=5, max_overflow=10` | sed |
| Meta.1 | CONFIRMED | UPHELD | 0023/0024 column-only; no FK/index DDL after 0022 | — |
| Meta.2 | CONFIRMED | QUALIFIED (mis-cite only) | round trip is real (`ci.sh:229-231`, `MIGRATION_FLOOR` `:70`) but the cited `:103-118` / `:57-70` windows are off: dup-id check `:109`, single-head `:119-121`, docker run `:210` | grep |
| NEW-1 (first agent) | 4 extra bare engines | UPHELD | `cli.py:24`, `worker/main.py:119` (`echo=False` only), `grantbot.py:473`, `agent/main.py:160` — no pool kwargs on any | grep |
| NEW-2 (first agent) | 10 alembic-only indexed FKs; 3 placeholder `__table_args__` comments | UPHELD | my enumerator lists the same 10; comments at `agent_registry.py:105-107`, `delegate.py:92-94`, `email_notification.py:61-63` | — |
| NEW-3 (first agent) | model-only scan gives 28, +create_index 21, +uq 18 | UPHELD (28 and 18 reproduced) | `MODEL-only unindexed count: 28` | — |
| NEW-4 (first agent) | no test hits `POST /admin/users/{id}/delete` | UPHELD | `grep -rn "users/.*delete\|admin_delete_user" tests/` → 0 | — |

## 2. Detail

### P2.3 — DB-side `ondelete` for the 11 cascade children (model vs creating migration)

| child FK | model | alembic (creating migration) |
|---|---|---|
| researcher_profiles.user_id | CASCADE `profile.py:20` | CASCADE `0001:58` |
| publications.user_id | CASCADE `publication.py:20` | CASCADE `0001:96` |
| jobs.user_id | CASCADE `job.py:28` | CASCADE `0001:152` (inside `jobs` table `:126-`) |
| agent_delegates.user_id | CASCADE `delegate.py:72` | CASCADE `0007:80` |
| agent_delegates.agent_registry_id | CASCADE `delegate.py:67` | CASCADE `0007:74` |
| delegate_invitations.agent_registry_id | CASCADE `delegate.py:21` | CASCADE `0007:30` |
| agent_messages.simulation_run_id | CASCADE `agent_activity.py:79` | CASCADE `0001:206` |
| agent_channels.simulation_run_id | CASCADE `:145` | CASCADE `0001:237` |
| llm_call_logs.simulation_run_id | CASCADE `:183` | CASCADE `0002:29` |
| private_channel_members.agent_channel_id | CASCADE `:276` | CASCADE `0011:59` |
| cohort_memberships.cohort_id | CASCADE `cohort.py:72` | CASCADE `0022:61` |

`grep -n "create_foreign_key\|drop_constraint\|alter_column" alembic/versions/*.py` → no `create_foreign_key`
anywhere; `drop_constraint` only on unique constraints (0004 downgrade, 0016, 0019 downgrade); `alter_column`
only server_default/nullable. **No model/migration disagreement.** The issue's "with no DB-side delete" is wrong;
the first agent's correction stands. (Only `passive_deletes=True` is missing.)

### D1.5 — the characterization test (quoted)

```python
268 async def test_dat1_deleting_pi_member_user_violates_pcm_check(db_session):
269     ch = await factories.make_agent_channel(db_session, visibility="collab_private")
270     u = await factories.make_user(db_session)
271     await factories.make_private_channel_member(
272         db_session, channel=ch, agent_id=None, user_id=u.id, role="pi"
273     )
274     with pytest.raises(IntegrityError) as ei:
275         async with db_session.begin_nested():
276             await db_session.execute(
277                 text("DELETE FROM users WHERE id = :id"), {"id": u.id}
278             )
281     assert "pcm_exactly_one_of_agent_or_user" in str(ei.value)
```
Raw SQL, so it pins the DB behaviour independent of ORM cascades. Would flip to failing if D1 is fixed via
`ondelete="CASCADE"`/constraint relaxation; would keep passing if D1 is fixed ORM-side only (a pre-clean in the
routes) — worth noting for the "invert the test" DoD: an ORM-only fix needs a route-level test instead.

### P1.6 — independent enumeration (`rt_fk.py`, run with `.venv-test/bin/python`)

```
FK columns: 37  without ondelete: []
UNINDEXED: 18
   access_allowlist.added_by_user_id  agent_delegates.invitation_id  agent_delegates.user_id
   agents.approved_by  cohort_audit_events.actor_id  cohort_memberships.added_by  cohorts.created_by
   delegate_invitations.accepted_by_user_id  delegate_invitations.invited_by_user_id
   email_notifications.agent_registry_id  email_notifications.thread_decision_id
   private_channel_members.added_by_user_id  private_channel_members.user_id
   profile_revisions.changed_by_user_id  proposal_reviews.delegate_user_id
   proposal_reviews.reviewed_by_user_id  proposal_reviews.user_id  slack_app_provisions.agent_registry_id
issue-not-now: []   now-not-issue: []
ALEMBIC-ONLY indexed FKs: 10  (agent_channels.simulation_run_id, agent_delegates.agent_registry_id,
   cohort_memberships.cohort_id, delegate_invitations.agent_registry_id, email_notifications.user_id,
   jobs.user_id, llm_call_logs.simulation_run_id, proposal_reviews.thread_decision_id, publications.user_id,
   thread_decisions.simulation_run_id)
MODEL-only unindexed count (ignoring alembic): 28
```
Method: leading column of any model `Index`/`UniqueConstraint`/PK, or of any `op.create_index` /
`op.create_unique_constraint` / in-`create_table` `sa.UniqueConstraint`/`sa.Index`/`PrimaryKeyConstraint`/
`unique=True|index=True|primary_key=True` in an `upgrade()` body, minus `drop_index` in `upgrade()`. Matches the
issue and the first agent exactly.

### Meta.2 — mis-cite

`scripts/ci.sh:103-118` → duplicate-id check is at `:109-114`, single-head at `:119-121`; `:57-70` are the
throwaway-Postgres settings, the round trip itself is `:192-240` (`docker run … postgres:15` `:210`, alembic
`:229-231`). Substance (round trip exists, floor 0018, skippable) is correct.

## 3. Mis-cites in the first agent's report

- `scripts/ci.sh:103-118` / `:57-70` for the round trip (see above). Everything else spot-checked exact:
  `agent_activity.py:252-256,280-284,296-298`; `private_channels.py:365-368,582-587`; `admin.py:229-230`;
  `profile.py:246-247`; `user.py:50-65`; `agent/main.py:163-190,191-223,291`; `main.py:28-32,80-92,122,125,136`;
  `database.py:15-22`; `0003:50`; `0022:42,61,68,105`; `cohort.py:45,72,80,141`.
- Wording: "no pool kwargs at all" for the four extra engines — `worker/main.py:119` passes `echo=False`
  (not a pool kwarg); immaterial.

## 4. Counts

Rows evaluated: 23 (18 first-agent table rows + 4 first-agent NEW claims + Meta.2 split).
**UPHELD 22 · OVERTURNED 0 · QUALIFIED 1 (Meta.2 line cites) · UNVERIFIABLE 0.**
Nothing in #25 is fixed; the issue's "no DB-side delete" wording (P2) is the only claim that is wrong, and it is
wrong in the direction that makes the fix smaller.
