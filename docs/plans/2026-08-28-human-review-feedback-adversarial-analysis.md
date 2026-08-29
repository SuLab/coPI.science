# Human review of assessments — adversarial design analysis

Date: 2026-08-28. Status: **analysis only — nothing implemented.** Every claim
below was verified against the working tree on this date (file:line cited);
nothing is taken from memory or assumed.

Features under analysis:

1. **Reviewer feedback per assessment** — numeric score + comment + a
   "learn" / "don't learn" dropdown, submitted by human staff in the web UI.
2. **"Don't learn"** feedback: logged in the DB, displayed on the assessment
   detail page for other humans.
3. **"Learn"** feedback: feeds a *system review bot* that analyzes the
   feedback + the assessment + the interview transcript and writes
   prompt-change **suggestions** (hub / PI / specialist prompts) to a new
   table, displayed on a new web page. The bot never edits anything itself.
4. **Two new list-page columns**: "Assigned reviewer" and "Reviewed by"
   (names of the person or people assigned / who reviewed).
5. **Approve / disapprove** of an assessment by human review staff.
6. **Formatting audit**: how bot-generated assessment text is formatted and
   rendered on the assessment detail page; make bot formatting and web
   markdown rendering correct.

---

## 0. Current-state facts that constrain everything

These are the verified facts the design must not fight. Each is load-bearing
for at least one finding below.

