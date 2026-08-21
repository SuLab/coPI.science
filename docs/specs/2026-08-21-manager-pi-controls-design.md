# Manager PI controls, assessment→profile links, assessments-summary channel

**Status:** approved design, not yet planned/implemented.

**Context:** three requested features, bundled because they share the PI/assessment
data model even though they touch different surfaces:

1. Add, edit, and mute PI profiles from the manager view.
2. Link from an assessment row to the associated PI's profile.
3. A new Slack channel where BlackbirdBot posts a one-line summary of every
   concluded interview (pass or fail), linking back to the interview thread.

All three were scoped through adversarial review against the current codebase,
not assumption — see §0 for the invariants each feature bumps into, and §9 for
what was deliberately rejected.

**Concurrent work — read before implementing:** `docs/plans/2026-08-21-perf-memory-race-remediation.md`
is in flight (unstarted as of this writing — alembic head is still `0032`,
none of its markers exist in the tree yet) and touches two files this design
also touches: `src/routers/profile.py` (Task 13 rewrites the exact
`profile_version` read-modify-write at line 159) and `src/agent/slack_client.py`
(Task 2 edits the async-wrappers block around lines 1167-1186). §8 covers
sequencing.

---

## §0 — Invariants this design deliberately changes, and why

| Invariant | Where documented | This design's stance |
|---|---|---|
| `/manager` has zero non-GET routes (D12) | `docs/specs/2026-08-17-user-account-types-design.md:139`, enforced by `tests/integration/test_manager_views.py:53-57` | Amended, not abolished: three named write routes, allowlisted by an updated version of the same test (§3). |
| The hub's assessment is "a courtesy note, not a public verdict," never posted where PIs/other labs see it | commit `f7a9f68`; CLAUDE.md's "BlackbirdBot" section | Deliberately reversed for a headline-only summary, in a channel kept out of the bot ecosystem's own read/discovery paths (§6). Full assessment detail (rationale, red flags, gating, raw verdict) stays exactly as hidden as it is today. |
| There is no "mute" concept distinct from `active`/`inactive`/`suspended`/`pending` | `src/models/agent_registry.py:28-30`, `src/routers/admin.py:68` | Not adding a new status value. Mute is a purpose-built UI/route over the existing `inactive` state, plus new attribution columns (§2). |

These were confirmed with the user directly (manager write access: reverse D12
for named routes; summary channel: public, headline-only) rather than assumed.

---

## §1 — Decisions ledger

