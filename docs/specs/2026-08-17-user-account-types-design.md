# User account types: PI, manager, admin

**Status:** DESIGNED, not implemented. No code in this document has been written.
**Companion plan:** `docs/plans/2026-08-17-user-account-types-plan.md` (the *how* —
not yet written).
**Scope:** web tier only (`src/routers/`, `src/models/user.py`, `src/dependencies.py`,
`templates/`). Nothing in `src/agent/` changes, so the simulation needs no restart for
correctness — see §8.

**Purpose.** Introduce a third account type. Today the platform has exactly two, and
they are expressed as one boolean: `User.is_admin` (`src/models/user.py:24`). PIs get
the profile/agent surfaces; admins get `/admin/*`. We add **manager** — a global,
strictly read-only account that can see the PI directory, BlackbirdBot's opportunity
assessments, and agent discussion/activity, but cannot mutate anything, cannot
impersonate, and has no lab of its own.

Every constraint recorded below was verified by reading the code at the cited
`file:line`, not inferred. The audit that produced them is in §0; several of them
invalidate the obvious implementation, so they are stated before the design rather
than after it.

---

## §0 — Audit findings that constrain the design

These are the measured properties of the current system. Numbering is stable; the
design sections refer back to them.

**F1. One authorization bit exists.** `User.is_admin` (`src/models/user.py:24`). No
role column, no role table, no permission system. `grep -ri manager` over `src/`,
`templates/` and `docs/` returns only `asynccontextmanager` and one marketing line at
`templates/landing.html:536`. There is no prior art to extend.

**F2. Two gates look like roles and are not.** `access_status` (`allowed`/`pending`/
`denied`, `user.py:35`) and `onboarding_complete` (`user.py:34`). Both are orthogonal
to account type. A manager needs `access_status='allowed'` independently of its role.

**F3. `role` is already taken, twice.** `AgentRegistry.role`
(`src/models/agent_registry.py:31`) selects per-role *bot* prompts (`pi_lab`,
`scout_hub`). `PrivateChannelMember.role` (`src/models/agent_activity.py:285`) is
`'bot'`/`'pi'`/`'delegate'`. A user column named `role` would be a permanent source of
confusion.

**F4. Login is ORCID OAuth only.** `src/routers/auth.py` is the whole auth surface, and
`User.orcid` is `nullable=False, unique=True` (`user.py:23`). There is no password,
email-link, or SSO path. Any account type must have an ORCID iD to exist.

**F5. Authorization is per-endpoint, not per-router.** 34 of the 35 route functions in
`admin.py` each independently declare `Depends(get_admin_user)`; the exception is
`POST /impersonate/stop` (`admin.py:1232`), which correctly uses `get_current_user`.
`main.py:146` mounts the router with no `dependencies=[...]`. There is therefore **no
deny-by-default property**: a future route that omits its dependency is open to any
logged-in user.

**F6. `/admin/users` is not a PI profile view.** It is account administration. Handing
it to a manager ships: the impersonation form (`templates/admin/users.html:10-20`), a
working **Delete User** button (`templates/admin/user_detail.html:122-134`), every
user's email and last-login, and an `Admin: Yes/No` row (`user_detail.html:38`) that
enumerates the admin roster. The two form controls POST to admin-only endpoints, so
they would render live and 403 on click.

**F7. Impersonation is the escalation path, and the check is duplicated.**
`get_current_user` (`dependencies.py:73-89`) honours an **unsigned, client-supplied**
`copi-impersonate` cookie for anyone with `is_admin` true and returns a fully
substituted `User`. The same check is independently re-implemented at `main.py:50-59`.
If the gate is ever written as "not a PI" rather than "is an admin", a manager
impersonates an admin and *is* an admin.

**F8. A manager account is dragged into PI onboarding, and it fires a pipeline job.**
`auth.py:295` redirects any user with `onboarding_complete=False` to `/onboarding`.
`onboarding.py:75` then *self-heals* by enqueuing a `generate_profile` job for any
allowed user with no profile — so creating a manager and logging in starts
ORCID/PubMed profile generation against someone who may have no relevant publications.
Verified by grep: `onboarding.py:193` is the **only** write of
`onboarding_complete = True` in `src/`, and it sits inside
`POST /onboarding/save-profile`. The only exit from onboarding is saving a research
profile.

