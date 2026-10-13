# Managers and reviewers without a lab bot — plan

Date: 2026-10-13 · Status: design approved by the owner 2026-10-13 (§3), follow-ups O1 and O2 decided
the same day; both adversarial audits folded in (§10); work packages in §11 · Code baseline:
`blackbird` @ `b62eba5d` (working tree), alembic head `0065` · No schema change.

## 1. Problem

The only web way to put a person into the app is **Add a PI** (`POST /workspace/pis`,
`src/routers/workspace/pi_directory.py:157`). For a new ORCID it creates a `pi` user, a
`generate_profile` job and a **pending** `AgentRegistry` row in one commit (`:191-198`); for a `pi`
account that signed in first it adopts the account and mints the agent, queueing a job only when there
is no profile (`src/services/pi_onboarding.py:187-191`). Either way the account owns a pending agent,
and `change_user_role` refuses a move off the PI surfaces while the user owns an agent in `active`,
`pending` or `inactive` (`src/services/user_roles.py:12,29-40`). The approve form on
`/admin/agents/{id}` always *activates* a pending agent, whatever its status dropdown says
(`src/routers/admin/agents.py:322-326`). Hence today's route: Add PI → wait for the profile → approve
(activate) → set the agent to suspended → change the role.

What that route costs:

- a full profile pipeline for a non-PI, and the `enrich_grants`, `industry_evidence` and
  `company_discovery` jobs it enqueues (`src/services/profile_pipeline.py:1058-1074`);
- a JHU tenure entry when ORCID shows a Hopkins employment with a start year
  (`pi_onboarding.py:63-69`), a persona file `profiles/public/<slug>.md`, and an agent row whose slug
  and bot name stay reserved (no code deletes agent rows; `user_deletion.py:153-158` keeps them);
- activation wires the lab into its `hub-{slug}` star spoke (`agents.py:348-353`);
- Add PI leaves a new account `pending` access (model default, `src/models/user.py:67-69`), so it still
  needs an access approval unless the ORCID is allowlisted.

## 2. Interim, with no code change

- **Before the person signs in** (CLI, from a `blackbird-app` shell): `python -m src.cli seed-profile
  --orcid <iD> --no-pipeline` (creates a `pi` user, pending, no job; it does record a tenure key when
  ORCID shows a Hopkins employment, `src/cli.py:29-105`), then `python -m src.cli role:set --orcid <iD>
  --role reviewer|manager`, then Approve on `/admin/access-requests`.
- **After the person signed in** (a `pending` `pi` user with no agent): set the Account Type first
  (`/admin/users/{id}`, which for a `pi` redirects to the workspace PI page's account block,
  `src/routers/admin/users.py:85-86`), **then** Approve. `enqueue_profile_job_if_absent` queues nothing
  for a manager or reviewer (`src/services/profile_jobs.py:18,35`); approving first, while the role is
  `pi`, queues a profile pipeline (`src/routers/admin/access.py:88-94`).
  `docs/operations/pis-and-access.md:418-420` documents the wrong order today.
- **Already made through Add PI:** Reject on `/admin/agents` suspends the pending agent without
  activating it (`agents.py:447-456`); the role change is then allowed. The profile job has already
  been queued.

## 3. Decisions

### 3.1 Owner decisions (2026-10-13)

| # | Decision |
|---|---|
| D1 | Admins create managers and reviewers; managers create reviewers only. No admin is created this way. |
| D2 | An ORCID already held by an account may be converted: an admin may convert an agentless, non-denied `pi` account, `allowed` and with a profile included (O1); a manager only a `pending` agentless `pi` account with no profile. §5.1's delegate and running-job refusals apply to both. |
| D3 | Every create, convert, revoke, restore and delete writes an `admin_audit_events` row and a log line. |
| D4 | A new workspace **Staff** page, visible to admins and managers, lists every manager, reviewer and admin, with the add form. |
| D5 | Managers may revoke a reviewer's access (deny + sign out everywhere) and delete a reviewer account. |
| D6 | Managers may restore any denied reviewer, including one an admin denied. |
| D7 | (after the implementation audit) A manager may revoke only a reviewer who has signed in with access (`last_login_at` set; refusal `never_signed_in`); admins any reviewer. Closes create/convert-then-revoke as a manager deny power. |
| D8 | (after the implementation audit) A manager may not create or convert an ORCID on the access allowlist (`ask_admin`, before any fetch); admins may. Closes create-then-delete stripping an admin's allowlist row. |

