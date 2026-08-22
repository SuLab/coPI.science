# Post-hotfix adversarial audit — application + security half

**Target:** deployed production code, branch `blackbird`, HEAD `c6cca1e`
**Hotfix under review:** `c6cca1e` — *"give an impersonating admin a way back on /manager and /admin"*
**Method:** read-only. No writes, no `pytest`, no container lifecycle commands. Evidence is
file reads, `git show`, an in-process import of the FastAPI routers (resolves no database),
and read-only `psql` SELECTs against `copi-blackbird-postgres-1`.
**Scope:** correctness and security of the deployed application. Infrastructure and
provenance are a second auditor's.

---

## Deployment provenance (one check, to establish what "deployed" means)

`copi-blackbird-app-1` (project label `copi-blackbird`, started 2026-08-17T21:21:33Z) runs
image `copi-blackbird-blackbird-app`. The four files that matter md5-match `git show HEAD:`
exactly:

| file | container | HEAD `c6cca1e` |
|---|---|---|
| `src/routers/manager.py` | `8a6dd6f5…` | `8a6dd6f5…` |
| `src/routers/admin.py` | `2cf9f83f…` | `2cf9f83f…` |
| `src/routers/dependencies.py` (`src/dependencies.py`) | `c38694b8…` | `c38694b8…` |
| `templates/base.html` | `062c8afd…` | `062c8afd…` |

`docker-compose.prod.yml` mounts only `./profiles` and `./prompts` into `blackbird-app`;
`templates/` and `src/` are baked in. So the **uncommitted worktree edits to
`templates/base.html` and `tests/integration/test_manager_views.py` are NOT live** — see F11.

---

## Verdict

**The hotfix is correct and should stay. It weakens no security boundary.** Every
server-side authorization decision still runs on the *effective* (impersonated) user; the
diff touches only the dict handed to Jinja. `/manager` is deny-by-default, GET-only, and
carries no admin functionality. Production role state is coherent.

**No Critical. One Medium, pre-existing and unchanged by the hotfix** (impersonation
bypasses the self-role-change and self-delete guards, F1). Six Low, three Informational.

The implementer's stated argument about `templates/admin/user_detail.html:64` — "the POST
handler independently re-blocks self-role-change with a 400" — **is correct**, verified at
`src/routers/admin.py:199-200`. But they missed the *inverse* half of the same inversion
(F2) and they missed that the pattern they copied from `onboarding.py` is itself broken for
a data-bearing field (F4).

I also disagree with the framing of the bug that motivated the hotfix: the admin was
**not** stranded with no way back. See F6.

---

## 1. The `current_user` swap — every use, enumerated

`admin.py` has 17 `TemplateResponse` sites; 16 go through `_template_context`, one
(`admin/discussions_export.html`, `src/routers/admin.py:503`) is a download attachment
built from a raw dict and does not extend `base.html`. `manager.py` has 6, all through
`_template_context`. Across the whole `templates/` tree `current_user` appears in exactly
**three** files:

| site | what it decides | effect of the swap |
|---|---|---|
| `templates/base.html:19-21` | PostHog `identify()` | now attributes the session to the real admin on `/admin` + `/manager`. Matches what `/profile`, `/settings`, `/agent`, `/onboarding` already did. No finding. |
| `templates/base.html:52,62` | "My Profile" / "My Agent" nav links | **changed** — see F3 |
| `templates/base.html:73,79` | "Admin" / "Manager" nav links | both are additionally gated on `not impersonation_banner`, which is now truthy, so these links now *disappear* during impersonation. Navigation still works: the admin sub-nav (`base.html:103`) and manager sub-nav (`base.html:121`) both gate on `current_user.is_admin` / `.is_staff`, which the real admin satisfies. No finding. |
| `templates/base.html:90` | display name in the header | now shows the admin's name next to a banner saying "Viewing as X". Arguably clearer. No finding. |
| `templates/admin/user_detail.html:64` | shows/hides the role-change form | **changed** — see F2 |
| `templates/onboarding/profile_review.html:79` | **the value written into a form field** | pre-existing, and it is the one real data-bearing use — see F4 |

Manager templates use `current_user` **nowhere**; every value they render comes from
`**kwargs` (`target_user`, `user_data`, `view`). The hotfix docstring's claim is accurate
for `manager.py`. The three includes shared between `/admin` and `/manager`
(`templates/admin/_run_detail_body.html`, `_discussions_threads.html`,
`_assessments_body.html`) contain no `/admin/` URL, no export link and no `llm-calls`
link — nothing admin-only bleeds through the shared partials.

**No server-side decision changed.** The handlers' `current_user` parameter is still the
`Depends(get_admin_user)` / `Depends(get_staff_user)` result, i.e. the effective user. The
diff is confined to the returned dict (`git show c6cca1e`).