**F9. The PI nav is unconditional.** `templates/base.html:52-68` shows My Profile /
Settings / My Agent to every logged-in user. A manager clicking "My Agent" reaches
`agent_landing`, which finds no agent and serves `templates/agent/request.html` — an
invitation to request a PI bot.

**F10. The admin sub-nav is gated on `is_admin`.** `base.html:93`, and the Admin link
at `base.html:69`. Any existing admin template rendered for a non-admin arrives with
no sub-navigation: a dead-end page.

**F11. Nothing is scoped, and institution is a known-bad scoping key.**
`admin.py:89` is a bare `select(User)`. `User.institution` is free text, and
`src/routers/public.py:291-396` carries a whole `_normalize_inst` /
`_group_institutions` / `_alias_label` machinery precisely because those strings are
inconsistent.

**F12. `/admin/discussions` applies no visibility filter.** `grep -n
"visibility\|collab_private\|is_private" src/routers/admin.py` returns **nothing**. It
reads `AgentMessage` directly, so content from `collab_private` channels — Slack
`is_private=true`, 2 bots + up to 2 PIs (`src/visibility.py:17-18`) — renders
indistinguishably from public channels. "Read-only, therefore safe" is false here.

**F13. `is_admin` cannot become a plain Python property.** `main.py:52` executes
`select(User.is_admin).where(...)` — that is SQL, and a `@property` is invisible to it.
`cli.py:124` and `cli.py:155` assign `user.is_admin = True/False`, which a read-only
property rejects. `tests/e2e/seed.py:137` does the same.

**F14. Dropping the `is_admin` column breaks user creation.**
`alembic/versions/0001_initial.py:33` is
`sa.Column("is_admin", sa.Boolean, default=False, nullable=False)`. In Alembic,
`default=` is Python-side and emits **no DDL DEFAULT**, so production's column is
`NOT NULL` with no server default. The moment the model stops mapping it, `INSERT INTO
users` omits the column, Postgres rejects the row, and `auth.py:215` fails — **no new
user can log in**.

**F15. The local gate is strict.** `scripts/ci.sh` enforces a 60% branch-coverage floor
(`COV_MIN`), a ruff ceiling of **231** findings on `src/` that must not rise
(`SRC_LINT_MAX`), exactly one alembic head, and an upgrade→downgrade→upgrade round trip
with `MIGRATION_FLOOR=0018` — so a new migration's **downgrade is exercised on every
push**. There is no server-side CI; this script is the whole gate.

---

## §1 — Decisions ledger

Recorded so a later reader can tell a decision from an accident.

| # | Decision | Consequence |
|---|---|---|
| D1 | Managers authenticate via ORCID, like everyone else | No new auth path. F4 satisfied by requiring an ORCID iD (free, self-registerable) |
| D2 | Manager access is **global**, not scoped | Avoids F11 entirely. No tenancy column |
| D3 | Managers see research profile **and** contact info **and** account/engagement metadata **and** agent activity | Effectively "admin's read surface", minus §2's exclusions |
| D4 | Single enum column; `is_admin` derived | One source of truth. Requires F13's `hybrid_property` |
| D5 | Managers **do** see private (`collab_private`) threads | Explicit policy, not an oversight. F12 accepted as-is; no filtering built |
| D6 | New `/manager` router, deny-by-default; `/admin` gates untouched | Fixes F5 for the new surface without editing 34 live gate declarations |
| D7 | Manager and PI are **mutually exclusive** | No PI nav, no PI onboarding, no bot for a manager. Drives §4 |
| D8 | Appointment via admin UI | New admin-gated POST. No allowlist role hint, no self-service |
| D9 | Bootstrap is two-step: manager logs in, then an admin promotes | Reuses `/admin/access-requests`. Manager sees `/access-pending` once |
| D10 | Discussions + run activity, but **not** `llm-calls` | Those rows carry full system prompts (including the Blackbird rubric) and raw model output |
| D11 | PI list shows all `user_role='pi'` rows, unclaimed stubs included | Managers can see recruitment coverage. Staff accounts excluded (F6) |
| D12 | Strictly read-only: **zero** POST routes on `/manager` | Makes the no-mutation guarantee mechanically checkable (§6) |

**D5 is the one to re-read before deploying.** It means a manager can read PI-to-PI
collaboration discussions that the participating PIs believe are private, because the
channels are Slack-private. That is a product policy choice, and it is not visible
anywhere in the UI.