| # | Decision | Rationale |
|---|---|---|
| D1 | Reverse D12 for exactly three named routes, not a general capability system | A capability/permission framework for three roles was already rejected as YAGNI in the account-types design (§9 there); naming the three exceptions keeps deny-by-default mechanically checkable |
| D2 | Mute → `AgentRegistry.status = "inactive"`; unmute → `"active"`. No new status value | `inactive` is already documented as "parked: excluded from sim runs, reversible" with the owner keeping read/rate access — exactly mute's semantics. `suspended` is rejection and locks the owner out; wrong axis |
| D3 | Add `muted_at`/`muted_by` (nullable), reflecting current state only, not a history log | Closes an accountability gap the account-types design explicitly flagged as unbuilt even for manager *reads*; a full audit-log table is out of scope (§10) |
| D4 | Mute/unmute only when current status is `active`/`inactive`; no-op/error otherwise | `pending`/`suspended` are admin-only concerns (approval, rejection) and stay that way |
| D5 | "Add a PI" wraps the existing ORCID pipeline (fetch → create `User` → enqueue `generate_profile` Job); no manual profile-field form | Every profile in the system today is ORCID/publication-derived; a hand-typed profile would be a new capability nothing else in the app has |
| D6 | Add-PI rejects if the ORCID already belongs to any user (any role), rather than silently reusing it | Unlike impersonate's forgiving reuse, this is an explicit creation action — silent reuse would be surprising |
| D7 | Extract the ORCID fetch→create→enqueue logic (currently duplicated in `cli.py:_seed_one_orcid` and `admin.py`'s impersonate handler) into one shared function; refactor impersonate to call it | A third inline copy makes an existing duplication problem worse; consolidating means the manager route and future admin work stay in sync |
| D8 | Extract `profile_save`'s field-application logic into a function parameterized by target vs. acting user, shared by the PI's own save route and the new manager edit route | Avoids a fifth call site of `profile.py:159`'s RMW pattern (four are already tracked by the in-flight remediation plan's Task 13); manager attribution uses `create_revision`'s existing `changed_by_user_id` param, unchanged |
| D9 | Assessment→PI link resolved via `AgentRegistry.agent_id == subject_agent_id → AgentRegistry.user_id`, batched per page load | The only path that exists — there is no FK. Must tolerate null `subject_agent_id`, a stale/decommissioned slug, and an unlinked agent (`user_id is None`) |
| D10 | Link markup lives in per-surface wrapper templates, not the shared `_assessments_body.html`/`_assessment_detail_body.html` partials | Those partials are contractually free of absolute `/admin`/`/manager` URLs (documented header comment, enforced by `tests/unit/test_reachability.py`'s literal-path matching) |
| D11 | Assessments-summary channel is human-joinable/readable (public in the Slack sense) but is **not** added to `SEEDED_CHANNELS` or any per-agent subscription set | Confirmed with the user as "public" meaning workspace-visible, not "wired into every PI bot's topical channel rotation" — the latter would make PI-lab bots scan it and potentially reply to the hub's own headline posts |
| D12 | Summary post content is headline-only: PI/lab name, project name, recommendation, band/score, permalink. No rationale, red flags, gating detail, or raw_verdict | Confirmed with the user. Today's manager *read-only* detail view already hides this level of detail (`admin_view=False`) from staff; a public post must not expose more than that |
| D13 | Summary post fires once, synchronously, right after `_persist_assessment` commits inside `_capture_hub_assessment` — not at `_close_thread` | `_close_thread` fires immediately for the fail (⏸️) path but a full turn late for a pass (which closes via the `max_thread_messages` timeout on the *next* turn); hooking the capture point is the only place both cases are handled promptly and symmetrically |
| D14 | Only a persisted `OpportunityAssessment` row triggers a post. An `AssessmentDrop` row (e.g. the `empty_reply`/abandoned-interview case) never does | "Positive or negative result" implies a verdict exists; an abandoned interview has none |
| D15 | Accept a rare double-post when `_retire_superseded_verdict` replaces a provisional row with a final one; do not build message-edit/delete logic to prevent it | The DB itself tolerates this by deleting the stale row rather than preventing it from ever being written; matching that tolerance in Slack avoids real complexity for a rare, already-mitigated-by-gating edge case |
| D16 | Slack failures in the summary-post step (post or permalink) are caught and logged, never raised into the calling turn | Matches the existing "`_persist_assessment` ... a persistence failure never crashes the reply" pattern |
| D17 | Permalink via `chat.getPermalink`, added as a new method on `AgentSlackClient` through the existing `_api` chokepoint | No stored Slack team subdomain exists anywhere to hand-construct a URL; this is the only viable API |
| D18 | Spike `chat.getPermalink` against the real workspace/token before building the rest of Feature 3 | Unverified whether current bot OAuth scopes cover it. If not, the fix is a new scope requiring **every** agent's Slack app to be reinstalled (per the existing scope-comment precedent in `slack_provisioning.py`) — an operational undertaking, not a code change, and it should gate feasibility before more code is written around it |

---

## §2 — Data model

### `agents` table (model: `AgentRegistry`, `src/models/agent_registry.py`)

Add two nullable columns:

```python
muted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
muted_by: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
)
```

Semantics: both set when a mute action sets `status = "inactive"`; both cleared
(set to `NULL`) when an unmute action sets `status = "active"`. This is
current-state attribution, not a history log (D3) — if `status` is flipped by
some other path (e.g. today's generic admin edit form), these columns are left
as whatever they last were; they answer "is this currently mute-flagged and by
whom," not "show me every status change ever."

**Migration sequencing (real, not hypothetical):** current alembic head is
`0032_add_llm_call_stats`. The in-flight remediation plan's Task 10 will add
`0033_badge_and_fk_indexes` (additive, no deploy-order constraint per that
plan). This design's migration must be created with `down_revision` set to
whatever `alembic heads` actually reports at implementation time — **run
`alembic heads` and confirm before writing the revision file**; do not assume
`0033` already exists.

**Deploy order for this migration — migrate before serving new code**, same
class of risk as `0028`/`0030`: once `AgentRegistry` maps `muted_at`/`muted_by`,
every existing `select(AgentRegistry)` in the app — roster sync included —
names them in its column list, and against a pre-migration database every one
raises `UndefinedColumn`. This migration is additive-only (nullable columns,
no backfill needed), so old code against the new schema is safe; only the
reverse direction is not.

---

## §3 — Manager write routes

All three live in `src/routers/manager.py`, under the existing router-level
`Depends(get_staff_user)` — unchanged gate, so both `admin` and `manager`
reach them (admin already has superset capability via `/admin`; this doesn't
grant anything new to admin).

| Method | Path | Behavior |
|---|---|---|
| POST | `/manager/pis` | Body: `orcid`. Calls `find_or_create_pi_by_orcid` (§4). On success, redirect to `/manager/pis/{new_user_id}`. On "ORCID already exists" or ORCID-fetch failure, redirect back to `/manager/pis` with an error query param, rendered the same way `/profile/edit?error=...` already does it. |
| POST | `/manager/pis/{user_id}/profile` | Same form fields as `/profile/save` (name, email, institution, department, research_summary, techniques, experimental_models, disease_areas, key_targets, keywords). 404s if `user.user_role != 'pi'` (mirrors `manager_pi_detail`'s existing guard). Calls `apply_profile_edits` (§4) with `changed_by_user_id = current_user.id`. |
| POST | `/manager/pis/{user_id}/mute` | 404s under the same PI-only guard. Resolves `user.agent` (the `AgentRegistry` row via the existing relationship). If none, or `status` not in `("active", "inactive")`, redirect with an error — no-op, not a 500. Otherwise calls `set_agent_mute_state(muted=True)`. |
| POST | `/manager/pis/{user_id}/unmute` | Same shape, `set_agent_mute_state(muted=False)`. |

**Test allowlist update:** `tests/integration/test_manager_views.py`'s
`test_manager_router_exposes_no_mutating_routes` (D12's check) gets rewritten
to assert the exact route set `{"GET", "POST"}` *and* that every POST path is
one of these three (plus their trailing verb variants) — enumerated from the
live router the same way the file's existing `_manager_get_paths` helper
enumerates GETs, so a future accidental fourth write route still fails loudly
instead of silently expanding the allowlist.

**Template changes:** `templates/manager/pis.html` gets an "Add PI" form
(single ORCID input) above the directory table. `templates/manager/pi_detail.html`
gets an edit form mirroring `templates/profile/edit.html`'s fields, and a
Mute/Unmute button conditioned on `user.agent` existing and its `status` being
`active` or `inactive` (hidden/disabled otherwise, with a short explanation —
e.g. "Agent is pending approval" — rather than a dead button).

---

## §4 — Service extractions

Three small, single-purpose functions, each replacing or de-duplicating logic
that already exists inline elsewhere:

**`src/services/pi_onboarding.py`** (new module — `orcid.py` is purely the
external API client and shouldn't grow DB-write responsibilities):

```python
async def find_or_create_pi_by_orcid(db: AsyncSession, orcid: str) -> User:
    """Fetch the ORCID profile, create a User(user_role='pi') and enqueue a
    generate_profile Job. Raises ValueError if a User with this ORCID
    already exists (any role) — this is an explicit creation action, not a
    lookup-or-create; D6."""
```

Ports the body of `cli.py:_seed_one_orcid` (minus its CLI-specific
`_get_db()` engine plumbing — the route supplies a request-scoped session).
`admin.py`'s `impersonate_user` handler is refactored to call this for its
"doesn't exist yet" branch, preserving impersonate's own forgiving-reuse
wrapper (check-if-exists stays in the caller; only the create path moves).

**`src/services/profile_edit.py`** (new module):

```python
async def apply_profile_edits(
    db: AsyncSession, *, target_user: User, changed_by_user_id: uuid.UUID,
    name: str, email: str, institution: str, department: str,
    research_summary: str, techniques: str, experimental_models: str,
    disease_areas: str, key_targets: str, keywords: str,
) -> None:
    """The field-validation, ResearcherProfile upsert, markdown export, and
    revision-recording logic currently inlined in profile.py's profile_save
    (lines ~116-193), parameterized so target_user and changed_by_user_id
    can differ (self-edit vs. manager-edit)."""
```

`POST /profile/save` becomes a thin wrapper calling this with
`target_user = changed_by_user_id = current_user`; behavior for the existing
route is unchanged. **Sequencing note (real risk, not hypothetical):** this
function's body includes the exact `profile.profile_version = (profile.profile_version or 0) + 1`
line the remediation plan's Task 13 is independently rewriting to be atomic.
Land Task 13 first and extract on top of its atomic pattern, or expect a
head-on merge conflict on `profile.py:159` if both are developed in parallel.

**`src/services/agent_mute.py`** (new module, or add to wherever agent-status
helpers already live if one exists — check at implementation time):

```python
async def set_agent_mute_state(
    db: AsyncSession, *, agent: AgentRegistry, muted: bool, actor_user_id: uuid.UUID,
) -> None:
    """Guards status in {"active", "inactive"}; sets status + muted_at/muted_by
    (or clears the latter two) accordingly. No-ops (returns False) if the
    agent isn't currently in a mutable state — caller turns that into a
    flash message, not a 500."""
```

---

## §5 — Assessment → PI profile link

Add a batched lookup — `{subject_agent_id: user_id}` built from one query
(`select(AgentRegistry.agent_id, AgentRegistry.user_id).where(AgentRegistry.agent_id.in_(...))`)
— to `list_assessments` (`src/services/directory.py`) and
`build_assessment_detail` (`src/services/assessment_detail.py`), exposed to
the template context alongside the existing assessment rows.

In each of the four wrapper templates (`admin/assessments.html`,
`admin/assessment_detail.html`, `manager/assessments.html`,
`manager/assessment_detail.html`), define a small macro, e.g.:

```jinja
{% macro pi_link(subject_agent_id, pi_user_ids) %}
  {% if subject_agent_id and pi_user_ids.get(subject_agent_id) %}
    <a href="/admin/users/{{ pi_user_ids[subject_agent_id] }}">{{ subject_agent_id }}</a>
  {% else %}
    {{ subject_agent_id or "—" }}
  {% endif %}
{% endmacro %}
```

(manager's macro points at `/manager/pis/{id}` instead), and pass it into the
shared `_assessments_body.html`/`_assessment_detail_body.html` include so the
existing "Lab" cell becomes a link when resolvable, plain text otherwise —
per D10, the shared partials themselves stay free of absolute URLs.

---

## §6 — Assessments-summary Slack channel

**Channel identity:** a new constant, `ASSESSMENTS_SUMMARY_CHANNEL = "assessments-summary"`
(name confirmable/changeable at implementation time), defined **separately**
from `SEEDED_CHANNELS` in `src/agent/channels.py` — not added to that list, so
it never enters Phase-1 topical channel-discovery/matching for PI-lab agents
(D11). Created idempotently (create-or-adopt-existing, same pattern as
`_ensure_seeded_channels`) at hub startup, joined only by the hub's own
`AgentSlackClient`.

**Trigger:** inside `_capture_hub_assessment` (`src/agent/simulation.py:2756`),
immediately after `_persist_assessment` (`:2958`) successfully commits the row
(D13). A new private helper, e.g. `_post_assessment_summary(self, assessment_row, thread, slack_ts)`,
resolves:
- PI/lab display name — via `self.agents.get(thread.other_agent_id)` (in-process, cheap) or an `AgentRegistry` lookup as fallback.
- The permalink — `await hub_client.aget_permalink(channel_id, slack_ts)` (new method, §below), where `channel_id = self._channel_id_map.get(thread.channel)`.
- Formats a single line: PI/lab name, `company_or_project` (or a placeholder if absent), `recommendation`, `band`/`weighted_score`, and the permalink (or a "(link unavailable)" note if the permalink call failed — D16's graceful degradation, not a dropped post).
- Posts via the hub's own `client.apost_message(ASSESSMENTS_SUMMARY_CHANNEL, text)` — a new top-level post, no `thread_ts`.

The whole helper is wrapped in `try/except Exception`, logged on failure,
never re-raised (D16) — mirrors `_persist_assessment`'s own failure-isolation
comment.

**New `AgentSlackClient` method** (`src/agent/slack_client.py`, near the
existing async-wrappers block — **coordinate with the remediation plan's
Task 2**, which edits that exact region):

```python
def get_permalink(self, channel_id: str, message_ts: str) -> str | None:
    """chat.getPermalink through the existing _api chokepoint (retry/backoff
    for free). Returns None on any failure — callers degrade gracefully,
    they never treat a missing permalink as a reason to skip the post."""

async def aget_permalink(self, *args, **kwargs) -> str | None:
    return await asyncio.to_thread(self.get_permalink, *args, **kwargs)
```

**Required pre-implementation spike (D18):** call `chat.getPermalink` with an
existing agent's real bot token against a real message in the live workspace
before building the rest of this feature. If it 403s on scope, stop and
report back — the fix is a new OAuth scope requiring every agent's Slack app
to be reinstalled, which is a deploy-planning decision, not something to
discover mid-implementation.

**Scope note:** `docs/blackbird-star-topology-runbook.md` and CLAUDE.md's
"BlackbirdBot" section currently assert the hub never makes a top-level post
and that its assessment "never appears on anything a PI or another lab sees."
Both become stale once this ships and should be corrected as part of this
work (matching the repo's existing practice of fixing CLAUDE.md when reality
changes — see commit `e56e63f`).

---

## §7 — Testing

**Feature 1:**
- Replace `test_manager_router_exposes_no_mutating_routes` with an allowlist version (§3).
- PI is denied all three new routes (403, via existing `get_staff_user` behavior — no new gate logic to test beyond the allowlist itself).
- Non-PI `user_id` 404s on all three (mirrors `manager_pi_detail`'s existing guard).
- Create: success path creates `User` + enqueues `Job`; ORCID-already-exists (any role) rejects without creating a duplicate; ORCID-fetch failure surfaces as an error, not a 500.
- Edit: manager-driven edit produces the same `ResearcherProfile`/export/revision side effects as self-edit, with `changed_by_user_id` correctly attributed to the manager, not the PI.
- Mute/unmute: round-trip sets/clears `muted_at`/`muted_by`; rejected when status is `pending`/`suspended`; the existing roster-sync tests should need no changes since muting is just a `status` flip they already handle.

**Feature 2:**
- Unit test on the batched lookup: resolvable, null `subject_agent_id`, stale slug, unlinked agent — each renders the documented fallback.
- Template test asserting the macro emits `/admin/users/{id}` on the admin page and `/manager/pis/{id}` on the manager page for the same row, and plain text (no `<a>`) when unresolved.

**Feature 3:**
- `_capture_hub_assessment` posts exactly one summary for a closes_thread (fail) case and one for a CONCLUDE (pass) case, with the documented headline fields and no leaked rationale/red-flags/gating content.
- An `AssessmentDrop`-only turn (e.g. `empty_reply`) never posts.
- A Slack failure (post or permalink) in the summary step doesn't propagate into the calling turn or affect the assessment row's persistence.
- A regression test confirming `ASSESSMENTS_SUMMARY_CHANNEL` is absent from `SEEDED_CHANNELS` and from whatever collection drives Phase-1 subscription/discovery for PI-lab agents.

---

## §8 — Deploy sequence and coordination with the in-flight remediation plan

1. This design's migration (§2) is written last, after confirming `alembic heads` against whatever the remediation plan has landed by then.
2. Feature 1's profile-edit extraction (§4) should land after the remediation plan's Task 13 (`profile.py:159`'s atomic rewrite), not in parallel against the same lines.
3. Feature 3's `AgentSlackClient.get_permalink` addition (§6) should land after the remediation plan's Task 2 (same file, adjacent region), or be diffed carefully against it rather than assuming current line numbers.
4. Migrate-before-serve applies to this design's migration exactly as it does to `0028`/`0030` (§2).
5. None of this design's other files (`manager.py`, `profile.py`'s route wrapper, `directory.py`, `assessment_detail.py`, the assessment templates, `simulation.py`'s `_capture_hub_assessment`/`_persist_assessment`, `channels.py`) are touched by the remediation plan, per that plan's own file-overlap statement — low collision risk elsewhere.

---

## §9 — Rejected alternatives

**A capability/permission system** for the new manager writes (e.g.
`require_write("pis")`). Rejected for the same reason the account-types
design rejected it originally: three roles, YAGNI.

**Manager impersonation** as the mechanism for profile edits (mirroring how
admin edits PI profiles today). Rejected: impersonation is structurally
admin-only (`is_admin` is false for a manager *by construction* — the
account-types design's F7 fix), and reusing it would mean either weakening
that guarantee or building a parallel "manager impersonation" that's a much
bigger privilege grant than a scoped profile-edit route. The scoped route is
strictly narrower than what admin does today, which is arguably an
improvement, not just a workaround.

**A distinct `muted` status value** instead of reusing `inactive`. Rejected:
`inactive` already has exactly the right reversible-park semantics; a new
enum value would fragment the status axis for no behavioral difference,
and every existing `status == "active"` check (roster sync, `agent_page.py`'s
gates) would need auditing for a fourth value it doesn't need to know about.

**Building message-edit/delete logic** to guarantee exactly one summary post
per interview even across a verdict supersession. Rejected as more machinery
than the rarity of the case justifies (D15) — revisit only if it turns out to
happen often in practice.

**Restricting the summary channel to staff-only (private)**. This was my
initial recommendation given the "courtesy note, not a public verdict"
precedent; the user explicitly chose public instead, with headline-only
content as the mitigation (D12). Recorded here so a future reader can tell
this was a deliberate choice, not an oversight.

---

## §10 — Out of scope, known gaps

- **A full audit-log table** for manager actions (reads or writes). The
  account-types design already flagged read-auditing as unbuilt; this design
  adds only current-state mute attribution (D3), not a history log.
- **Cleanly closing an in-flight interview thread when its PI is muted.**
  The thread simply stalls waiting for a reply that won't come — identical to
  today's admin-driven inactivation, not newly introduced or newly fixed here.
- **A UI warning when muting a PI with an open interview thread.** Would
  require the web tier to query in-process engine state or infer "open
  thread" from `MessageLog`/`ThreadDecision` rows; not built.
- **Editing/deleting a summary-channel post when its verdict is later
  superseded** (D15).
- **A fallback permalink construction** if `chat.getPermalink` turns out to
  need a scope the current bots don't have. The spike (D18) should surface
  this before any fallback design is needed; none is designed here.