---

## 2. Does the fix restore an exit in every case?

`get_current_user` (`src/dependencies.py:73-88`) honours `copi-impersonate` only when the
**session** user `is_admin`, so the matrix is:

| admin impersonates | reachable surfaces | banner? |
|---|---|---|
| **manager** | `/manager/*` (`get_staff_user` passes), `/settings`, `/agent`, `/onboarding`→`/manager/pis`. `/admin/*` 403s. | ✅ everywhere post-fix (`/manager` was the gap) |
| **PI (onboarded)** | `/profile`, `/agent`, `/settings`. `/manager` and `/admin` 403. | ✅ (already worked) |
| **another admin** | everything, incl. `/admin/*` and `/manager/*` | ✅ everywhere post-fix (`/admin` was the gap) |
| **user with no profile / incomplete onboarding** | `/profile`→302→`/onboarding` | ✅ `onboarding.py:34-44` |

`auth.py`, `invite.py` and `public.py` do not set `impersonation_banner`, but **none of them
can strand anyone**:

- `login.html` (`auth.py:124`) — unreachable with a live session; `_login_location`
  only 302s there when there is no session.
- `landing.html` (`public.py:444`) — `GET /` redirects any logged-in user to `/profile`
  before rendering. `POST /waitlist` renders it but is only reachable from the page you
  cannot reach.
- `access_pending.html` (`public.py:519`), `invite/error.html`, `invite/accept.html` — do
  render `base.html` with no `current_user` and no banner (nav degrades to a "Sign in"
  link). They are dead ends, not traps: the CoPI logo (`base.html:48`) links to `/`, which
  302s to `/profile`, which always terminates on a banner-bearing page.

**No page renders `base.html` with no banner and no way out.**

---

## 3. Did the fix weaken the impersonation boundary? No.

- `src/dependencies.py:74` — `if impersonate_id and session_user.is_admin`. Tests the
  **session** user, never the substituted one.
- `src/main.py:52-55` — the duplicate, as SQL: `select(User.is_admin).where(User.id == uid)`
  where `uid` is the session user. `is_admin` is a hybrid whose SQL form is
  `users.user_role = 'admin'` (`src/models/user.py:88-95`), so a manager fails both copies
  identically.
- `is_admin` has no setter; `is_staff` (`user.py:107-115`) is a distinct symbol and no call
  site conflates them. `grep -rn is_admin src/` returns four live readers, none spelled
  `is_staff`.
- The hotfix adds no new reader of either.

Router enumeration, done in-process rather than by status-code probing (the brief's warning
about `/manager/pis/{user_id}` is well taken):

```
src.routers.manager.router.routes  →  7 routes, ALL {'GET'}, ALL deps ['get_staff_user']
    ''  /pis  /pis/{user_id}  /assessments  /discussions  /activity  /activity/{run_id}
router-level dependencies: ['get_staff_user']
```

No non-GET route. No `llm-calls`. No `export` query parameter (the admin twin
`admin.py:487` has one; `manager.py:155-176` deliberately does not). On the admin side, a
mechanical walk of every handler's signature found exactly one route not carrying
`get_admin_user`: `POST /admin/impersonate/stop` (`admin.py:928-933`), deliberately on
`get_current_user` so an impersonated **non**-admin can still press the button the banner
renders. That is the correct gate and it is what makes the hotfix's banner useful.

---

## Findings

### Medium

**F1 — Impersonation bypasses the self-role-change and self-delete guards.**
`src/routers/admin.py:199-200` and `src/routers/admin.py:166-167`.

Both guards compare against `current_user`, which under impersonation **is the substituted
user**. So admin A impersonating admin B can `POST /admin/users/{A}/role` — the guard sees
`A != B` and stands down — and demote **their own** account to `pi`. The last-admin
backstop (`admin.py:218-227`) does not save them: with two allowed admins the count is 2 and
`<= 1` is false. The moment A's role flips, `dependencies.py:74` stops honouring A's
impersonate cookie (it tests `session_user.is_admin`), so A loses `/admin` in the same
request cycle. The same applies to `POST /admin/users/{A}/delete`, which has **no**
last-admin guard at all.

Production has exactly two admins (`Mohammad Alanjary`, `Alan Huebschen`), so this is one
form-post from a state recoverable only by `python -m src.cli role:set` from a container
shell. Not an escalation — a manager can never impersonate — but it is a real defeat of a
guard whose entire purpose is "you cannot lock yourself out mid-session".

*Fix (one line each):* compare against
`getattr(current_user, "_real_admin", None) or current_user`. That also fixes the prior
audit's **M1** (the log line at `admin.py:232-235` names the impersonated identity, not the
actor) for free.

