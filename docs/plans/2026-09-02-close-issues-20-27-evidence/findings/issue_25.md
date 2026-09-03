# Issue #25 verification — Data layer: undeletable PI users, cascade performance, missing indexes, pool settings

Verified against `copi-prod` @ `18ba52c` (clean tree), 2026-09-02. Alembic head on this tree: **0024** (single head; no migrations after 0024 exist in `alembic/versions/`). Of the 52 commits since the issue's re-verification baseline `b1d54da`, only `18ba52c` touched any in-scope file (`src/routers/admin.py`, cohort code only; the delete route body is unchanged since `e5a31fc0`, 2026-03-30). Every in-scope model, `database.py`, `profile.py`, `main.py`, `nginx.conf`, `agent/main.py`, `private_channels.py` and `test_db_contract.py` are byte-identical to the state the issue described. **Nothing in this issue has been fixed.**

## 1. Summary table

| id | claim | verdict | key evidence | conf |
|---|---|---|---|---|
| D1.1 | `private_channel_members` CHECK `(agent_id IS NULL) != (user_id IS NULL)` while `user_id` FK is `ondelete="SET NULL"` | STILL PRESENT | `src/models/agent_activity.py:252-256`, `:280-284` | high |
| D1.2 | PI member rows are user_id-only (`role="pi"`) | STILL PRESENT | `src/services/private_channels.py:365-368`, `:582-587` | high |
| D1.3 | `User` has no `PrivateChannelMember` relationship, so the ORM never cascades those rows | STILL PRESENT | `src/models/user.py:50-65` (profile/publications/jobs/agent/delegated_agents only); PCM's only relationship is `agent_channel` (`agent_activity.py:296-298`) | high |
| D1.4 | Both delete paths are bare deletes with no `IntegrityError` handling | STILL PRESENT | `src/routers/admin.py:229-230`, `src/routers/profile.py:246-247`; `IntegrityError` handled only in `public.py:1085`, `agent_page.py:1018/1022`; no `exception_handler` anywhere in `src/` | high |
| D1.5 | Stack ships a characterization test asserting the bug at `test_db_contract.py:268-281` | STILL PRESENT (asserts bug) | `tests/integration/test_db_contract.py:268-281` `pytest.raises(IntegrityError)` + `assert "pcm_exactly_one_of_agent_or_user" in str(ei.value)` | high |
| P2.1 | Zero `passive_deletes` anywhere in `src/` | STILL PRESENT | `grep -rn passive_deletes src/` -> 0 hits; `git grep passive_deletes HEAD` -> 0 hits in whole tree | high |
| P2.2 | 11 relationships use `cascade="all, delete-orphan"` | STILL PRESENT (count exact) | `user.py:52,55,58,64`; `agent_registry.py:54,59`; `agent_activity.py:58,61,64,168`; `cohort.py:54` = 11; no newer model adds one | high |
| P2.3 | "...with no DB-side delete" | NOT REPRODUCIBLE as worded | All 11 child FKs already carry `ondelete="CASCADE"` at the DB level (metadata dump below). Only `passive_deletes=True` is missing; the ORM pre-SELECT behaviour the issue describes is real | high |
| D2.1 | `run.status = "stopped"` written only in the `finally`; sole writer of that value | STILL PRESENT | `src/agent/main.py:291`; tree-wide grep finds no other writer of `"stopped"` | high |
| D2.2 | `--fresh` inserts a new run without reconciling; resume repairs only the single latest run | STILL PRESENT | `src/agent/main.py:163-190` (fresh), `:191-223` (resume: `order_by(started_at.desc()).limit(1)` then `status = "running"`) | high |
| P1.1 | `thread_decisions` has no `__table_args__`; only index is `ix_thread_decisions_run_id` | STILL PRESENT | `src/models/agent_activity.py:209-240` (no `__table_args__`); `alembic/versions/0003_...py:50` is the only `create_index` on the table; 0011 only adds columns | high |
| P1.2 | Badge middleware runs per-request, per-agent `COUNT`s on `outcome` + `(agent_a\|agent_b)` | STILL PRESENT | `src/main.py:80-94` loop over `agent_ids`; ~129x speedup figure not re-measurable here | high (mechanism) / n.a. (figure) |
| P1.3 | `ProposalReview` half of the middleware is served by `ix_proposal_reviews_agent_id` | CONFIRMED | `alembic/versions/0004_...py:89-91` creates it; `src/main.py:88-92` filters on `ProposalReview.agent_id` | high |
| P1.4 | `AgentBadgeMiddleware.dispatch` has no `/static` path guard | STILL PRESENT | `src/main.py:28-104`; only gate is `if user_id_str:` (line 32); `/static` mount at line 136 sits inside the middleware stack | high |
| P1.5 | `nginx.conf` has no `location /static/` block | STILL PRESENT | `nginx/nginx.conf`: only `/ingest/static/` (PostHog, :154) and `/_next/static/` (:167, proxies to app anyway); no nginx volume serves static files (`docker-compose.prod.yml:156-157`) | high |
| P1.6 | 18 unindexed `ondelete` FK targets (15 original + 3 from 0022) | STILL PRESENT (exact match) | Mechanical cross-check (models + all alembic index/unique/pk DDL): 37 FK columns, 18 unindexed, set identical to the issue's list | high |
| P3.1 | `database.py` sets `pool_size=5, max_overflow=10` with no `pool_pre_ping`/`pool_recycle`/`pool_timeout` | STILL PRESENT | `src/database.py:17-22`, unchanged since `e944d184` (2026-03-20) | high |
| Meta.1 | Stack changes none of these files; 0022-0024 add none of the needed constraints/indexes | CONFIRMED | `git log b1d54da..HEAD -- <in-scope files>` -> only `18ba52c` (admin.py, cohort code); 0023/0024 are `add_column`/`drop_column` only | high |
| Meta.2 | DoD: "P1 ships an `alembic upgrade head` check in CI (the offline gate's round-trip now covers this)" | CONFIRMED (already exists) | `scripts/ci.sh:11-13,57-70,103-118`: single-head check + upgrade/downgrade/upgrade round trip against a throwaway Postgres (floor `0018`, skippable with `CI_MIGRATION_DB=none`) | high |