| # | Fact | Evidence |
|---|------|----------|
| F1 | `opportunity_assessments` has exactly one FK: `simulation_run_id → simulation_runs.id ON DELETE CASCADE`. No user FK, no status/`final` column, no `updated_at`. | `src/models/opportunity.py:31-36`; `alembic/versions/0025_add_opportunity_assessments.py:55-57` |
| F2 | The engine **hard-DELETEs** superseded assessment rows: `_retire_superseded_verdict` issues `sa_delete(OpportunityAssessment)` when a later provisional verdict replaces an earlier one, wrapped in `except Exception` (never raises). Provisional-vs-final is **in-memory only** (`_assessed_threads`); no DB column distinguishes them. | `src/agent/simulation.py:4045-4055`, `:3336-3340`, `:302-333`, `:489` |
| F3 | Migration head is `0038`, single linear chain; no `0039` exists. `scripts/migrate/preflight.py` hand-maintains `DEFAULT_TARGET = "0038"` plus an ordered revision list and a `PlannedObject` inventory that a new migration must be added to. | `alembic/versions/`; `scripts/migrate/preflight.py:74,240-344,349` |
| F4 | `users` has **no username**. Display identity everywhere is `User.name` (NOT NULL); `email` nullable-unique, `orcid` NOT NULL-unique. | `src/models/user.py:28-32`; `templates/base.html:99` etc. |
| F5 | The staff predicate exists: `User.is_staff` (admin OR manager, hybrid) and a ready dependency `get_staff_user` (403s non-staff **and** an admin impersonating a PI). | `src/models/user.py:108-115`; `src/dependencies.py:210-227` |
| F6 | The manager router's POSTs are pinned by `test_manager_router_mutations_are_an_explicit_allowlist`, which walks **only `manager_router.router.routes`**. A new router is invisible to it. The admin router's guard is **per-handler** (a forgotten `Depends(get_admin_user)` = route open to any logged-in user); the manager router's guard is router-level. | `tests/integration/test_manager_views.py:54-73`; `src/routers/admin.py:66`; `src/routers/manager.py:50` |
| F7 | Impersonation returns the **impersonated** user from `get_current_user`; the real admin survives only as `_real_admin` attribute. Exactly one mutation route refuses impersonated sessions (`POST /admin/users/{id}/delete`), via `getattr(current_user, "_is_impersonated", False)` → 403. | `src/dependencies.py:112-129`; `src/routers/admin.py:190-199` |
| F8 | `OriginGuardMiddleware` is global; new POSTs need nothing per-route. No CSRF tokens exist anywhere; plain same-origin forms pass. Tests must use the `client` fixture (injects `Origin`). | `src/main.py:143-213`; `tests/conftest.py:146-163` |
| F9 | `test_reachability.py` builds the route table from the live app. A new route must be credited by a **literal-path** `href`/`action` in a reachable template (Jinja expressions allowed only in `{path_param}` slots) or by a string in `src/`, else allowlisted with a reason. A new template must be rendered **by name** from `src/`. | `tests/unit/test_reachability.py:247-259, 672-689, 838-845` |
| F10 | Both assessments list pages share `templates/admin/_assessments_body.html`; both detail pages share `templates/admin/_assessment_detail_body.html`. Contract: the including wrapper defines `assessment_link` / `pi_link` macros; the shared body carries no surface-specific absolute URLs. The **admin** handler forwards context keys by explicit allowlist; the **manager** handler splats — a key added only to the service silently becomes Jinja `Undefined` on admin. The established workaround is attaching computed values to row objects (the `panel_state` pattern). | `templates/admin/_assessments_body.html:1-16`; `src/routers/admin.py:771-806`; `src/routers/manager.py:311`; `src/services/directory.py:385-402` |
| F11 | `jobs.type` is a closed Postgres ENUM (`generate_profile`, `monthly_refresh`); no migration has ever altered it. Worker dispatch is a hardcoded if/elif; `/admin/jobs` hardcodes the type dropdown. Retry: attempts increment at claim, 3 attempts → `dead` (`failed` is never written). | `src/models/job.py:19-26`; `src/worker/main.py:100-105, 112-141`; `templates/admin/jobs.html:45-46` |
| F12 | The worker already makes LLM calls (`synthesize_profile` for profiles; opt-in inbound-email classification) and has `ANTHROPIC_API_KEY` via `env_file: .env`. But the prod worker mounts **only `./profiles`** — it sees the **image-baked** copy of `prompts/` (`Dockerfile:17 COPY . .`), frozen at build time. `blackbird-app` and `agent` bind-mount `./prompts`; worker does not. | `docker-compose.prod.yml:67-89`; `src/services/llm.py:849-889`; `src/services/blackbird_rubric.py:56-59` |
| F13 | `llm_call_logs` structurally cannot host offline-bot calls: `simulation_run_id` is NOT NULL FK CASCADE, and the engine rebuilds its rate-limiter ledger from that table on restart — synthetic rows under a borrowed run id would tighten the live throttle. Worker LLM calls today log **nothing** (callback never registered outside the engine). | `src/models/agent_activity.py:187-197`; `src/agent/simulation.py:826, 6569`; `src/services/llm.py:683-685` |
| F14 | Interview-transcript reconstruction anchors on `assessment.slack_ts` → `agent_messages`, **not** on `assessment.thread_id`. NULL `slack_ts` or a missing anchor yields an empty timeline, documented as a normal outcome (verdicts legitimately outlive transcripts). 500-message scan cap. | `src/services/assessment_detail.py:703-757, 58` |
| F15 | Bots emit **GitHub-flavored Markdown**; Slack gets a converted copy (`markdown_to_mrkdwn`: only `**`→`*` and `- `→`• `); the DB stores the **source Markdown** (`agent_messages.content`). Sidecar prose (`rationale`, `recommended_next_experiment`, `red_flags` entries) is stored verbatim and is, empirically, **near-plain text**: over 82 real rows — 0 uses of `**bold**`, 0 bullet lists, 0 headers, 0 Slack `<url\|label>`, 4 rows with stray `*single-asterisk*`, 18 with newlines; ALL-CAPS section labels are the convention; scientific literals like `HLA-A*02:01` contain bare `*`. Transcript messages, by contrast, are genuinely Markdown: 20/118 hub and 37/63 lab messages use `**bold**`, 44 and 53 use `*italic*`. | `src/agent/slack_client.py:103-118, 736-798`; `src/agent/simulation.py:5536-5541, 5659`; corpus: `backups/opportunity_assessments_pre_purge_1787862739.dump` (82 rows), `backups/copi_pre0037_20260824T142900.dump` (181 messages) |
| F16 | Every assessment-page field is autoescaped plain text today (`whitespace-pre-line` on rationale/next-experiment; `<pre>` for raw JSON). No `\|safe`, no server-side markdown lib in the dependency tree, no custom Jinja filters. The repo's established markdown path is client-side: `static/js/markdown.js` = `DOMPurify.sanitize(marked.parse(md))`, fail-closed to `textContent`, consumed via `data-markdown="{{ x \| e }}"` — already used for **the same message content** on the agent dashboard and discussions pages, but not on assessment pages. Each router has its **own** Jinja environment, so a server-side filter registered in one router is invisible to the other. | `templates/admin/_assessment_detail_body.html:326,336,428`; `static/js/markdown.js`; `templates/agent/dashboard.html:161`; `pyproject.toml` |
| F17 | The closest schema precedent for human feedback is `ProposalReview`: `rating` SmallInteger NOT NULL, `comment` Text, `user_id` FK CASCADE, `reviewed_by_user_id`/`delegate_user_id` FK SET NULL, `submitted_via`. The audit-attribution idiom is columns-on-the-mutated-row plus, where an event log is wanted, `CohortAuditEvent` with a **denormalized actor email** surviving actor deletion. | `src/models/agent_registry.py:74-118`; `src/models/cohort.py:117-166` |
| F18 | Repo vocabulary conventions: tri-states are **strings, never booleans** ("declined" ≠ "never asked"); unknown states render as the alarming `{% else %}` branch, pinned by tests; NULL on pre-migration rows means "no claim available", deliberately never backfilled. | CLAUDE.md gating/`panel_owed` sections; `tests/unit/test_panel_state.py`; `tests/integration/test_assessment_detail_page.py::test_an_unknown_panel_state_never_renders_green` |

---

## 1. Recommended architecture (summary)