*Pre-existing.* The hotfix neither caused nor worsened it — but see F2 for how the hotfix
now hides it.

### Low

**F2 — `templates/admin/user_detail.html:64` is now inverted in *both* directions, and the
implementer only reasoned about one.**

The implementer's argument is **verified and correct**: with A impersonating B and viewing
`/admin/users/{B}`, the form is now *shown* (`target B != current_user A`) where it used to
be hidden, but clicking it 400s at `admin.py:199-200`. Shown-but-inert. Fine.

What they missed is the mirror case. Viewing `/admin/users/{A}` — the real admin's own row —
the form is now **hidden** (`target A == current_user A`) while the POST would **succeed**,
because the server compares against B. So the UI now conceals precisely the dangerous
action from F1. Net effect is fail-closed and therefore an improvement, but the template
and the handler now disagree in both directions and neither is a security control. Fixing
F1 makes both directions agree.

**F3 — The swap re-exposes "My Profile" / "My Agent" on `/manager/*`, and "My Agent" leads
to a form that 403s.** `templates/base.html:52,62`.

Those links gate on `current_user.user_role == 'pi' or current_user.is_admin`. Pre-fix on
`/manager/*` `current_user` was the impersonated manager, so both were hidden. Post-fix
`current_user` is the real admin, so both render. Following "My Agent" reaches
`GET /agent` (`src/routers/agent_page.py:119`), which for a manager with no
`AgentRegistry` renders `agent/request.html` **with a live Request form**; the POST 403s at
`agent_page.py:413` (`get_pi_user`). This is the prior audit's "no manager control 403s on
click" invariant, now violated by one control. Cosmetic, and identical to the posture
`/settings` has had all along.

**F4 — The pattern the hotfix copied is itself broken where a template uses `current_user`
for *data*: `templates/onboarding/profile_review.html:79`.**

`value="{{ current_user.email or '' }}"` prefills the required email input from the **real
admin** while `POST /onboarding/save-profile` writes to the **effective** user
(`src/routers/onboarding.py:157-163`). Consequence: an admin impersonating a PI to help
them onboard sees their *own* email in the box, and submitting it hits the duplicate check
(`onboarding.py:158-162`) against the admin's own row and bounces to
`?error=email_taken`. It fails closed rather than corrupting data, but the onboarding form
is unusable while impersonating.

The root cause is that `onboarding.py:34-44` is the **only** `_template_context` that does
not also pass a separate `user` key — `profile.py:27`, `settings.py:43` and
`agent_page.py:106` all do, which is why their templates are unaffected. The hotfix
propagated the no-`user`-key variant into `admin.py:82-87` and `manager.py:68-73`, so the
next admin/manager template that needs the effective user has no way to name it and will
reach for `current_user`, reproducing F2. *Recommend:* add `"user": current_user` to both
new contexts and fix `onboarding.py`'s template to use it.

**F5 — Vacuous assertion in the hotfix's own test.**
`tests/integration/test_manager_views.py:269` — `assert "Adm Borrowed" in r.text`, commented
"banner names the impersonated user". `/admin/users` renders every user's name
(`templates/admin/users.html:68`), so that line passes with the banner deleted. Only the
`action="/admin/impersonate/stop"` assertion at `:268` actually pins the fix. The paired
manager test (`:253`) *is* load-bearing, because `manager/pis.html` is role-filtered to PIs
(`manager.py:97`) and a manager never appears in it — a good asymmetry the author did not
notice.

**F6 — The premise of the hotfix commit message is overstated: there was always a way
back.** `templates/base.html:60-63` renders the **Settings** link for every logged-in user
with no role gate, and `src/routers/settings.py:44` has set `impersonation_banner` since
long before this branch — so from any `/manager/*` page one click reached a Stop
Impersonating button. Independently, `templates/base.html:91-95` renders **Sign out**
unconditionally and `POST /logout` deletes the `copi-impersonate` cookie
(`src/routers/auth.py:328`). The real defect was a missing *in-place* exit and a nav that
misrepresented the session — bad, worth fixing, but not an unrecoverable trap. The commit
message ("no visible route back to their own account") and the brief's "stranded with no way
back" both overstate it.

**F7 — `POST /profile/save` and `GET /profile/edit` remain on `get_current_user`; assessed
as *not* an escalation.** `src/routers/profile.py:99-113` and `:76-96`. A manager who
navigates directly (the nav hides both) can create/overwrite their own
`ResearcherProfile` and edit `name`/`email`/`institution`/`department`. The chain to a lab
bot is genuinely closed at three independent points:
`export_profile_to_markdown` returns `None` with no `AgentRegistry`
(`src/services/profile_export.py:24`), so no `profiles/public/*.md` is written and the
simulation never sees it; `profile_save` never writes `onboarding_complete`; and
`POST /agent/request` is on `get_pi_user` (`agent_page.py:413`), which 403s a manager
*regardless* of `onboarding_complete`/profile — the handler's own docstring calls the body
check a readiness test, not an authorization one, and that is accurate. Residual impact is
a DB-only PI-shaped row on the manager's own record. Ship-able; gate it for tidiness.

