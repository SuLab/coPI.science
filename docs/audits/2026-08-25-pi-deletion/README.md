# PI-deletion audit — findings and remediation decisions (2026-08-25)

An adversarial review of the process used to delete PIs. Two entry points
exist — `POST /profile/delete-account` (`src/routers/profile.py:172`) and
`POST /admin/users/{user_id}/delete` (`src/routers/admin.py:180`) — and both
are a bare `await db.delete(user)`. There is no service-level teardown, no CLI
deletion path, and no cleanup hook anywhere in `src/`. Everything that happens
after the row delete is decided by FK topology, which was audited
migration-by-migration (0001→0037) against a production dump and found
internally sound: all 22 user-referencing FKs carry explicit `ondelete` rules,
zero model/migration discrepancies, no RESTRICT, no NOT NULL + SET NULL pair,
and the one CHECK-violation class (`private_channel_members`) was fixed in
0036. **A user delete cannot raise at the DB level. Every defect below lives
above the DB.**

## Findings

### F1 — deleting a PI does not stop their agent (root cause of the cluster)
`agents.user_id` is SET NULL and `status` is untouched. The roster sync loads
by `status == 'active'` alone (`src/agent/simulation.py:7157-7165`; same
criterion at startup, `src/agent/main.py:164-176`) and the runtime reads
nothing from `users`/`researcher_profiles` — persona comes from
`profiles/public/{agent_id}.md` on disk plus the denormalized
`pi_name`/`bot_name` on the surviving agent row. The bot keeps taking turns
under the deleted person's name, and the cross-lab directory keeps injecting
their publications into other agents' prompts (`simulation.py:5726-5757`).
The activation gate itself names this state invalid — `user_id IS NULL` ⇒
"no profile to stand behind this lab" (`src/services/agent_activation.py:32`)
— but only runs at approval time.

### F2 — the Slack bot token is never revoked
No revoke code exists anywhere in the repo (`auth.revoke` is called nowhere;
the only `apps.manifest.delete` is `scripts/slack_test_teardown.py`, a manual
test-workspace script that deletes probe apps, not tokens). The valid `xoxb-`
token stays on the unowned agent row and keeps being handed to a live
`AgentSlackClient`.

### F3 — disk artifacts survive and keep feeding the agent
`profiles/public/{agent_id}.md` (written by `src/services/profile_export.py`)
and `profiles/memory/{agent_id}/**` (written by
`src/agent/agent.py:563-603`) are deleted by nothing.

### F4 — the deletion promise is false
`templates/profile/delete_account.html` promises permanent deletion of the
research profile, but full-text snapshots of every revision survive in
`profile_revisions` (keyed to `agents.id`, which survives), the disk export
survives (F3), DM content survives in `pi_dm_messages` (its `pi_user_id`
string `local:<users.id>` is the one place a users.id value outlives the row),
and the page names "supplementary text submissions", a table that does not
exist. The admin Danger Zone copy has the same gaps.

### F5 — orphaned KV rows
`app_settings` key `jhu_tenure_start:{user_id}` (`src/services/jhu_rules.py:36`)
has upsert functions but no delete, and no caller removes it.

### F6 — deletion is weaker than denial as a removal tool
`access_allowlist` is keyed by ORCID with no user FK; a deleted user
re-registers instantly as `allowed` with a fresh profile job
(`src/routers/auth.py:198-249`). Worse, `auth.py:248` promotes any
non-`allowed` status — including an explicit admin **denial** — back to
`allowed` at next login for allowlisted ORCIDs.

### F7 — the last admin can self-delete
`POST /admin/users/{id}/role` defends "at least one loginable admin"
(`admin.py:244-254`) and the admin delete blocks self-deletion
(`admin.py:192`), but `POST /profile/delete-account` has no role awareness:
the last admin typing "delete" locks every human out of `/admin` (recovery is
CLI-only).

### F8 — impersonation is not read-only for deletion
`get_current_user` returns the impersonated user; the delete route takes plain
`get_current_user`, so an admin impersonating a PI can delete the PI's account
(and is themselves logged out by `request.session.clear()`). Flagged in the
2026-08-22 correctness audit (§2.13) but never remediated.