Consequences recorded with the owner:

- Admin and manager accounts are changed only by admins, on `/admin/users/{id}`; the Staff page links
  there for admins and offers no buttons on those rows.
- Keeping a person out of managers' reach: an admin sets the account to `pi` **first**, then denies
  it (the reverse order leaves a moment where it is a denied reviewer a manager can restore). Manager
  restore covers only `reviewer` accounts and manager conversion refuses `denied` accounts. Deleting is
  not enough: the next sign-in is a pending `pi` account with no profile, which D2 lets a manager
  convert.
- Convert (D2) then delete (D5) lets a manager remove a pending, profile-less PI sign-up; the person can
  sign in again and the audit rows record both steps.
- A reviewer can own an agent row (a workaround conversion keeps its suspended agent). Deleting such a
  reviewer runs the full teardown: persona files removed, Slack bot token revoked
  (`src/services/user_deletion.py:153-168,216-277`). The delete confirmation says so.
- A manager's delete always removes the ORCID from the access allowlist; an allowlisted ORCID otherwise
  signs straight back in as an `allowed` PI and queues a profile pipeline
  (`src/routers/auth.py:188,201-203`).

### 3.2 Raised by audit 1, decided by the owner

- **O1 — decided 2026-10-13: no.** Admins keep D2's wider conversion (an `allowed` PI with a
  profile included), subject to §5.1's delegate and running-job refusals; the owner accepts the
  industry-score and mistyped-iD risks below. The audit found that converting an `allowed` PI with a
  profile leaves things a staff account must not keep: `AgentDelegate` rows (delegate *reads* of another
  lab's conversations are not role-gated, `src/dependencies.py:228-246`, `src/routers/agent_page.py:330-350`),
  running `enrich_grants`/`industry_evidence`/`company_discovery` jobs (no role check in the worker), and
  industry-score rows that stay in every PI's percentile group (`src/services/directory.py:330-340`).
  A mistyped iD would also silently turn a working PI into staff. Option: both actors convert only a
  `pending` account with no profile; admins handle anything else with the existing role route.
- **O2 — decided 2026-10-13: a confirm step.** `validate_orcid` checks the format only and the account
  is `allowed` at once, so a real but wrong iD (a namesake's) would make a stranger staff at their first
  ORCID sign-in, or (O1) turn a working PI into staff. The first submit previews who the iD belongs to
  and what will happen; a second submit acts (§5.1–5.2).

## 4. Acceptance criteria

1. The confirm POST creates a `users` row with the chosen role and `access_status='allowed'`, and creates no
   `agents` row, no `jobs` row, no tenure `app_settings` key and no persona file.
2. That person's first ORCID sign-in lands on `/workspace/pis` (manager) or `/workspace/assessments`
   (reviewer), skips onboarding and queues nothing (`src/routers/auth.py:238-268,297-307`; a saved
   `next` or a pending invite token takes precedence over the landing page, as for everyone).
3. D1, D2, D5 and D6 hold for every combination of actor (admin, manager, reviewer, PI, anonymous,
   impersonating admin) and target (pi, manager, reviewer, admin, self).
4. Each action writes one audit row in the same transaction as its effect.
5. `./scripts/ci.sh` is green.

## 5. Design

### 5.1 Service — `src/services/staff_accounts.py`

The module's public interface is fixed by the stub committed to the working tree before fan-out
(signatures, `StaffPreview`, `StaffAccountRefused`, `REFUSAL_MESSAGES`, `AUDIT_*`,
`STAFF_REFILL_SESSION_KEY`); §11 lists who consumes what. HTTP-free; nothing commits except
`delete_reviewer`.

**Locking rule (audit 2, defect 3).** Every `SELECT … FOR UPDATE` that may return an object already in
the session carries `.execution_options(populate_existing=True)`, as `agents.py:280-284` and
`user_deletion.py:147` do; otherwise the lock is taken on stale attribute values. Revoke, restore and
delete load their target **once**, locked.

**`preview` / `create_or_convert` precedence** (identical in both; the first failing check wins):

1. `role not in creatable_roles(actor)` → `invalid_role`.
2. `validate_orcid` fails → `invalid_orcid`.
3. Holder of the iD (unlocked in `preview`; in `create_or_convert` re-read `FOR UPDATE` +
   `populate_existing` once a holder is known):
   - **no holder** → `fetch_orcid_profile` (in `create_or_convert` before any lock; any failure →
     `fetch_failed`); name from `name_from_machine_source(profile["name"])`, else
     `validate_person_name(name)` (`InvalidPersonName` → `invalid_name`), else `name_required`.
     Outcome `create` with ORCID's name, institution and `profile["employments"]`.
   - **holder** whose role is not `pi`, or `access_status == "denied"` → `exists`.
   - holder owning any `agents` row or any `agent_delegates` row → admin: `use_account_page`
     (with `user_id`); manager: `ask_admin`.
   - holder with any `PER_USER_JOB_TYPES` job `pending`/`processing` → `jobs_running`.
   - manager actor and (`access_status != "pending"` or a `researcher_profiles` row exists) →
     `ask_admin`.
   - stored name empty or `is_orcid_like` → a typed name is required (`name_required`; invalid →
     `invalid_name`) and replaces it.
   - otherwise outcome `convert`.
4. `create_or_convert` only: `fresh.expectation() != expectation` → `StaffAccountRefused("changed",
   preview=fresh)`, before any write.
5. Create: `User(orcid, name, name_sanitized_at, institution, department, user_role=role,
   access_status="allowed")` inside `db.begin_nested()`; `IntegrityError` → `exists`. Then the ORCID
   public email through `assign_user_email`. No tenure, job, agent or persona. Convert:
   `change_user_role(db, user, role)` (bumps the epoch), `access_status = "allowed"`, the typed name
   when required. Either way, the audit row (§5.4).

**Reviewer actions.** `revoke_reviewer` / `restore_reviewer` / `delete_reviewer` load the target by id
`FOR UPDATE` + `populate_existing`; refusals in order: missing → `not_found`; role not `reviewer` →
`not_reviewer`; the actor (or, under impersonation, the real admin) → `self`; then revoke needs
`allowed` (`not_allowed`), restore needs `denied` (`not_denied`). Revoke sets `denied` and
`bump_session_epoch` (as `admin_deny_access`); restore sets `allowed` and queues nothing (the bumped
epoch keeps old cookies dead); delete adds the audit row, then `delete_user_account(db, target,
remove_from_allowlist=True)`, which commits both or propagates for the caller to roll back
(`user_deletion.py:193-209`). What a reviewer delete keeps (`ON DELETE SET NULL`): reviews and review
events with the name kept (`src/models/review.py:74-79,156-161`), assessment-chat usage and opens,
prompt-suggestion status attribution, allowlist `added_by`, audit-row actors. What goes (`CASCADE`):
review assignments (`review.py:204-208`) and their own chat turns (`assessment_chat.py:125-127`).

**Lists.** `list_staff_accounts(viewer)` and `recent_staff_changes()` return plain dicts for the page
(§5.3).

### 5.2 Routes — new `src/routers/workspace/staff.py`

Each handler declares `current_user: User = _STAFF` itself (`_STAFF = Depends(get_staff_user)`, module
level). **No router-level dependency**: the matrix requires exactly one `get_staff_user` per POST
(`test_workspace_permissions_matrix.py:280-283`). Each POST calls `refuse_impersonation` first.

| Route | Name |
|---|---|
| `GET /workspace/staff` | `workspace_staff` |
| `POST /workspace/staff` | `workspace_create_staff_account` |
| `POST /workspace/staff/{user_id}/revoke` | `workspace_revoke_reviewer` |
| `POST /workspace/staff/{user_id}/restore` | `workspace_restore_reviewer` |
| `POST /workspace/staff/{user_id}/delete` | `workspace_delete_reviewer` |

- Form fields all default to `""` (`orcid`, `user_role`, `name`, `confirm`, `expectation`), so a
  malformed post is refused with a code, never FastAPI's 422.
- `POST /workspace/staff` without `confirm == "1"` → `preview`; success renders the Staff page (200)
  with `staff_preview`; a refusal redirects. With `confirm == "1"` → `create_or_convert`; success
  commits, flashes, redirects; `changed` rolls back and renders the page (200) with
  `staff_preview=exc.preview.as_view()` and an inline "something changed" notice; any other refusal
  redirects.
- **Every refusal**: `await db.rollback()`, `flash(request, REFUSAL_MESSAGES[code], "error")` (for
  `use_account_page` the admin is also given the account link via the flash text or the refill), store
  the validated refill in `request.session[STAFF_REFILL_SESSION_KEY]` (`orcid` only when
  `validate_orcid` passes, `user_role` only when in `STAFF_ACCOUNT_ROLES`, `name` cut to 100), and
  redirect 302 to plain `/workspace/staff` — **nothing in the URL** (audit 2, defect 1: the matrix
  compares manager and admin `Location`s for targets with different ORCIDs). An unexpected exception →
  `create_failed`, logged with `logger.exception`.
- Revoke/restore/delete **redirect** (302 + flash) on every refusal, including a non-reviewer or missing
  target, never 404 (the matrix expects 302 for a PI target).
- `GET /workspace/staff` pops the refill and renders `workspace/staff.html` through
  `workspace_context(request, current_user, section="staff", …)` with the §5.3 keys.
- Wiring: included in `src/routers/workspace/__init__.py`; each POST mirrored in the legacy loop in
  `src/routers/manager.py:111-141` with the same endpoint object (`legacy_manager_create_staff_account`,
  `legacy_manager_revoke_reviewer`, `legacy_manager_restore_reviewer`, `legacy_manager_delete_reviewer`);
  plus `@router.get('/staff', name='legacy_manager_staff')` taking `_STAFF` and returning
  `redirect(request, current_user, 'staff', 'workspace_staff', manager=True)` (as `/manager/slack-bots`,
  `manager.py:153-155`), because the POST alias at `/manager/staff` would otherwise make a GET there a
  405 (audit 2, defect 2); `QUERY_FIELDS["staff"] = ()` in `src/web/urls.py`.

### 5.3 Page, navigation and projection

Context keys (page-unique names; audit 2, defect 4), all passed to `workspace_context`:

| Key | Value |
|---|---|
| `staff_rows` | `list_staff_accounts(...)`: `[{"user": User, "is_self", "has_agent", "can_revoke", "can_restore", "can_delete"}]` |
| `staff_roles` | `list(creatable_roles(current_user))` |
| `staff_preview` | `None` or `StaffPreview.as_view()`: `outcome, orcid, role, name, institution, employments[{organization, start_year, current}], user_id, access_status, has_profile, expectation` |
| `staff_preview_changed` | `bool` (the `changed` re-render) |
| `staff_refill` | `None` or `{"orcid", "user_role", "name"}` |
| `staff_changes` | `recent_staff_changes(...)`: `[{"action", "actor_name", "target_name", "target_orcid", "role", "created_at"}]` |

- `src/web/presentation.py`: add every key above and every nested key not already listed to
  `VIEW_FIELDS` only; add `access_status` to `ACCOUNT_USER` (staff-only by construction). **Add nothing
  to `STAFF_KEYS`**: it strips names globally for non-staff (`presentation.py:240-241,288-292`), and
  e.g. `preview` is already a reviewer-visible key (`assessment_detail.py:1153`). Pass plain dicts, never
  the `StaffPreview` dataclass (`_project` raises `TypeError` on unregistered types,
  `presentation.py:305-320`).
- `templates/workspace/staff.html` (extends `base.html`, styled like `workspace/slack_bots.html`): the
  add form (ORCID iD; role `<select>` from `staff_roles`; name "only if ORCID shows none"; refilled from
  `staff_refill`); when `staff_preview` is set, a confirmation panel ("Create a *reviewer* account for
  *Jane Doe*, *Johns Hopkins University*" with current employments, or "Convert *Jane Doe*'s existing PI
  account (access: approved, has a profile)"), its confirm form posting `orcid`, `user_role`, `name`,
  `confirm=1`, `expectation` as hidden fields, and a Cancel link; the accounts table (name, ORCID, role,
  access status, last sign-in) with Revoke / Restore / Delete forms per the `can_*` flags (Delete's
  confirmation names what is kept and what goes, and the full teardown when `has_agent`, using the same
  CSP-safe confirmation mechanism as the admin delete form in `templates/admin/user_detail.html`);
  `/admin/users/{id}` links only under `permissions.admin`; a "Recent changes" list from
  `staff_changes`. No `data-row-href` (it would pull in `test_ui_behaviours.py`/`test_row_links.py`).
  Every control has an accessible name (`tests/unit/test_control_names.py`); headings, labels and
  contrast pass the rendered-page gate.
- `templates/workspace/pis.html`: under Add PI, one line linking `/workspace/staff` ("Adding a manager or
  reviewer? Use the Staff page — Add PI always creates a lab bot."). No `/admin/` in it
  (`test_pi_directory_has_no_admin_controls`).
- `src/web/navigation.py`: `NavigationItem("staff", "Staff", "workspace_staff", True)`.

### 5.4 Audit rows

`AdminAuditEvent(action=…, actor_user_id=<real actor: the impersonating admin never acts here, so the
session user>, payload={"user_id": str, "orcid": str, "name": str, "role": str})`, added to the session
so it commits with the effect (not `record_audit`, which commits by itself). They show on the Staff
page's "Recent changes" and, as action and time only, in `/admin/simulation`'s 20-row "Recent admin
actions" (where they can push simulation-control rows out of the window; accepted).

### 5.5 Concurrency (READ COMMITTED; the web engine sets no isolation level)

- **Profile jobs queued with a stale role.** Approve (`access.py:83-94`), allowlist add
  (`access.py:170-178`), login (`auth.py:256-263`) and Add-PI adoption modify the user row, then call
  `enqueue_profile_job_if_absent`, which reads the role from the in-memory object. Fix: it re-reads with
  a **column** select, `select(User.user_role, User.access_status).where(User.id == user.id)` (autoflush
  sends pending edits first; never an entity select, which returns the stale object, nor `refresh()`,
  which can discard in-memory edits), and decides on those values. Every current caller has flushed the
  row first (`pi_onboarding.py:131`, `auth.py:191-193`, `cli.py:88`, autoflush elsewhere).
- **Convert vs. Add-PI adoption of the same iD.** `adopt_agentless_pi` (`pi_onboarding.py:164-166`)
  does its ORCID fetch (`:180-186`) first, then re-reads the user `FOR UPDATE` + `populate_existing` and
  re-checks role, denial and agent ownership before writing; no lock is held across the network call.
- **Accepted residuals.** The role route, `/agent/request` and admin Link can still race a convert and
  leave a staff account owning a pending agent; activation is refused
  (`src/services/agent_activation.py:115-116`) and, with the re-read, no job is queued.

### 5.6 Side effects to know

- The first `reviewer` account turns on the reviewer tier of the worker's assessment-chat opening
  questions (`assessment_chat_suggestions._tiers`, which counts any reviewer row), spending Claude
  credit under `assessment_chat_suggestions_daily_usd_limit`. Same as a role change today.
- A new staff account can be assigned assessments before it ever signs in (the assignee list filters on
  role and `allowed` only, `src/services/assessment_detail.py:2110-2118`).
- Pre-existing, out of scope: the role route does not check `AgentDelegate` rows either.

## 6. Tests

New, Postgres-backed (`fetch_orcid_profile` monkeypatched on `src.services.staff_accounts`, as
`tests/integration/test_manager_add_pi_name.py:19` does on `pi_onboarding`):

- `tests/integration/test_staff_accounts_service.py` (service):
  - create: admin → manager and reviewer, manager → reviewer; row state, no agent/job/tenure key,
    email through `assign_user_email`, audit row; manager asking for `manager` → `invalid_role`;
    `pi`/`admin` refused; bad iD; fetch failure; name required / typed / invalid; concurrent create →
    `exists`;
  - convert: admin converts pending and allowed agentless PIs (profile kept); manager converts a pending
    one with no profile; `ask_admin` for a manager on allowed or profiled accounts or any agent/delegate
    row; `use_account_page` for an admin on any agent row (each status incl. `suspended`) or delegate
    row; `jobs_running` for each per-user job type; `exists` for denied or already staff; empty stored
    name requires a typed name; epoch bumped;
  - expectation: `changed` (with `preview`) when the role, access status, has-profile, name or outcome
    differs from the previewed digest; nothing written;
  - stale identity map: load the target unlocked, change it through a second session, then convert /
    revoke / delete → refused on the fresh values;
  - revoke / restore / delete: reviewer targets only; `self`; `not_found`; revoke bumps the epoch;
    restore queues nothing; delete keeps reviews (name kept), drops assignments and chat turns, removes
    the allowlist row, and tears down an owned agent; audit rows for all.
- `tests/integration/test_staff_concurrency_guards.py`: `enqueue_profile_job_if_absent` queues nothing
  when the stored role is manager/reviewer though the in-memory object says `pi`; still queues for a
  stored `pi`; `adopt_agentless_pi` refuses when, after its fetch, the account is no longer an agentless
  `pi` (role changed through a second session).
- `tests/integration/test_staff_routes.py` (HTTP): the confirm flow (first submit writes nothing and
  shows name, institution and employments, or the account's state; confirm acts; `changed` re-renders
  with 200 and writes nothing, rolled back); refusals redirect to plain `/workspace/staff` with the
  flash and the session refill; every POST refuses PI, reviewer, anonymous and impersonating admin with
  nothing written; revoke ends an existing session (`get_current_user` refuses the old cookie); a
  created account's first ORCID login lands on its role page with no job; `/manager/staff` redirects for
  staff and 403s a reviewer; the Add-PI hint renders without `/admin/`.

Existing tests to update:

- `tests/workspace_support.py`: `POST_SUFFIXES` += `/staff`, `/staff/{user_id}/revoke`,
  `/staff/{user_id}/restore`, `/staff/{user_id}/delete`.
- `tests/integration/test_workspace_permissions_matrix.py`: rename
  `test_all_28_writes_deny_untrusted_effective_identities` (no count); `_effect` adds the target's
  `(user_role, access_status, session_epoch)` and `count(users)`; `_snapshot` adds `count(users)`
  (audit 2, defect 6); add `/workspace/staff` to the GET path list in
  `test_all_workspace_get_and_head_routes_follow_effective_role` (reviewer 403; legacy `/manager/staff`
  403 for a reviewer, 302 for staff, as for Slack Bots).
- `tests/integration/test_manager_views.py::test_manager_router_mutations_are_an_explicit_allowlist`:
  list + docstring, 28 → 32. `test_pi_directory_has_no_admin_controls` stays unedited.
- `tests/unit/test_reachability.py:152`: add `/manager/staff` to the legacy GET tuple.
- `tests/integration/test_rendered_page_gate.py`: `PAGES` entries for `/workspace/staff` and
  `/manager/staff`, and a test applying the gate's page checks to the POST-rendered confirmation panel
  (the gate walks GETs only).
- Re-run unedited: `tests/integration/test_orcid_id_names.py`, `tests/integration/test_workspace_data_contract.py`,
  `tests/unit/test_control_names.py`.

## 7. Docs

`docs/operations/pis-and-access.md`: the Staff page and D1–D6 with §3.1's consequences (keep-out
recipe order, convert-then-delete, reviewer-with-agent teardown); replace "New managers are
provisioned in two steps…" (`:418-420`, wrong order today) and the reviewer "Provisioned the same way…"
line (`:335-336`); the CLI interim from §2; the manager write-route paragraph (stale "nineteen", and it
lists only `/manager/pis…` routes) to say the pinned set is 32 including the four Staff routes; the
stale `POST /manager/pis` (`:15`); §5.6's side effects; what a reviewer delete keeps and removes. No
`CLAUDE.md` change.

## 8. Deploy

No migration. Commit; build `blackbird-app`; `up -d blackbird-app`. The worker imports none of the
touched modules (`profile_jobs`: web routers, `pi_onboarding`, the CLI and
`scripts/rederive_tenure_starts.py` only), so it is not recreated. Nothing under `src/agent/` imports
them: no agent rebuild, no effect on a live run.

## 9. Out of scope

Cleaning up accounts already made through the workaround (suspended agent rows, persona files,
profiles, Slack apps; list them read-only first if wanted). Microsoft sign-in
(`docs/specs/2026-10-13-microsoft-staff-sign-in-design.md`) is unaffected: it keeps ORCID as the only
way to create an account, and a created staff account signs in with ORCID once before linking. A CLI
command (the §2 CLI path and `role:set` remain). A filtered audit view beyond the Staff page's list.

## 10. Adversarial audits

**Audit 1 (of the admin-only draft).** All load-bearing claims confirmed with file:line evidence, no
high-severity defect. Folded in: the CLI interim and the wrong-order doc line (§2); conversion excludes
`AgentDelegate` rows and every running per-user job and requires a name when the stored one is empty;
O1 and O2 put to the owner; the stale-role job race and its fix; the lock after the ORCID fetch;
residual races listed; typed refusal codes with `create_failed`; the `/admin/`-free Add-PI hint and the
control-name/rendered-page gates; app-only deploy; the assignable-before-sign-in side effect.

**Audit 2 (delta: manager actions, Staff page, confirm step, matrix).** Authorization holds: no path for a
manager to reach a manager or admin role or act outside §5.1; confirm tampering, the role parameter,
the legacy alias and impersonation all fail; a 200 POST response breaks no gate. Folded in: refill via
the session, not the URL (defect 1); `GET /manager/staff` + `QUERY_FIELDS` + reachability + page-gate
entries (2); `populate_existing` on every locked re-read and single locked loads (3); page-unique
projection keys in `VIEW_FIELDS` only, nothing in `STAFF_KEYS`, plain dicts (4); the expectation
digests every shown fact and `changed` rolls back (5); matrix oracles read `users` (6); per-endpoint
`_STAFF` only (7); convert-then-delete and reviewer-with-agent consequences (8, 9); a "Recent changes"
list on the Staff page (10); the column-select re-read (11); explicit refusal precedence; the keep-out
recipe order. Unverified by it: identity-map behaviour under `FOR UPDATE` (documented semantics, not
reproduced); the 405 (reasoned); production reviewer accounts owning agent rows.

## 11. Work packages (plan-execution, 2026-10-13)

Disjoint write ownership; the service stub (`src/services/staff_accounts.py`) and this plan are the
shared contract. Nobody builds, tests or commits until all packages are merged.

| Pkg | Owns (create/modify only these) | Consumes |
|---|---|---|
| P1 service | `src/services/staff_accounts.py` (implement the stub, keep its public API), `tests/integration/test_staff_accounts_service.py` | §5.1, §5.4 |
| P2 guards | `src/services/profile_jobs.py`, `src/services/pi_onboarding.py`, `tests/integration/test_staff_concurrency_guards.py` | §5.5 |
| P3 routes | `src/routers/workspace/staff.py`, `src/routers/workspace/__init__.py`, `src/routers/manager.py`, `src/web/urls.py`, `src/web/navigation.py`, `tests/integration/test_staff_routes.py` | stub API, §5.2, §5.3 context keys |
| P4 page | `templates/workspace/staff.html`, `templates/workspace/pis.html`, `src/web/presentation.py` | §5.3 context keys |
| P5 existing tests | `tests/workspace_support.py`, `tests/integration/test_workspace_permissions_matrix.py`, `tests/integration/test_manager_views.py`, `tests/unit/test_reachability.py`, `tests/integration/test_rendered_page_gate.py` | §5.2 routes, §6 |
| P6 docs | `docs/operations/pis-and-access.md` | §3, §5, §7 |

Edited outside the packages during integration (needed, not in §6's list): `static/css/app.css`
(rebuilt for the new utility classes), `tests/integration/test_reviewer_role.py` (the reviewer
read-slice map gains the five `/manager/staff` routes), `tests/integration/test_workspace_contract.py`
(test name no longer carries the count), `tests/integration/test_workspace_security_and_export_contracts.py`
(`len(POST_SUFFIXES)` 28 → 32), `tests/unit/test_reachability.py` (unresolvable-link pin 55 → 58 for
three orcid.org links).

**Implementation audit (2026-10-13).** Plan audit, semantic and security reviews: authorization model
confirmed (no escalation, CSRF, XSS, IDOR, open redirect or revocation defect). Fixed: the digest now
covers employments; the role re-read takes `FOR SHARE` (non-writing callers such as retry could still
race a conversion); `adopt_agentless_pi` returns None when the account vanished during its fetch;
test teeth for the locked convert read, no persona file, admin actors, delete teardown, revoke-then-
restore keeping old cookies dead, and the convert preview's state; D7 and D8 above.