**All review state lives in new tables — zero new columns on
`opportunity_assessments`.** This is the single most consequential choice, and
it is driven by deploy mechanics, not taste (see finding C-1): new mapped
columns on the assessment model would put every existing
`select(OpportunityAssessment)` — both list pages, both detail pages, *and the
engine's own rehydration/supersession selects and INSERT* — into the
`UndefinedColumn` blast radius and force an agent-image rebuild for a feature
the agent doesn't use. New tables confine the pre-migration failure surface to
the new review UI alone and leave the running simulation untouched.

### New tables (one migration, `0039`)

**`assessment_reviews`** — append-only feedback rows (many per assessment):

- `id` UUID PK
- `assessment_id` UUID NOT NULL, FK `opportunity_assessments.id`
  **ON DELETE CASCADE**, indexed (see A-1 for why CASCADE and what else is
  required)
- `reviewer_user_id` UUID nullable, FK `users.id` **ON DELETE SET NULL**
- `reviewer_name` String(255) NOT NULL — denormalized at write time
  (CohortAuditEvent precedent, F17): the review must remain attributable after
  the reviewer's account is deleted
- `score` SmallInteger NOT NULL — server-validated range (recommend 1–5, the
  scale already used by the rubric and by `ProposalReview.rating`)
- `comment` Text NOT NULL default `''` — server-side length cap (recommend
  10,000 chars), autoescaped plain text on display
- `feedback_mode` String(20) NOT NULL — `'learn'` / `'log_only'` (strings per
  F18; `'log_only'` = the "don't learn" dropdown choice)
- `consumed_at` DateTime(tz) nullable — set when a review-bot job has
  incorporated this row (idempotency ledger, see D-4)
- `created_at` server_default now()

**`assessment_review_status`** — one row per assessment carrying the current
approve/disapprove state plus assignment:

- `assessment_id` UUID PK, FK `opportunity_assessments.id` ON DELETE CASCADE
- `status` String(20) nullable — `'approved'` / `'disapproved'`; **NULL =
  unreviewed** (three states, strings, F18)
- `status_set_by_user_id` FK users SET NULL + `status_set_by_name`
  String(255) + `status_set_at`
- Assignment as a separate child table (the user said "person or people"):

**`assessment_review_assignments`** — many per assessment:

- `id` UUID PK; `assessment_id` FK CASCADE, indexed;
  `assignee_user_id` FK users **CASCADE** (an assignment to a deleted user is
  meaningless, unlike a review *by* one) + `assignee_name` denormalized;
  `assigned_by_user_id` FK SET NULL + name; `created_at`;
  `UNIQUE (assessment_id, assignee_user_id)`

**`prompt_change_suggestions`** — the review bot's output:

- `id` UUID PK
- `assessment_id` UUID nullable, FK `opportunity_assessments.id`
  **ON DELETE SET NULL** — a suggestion is meta-level knowledge about the
  prompts and must survive assessment supersession and run deletion (A-2);
  snapshot columns make it self-describing after the FK nulls:
  `subject_label` Text (e.g. "pardoll — anti-R175H ladder"),
  `assessment_created_at`, `rubric_version` String(20)
- `feedback_ids` JSONB — the `assessment_reviews.id`s consumed (provenance)
- `target` String(40) NOT NULL — `'scout_hub'` / `'pi_lab'` /
  `'specialist:<domain>'` / `'out_of_scope'` (see D-7 for why the escape hatch)
- `prompt_files` JSONB NOT NULL — `[{path, sha256_12}]` for every prompt file
  the bot read (staleness detection; rubric-stamp precedent)
- `suggestion` Text NOT NULL — the bot's proposal, Markdown
- `model` String(100), `input_truncated` Boolean NOT NULL default false,
  `transcript_available` Boolean NOT NULL (D-2), `raw_response` Text —
  the audit trail `llm_call_logs` cannot host (F13)
- `status` String(20) NOT NULL default `'open'` — `'open'` / `'dismissed'` /
  `'implemented'`, with `status_set_by_user_id` SET NULL + name + at
- `created_at`

Plus in the same migration: `ALTER TYPE job_type_enum ADD VALUE
'review_feedback_analysis'` (see C-2).

### New routes