---

## §2 — Data model

Column name is **`user_role`**, not `role`, per F3.

`src/models/user.py`:

```python
USER_ROLE_PI = "pi"
USER_ROLE_MANAGER = "manager"
USER_ROLE_ADMIN = "admin"
VALID_USER_ROLES = (USER_ROLE_PI, USER_ROLE_MANAGER, USER_ROLE_ADMIN)

user_role: Mapped[str] = mapped_column(
    String(20), nullable=False, default=USER_ROLE_PI, server_default=USER_ROLE_PI
)

@hybrid_property
def is_admin(self) -> bool:
    return self.user_role == USER_ROLE_ADMIN

@is_admin.inplace.expression
@classmethod
def _is_admin_expr(cls):
    return cls.user_role == USER_ROLE_ADMIN

# is_manager: user_role == USER_ROLE_MANAGER  — NEVER true for an admin
# is_staff:   user_role in (USER_ROLE_MANAGER, USER_ROLE_ADMIN)
```

`hybrid_property` rather than `property` is F13's fix: it compiles to SQL *and* reads
in Python, so `main.py:52`, `base.html:69`, `base.html:93`,
`user_detail.html:38` and `tests/integration/test_cli.py:383` keep working with **no
edit**. It is read-only, so the three assignment sites (`cli.py:124`, `cli.py:155`,
`tests/e2e/seed.py:137`) must be rewritten to set `user_role`.

The `.inplace.expression` + `@classmethod` spelling is the SQLAlchemy 2.0 form and is
what was verified; the legacy `@is_admin.expression` decorator re-binds the same name
and confuses type checkers. Measured on this repo's pinned SQLAlchemy 2.0.51:

```
select(User.is_admin)          ->  SELECT u.user_role = :user_role_1 AS is_admin
select(User).where(User.is_admin) -> ... WHERE u.user_role = :user_role_1
User(user_role='admin').is_admin   -> True
User(user_role='manager').is_admin -> False        # the F7 guard, confirmed
User(...).is_admin = True          -> AttributeError  # confirms the 3 rewrite sites
```

**`is_manager` means exactly `user_role == 'manager'` and is never true for an admin.**
The "may see manager views" predicate is the separate `is_staff`. Keeping those two
distinct is the structural answer to F7: `is_admin` is false for a manager *by
construction*, so `dependencies.py:74` and `main.py:52` need no defensive change for
impersonation to stay admin-only. Any future code that wants "admin or manager" must
name `is_staff`; there is no formulation of `is_admin` that a manager satisfies.

### Migration `0028_add_user_role` — additive only

```
1. add_column users.user_role VARCHAR(20) NOT NULL SERVER_DEFAULT 'pi'
2. UPDATE users SET user_role = 'admin' WHERE is_admin = true
3. ALTER TABLE users ALTER COLUMN is_admin SET DEFAULT false
4. CREATE CHECK (user_role IN ('pi', 'manager', 'admin'))
```

Step 3 is F14's fix and is the reason this migration is safe to apply *before* the new
code is running: the unmapped `is_admin` column stays insertable.

Step 4's constraint is belt-and-braces — a typo'd role already fails closed, since all
three predicates return false and the account degrades to PI-equivalent — but a
constraint is cheaper than relying on that.

Downgrade: `UPDATE users SET is_admin = (user_role = 'admin')`, drop the constraint,
drop the column. Data-preserving and real, which matters because F15 means this
downgrade runs on every push.

**Dropping `is_admin` is deliberately deferred** to a separate `0029_drop_is_admin`,
shipped after the new code is confirmed live. See §8.

---

## §3 — Auth and routing

`src/dependencies.py` — `get_current_user` and `get_admin_user` are **not modified**.
One addition:

```python
async def get_staff_user(current_user: User = Depends(get_current_user)) -> User:
    """Admin or manager. Used ONLY by the /manager router."""
    if not current_user.is_staff:
        raise HTTPException(status_code=403, detail="Manager access required")
    return current_user
```

New `src/routers/manager.py`, mounted in `main.py` at `prefix="/manager"` with a
**router-level dependency**:

```python
router = APIRouter(dependencies=[Depends(get_staff_user)])
```