### F9 — delegate surfaces 500 or corrupt on an orphaned agent
`AgentDelegate` rows hang off `agents.id`, so other users' delegations survive
and `get_agent_with_access` (`src/dependencies.py:132-165`) still admits them.
Then: `scalar_one()` on the missing PI user 500s
(`src/routers/agent_page.py:874`, `:905`, `:964`); `save_public_profile`
builds `ResearcherProfile(user_id=None)` into a NOT NULL column (`:941`); and
both review submission and reopen-with-guidance build
`ProposalReview(user_id=None)` into a NOT NULL CASCADE column (`:475`, `:616`).

### F10 — worker races on mid-flight deletion
`src/worker/main.py`: the job re-fetch `scalar_one()` (`:85`) sits outside the
`try`, so a job row cascade-deleted between claim and re-fetch raises
`NoResultFound` into the loop's catch-all; the `except` block (`:100-111`)
never rolls back, so after a pipeline FK failure its own
`job.last_error`/`commit()` raises `StaleDataError` or
`InFailedSQLTransactionError`. The pipeline's markdown export
(`profile_pipeline.py:565-567`) is a plain filesystem write outside any
transaction, so a freshly generated profile can land on disk *after* the
account is deleted. Also: the pipeline's early autoflush holds the jobs-row
lock, so the deleting HTTP request blocks until the pipeline's LLM calls
finish.

### F11 — coverage
No test exercises `POST /admin/users/{id}/delete` at all; the self-delete
tests pin only the confirm word and two cascades. Nothing pins post-delete
system state.

### Verified sound (do not "fix")
The DB cascade graph (above); the Origin guard on both POSTs; stale session
cookies of a deleted user bounce cleanly to `/login`; a stale
`copi-impersonate` cookie naming a deleted user is ignored.
`cohort_audit_events.actor_email` denormalization is a documented, deliberate
audit-trail design — retained on purpose.

## Remediation decisions

- **D1 — one teardown service.** All deletion policy moves into
  `src/services/user_deletion.py::delete_user_account`, called by both routes.
  Nothing else deletes users.
- **D2 — the linked agent is suspended, not deleted.** `status='suspended'` is
  the admin-only parked state; `set_agent_mute_state` refuses non-active/
  inactive agents, so a manager unmute cannot resurrect it. The row (and its
  `agent_id` slug, `pi_name`) is retained as the operational record behind old
  messages/assessments — disclosed in the UI (D6).
- **D3 — revoke the bot token, keep the app.** `auth.revoke` via a new
  `slack_web.revoke_token` (the module is the mandated Slack boundary —
  `tests/unit/test_slack_boundary.py` pins slack_sdk to exactly two modules).
  Post-commit, best-effort; the column is cleared only after a successful (or
  already-dead) revocation. Workspace app uninstall stays a manual operation.
- **D4 — purge what the promise covers; retain the shared record; disclose
  both.** Purged: profile + revisions (by `agent_registry_id`), disk export +
  memory, per-user tenure key, `pi_dm_messages` for the linked agent and for
  `local:<user_id>`. Retained: `agent_messages`, `llm_call_logs`, assessments,
  Slack-side history (external), `actor_email` audit copies. Both confirmation
  pages state exactly this.
- **D5 — allowlist.** Self-deletion leaves the allowlist entry (the user may
  return by choice). The admin delete form gains a default-checked "also
  remove from the access allowlist" checkbox. Separately, `auth.py` promotes
  only `pending` → `allowed`; `denied` stays denied.
- **D6 — route guards.** Self-delete refuses impersonated sessions (403) and
  the last loginable admin (redirect + message). The admin route needs no
  last-admin guard: the acting admin survives any deletion it can perform
  (self-deletion is already blocked).
- **D7 — legacy-orphan defense in depth.** The roster criterion (one shared
  helper, both call sites) excludes `pi_lab` agents with `user_id IS NULL`;
  the delegate-facing routes degrade to redirects/409s instead of 500s.
  Deploy preflight: `SELECT agent_id FROM agents WHERE role='pi_lab' AND
  user_id IS NULL AND status='active';` — relink survivors or accept eviction.
- **D8 — worker hardening.** Tolerate a vanished job row at re-fetch; rollback
  then re-fetch in the except path; re-check user existence immediately before
  the markdown export.
- **D9 — no schema migration.** Every fix is code/templates/docs; nothing in
  this plan touches DDL.

Implementation plan: `docs/plans/2026-08-25-pi-deletion-teardown-plan.md`.