A **new router** `src/routers/reviews.py`, mounted `prefix="/reviews"`, with a
**router-level** `dependencies=[Depends(get_staff_user)]` (F5, F6 — never the
admin router's per-handler pattern):

- `POST /reviews/assessments/{assessment_id}` — submit score+comment+mode;
  when mode is `learn`, enqueue the job **in the same commit** (atomicity
  precedent: the manager Add-PI flow)
- `POST /reviews/assessments/{assessment_id}/status` — approve / disapprove /
  clear
- `POST /reviews/assessments/{assessment_id}/assign` and `/unassign`
- `GET /admin/prompt-suggestions` (+ detail `/{id}`, + `POST .../{id}/status`)
  — recommend **admin-only**, in the admin router with the `_ADMIN` singleton:
  the audience for prompt-change suggestions is whoever can implement them,
  which is operators; managers gain nothing actionable (open decision H-6)

Every mutation: refuse impersonated sessions with the F7 idiom; write the
denormalized actor name; `logger.info` attribution line; 302 redirect with
`?saved=1`/`?error=code` query-param feedback (the live convention — the
`flash_message` block in base.html is dead code).

### Review bot execution

A third job type on the existing worker (F11, F12): payload
`{"assessment_id": ...}`; handler loads the assessment, all **unconsumed
`learn`** reviews, the transcript via the same anchor logic as
`build_assessment_detail` (F14 — reuse, don't reimplement), and the current
prompt files + rubric for context; one call through
`generate_agent_response(...)` with `model=settings.llm_review_model` (new
setting, default `claude-opus-5`) and `max_tokens` well under the 21,333
non-streaming ceiling; parse with the `_extract_json` idiom; **store the raw
response even when parsing fails** (status `'open'`, target `'out_of_scope'`,
suggestion = raw text — the `assessment_drops` lesson: never discard the
model's output); mark the consumed reviews' `consumed_at` in the same commit
as the suggestion row.

### UI

- Detail pages (shared body): a "Human review" card — all feedback rows
  (score, mode chip, reviewer name, timestamp, autoescaped comment),
  the approve/disapprove control, the assignment control. Forms can live in
  the shared body because `/reviews/...` paths are surface-independent literal
  paths (F9, F10) — unlike `assessment_link`, they don't vary by wrapper.
- List pages (shared body): two new cells fed by **one batched query** over
  the ≤500 visible assessment ids (the PI-link-resolution pattern,
  `directory.py:414-420`), values attached to row objects (the `panel_state`
  pattern) so the admin context allowlist needs no new keys. "Assigned
  reviewer" = assignment names; "Reviewed by" = distinct names from
  `assessment_reviews` ∪ the status setter. Also surface the
  approve/disapprove state as a chip (F18 conventions: NULL renders as quiet
  "unreviewed", unknown strings render alarming).
- New admin page `admin/prompt_suggestions.html`, nav item in the admin
  sub-nav (`base.html:116-124`), suggestion text rendered via the
  `data-markdown` + DOMPurify fail-closed pattern (F16).

---

## 2. Adversarial findings

### A. Data lifecycle

**A-1 — The engine deletes assessment rows out from under reviews (the
single nastiest interaction in this feature).**
`_retire_superseded_verdict` hard-DELETEs a stored provisional verdict when a
later sidecar supersedes it, minutes apart, mid-run (F2). Staff read these
pages daily *during* runs. Every FK choice loses differently:

- `CASCADE`: a human's score/comment/approval on a provisional row is
  silently destroyed by the simulation. No error anywhere; the reviewer's
  work just vanishes.
- `RESTRICT`: the engine's delete fails — but it is wrapped in
  `except Exception` and never raises, so the engine logs and moves on with
  **two rows for one interview**, breaking the one-row invariant the whole
  capture pipeline defends. Worse: whether the invariant holds would now
  depend on whether a human happened to click a button first.
- `SET NULL`: orphaned reviews pointing at nothing, with no way to render
  them on any assessment page.

**Recommendation:** CASCADE, **plus a re-point step inside
`_retire_superseded_verdict`** immediately before the DELETE: `UPDATE
assessment_reviews SET assessment_id = :replacement WHERE assessment_id = :old`
(same for status/assignment rows, with a conflict-tolerant upsert for the PK
table), inside the same try/except the delete already lives in. The
replacement row is the same interview seconds later, so the human's feedback
still describes the thing they read. This is a ~15-line engine change — and it
is the **one** part of the feature that touches `src/agent/`, which means it
alone forces an agent-image rebuild (see C-3). A legitimate cheaper variant:
skip the re-point, accept the (small) loss window, and show a "this run is
still active — a provisional verdict may be superseded" banner on rows
belonging to the newest run. Decide explicitly; don't default into it.

Note also: nothing in the DB marks a row provisional (F2), so the UI *cannot*
reliably gate review submission on "terminal only". Don't attempt it; it would
be a false guarantee.

**A-2 — Run deletion transitively destroys human work.** Everything FK'd
CASCADE to `opportunity_assessments` inherits the CASCADE from
`simulation_runs` (F1). The standing rule "never DELETE from
`simulation_runs`" now protects *human-authored* data, not just bot output —
worth an explicit line in the CLAUDE.md archive box when implementing. This is
also why `prompt_change_suggestions.assessment_id` must be **SET NULL** with
snapshot columns: suggestions are distilled human+bot judgment about the
*prompts*; they must survive both supersession and any future archival
mistake.

**A-3 — Reviewer FKs vs. user deletion.** `delete_user_account` touches nine
places and relies on FK topology for the rest. Reviews *by* a deleted staff
member must survive (SET NULL + denormalized name — F17's
`CohortAuditEvent.actor_email` precedent); assignments *to* a deleted user
should vanish (CASCADE). Do **not** couple two nullable owner columns with a
CHECK constraint — that exact combination (SET NULL driving a CHECK violation)
is the 500 that migration `0036` had to fix on `private_channel_members`. Add
the new tables to `DeletionReport` only if the operator wants them reported;
FK topology alone is correct either way.

**A-4 — "Reviewed by" must be derivable after account deletion.** The list
column renders names. If it renders `User.name` via join, a deleted reviewer
blanks history. The denormalized `reviewer_name` columns (A-3) are what the
list should render — the join is only for linking to a live user page.

**A-5 — There is no `username`.** The request says "username(s)"; the system's
only display identity is `User.name` (F4), which is neither unique nor
immutable (managers can edit PI names; users edit their own). Accept
`User.name` + denormalization as the answer, or display `name (email)` for
staff. Do not invent a username column for this.

### B. Auth and routing

**B-1 — Where the write routes live is a policy decision with a tripwire
either way.** The manager router's POSTs are pinned by an allowlist test that
walks only that router (F6). Adding review POSTs there means amending a
deliberately loud test — a recorded design reversal (precedent exists: D1 was
amended once). Adding them to the admin router excludes managers, contradicting
"human review staff". A new `/reviews` router guarded router-level by
`get_staff_user` trips nothing — which is precisely why the analysis must say
out loud: **this extends manager write powers without touching the pinned
allowlist.** That is defensible (the allowlist pins the manager *router*, not
the manager *role*), but it should be a conscious decision recorded in the
design, and the new router should get its own allowlist-style pin test so the
same discipline applies to it.

**B-2 — Impersonation attribution.** `get_staff_user` already 403s an admin
impersonating a PI (F5), but an admin impersonating a **manager** would pass
and the review would be attributed to a manager who never acted. Mirror the
one existing refusal idiom (F7) on every review mutation:
`getattr(current_user, "_is_impersonated", False)` → 403. Test it.

**B-3 — Do not copy the admin router's guard pattern.** Its per-handler
`Depends(get_admin_user)` has a documented failure mode: forget it once and
the route is open to every logged-in PI (F6, and the manager router's own
docstring calls it out as "F5"). Router-level dependency on the new router;
`_STAFF`-style module singletons to stay under the ruff B008 ratchet.

**B-4 — PI exposure.** PIs must never see review scores, approve state, or
suggestions: nothing here may leak into any PI-facing surface
(`/agent`, public profiles) or into Slack. The review bot has no transport —
keep it that way (no imports from `slack_client`/`slack_web` in the job
handler; assert in a test if cheap). The suggestions page will quote
unpublished-disclosure-bearing rationale (one production rationale literally
begins "CONFIDENTIAL — contains unpublished lab disclosures", F15 corpus), so
its audience question (H-6) is a confidentiality question, not just UX.

**B-5 — Reachability/test tripwires for the new surface** (each verified
against the mechanism, F9): review-form `action`s must be literal paths
(`/reviews/assessments/{{ a.id }}` is creditable; a macro-built base path is
not); the suggestions page template must be rendered by its literal name from
`src/`; its nav link belongs in `base.html`'s admin sub-nav (and nav-link
visibility tests exist for role gating); any new GET on the *manager* router
would enter two auto-sweeps that need seeded rows — a reason to keep review
GETs off that router entirely.

### C. Migrations and deploy ordering

**C-1 — Keep the assessment model untouched.** If approve/assignment state
were columns on `opportunity_assessments`, the next deploy inherits the full
`0036`/`0037`/`0038` box: every `select(OpportunityAssessment)` (both list
pages, both detail pages) raises `UndefinedColumn` against a pre-migration DB,
`_persist_assessment`'s INSERT fails on the engine side, and the agent image
must be rebuilt in lockstep. With separate tables, old code doesn't know the
tables exist (safe), and new code fails only on the new review surfaces if
migration is late. Same migrate-before-serve order applies, but the blast
radius shrinks from "the whole assessments surface + the engine" to "the new
feature". The one engine touch that remains is the A-1 re-point (optional).

**C-2 — The enum migration is a novel hazard in this repo.** No migration has
ever done `ALTER TYPE ... ADD VALUE` (F11). Specifics: use `ADD VALUE IF NOT
EXISTS`; on Postgres 15 it may run inside the migration's transaction so long
as the migration doesn't *use* the value; the **downgrade must be a no-op**
(Postgres cannot drop enum values) — and `scripts/ci.sh` runs an
upgrade→downgrade→upgrade round trip, so the second upgrade re-running `ADD
VALUE IF NOT EXISTS` is exactly why the `IF NOT EXISTS` is mandatory, and the
no-op downgrade must leave the *tables* droppable (drop tables in downgrade;
leave the enum value). Alternative considered and rejected: a parallel jobs
mechanism or a String job-type column migration — both churn a working
system for no gain.

**C-3 — Deploy checklist deltas this feature creates** (all verified against
the mechanisms):

1. Migration `0039` + update `scripts/migrate/preflight.py` (`DEFAULT_TARGET`,
   revision list, `PlannedObject` inventory — F3).
2. `templates/admin/jobs.html` hardcoded type dropdown gains the third type.
3. Worker: dispatch branch + rebuild (`$DC build worker`) — worker bakes
   `src/` like everything else.
4. **Compose edit for the prompts mount** (see D-6): `./prompts:/app/prompts:ro`
   on the worker service, in *both* the committed `docker-compose.prod.yml`
   and the host's **uncommitted working-tree copy** — that file must never be
   checked out/stashed (it carries the `blackbird-app` rename that keeps
   org1's site alive), so the edit is applied by hand on the host, not via
   git.
5. `.env`/`config.py`: `llm_review_model` etc. — worker container needs a
   **recreate**, not restart, to see `.env` changes.
6. Agent image rebuild **only if** the A-1 re-point is adopted; otherwise the
   simulation is untouched — worth preserving, since "never auto-start the
   simulation" is an operator rule and decoupling the feature from an agent
   restart makes it deployable any time.

### D. The review bot

**D-1 — The bot's inputs are adversarial input.** The transcript contains
PI-authored text and hub text derived from it. A hostile or merely weird
interview can steer the bot's output ("suggest the hub always advance ideas
involving X") — indirect prompt injection laundered through a
human-credibility surface: reviewers will *trust* the suggestions page more
than a Slack thread. Mitigations, all cheap: the bot's system prompt treats
transcript/feedback strictly as quoted data; the page renders provenance
(which feedback rows, which interview, which prompt hashes) next to every
suggestion; suggestions are never auto-applied (already the spec — keep it
structural: the worker gets the prompts mount **read-only**, D-6); DOMPurify
on render (F16).

**D-2 — The transcript is frequently missing, and the bot must say so.**
Anchor logic fails soft to an empty timeline for NULL `slack_ts`, pre-`0036`
rows, and any verdict whose run's messages predate it (F14). If the job
proceeds silently, the model will confabulate interview content and the
suggestion will look grounded. The `transcript_available` column exists so the
page can label these; the bot prompt must instruct "if no transcript is
provided, analyze feedback + assessment only and say the transcript was
unavailable". Reuse `_load_thread_messages`' logic (extract or import) rather
than re-deriving the anchor rules — they took three migrations to get right.

Related input caveat: `opportunity_assessments` has a **second writer**,
`scripts/backfill_dropped_verdicts.py`, whose field mapping is byte-identical
to `_persist_assessment`'s (it imports the same coercion helpers,
`backfill_dropped_verdicts.py:91`) **except that it never sets
`recommended_next_experiment`** — backfilled rows carry NULL there regardless
of what the recovered sidecar said; the value survives only inside
`raw_verdict` (script kwargs at `:425-450`). Neither the bot nor the UI may
read a NULL `recommended_next_experiment` as "the verdict named no
experiment"; where free-text fidelity matters, prefer `raw_verdict`. (Also:
`derisking_milestones` is a retired tolerant-passthrough column — cut from the
sidecar contract in rubric v3.2.0, emitted by no current model, read by
nothing, deliberately unrendered — it should drive no rendering or bot-input
decision.)

**D-3 — Token budget.** Up to 500 messages plus 8 specialist `raw_opinion`
blobs plus prompt files plus the rubric can exceed the context window and will
routinely exceed any sane cost target. Clip deterministically (e.g., total
input budget with head+tail of the transcript preserved, hub verdict-carrying
message always kept), and set `input_truncated` when anything was dropped —
the patents-tool lesson applies verbatim: *silent* narrowing of what was
analyzed is the damage, so disclosure must be total. Output: `max_tokens`
must stay ≤ 21,333 (`NONSTREAMING_MAX_TOKENS`, enforced pre-flight in
`_acreate`; the ceiling test scans call sites for literals above it — don't
trip it); ~8,000 is ample for a suggestion.

**D-4 — Idempotency and duplicate suggestions.** Retries (3 attempts) rerun
the handler; multiple `learn` reviews enqueue multiple jobs for one
assessment. The `consumed_at` ledger makes both safe: the job consumes all
currently-unconsumed learn reviews for its assessment; if none exist, complete
as a no-op. Consumption and the suggestion row commit together. A crash after
commit → retry sees nothing unconsumed → no duplicate. A second review after
the first job ran → second job, second suggestion, distinct `feedback_ids` —
correct behavior, not a bug.

**D-5 — The job/user CASCADE races that already bit this repo.** `jobs.user_id`
is CASCADE from users and the worker already handles the row vanishing
mid-flight (`test_worker_deletion_races.py`). The review job should set
`user_id` to the submitting reviewer (visibility on `/admin/jobs`) and
tolerate the assessment disappearing mid-job (supersession! A-1) by completing
with a no-op or a SET-NULL'd suggestion — never `dead` for a normal lifecycle
event.

**D-6 — Prompt staleness is a correctness bug for this bot specifically.** A
bot whose whole job is proposing edits to prompt files must read the *live*
files, and the prod worker cannot (F12): it would analyze the image-baked copy,
which after any operator prompt edit is silently stale — suggestions would
propose changes against text that no longer exists. Fix: bind-mount
`./prompts` into the worker **read-only** (`:ro` — which is also the
structural guarantee behind "the bot never makes changes itself"), and stamp
every suggestion with the sha256[:12] of each file read (`prompt_files`), so
the page can badge suggestions whose target files have since changed (rubric
content-hash precedent). Note the in-repo comment "worker … never imports this
module" (`blackbird_rubric.py:56-59`) becomes stale if the job imports the
rubric loader — update it.

**D-7 — Scope creep in the target vocabulary.** Much "learn" feedback will
actually be *rubric calibration* feedback (weights, bands, gates) — the rubric
is rendered into the hub's system prompt but lives in
`prompts/rubric/blackbird-rubric.toml` with its own versioning/regime rules
and an operator-controlled change process. The bot must be able to answer
"this is a rubric change, out of scope for prompt suggestions" rather than
disguising a rubric change as a prompt edit — hence the `'out_of_scope'`
target. Whether `'rubric'` becomes a first-class target is an operator
decision (H-7); defaulting it in would let a background bot generate pressure
on the rubric change process the repo deliberately gates.

**D-8 — Where the bot's prompt lives.** Not under `prompts/roles/` —
`available_roles()` globs that directory and feeds the admin agent-role
dropdown and assignment validator, so a `system_review` role dir would appear
as an assignable *agent role*. Use a top-level file (`prompts/review-bot.md`),
the `profile-synthesis.md` precedent. Tripwires if it's a `.md` under
`prompts/`: the forbidden-phrase rglob test scans every `prompts/**/*.md` for
retired phrases — "phase 2" (easy to write in a review-bot prompt), "private
instructions", "Baltimore", etc. Check the `_FORBIDDEN` list before writing
the prompt. The two prompt-set sync docs enumerate doc-side, so a new prompt
file does **not** trip `test_doc_prompt_sync` unless embedded there.

### E. Web UI

**E-1 — Admin context allowlist vs. manager splat (F10).** Feed the new list
columns by attaching values to row objects (the `panel_state` idiom), not by
new context keys — otherwise the admin page silently renders empty cells
while manager works, the exact asymmetry the allowlist comment warns about.
Detail pages: both handlers splat `**detail`, so new keys from
`build_assessment_detail` flow to both — but remember `admin_view` gating if
any review data should be admin-only (none should).

**E-2 — One batched query, not a join, not N+1.** 500 rows × 2 lookups is the
trap. The service already demonstrates the pattern (PI-link resolution:
one `IN (...)` query over subject ids). Same for reviews/assignments/status:
three `IN` queries over the visible assessment ids, grouped in Python.

**E-3 — Render conventions.** Approve state is a tri-state string; NULL is
"unreviewed" (quiet, the common case — the `0037` unvetted-banner lesson says
a loud default would drown the page), unknown values alarm via the terminal
`{% else %}` (pinned convention, F18). Reviewer comments: autoescaped plain
text with `whitespace-pre-line`, **not** markdown-rendered (human input;
markdown adds nothing but an injection review). Bot suggestions:
`data-markdown` + DOMPurify, fail-closed (F16). Score input: server-side
range validation on the Form int; the DB column is SmallInteger — don't rely
on the `<select>`.

**E-4 — The list page purity test.**
`test_admin_assessments_page_renders_no_inline_detail_rows` pins that the list
page contains no detail-field prose. Reviewer *names* and a status chip are
fine; do not surface comment text in the new columns.

### F. Formatting audit — findings and the fix

The audit question was "are the bots formatting correctly, and does the web UI
render markdown appropriately?" The verified answer (F15, F16):

1. **The interview timeline is the real defect.** `agent_messages.content` is
   genuine GitHub-flavored Markdown (a fifth to a half of messages use
   bold/italic), and the assessment detail page renders it as literal
   asterisks — while the agent dashboard and discussions pages already render
   the *same class of content* through `data-markdown` + marked + DOMPurify.
   **Fix: apply that exact existing pattern to the timeline message bodies**
   (`_assessment_detail_body.html:428`), loading the same three scripts in
   both detail wrappers. Keep the `max-h-64` scroll container. This is a
   template-only change with a fail-closed sanitizer, no new dependency
   decisions.
2. **Do NOT markdown-render the sidecar prose fields** (`rationale`,
   `recommended_next_experiment`, `red_flags` entries). Empirically they are
   near-plain text whose convention is ALL-CAPS labels and `\n\n` paragraphs —
   and they contain bare `*` inside scientific literals (`HLA-A*02:01`,
   `Methods … relating to ecDNA biogenesis` titles quoted with `*…*`). A
   markdown pass would eat or italicize those. The current
   `whitespace-pre-line` treatment is correct for this corpus; leave it.
3. **Bots are formatting "correctly" by imitation, not instruction.** No
   prompt anywhere instructs a formatting dialect; the models emit GFM because
   the prompts and injected history are GFM, and `markdown_to_mrkdwn` converts
   only `**`→`*` and `- `→`•` on the Slack copy. Measured corpus shows the
   gaps are latent, not live (0 headers, 0 Slack-link syntax, 0 fenced blocks
   in messages). Two optional hardening lines, each with a real cost: adding
   "write rationale as plain text; the visible reply may use simple Markdown
   (bold/italic only)" to `phase4-thread-reply.md` pins the behavior, **but**
   any scout_hub prompt edit requires the `sync_prompt_set_docs.py` run and a
   characterization-snapshot review (three reviewed regenerations are the
   precedent — never `--snapshot-update` casually). Recommend deferring prompt
   edits: the corpus says behavior is already stable, and the render-side fix
   (item 1) plus render-side restraint (item 2) capture the value without
   touching pinned prompts.
4. One inconsistency worth knowing, not fixing: the canonical sidecar test
   fixture (`test_assessment_sidecar.py`) shows a *Slack-mrkdwn-styled*
   visible reply and stale sidecar keys — it documents the wire format of an
   older regime. Don't model new prompt guidance on it.

### G. Test-suite tripwire checklist (all mechanisms verified)

- New router → its own POST-allowlist pin test; no edit to the manager
  allowlist test (unless routes are placed there — H-1).
- Reachability: literal-path form actions; template rendered by name;
  possible `ROUTE_ALLOWLIST` entries for any JS-only endpoint (none planned).
- Role tests: PI 403 on every review route; manager 200; impersonated-admin
  403 (B-2).
- Origin guard: use the `client` fixture for POST tests.
- Migration: preflight inventory + revision list; ci.sh
  upgrade→downgrade→upgrade must survive the enum no-op downgrade (C-2).
- Ruff ratchet: module-singleton `Depends` idiom; new files zero findings
  (test suite is at zero-findings ceiling).
- Coverage floor: branch coverage on the job handler's failure paths
  (unparseable output, missing transcript, vanished assessment).
- Forbidden-phrase rglob if a new `prompts/**/*.md` is added (D-8).
- The engine re-point (if adopted, A-1) sits near `.ambr`-pinned
  guidance strings — the re-point itself is Python in `simulation.py`, not
  guidance text, so no snapshot regen is expected; verify by running the
  characterization suite before assuming.

---

## 3. Open decisions (recommendation first)

| # | Decision | Recommendation |
|---|----------|----------------|
| H-1 | Route placement for staff writes | New `/reviews` router, router-level `get_staff_user`, own pin test. Alternative: amend the manager allowlist (recorded reversal). |
| H-2 | Score scale | 1–5 integer, matching `ProposalReview.rating` and the rubric's scale. |
| H-3 | Feedback mutability | Append-only; no edit/delete in v1 (matches the archive philosophy; deletion questions all get harder with the bot's `consumed_at` ledger). |
| H-4 | Approve/disapprove granularity | Single current status with attribution + timestamp; the feedback stream is the history. Full history table only if the operator wants dissent tracking. |
| H-5 | Supersession handling | CASCADE + engine re-point (A-1). Cheaper variant: CASCADE + active-run banner, no engine change, no agent rebuild. |
| H-6 | Suggestions page audience | Admin-only (implementers are operators; page quotes confidential rationale). Staff-wide is defensible since managers already see rationale on detail pages. |
| H-7 | Is `'rubric'` a legal suggestion target? | No in v1 — bot marks rubric-shaped feedback `out_of_scope`; the rubric has its own gated change process. |
| H-8 | Who assigns reviewers | Staff (it's coordination metadata, not privilege). Multiple assignees supported per the request's "person or people". |
| H-9 | Does "Reviewed by" include the approve/disapprove actor? | Yes — distinct union of feedback authors and status setter. |
| H-10 | Learn-feedback visibility | All feedback (learn and don't-learn) displays on the detail page; the mode only controls bot consumption. (The request explicitly displays don't-learn; hiding learn feedback would be stranger.) |

---

## 4. Sizing (for planning, not commitment)

- Migration `0039`: 4 tables + enum value + preflight updates — one sitting.
- `src/routers/reviews.py` + templates + list-page columns + batched queries —
  the largest UI chunk.
- Worker job handler + `prompts/review-bot.md` + config + compose mount —
  medium; the transcript-loading reuse is the fiddly part.
- Suggestions page (admin router + template + nav) — small.
- Formatting fix (timeline `data-markdown`) — tiny, independent, could ship
  first and alone.
- Engine re-point (if H-5 adopts it) — small code, but drags in agent rebuild
  + restart coordination with the operator's "never auto-start" rule.

Nothing here blocks on anything external. The independent, zero-risk first
increment is the formatting fix; the data model + routes are the second; the
bot is third and can lag without weakening the first two (don't-learn logging
works with no bot at all).