This is where F5's structural weakness is fixed for new code. Unlike `admin.py`, a
route added to this router later cannot be left un-gated by forgetting a declaration.
Handlers that need the object still take `current_user: User = Depends(get_staff_user)`;
FastAPI caches the dependency per request, so it resolves once.

All routes are GET. There are none other than these:

| Route | Notes |
|---|---|
| `GET /manager` | redirect → `/manager/pis` |
| `GET /manager/pis` | all `user_role='pi'`, unclaimed stubs included (D11); staff rows excluded |
| `GET /manager/pis/{user_id}` | **404 if the target's `user_role != 'pi'`** |
| `GET /manager/assessments` | run selector included, mirrors `/admin/assessments` |
| `GET /manager/discussions` | private threads included per D5; **no `export` query param** |
| `GET /manager/activity` | run list |
| `GET /manager/activity/{run_id}` | run detail |

The `{user_id}` 404 closes staff-account enumeration: without it a manager could read
any admin's record, including their email, by guessing or harvesting a UUID.

There is **no** `/manager/activity/{run_id}/llm-calls` route (D10). A manager typing
that path gets 404 from the manager prefix and 403 from `/admin/...`.

Admins satisfy `is_staff`, so they can browse `/manager/*` too. That is intentional: it
is how an admin verifies what managers actually see.

---

## §4 — Service extraction

New `src/services/directory.py`: pure `(db, filters) -> data` functions with no
`Request`, no `Jinja2Templates`, no `HTTPException`.

| Function | Lifted from |
|---|---|
| `list_pi_directory(db, *, status_filter, institution_filter, claimed_filter)` | `admin.py:89-142` |
| `load_user_detail(db, user_id)` | `admin.py:167-193` |
| `list_assessments(db, run_id)` | `admin.py:885-936` |
| `build_discussions_view(db, ...)` | `admin.py:519-771` |
| `list_runs(db)` | `admin.py:262-311` |
| `build_run_detail(db, run_id)` | `admin.py:311-387` |

Both routers become thin callers: helper → `_template_context(...)` →
`TemplateResponse`. **Every `@router.get` / `@router.post` decorator and every
`Depends(get_admin_user)` declaration in `admin.py` is untouched** — only bodies move.

This is extraction, not rewriting. It is worth doing rather than duplicating because
`build_discussions_view` alone is ~280 lines; a copy would drift, and every future fix
would have to be made twice or silently would not be. It also pays down debt in the
file the change touches: `admin.py` is 73KB and holds 16 of the findings that motivated
`SRC_LINT_MAX` (F15).

The safety net for the refactor already exists:
`tests/characterization/test_auth_and_admin_routes.py:171-266` pins
`/admin/discussions` and `/admin/activity/{run_id}` against regression,
`tests/integration/test_opportunity_assessment_persistence.py:26` pins assessments, and
`tests/integration/test_cohort_admin.py` pins the cohort admin paths.

`admin_discussions`'s `export` parameter and `templates/admin/discussions_export.html`
stay admin-only; the manager route simply does not accept the parameter.

---

## §5 — Manager ≠ PI plumbing

D7 makes the roles exclusive, so every PI-shaped assumption about "any logged-in user"
has to be narrowed. F8, F9 and F10 are all in this section.

- `auth.py:295` — gate the `/onboarding` redirect on `user_role == 'pi'`. Non-PI staff
  skip onboarding entirely.
- `auth.py:300` — default post-login landing becomes `/manager/pis` for managers. PIs
  and admins keep `/profile`, unchanged.
- `onboarding.py:55` — bounce non-PI roles to `/manager/pis`.
- `onboarding.py:75` — add `and current_user.user_role == 'pi'` to the self-heal, so a
  manager who reaches the URL never fires `generate_profile` (F8).
- `templates/base.html:52-68` — gate **My Profile** and **My Agent** on
  `user_role == 'pi' or is_admin`. **Settings stays visible to everyone**: it is email
  notification preferences, which a manager still needs.
- `templates/base.html` — add a top-level **Manager** link for `is_staff`, and an
  `active_page == 'manager'` sub-nav block (PIs / Assessments / Discussions / Activity)
  gated on `is_staff`. Without this the new pages inherit F10's dead end.

Direct-URL access degrades to a terminating bounce, not a loop: manager → `/profile`
(`profile.py:48`, onboarding incomplete) → `/onboarding` (role is not `pi`) →
`/manager/pis`. Pinned by a test in §7.