## 2. Per-item detail

### PR D1 — Make PI users deletable (HIGH) — STILL PRESENT in full

**Constraint and FK** (`src/models/agent_activity.py`):

```python
251    __tablename__ = "private_channel_members"
252    __table_args__ = (
253        CheckConstraint(
254            "(agent_id IS NULL) != (user_id IS NULL)",
255            name="pcm_exactly_one_of_agent_or_user",
256        ),
...
279    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
280    user_id: Mapped[uuid.UUID | None] = mapped_column(
281        UUID(as_uuid=True),
282        ForeignKey("users.id", ondelete="SET NULL"),
283        nullable=True,
284    )
...
286    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
287        UUID(as_uuid=True),
288        ForeignKey("users.id", ondelete="SET NULL"),
289        nullable=True,
290    )
...
296    agent_channel: Mapped["AgentChannel"] = relationship(
297        "AgentChannel", back_populates="private_members"
298    )
```

The issue's line numbers for this file (252-256, 280-284) are still exact.

**User relationships** (`src/models/user.py:50-65`): `profile`, `publications`, `jobs`, `agent`, `delegated_agents`. No `PrivateChannelMember` relationship, and `PrivateChannelMember` has no `user` relationship back. So an ORM `db.delete(user)` cascades profile/publications/jobs/delegated_agents in Python, then issues `DELETE FROM users`, and the DB fires `SET NULL` on `private_channel_members.user_id`. For a `role="pi"` row (`agent_id IS NULL`), that produces `(NULL IS NULL) != (NULL IS NULL)` = false -> CHECK violation -> the whole DELETE fails.

**PI rows are user_id-only** (`src/services/private_channels.py`):

```python
365    db.add(PrivateChannelMember(
366        agent_channel_id=ac.id, user_id=creator_pi_user.id, role="pi",
367        added_by_user_id=creator_pi_user.id,
368    ))
...
582    db.add(PrivateChannelMember(
583        agent_channel_id=ac.id,
584        user_id=creator_pi_user.id,
585        role="pi",
586        added_by_user_id=creator_pi_user.id,
587    ))
```

**Delete paths** — both bare, no try/except:

`src/routers/admin.py:213-232` (`admin_delete_user`, blame `e5a31fc0` 2026-03-30, untouched since):
```python
228    name = user.name
229    await db.delete(user)
230    await db.commit()
231    logger.info("Admin %s deleted user %s (%s)", current_user.name, name, user_id)
232    return RedirectResponse(url="/admin/users", status_code=302)
```

`src/routers/profile.py:235-252` (`delete_account`, blame `b99fdfdd` 2026-03-20, untouched since):
```python
246    await db.delete(current_user)
247    await db.commit()
248
249    request.session.clear()
```

`IntegrityError` is caught in exactly two places in `src/` (`routers/public.py:1085`, `routers/agent_page.py:1018,1022`), neither on a user-delete path. There is no `@app.exception_handler` anywhere in `src/`. `get_db` (`src/database.py:47-58`) does `rollback()` then re-raises. **Consequence: both routes return an uncaught 500.** The issue's wording "constraint violation on the admin path; uncaught 500 on self-delete" draws a distinction that does not exist in the code — both paths fail identically (internal imprecision, not a wrong verdict).

**Characterization test** — `tests/integration/test_db_contract.py:268-281` (line range in the issue is still exact):
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
279     # Lock the SPECIFIC constraint that fires, ...
281     assert "pcm_exactly_one_of_agent_or_user" in str(ei.value)
```
It **asserts the bug** (raw SQL `DELETE FROM users` must raise, and must raise on that specific constraint). Added in `e02446e`, hardened in `13f1865`; no later commit touches it. A companion `test_dat1_deleting_added_by_user_is_safe` (`:317-336`) pins the contrast case (`added_by_user_id` SET NULL is harmless). Fixing D1 will make line 274-281 fail, so it must be inverted as the issue says.

**Route-level tests would not catch it:** `tests/integration/test_onboarding_flow.py:931-955` (`test_delete_account_needs_the_confirmation_word`) deletes a user who has a profile and a publication but no PCM row, so it passes today and would keep passing after a fix. No test exercises `POST /admin/users/{id}/delete` at all (grep `users/.*delete|admin_delete_user` in `tests/` -> 0 hits).

**Could not execute** the delete against a DB (integration tests need Docker; forbidden here), but the failure mode is fully determined by the DDL quoted above and is pinned by the existing contract test.

### PR P2 — `passive_deletes=True` on cascaded children — STILL PRESENT; one wording error

```
$ grep -rn passive_deletes src/        -> (no output, exit 1)
$ git grep -n passive_deletes HEAD -- . -> (no output)
```

The 11 `cascade="all, delete-orphan"` relationships on the current tree (`grep -rn 'delete-orphan' src/models/ | wc -l` = 11):

| file:line | parent -> child | child FK DB ondelete |
|---|---|---|
| `src/models/user.py:52` | User.profile -> ResearcherProfile | `researcher_profiles.user_id` CASCADE |
| `src/models/user.py:55` | User.publications -> Publication | `publications.user_id` CASCADE |
| `src/models/user.py:58` | User.jobs -> Job | `jobs.user_id` CASCADE |
| `src/models/user.py:64` | User.delegated_agents -> AgentDelegate | `agent_delegates.user_id` CASCADE |
| `src/models/agent_registry.py:54` | AgentRegistry.delegates -> AgentDelegate | `agent_delegates.agent_registry_id` CASCADE |
| `src/models/agent_registry.py:59` | AgentRegistry.invitations -> DelegateInvitation | `delegate_invitations.agent_registry_id` CASCADE |
| `src/models/agent_activity.py:58` | SimulationRun.messages -> AgentMessage | `agent_messages.simulation_run_id` CASCADE |
| `src/models/agent_activity.py:61` | SimulationRun.channels -> AgentChannel | `agent_channels.simulation_run_id` CASCADE |
| `src/models/agent_activity.py:64` | SimulationRun.llm_call_logs -> LlmCallLog | `llm_call_logs.simulation_run_id` CASCADE |
| `src/models/agent_activity.py:168` | AgentChannel.private_members -> PrivateChannelMember | `private_channel_members.agent_channel_id` CASCADE |
| `src/models/cohort.py:54` | Cohort.memberships -> CohortMembership | `cohort_memberships.cohort_id` CASCADE |

Count and locations match the issue exactly; none of the models added after the audit (`grantbot_posted.py`, `provisioning.py`, `proposal_vote.py`, `access.py`, `profile_revision.py`, `email_notification.py`) declare a cascade relationship.

**Where the issue is wrong:** "with no DB-side delete" is false — every one of the 11 child FKs already has `ondelete="CASCADE"` (confirmed from the SQLAlchemy metadata dump in the P1 section). The fix's "confirm DB `ondelete` on the children" step is therefore already satisfied; the only missing piece is `passive_deletes=True` on the 11 relationships so SQLAlchemy stops SELECTing children before the parent DELETE. The performance claim itself (ORM loads every child into memory before deleting) is correct SQLAlchemy default behaviour when `passive_deletes` is absent.

No test references `passive_deletes` or asserts the number of statements emitted on a user delete.

### PR D2 — Reconcile stale `running` runs on startup — STILL PRESENT

`src/agent/main.py`:
```python
191            else:
192                # Resume: find the latest simulation run
193                async with session_factory() as db:
194                    result = await db.execute(
195                        select(SimulationRun)
196                        .order_by(SimulationRun.started_at.desc())
197                        .limit(1)
198                    )
199                    existing_run = result.scalar_one_or_none()
200
201                    if existing_run:
202                        simulation_run_id = existing_run.id
203                        existing_run.status = "running"
204                        existing_run.ended_at = None
...
271    finally:
...
290                if run:
291                    run.status = "stopped"
292                    run.ended_at = datetime.now(timezone.utc)
```

- `"stopped"` is written at line 291 only. A tree-wide grep for `"stopped"` / `.status = ` finds no other writer of run status; `pi_inbox.py`, `private_channels.py`, `admin.py` (`:285`, `:335`, `:416`, `:522`) only read `SimulationRun`.
- `--fresh` (`:163-190`) wipes `agent_messages`/`agent_channels`/`pi_dm_messages` and inserts a new `status="running"` row; it never touches prior runs. So after a crash + `--fresh`, the old row stays `running` forever.
- Resume (`:191-223`) picks the newest by `started_at` regardless of status and forces it to `running`; any older stale `running` row is never repaired.
- Side note the issue does not mention: `sim_run_status_enum` has a third value `"completed"` (`agent_activity.py:48`) that nothing in `src/` ever writes.
- Issue line refs (163-190, 194-215, 291) are within a few lines of current positions. Cosmetic as the issue says; no test covers it.

### PR P1 — Composite + FK indexes and a `/static` guard — STILL PRESENT in full

**ThreadDecision** (`src/models/agent_activity.py:209-240`): the class body has `id`, `simulation_run_id` (FK CASCADE), `thread_id`, `channel`, `agent_a`, `agent_b`, `outcome` (enum), `summary_text`, `origin_visibility`, `refined_in_channel`, `decided_at`, `__repr__`. **No `__table_args__`, no `index=True`.** Alembic indexes on `thread_decisions`: exactly one — `0003_...py:50 op.create_index("ix_thread_decisions_run_id", "thread_decisions", ["simulation_run_id"])`. `0011` only `add_column`s `origin_visibility`/`refined_in_channel`. No `(agent_a, outcome)` / `(agent_b, outcome)` index exists.

**Badge middleware** (`src/main.py`):
```python
28    async def dispatch(self, request: Request, call_next):
29        request.state.posthog_api_key = get_settings().posthog_api_key
30        request.state.agent_badge_count = 0
31        user_id_str = request.session.get("user_id") if "session" in request.scope else None
32        if user_id_str:
...
80                    for aid in agent_ids:
81                        total_result = await db.execute(
82                            select(func.count(ThreadDecision.id)).where(
83                                ThreadDecision.outcome == "proposal",
84                                (ThreadDecision.agent_a == aid) | (ThreadDecision.agent_b == aid),
85                            )
86                        )
87                        total = total_result.scalar() or 0
88                        reviewed_result = await db.execute(
89                            select(func.count(ProposalReview.id)).where(
90                                ProposalReview.agent_id == aid
91                            )
92                        )
...
104        return await call_next(request)
```
- Per-request, per-agent COUNT pair: confirmed. The `ProposalReview` count filters on `agent_id`, which `ix_proposal_reviews_agent_id` (0004) covers — the issue's parenthetical is correct.
- **No `/static` guard:** grep for `url.path|startswith|/static|/health` in `src/main.py` hits only the mount (`:136`) and `/api/health` route (`:150`). The only short-circuit is `if user_id_str:`, so unauthenticated asset requests skip the queries but any request carrying a valid session cookie — including `/static/*` — runs them. `AgentBadgeMiddleware` is added at `:122` before `SessionMiddleware` at `:125`, i.e. it runs inside the session layer and wraps the `/static` mount (`:136`).
- The ~129x figure is a measurement from the original audit; not re-measurable without a DB (and out of scope here).

**nginx**: `nginx/nginx.conf` has `location /` blocks (`:63,128,186,224,262,290`), `/.well-known/acme-challenge/`, the SEC-15 graph regex block (`:111`), `/ingest/static/` -> PostHog (`:154`) and `/_next/static/` -> `proxy_pass http://app` (`:167`, a Next.js leftover that still proxies to uvicorn). **No `location /static/`.** `docker-compose.prod.yml:156-157` mounts only `nginx.conf` into the nginx container, so nginx cannot serve the FastAPI `static/` dir directly.

**18 unindexed FK targets — mechanically verified.** Script: `/tmp/claude-1000/-home-a-scripps-coPI-science/c8a1ec5c-25b0-4282-b6cd-c2567cafee26/scratchpad/25/fk_index_check_final.py`. It imports `src.models`, iterates `Base.metadata.tables`, and for each FK column checks whether it is the LEADING column of any model `Index`/`UniqueConstraint`/PK/`unique=True`/`index=True`, **or** of any `op.create_index`, `op.create_unique_constraint`, or `sa.UniqueConstraint`/`sa.Index`/`unique=True`/`index=True`/PK inside `op.create_table` in any `alembic/versions/*.py` `upgrade()` (minus `drop_index`). Output:

```
TOTAL FK columns: 37   (all 37 carry an ondelete)
EFFECTIVELY UNINDEXED FK columns: 18
  access_allowlist.added_by_user_id          (SET NULL)
  agent_delegates.user_id                    (CASCADE)
  agent_delegates.invitation_id              (SET NULL)
  agents.approved_by                         (SET NULL)
  cohort_audit_events.actor_id               (SET NULL)   <- 0022
  cohort_memberships.added_by                (SET NULL)   <- 0022
  cohorts.created_by                         (SET NULL)   <- 0022
  delegate_invitations.invited_by_user_id    (CASCADE)
  delegate_invitations.accepted_by_user_id   (SET NULL)
  email_notifications.thread_decision_id     (CASCADE)
  email_notifications.agent_registry_id      (CASCADE)
  private_channel_members.user_id            (SET NULL)
  private_channel_members.added_by_user_id   (SET NULL)
  profile_revisions.changed_by_user_id       (SET NULL)
  proposal_reviews.user_id                   (CASCADE)
  proposal_reviews.delegate_user_id          (SET NULL)
  proposal_reviews.reviewed_by_user_id       (SET NULL)
  slack_app_provisions.agent_registry_id     (CASCADE)

issue lists 18; current tree has 18
in issue but NOT unindexed now: []
unindexed now but NOT in issue: []
```
The set is identical to the issue's, including the three 0022 additions. The cohort FKs are declared at `src/models/cohort.py:43-47` (`created_by`), `:78-82` (`added_by`), `:139-143` (`actor_id`) and in `alembic/versions/0022_add_cohorts.py:40-42,66-68,103-105`; 0022's four indexes (`:81,84,118,121`) are on `cohort_id`, `agent_id`, `cohort_id`, `created_at` — none on the three FK columns.

**Two methodological caveats worth recording for whoever writes the migration:**
1. A model-metadata-only scan (ignoring alembic) reports **28** unindexed FKs; merging `op.create_index` only brings it to **21**; the correct 18 requires also merging alembic-only unique constraints (`uq_proposal_reviews_decision_agent` 0004, `uq_agent_delegate_agent_user` 0007, `uq_email_notification_user_thread_category` 0016). The issue's number is right; a naive re-count would overshoot.
2. **Model/migration drift (not in the issue):** 10 FK columns are indexed only by alembic DDL and the ORM models carry no matching `Index`/`UniqueConstraint`: `agent_channels.simulation_run_id`, `agent_delegates.agent_registry_id`, `cohort_memberships.cohort_id`, `delegate_invitations.agent_registry_id`, `email_notifications.user_id`, `jobs.user_id`, `llm_call_logs.simulation_run_id`, `proposal_reviews.thread_decision_id`, `publications.user_id`, `thread_decisions.simulation_run_id`. Three models even carry a placeholder `__table_args__ = ({"comment": "unique constraint on (...) added in migration"},)` instead of the constraint (`agent_registry.py:105-108`, `delegate.py:92-95`, `email_notification.py:61-64`). Any `alembic revision --autogenerate` for P1 would try to drop those indexes/constraints; the P1 migration must be hand-written.

### PR P3 — `pool_pre_ping` / `pool_recycle` — STILL PRESENT

`src/database.py` (blame `e944d184`, 2026-03-20, untouched):
```python
15 def _get_engine():
16     settings = get_settings()
17     return create_async_engine(
18         settings.database_url,
19         echo=False,
20         pool_size=5,
21         max_overflow=10,
22     )
```
No `pool_pre_ping`, `pool_recycle`, or `pool_timeout`. `git grep pool_pre_ping HEAD` hits only `scripts/migrate/{postflight,preflight,remediate_duplicates}.py`, where one-off engines explicitly set `pool_pre_ping=False` — irrelevant to the app engine. `git log -S pool_pre_ping` shows no commit ever touched `src/database.py` with it.

**Scope note the issue misses:** there are four more engines in `src/` built with bare defaults: `src/worker/main.py:119`, `src/cli.py:24`, `src/agent/grantbot.py:473`, `src/agent/main.py:160` (`create_async_engine(settings.database_url)` — no pool kwargs at all). The long-running ones (worker, agent, grantbot) are as exposed to stale-connection errors after a DB restart as the web engine; a P3 fix limited to `database.py` would leave them unchanged.

No test asserts anything about pool configuration.

### Meta / definition of done

- **"The stack changes none of these files"**: `git log --oneline b1d54da..HEAD -- src/models src/database.py src/routers/admin.py src/routers/profile.py src/main.py nginx/ alembic/versions src/agent/main.py src/services/private_channels.py tests/integration/test_db_contract.py` returns only `18ba52c` (38 lines in `admin.py`, cohort/grantbot membership; delete route untouched per blame). Confirmed.
- **"migrations 0022-0024 add none of the needed constraints/indexes"**: 0023 = three `add_column` on `researcher_profiles`; 0024 = one `add_column` on `agents`. Confirmed.
- **"P1 ships an `alembic upgrade head` check in CI (the offline gate's migration round-trip now covers this)"**: `scripts/ci.sh` step 1 (`:103-118`) checks single head / no duplicate revision ids offline; step 2 (`:11-13`, `:57-70`) runs upgrade -> downgrade -> upgrade against a throwaway Postgres with `MIGRATION_FLOOR=0018`, skippable via `CI_MIGRATION_DB=none`. So that DoD item is already met by infrastructure; P1's migration will be exercised by it automatically.

## 3. Counts

**17 still present / confirmed, 0 fixed, 0 partially fixed, 0 changed, 1 not reproducible as worded** (P2.3 "no DB-side delete" — the 11 children all have DB `ondelete=CASCADE`; the missing piece is only `passive_deletes=True`).

## 4. What I could not verify and why

- **Live reproduction of D1** (an actual `DELETE FROM users` against Postgres) — requires the integration DB (Docker), which is out of bounds here. Verdict rests on the DDL, the absence of any ORM relationship or handler, and the existing contract test that pins the failure.
- **The ~129x speedup and the ~209x figure for `agent_delegates.user_id`** — measurements from the original audit; require a populated DB and `EXPLAIN ANALYZE`. Not re-measured. The structural claims (no index exists) are verified.
- **"Any PI who was ever a private-channel member is undeletable"** in production — I did not query prod; the claim follows from D1.1-D1.3 for any user with a surviving `role="pi"` PCM row.
- **P3's operational symptoms** (stale-connection 500s after DB restart, 30 s block on the 16th checkout) — behavioural claims about the default pool; not exercised. The `pool_timeout` default of 30 s and the 5+10 checkout ceiling are standard SQLAlchemy defaults consistent with the quoted kwargs.