**F8 — Live instance of the D9 promotion residue.** The sole production manager
(`Maisha Rahman`, ORCID `0000-0003-1795-0814`) carries `onboarding_complete = true` and a
`researcher_profiles` row from her PI-era onboarding; `admin_set_user_role`
(`src/routers/admin.py:231`) changes the role and cleans up nothing. She has **no**
`agents` row and `/manager/pis` filters her out (`manager.py:97`), so there is no exposure
today — but it is the exact precondition F7 would need if `get_pi_user` were ever relaxed.

**F9 — A GET that writes, reachable by navigation, and inconsistent with its own POST
twin.** `src/routers/onboarding.py:95-108` enqueues a `generate_profile` `Job` from
`GET /onboarding`, while `POST /onboarding/retry` is documented as POST-only *specifically*
because enqueuing that job over GET was a CSRF target (`onboarding.py:250-254`). Under
impersonation an admin merely *viewing* `/onboarding` as another user commits a row on that
user's behalf. Currently latent: all 62 production PIs already have profiles, so the
`profile is None` condition never holds. `SameSite=lax` blocks subresource forgeries but not
a top-level link click.

### Informational

**F10 — `users.is_admin` (the physical column) will silently drift.** It is unmapped
(`src/models/user.py:78-95`), so the ORM never writes it; the only writer left in the tree
is `0028`'s backfill. Verified today: **0 of 65 rows disagree** with `user_role`. But the
next `/admin/users/{id}/role` post makes them diverge, and anyone hand-writing a psql or BI
query against `is_admin` will get a stale answer. `0028`'s downgrade is safe — it
re-derives the column (`alembic/versions/0028_add_user_role.py:47`) before dropping the
enum. Worth a comment on the column, or just shipping `0029`.

**F11 — In-flight uncommitted work exists and is NOT deployed.** `templates/base.html` and
`tests/integration/test_manager_views.py` are dirty in the worktree; the edit introduces
`{% set effective_user = impersonation_banner or current_user %}` and re-gates the Manager
nav link on the *effective* user (plus two new tests at `:272` and `:293`). It is not live —
templates are baked into the image and the deployed `base.html` md5-matches `HEAD`. On its
merits the change looks right (it would hide the Manager link while impersonating a PI,
where `/manager` 403s, and show it while impersonating a manager, where it works), but it is
untested-as-deployed and it does not address F3, which is the same class of bug on the
"My Profile"/"My Agent" links two lines above.

**F12 — Production data is coherent.** `alembic_version = 0028`, as expected (`0029` not
applied). 65 users: 2 `admin`, 1 `manager`, 62 `pi`; all `access_status = 'allowed'`. Zero
NULL roles, zero out-of-vocabulary roles, zero `is_admin` ↔ `user_role` disagreements. The
`ck_users_user_role` CHECK constraint is present on the live table. Zero orphaned
`researcher_profiles`, zero orphaned `agents`. The 62 PIs are seeded stubs
(`onboarding_complete = false`, `email IS NULL`, one profile and one agent each), which is
the expected pre-claim shape — nothing about the deploy corrupted user or profile data.

---

## Where I disagree with a prior audit or the brief

1. **The brief and `c6cca1e`'s commit message: "stranded with no way back."** False as
   stated — Settings (banner-bearing since before this branch) and Sign out (which clears
   the impersonate cookie) were both always in the nav. F6.
2. **`final-security-audit.md` M2** describes the same omission and says the missing flag
   means "`base.html:79` still renders the Manager nav link, so the session looks entirely
   normal." That is right about the mechanism, but the report treats it purely as a
   *visibility* defect. It did not spot that fixing it by swapping `current_user` re-enables
   the *other* two nav gates (`base.html:52,62`) and un-hides a control that 403s — F3.
3. **`final-security-audit.md` I1** listed four PI-write endpoints and called
   `POST /agent/request` "gated only on `onboarding_complete` and profile". That was true at
   `8ee827d` and is now fixed at `agent_page.py:413`; the chain is closed at the terminal
   step irrespective of the earlier ones, which is why the still-ungated
   `POST /profile/save` (which I1 never listed) does not reopen it. F7.
4. **The hotfix's own docstring in `admin.py:70-79`** says the swap "mirrors the same
   pattern in onboarding.py". It does — including that router's latent bug. F4.