---

## §6 — Admin appointment UI

On `/admin/users/{user_id}`: replace the `Admin: Yes/No` row
(`templates/admin/user_detail.html:38`) with a **Role** row, plus a selector posting to
a new `POST /admin/users/{user_id}/role` under `Depends(get_admin_user)`.

Name the handler `admin_set_user_role`. `POST /agents/{agent_id}/role`
(`admin.py:1147`) already exists and sets a *bot* role; the paths do not collide but
the names would.

Guards, following the self-delete precedent at `admin.py:210`:

1. An admin cannot change their **own** role.
2. The submitted value must be in `VALID_USER_ROLES`.
3. **Refuse to demote the last remaining admin** — defense in depth.
4. Log the change, as `admin.py:216` does.

Guard 3 is deliberately kept, but note what actually protects the invariant: **guard 1
does.** Demoting the last admin X over HTTP requires an actor with admin rights who is
not X, and if X is the last admin no such actor exists — so guard 3 is unreachable
through the UI for as long as guard 1 stands. It stays as the backstop in case guard 1 is
ever relaxed, and is tested by calling the handler directly rather than through a
request. The CLI `role:set` carries **no** guards, by design: it is the recovery path
when no admin can log in.

`src/cli.py` changes: `admin:grant` sets `user_role='admin'`; `admin:revoke` sets
`'pi'`; add `role:set --orcid --role`; `list-users`' "Admin" column becomes "Role". The
CLI is retained deliberately — it is the escape hatch for guard 3 and for a database
where no admin exists yet.

Bootstrap sequence (D9), which is what an operator actually does:

1. The manager signs in with ORCID. `auth.py:215` creates the row with
   `access_status='pending'` and `user_role='pi'`; they land on `/access-pending`.
2. An admin approves them at `/admin/access-requests` (`admin.py:1293`).
3. An admin sets their role to `manager` at `/admin/users/{id}`.
4. The manager signs in again and lands on `/manager/pis`.

Step 1 leaves a window in which the account is a PI, so a manager who logs in between
steps 2 and 3 gets the PI experience. Acceptable for a hand-provisioned role; noted
because it is surprising.

---

## §7 — Testing

TDD: tests first, per `superpowers:test-driven-development`.

**`tests/unit/test_user_roles.py`** (new)
- `is_admin` is true iff `user_role == 'admin'`; **explicitly false for a manager** —
  this is the F7 escalation guard, and it is the single most important assertion in the
  change.
- `is_manager` and `is_staff` semantics, including that an admin is `is_staff` but not
  `is_manager`.
- The SQL-expression form resolves: `select(User.is_admin)` returns rows, pinning
  `main.py:52`'s query shape.

**`tests/integration/test_manager_views.py`** (new)
- Routes **enumerated from `manager.router.routes` and parametrized**, so a route added
  later is automatically covered by both the PI-gets-403 and manager-gets-200 sweeps.
  A hand-written list would rot; this is what keeps D6 honest.
- `{m for r in manager.router.routes for m in r.methods} == {"GET"}` — D12's
  no-mutation-routes property becomes machine-checked rather than a promise. Verified on
  FastAPI 0.141.1 that an `APIRouter.get()` route's `.methods` is exactly `{"GET"}`:
  FastAPI's `APIRoute` does **not** add `HEAD`, unlike Starlette's plain `Route`, so
  this assertion is not silently over-broad.
- Manager gets 403 on every `/admin/*` route (also parametrized).
- **Manager cannot impersonate**: `POST /admin/impersonate` → 403, *and* a hand-set
  `copi-impersonate` cookie is ignored for a manager (exercising `dependencies.py:74`
  and `main.py:50-59` from the manager side).
- `/manager/pis/{admin_user_id}` → 404.
- `/manager/activity/{run_id}/llm-calls` → 404.
- The PI list excludes admin and manager rows and includes unclaimed PI stubs.
- Manager login enqueues **no** `generate_profile` job and is not redirected to
  `/onboarding`; the `/profile` → `/onboarding` → `/manager/pis` bounce terminates.

