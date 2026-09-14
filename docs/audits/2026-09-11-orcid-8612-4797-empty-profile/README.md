# RCA: ORCID 0000-0002-8612-4797 — empty profile, name shows the ORCID iD

Date: 2026-09-11. Every fact below was measured against the production database
(`copi-blackbird-postgres-1`), the running app container's logs, the ORCID public
API, and the code on branch `feat/assessment-ui-review-score-login-proposal-limit`.
Nothing here is recalled from memory.

## Verdict

**Not a bug in the pipeline. The account is Virginia Burger (Blackbird staff,
XtalPi Inc), who signed in with ORCID; her ORCID record publishes no name, so
the code's documented fallback wrote the iD itself into `users.name`; and she
never received a profile because pending users are never given a
`generate_profile` job and her access request was never approved.** She was
later made a `manager` — a role that has no research profile by design.

Two real defects surfaced along the way (below): the name can never self-heal,
and approving her now would enqueue a profile job for a manager.

## Evidence

| Fact | Source |
|---|---|
| `users` row: `name='0000-0002-8612-4797'`, `institution='XtalPi, Inc'`, `email='virginia@blackbirdlab.org'`, `access_status='pending'`, `last_login_at=NULL`, `onboarding_complete=f`, `user_role='manager'`, `created_at=2026-09-04 16:55:28Z`, `updated_at=2026-09-11 12:22:32Z` | prod `users` |
| No `researcher_profiles`, `publications`, `jobs`, `agents`, `profile_revisions`, `app_settings` rows for this user | prod, all queried |
| Not on `access_allowlist`; no `admin_audit_events` row | prod |
| `email_notification_preferences` row created `2026-09-04 16:57:01Z` (93 s after the user). That row is created only by the status-overview digest sweep, which selects `users WHERE email IS NOT NULL` (`src/services/email_notifications.py:671-676`) — so the email was already on the row within 93 s of creation | prod + code |
| ORCID `/person`: `name: null`, `emails: []`, `biography: null`; `researcher-urls`: `Personal Website → http://www.virginiaburger.org`; 11 works (disordered-protein computation, 2014–2017) | `pub.orcid.org/v3.0/…/record` |
| ORCID employments: XtalPi Inc 2017–present; MIT postdoc 2014–2017. **No Johns Hopkins employment.** | `pub.orcid.org/v3.0/…/employments` |
| App log 2026-09-11 12:22:32: `Admin Alan Huebschen changed role of 0000-0002-8612-4797 (85e97ec9…) from pi to manager` | `docker logs copi-blackbird-app-1` |
| Logs from 2026-09-04 are gone: the app container was recreated 2026-09-11 03:42Z (deploy), and `json-file` logs die with the container | `docker inspect` |

## Causal chain

1. **Creation path = ORCID sign-in, not manager Add-PI or CLI.** Discriminators:
   `last_login_at` is NULL (auth sets it only for `allowed` users,
   `src/routers/auth.py:269-270`); there is no `Job` and no `AgentRegistry` row
   (`find_or_create_pi_by_orcid` in `src/services/pi_onboarding.py` commits both
   atomically with the user, so their absence rules that path out); the CLI
   `seed-profile` path would have enqueued a job unless `--no-pipeline` and
   could not have produced a `blackbirdlab.org` email. The email is explained
   by `POST /access-pending/email` (`src/routers/public.py:451`), the form a
   pending user fills in on the access-pending page — consistent with the
   93-second gap.
2. **Name = ORCID iD.** `fetch_orcid_profile` builds
   `f"{given} {family}".strip() or orcid_id` (`src/services/orcid.py:32`). The
   ORCID record's `person.name` is `null` — the holder has set name visibility
   to private (ORCID lets you). The OAuth token's `name` claim was also empty
   for the same reason, so `profile_data.get("name") or orcid_name`
   (`auth.py:217`) had nothing better. Institution `XtalPi, Inc` came from the
   same call's employment parse — which is also how we know the identity is
   real and non-JHU.
3. **Empty profile.** `auth.py:225` enqueues `generate_profile` **only if
   `access_status == "allowed"`**; she was `pending` and stayed pending — no
   admin ever hit `POST /access-requests/{id}/approve`. Nothing else creates a
   profile. Her role was then set to `manager` (2026-09-11 12:22Z), and
   managers have no `ResearcherProfile` by design (CLAUDE.md, Account Types).
4. **She still cannot log in.** Role-set does not change `access_status`;
   `pending` users get no session (`auth.py:274-283`). If the intent is a
   working manager account, approval at `/admin/access-requests` is still
   required.

## Alternative hypotheses considered and refuted

- *Profile job ran and failed / was deleted*: no `jobs` row exists; jobs cascade only on user delete, and the user exists. Refuted.
- *Profile created then purged by `delete_user_account`*: that suspends an agent and deletes the user; both absent. Refuted.
- *ORCID API outage during sign-in produced a stub*: the outage fallback is `{"orcid", "name": orcid_name}` with **no institution**; the row has `institution='XtalPi, Inc'`, so the fetch succeeded. Refuted.
- *Manager Add-PI with a JHU-less ORCID*: would have created `Job` + `AgentRegistry` + role `pi`, and derived no tenure (correct — no Hopkins employment). Absent. Refuted.
- *Wrong ORCID typed by an operator*: the person owns `virginiaburger.org` and works at XtalPi; the email is `virginia@blackbirdlab.org`. Same person; iD is correct.

## Defects found (not fixed — report only)

- **D1 — Name never self-heals.** The existing-user branch updates the name only `if not user.name` (`auth.py:239`). Because the fallback stores the iD (a non-empty string), a later login after the holder makes her name public will never overwrite it. Suggested fix: treat `name == orcid` as empty in that branch, or store `None` and render the iD at display time.
- **D2 — Approving a manager enqueues a PI profile job.** `admin_approve_access` (`src/routers/admin.py:1362-1391`) adds `generate_profile` for any approved user lacking a profile, regardless of `user_role`. Approving this account as-is would run the corpus pipeline against a non-JHU ORCID (and fail the job ×3 → dead, per the corpus-stage failure policy). Suggested fix: gate the enqueue on `user_role == 'pi'`.
- **D3 — No audit row for role changes.** `admin_set_user_role` only logs; `admin_audit_events` has 3 rows total. The log survived here only because the change happened after the 03:42Z container recreate.

## What the operator should do

1. If Virginia Burger is meant to be a manager: approve at `/admin/access-requests` **after** D2 is fixed (or accept one dead job), then set `users.name` by hand (`UPDATE users SET name='Virginia Burger' WHERE orcid='0000-0002-8612-4797';`) — there is no UI to edit a manager's name (`/manager/pis/{id}/profile` targets PIs).
2. Nothing to repair in the profile pipeline for this account.