**Existing tests to update** — mechanical but non-optional, because `is_admin=` is no
longer a constructor kwarg: `tests/factories.py:28-43` (add `user_role`),
`tests/e2e/seed.py:133-137`, `tests/integration/test_cli.py:369-434`,
`tests/characterization/test_auth_and_admin_routes.py:142-150`, plus the `is_admin=`
call sites in `test_cohort_admin.py`, `test_onboarding_flow.py` and
`test_opportunity_assessment_persistence.py` (~20 sites total).

**Gate arithmetic (F15).** New code lands in `src/routers/manager.py` and
`src/services/directory.py`; the extraction in §4 moves lines rather than adding them,
so the net `src/` growth is roughly the manager router plus the role plumbing. Both new
modules must be ruff-clean so `SRC_LINT_MAX=231` does not rise, and the new integration
tests should carry the added branches past `COV_MIN=60`. Verify with `./scripts/ci.sh`,
not by inspection.

---

## §8 — Deploy sequence

The two-phase migration (§2) is what buys zero downtime. Because `0028` is purely
additive **and** step 3 gives `is_admin` a server default, the currently-running
container keeps working against the new schema — there is no window in which live code
and applied schema disagree.

Per `CLAUDE.md`, in order:

```bash
DC="docker compose -f docker-compose.prod.yml"

./scripts/ci.sh                                    # must be green first
$DC up -d --build blackbird-app worker             # web tier carries the new code
$DC exec -T blackbird-app alembic upgrade head     # NOTHING ELSE DOES THIS
$DC exec -T blackbird-app alembic current          # must equal `alembic heads`
```

`src/agent/` is untouched, so the simulation does not need a rebuild or a restart for
this change. `$DC --profile agent build agent` is harmless if run anyway.

`0029_drop_is_admin` ships in a **later, separate deploy**, once `/admin` and
`/manager` are confirmed working on the new code. Until then the orphaned column sits
unmapped and defaulted, costing nothing.

---

## §9 — Rejected alternatives

**Relaxing `get_admin_user` on the read-only `/admin` endpoints.** Smaller diff, and it
was rejected for two reasons. It edits the live gate on a 73KB file where the gate is
declared 34 separate times (F5), and it requires an `{% if current_user.is_admin %}`
around every mutation control in shared templates — where one miss is F6's live-looking
Delete User button, and a *second* miss is a working one.

**A capability/permission system** (`require_read("users")` /
`require_write("users")`). More future-proof, and it is a permission framework for
three roles. YAGNI; revisit if a fourth type appears with a genuinely different
read/write split.

**Duplicating the queries in the manager router** instead of §4's extraction. Leaves
`admin.py` untouched, at the cost of two copies of a ~280-line discussions query that
will drift, and ~400 net new lines pressing on both gate thresholds in F15.

**An additive `is_manager` boolean** alongside `is_admin`. Cheapest schema change, but
it makes `admin AND manager AND pi` representable and turns every future check into a
two-flag conjunction — the exact ambiguity that F7 punishes.

**`role` as the column name.** Rejected per F3.

**Institution- or cohort-scoped managers.** Rejected by D2. Institution is unusable as
a key (F11); cohorts would need a manager↔cohort join table and a scoping clause on
every query in §4. Both are tractable later — nothing here forecloses them, since
`list_pi_directory` already takes a filter argument.

**Non-ORCID login for managers.** Rejected by D1. It would mean making `User.orcid`
nullable, reworking its unique constraint, and maintaining a second authentication path
— a much larger and more security-sensitive change than the rest of this design
combined.

---

## §10 — Out of scope, and known gaps

- **CSV export for managers.** No export endpoint (D12). The precedent exists
  (`admin.py:1437` with `csv_safe_cell`) if it is wanted later.
- **Assessment annotations.** Would need a new table and a POST route, giving up D12's
  no-mutation property.
- **An audit log of manager reads.** Nothing records that a manager viewed a PI's
  record. Given D3 includes contact information, this may matter for a real deployment
  and is not built.
- **F12 / D5 remains unmitigated by design.** Managers read private-channel content and
  no UI states that. If that policy is ever reversed, the filtering does not exist and
  would have to be built in `build_discussions_view`.
- **The `is_admin` column survives** until `0029`. Anyone reading the schema between
  the two deploys will see a stale boolean that no code consults.
- **F5 is fixed only for `/manager`.** `admin.py` still declares its gate 34 times with
  no router-level default. Converting it to `APIRouter(dependencies=[...])` is a safe,
  separate cleanup that this design does not undertake.
