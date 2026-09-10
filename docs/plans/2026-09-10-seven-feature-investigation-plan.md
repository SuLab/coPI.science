# Seven-feature investigation and plan — 2026-09-10

Status: **APPROVED 2026-09-10** (operator answers recorded under §Decisions).
Adversarially audited 2026-09-10 by a fresh-context reviewer against the repo;
14 findings folded in (see §Audit log).
Every claim below was checked against the working tree at `9878cb0`
(branch `feat/assessment-ui-review-score-login-proposal-limit`). Line numbers
are from that tree.

Ordering recommendation (cheapest / lowest-risk first): F7 → F6 → F4 → F5 →
F3 → F2 → F1. F1, F2 and F3 each need an agent-image rebuild; F1 and F3 need a
prompt/role bump or a migration. Each feature is independently shippable.

Shared constraints that apply to every feature:

- Alembic head is `0043`. F1, F2 and F4 each need a revision; **whichever
  ships first takes `0044`** and the others renumber. Every new revision must
  also update, in `scripts/migrate/preflight.py`: `DEFAULT_TARGET` (`:74`),
  `SUPPORTED_START_REVISIONS` (`:128`, must include `0043`), `REVISION_ORDER`
  (`:399`, last entry must equal the target) and `PLANNED_OBJECTS`
  (`:390-395`, one entry per new column/index) — all asserted by
  `tests/unit/test_migration_checks.py` (`:236`, `:834`, `:896-908`,
  `:1011`). All of them are migrate-before-serve.
- `tests/unit/test_reachability.py` requires every new route be linked from a
  template or be on `ROUTE_ALLOWLIST` with a reason.
- The manager router has an allowlist test
  (`tests/integration/test_manager_views.py:55-74`) that pins exactly four
  POST paths; any new manager POST must be added there deliberately.
- `scripts/ci.sh` is the whole gate (no server-side CI). Run it before commit.
- After any `prompts/` edit: `scripts/sync_prompt_set_docs.py` and a bump of
  `prompts/roles/scout_hub/role.toml` `version` (`:7`, currently `1.2.0`).

---

## F1 — Cost breakdown by interview-question type and specialist type

### What exists

- `/admin/simulation` Live tab already renders "Cost by agent / model /
  phase" (`templates/admin/simulation.html:340-354`, built at
  `src/routers/admin.py:2355-2374` via `hbar_list`), from
  `simulation_stats.cost_summary` (`src/services/simulation_stats.py:344`).
- `_grouped_cost(db, run_id, <column>)` (`:326`) is generic over any column,
  so a new breakdown on an existing column is one call.
- `llm_call_logs.phase` (`src/models/agent_activity.py:197`, String(30)) takes
  exactly four values: `thread_reply` (`src/agent/simulation.py:2472`),
  `new_post` (`:3304`), `memory` (`:8845`), and `consult_{domain}`
  (`src/agent/tools.py:680`). So **specialist type by domain is already
  recoverable** and already appears, undifferentiated, in "Cost by phase".
- `call_stats` JSONB (`:299`) holds per-call `{kind: round|final|forced_final|
  retry, input_tokens, output_tokens, thinking_tokens, stop_reason}`, so
  tool-round vs final vs retry cost is priceable via `jsonb_array_elements`.
  NULL on pre-`0032` rows.
- **Interview question type is recorded nowhere.** `phase4_guidance`
  (`src/agent/thread_guidance.py:205-218`) derives EXPLORE (≤4 messages) /
  DECIDE (≤11) / CONCLUDE from the ordinal at call time and it is never
  written to the LLM row. The `log_meta` dict at `simulation.py:2470-2475` is
  built in the same function where the ordinal is known.
- Any extra `log_meta` key is silently dropped by `_llm_log_record`
  (`simulation.py:8166+`), which maps a fixed key set — so a new key needs
  both the producer and the mapper changed (and the `call_id` already passed
  at `:3305` is an existing example of a dropped key).
- Specialist consult rows (`specialist_consults`, `src/models/specialist_consult.py:49-140`)
  have **no FK to `llm_call_logs`**; the only join is the fuzzy triple
  `(simulation_run_id, thread_id = thread_ts, domain = phase suffix)`, which
  is ambiguous when one thread consults the same domain twice.

### Interpretation of the request (needs your confirmation)

"Type of interview question" = the hub's phase-4 stage (EXPLORE / DECIDE /
CONCLUDE) of each `thread_reply`, split by role (hub vs lab bot).
"Type of specialist response or query" = (a) specialist domain and (b) the
specialist's verdict signal / read_state / truncated outcome, plus the
`search_prior_art` tool. If you meant something else (e.g. the
`thread_guidance` instruction text itself, or the post type of the opening
proposal), say so; the column design below changes.

### Design

1. **Migration**: add nullable `llm_call_logs.thread_phase String(20)`
   and `llm_call_logs.message_ordinal Integer`. Pattern: `0042` (`:71`).
   Nullable, never backfilled — pre-migration rows render in an explicit
   "unclassified" bucket. **`_grouped_cost` (`simulation_stats.py:326-341`)
   has no such bucket today**: it keys on the raw column value and
   `hbar_list` would receive a `None` label, so the new query must
   `coalesce(thread_phase, 'unclassified')`.
2. **Producer**: at `simulation.py:2470` add `thread_phase` and
   `message_ordinal` to `log_meta` (value from the same `phase4_guidance`
   call already made for the prompt). **Also** pass them through
   `consult_specialist`'s own `log_meta` at `src/agent/tools.py:678-683`,
   otherwise every `consult_*` row lands in "unclassified" and the
   specialist split cannot be crossed with interview stage. Map both keys in
   `_llm_log_record` (`simulation.py:8166-8215`).
3. **Consult link**: add nullable `specialist_consults.llm_call_log_id` FK
   (SET NULL) written by `_record_specialist_consult` — requires the consult
   path to know the log row id, which today is buffered and flushed later.
   Cheaper alternative: stamp `llm_call_logs.consult_domain` (already in
   `phase`) and join on the triple, accepting the same-domain-twice
   ambiguity and labelling it. **Recommendation: the fuzzy join, labelled**,
   because the FK forces a buffered-flush ordering change in the engine's
   hot path for a reporting feature.
4. **Aggregates** (`simulation_stats.py`): `cost_by_thread_phase(run)` grouped
   by `(agent role, thread_phase)`; `cost_by_specialist(run)` grouped by
   domain with a sub-split by `verdict_signal` via the triple join;
   `cost_by_call_kind(run)` from `call_stats` with its own floor flag.
   All three carry `is_floor` (pre-0036 NULL cache columns) exactly as
   `CostSummary` does (`:382-390`).
5. **UI**: two new panels next to "Cost by phase" in
   `templates/admin/simulation.html:340-354`, reusing `hbar_list`. Web-tier
   rebuild only for the read side; **agent image rebuild** for the producer.
6. **Tests**: `tests/unit/test_simulation_stats.py` (hand-computed numbers,
   per its docstring), `tests/integration/test_admin_simulation_page.py`
   (empty-run renders every section, `:442`), `tests/factories.make_llm_call_log`
   gains the two columns.

Deploy: migrate-before-serve (`0044` is additive; new code maps the columns,
so old DB + new code raises `UndefinedColumn` on every `select(LlmCallLog)`).

---

## F2 — Managers provision Slack bots through the UI

### What exists

- Admin flow: `POST /admin/agents/{id}/slack/provision`
  (`src/routers/admin.py:1116`) → `start_provisioning`
  (`src/services/admin_provisioning.py`) creates a Slack app via the
  Manifest API using the rotating config token in `app_settings`, writes a
  `SlackAppProvision` bridge row keyed by random `state`, and 302s to Slack.
  Slack redirects to `GET /admin/agents/slack/callback` (`:1143`), gated
  `Depends(get_admin_user)`, which calls `complete_provisioning` and saves
  `AgentRegistry.slack_bot_token`.
- The redirect URI is **baked into the app manifest at creation**
  (`slack_provisioning.py:115`, `redirect_urls: [redirect_uri]`;
  `CALLBACK_PATH = "/admin/agents/slack/callback"`, `admin_provisioning.py:33`).
  All 64+ existing apps carry that URL (`slack_install_links.md`).
- Activation: `POST /admin/agents/{id}/approve` (`:1023`) flips pending →
  active, gated by `activation_blockers` (`src/services/agent_activation.py`)
  with a logged override.
- Manager side today: `templates/manager/pi_detail.html:43-49` shows "Agent
  pending — Awaiting Slack install", a link to `/admin/agents/{id}` for
  admins, and for managers the literal "Slack install is admin-only — ask an
  admin." The design record says "Provisioning/activation stays admin-only"
  (`docs/specs/2026-08-21-manager-pi-controls-design.md:12`).
- Manager router: `APIRouter(dependencies=[Depends(get_review_user)])`, writes
  on `get_staff_user`; POST allowlist test pins four paths.
- Slack-side: workspace admins may install apps regardless of the app-approval
  setting; the Manifest-created app is owned by whichever Slack user minted
  the config token (the admin's), and that is unchanged by who installs it.

### Design (policy reversal — record it)

1. **Two new manager POST routes**, both `get_staff_user`-gated and both
   refusing impersonated sessions:
   - `POST /manager/pis/{user_id}/slack/provision` — loads the PI's agent
     (404 unless `user_role == 'pi'` and agent exists and `status ==
     'pending'`), calls the same `start_provisioning`, 302s to Slack.
   - `POST /manager/pis/{user_id}/activate` — calls a shared service
     function extracted from `admin_approve_agent`'s activation branch (same
     `activation_blockers`, **no override checkbox** for managers: a blocked
     activation shows the blockers and says to ask an admin).
2. **Callback**: keep the path (it is baked into every manifest). Change its
   gate from `get_admin_user` to `get_staff_user` (the session cookie is
   `same_site="lax"`, `src/main.py:350`, so it survives Slack's top-level GET
   redirect; the Origin guard ignores GETs). Redirect by role on **all four**
   exits — success `:1171` and the three error paths `:1155`, `:1160`,
   `:1168` — admin → `/admin/agents/...`, manager → `/manager/pis/{user_id}?slack_ok=1|slack_error=...`.
   The bridge row has no initiating-user column; add nullable
   `slack_app_provisions.initiated_by_user_id` (migration `0044` or `0045`)
   so the callback can refuse completion by a different user than the one
   who clicked Provision (defence against a leaked authorize URL).
3. **UI**: replace the "admin-only" copy in `pi_detail.html:43-49` with a
   Provision button (pending + no token), then an Activate button (pending +
   token). Copy the `slack_ok`/`slack_error` banners from
   `templates/admin/agent_detail.html:18-27`. Gate the buttons on
   `effective_user.is_staff and not impersonation_banner`, **never**
   `current_user` — `pi_detail.html:45` currently uses `current_user.is_admin`,
   and `manager.py:123` swaps `current_user` to the real admin under
   impersonation, so a `current_user` gate renders buttons that 403
   (the F6 trap documented at `manager.py:107-117`).
4. **Reuse**: move the activation branch of `admin_approve_agent` into
   `src/services/agent_activation.py::activate_agent(db, agent, actor,
   override=False)` so admin and manager share one gate.
5. **Update the allowlist test** to six paths; update
   `docs/specs/2026-08-21-manager-pi-controls-design.md` D1 and CLAUDE.md
   ("Still cannot … provision Slack bots") — this is a deliberate reversal
   of D1 and must be recorded as such.
6. **Tests**: manager can provision/activate a pending PI; reviewer 403;
   impersonating admin 403; callback completes for the initiating manager and
   refuses another staff user; activation blocked without profile;
   `test_reachability` sees the new buttons. **Invert**
   `tests/integration/test_manager_pi_writes.py:330-371`
   (`test_pi_detail_shows_agent_state_and_the_admin_deep_link_to_admins_only`),
   which asserts the "ask an admin" copy and no admin link for a manager.
7. Prerequisite check (operational, not code): confirm in Slack that the
   manager accounts really are workspace admins and that the workspace's
   app-approval setting does not route their installs into "Requests".

---

## F3 — Structured summary bullets (significance / innovation / commercial potential)

### What exists

- `opportunity_assessments.key_points` is JSONB, `Mapped[list | None]`
  (`src/models/opportunity.py:69-73`; widen to `list | dict | None`), written
  by `_persist_assessment` only when `isinstance(key_points, list)`
  (`simulation.py:4554-4555`) — a dict is **silently degraded to NULL
  today**. `tests/integration/test_assessment_narrative_fields.py:136-170`
  pins the *string* → NULL case; the dict case is untested.
- `key_points` has exactly two readers (the two templates below);
  `assessment_headline.py`, `review_bot.py`, `interview_transcript.py` and
  the export paths never touch it.
- Prompt contract item 7, `prompts/roles/scout_hub/phase4-thread-reply.md:236-241`:
  three to five bullets ≤160 chars, "array of strings"; skeleton `:294`
  `"key_points": []`. `tests/unit/test_rubric_prompt_sync.py:278` asserts
  `skeleton["key_points"] == []`.
- Render: `templates/admin/_assessments_body.html:294-298` (cards) and
  `templates/admin/_assessment_detail_body.html:91-99` (detail), both
  `{% for point in a.key_points %}`. Iterating a dict in Jinja yields keys —
  unchanged templates would print the three key names as bullets.
- Rubric dimensions (`prompts/rubric/blackbird-rubric.toml:239-291`) do not
  map cleanly: innovation ≈ `differentiation_unmet_need`, commercial ≈
  `venture_potential` + `translational_path`, significance has no single
  home. The rubric enforces exactly six dimensions at import — **do not
  implement this by touching the rubric**.

### Design

1. **Schema**: `key_points` becomes an object with fixed keys
   `{"significance": [1-2 strings], "innovation": [1-2 strings],
   "commercial_potential": [1-2 strings]}`. Same column, no migration;
   JSONB stores either shape. Existing list rows stay lists and render as
   today (`{% if a.key_points is mapping %}` branch).
2. **Prompt**: rewrite item 7 and skeleton `:294` to the object shape with
   the same ≤160-char rule per bullet; bump `role.toml` to `1.3.0`; run
   `sync_prompt_set_docs.py`.
3. **Write path**: accept `list` (legacy) or `dict` with exactly those three
   keys whose values are lists of strings; anything else → NULL with WARNING
   (keep A4/A20: warnings, never drops). Replace `_KEY_POINTS_MIN/MAX`
   (defined `simulation.py:9115-9116`, **used** at `:4516-4521` — that
   branch is the one to rewrite) with a per-bucket 1-2 check.
4. **Read path**: cards render three labelled groups (bold label, then
   bullets); detail "Key points" block the same. Order fixed: significance,
   innovation, commercial potential.
5. **Tests to update**: `test_rubric_prompt_sync.py:278` (skeleton shape),
   `test_assessment_narrative_fields.py` (dict round-trip; string still →
   NULL; legacy list still stored), `test_assessment_detail_page.py:1425-1462`,
   `test_opportunity_assessment_persistence.py:1500` fixture.
6. Deploy hazard (CLAUDE.md 0043 box): prompt and agent image must ship
   together — prompt-only leaves every new row NULL via the shape gate.

---

## F4 — Admin impersonating a user may leave reviews as that user

### What exists

- `get_current_user` returns the impersonated user tagged `_is_impersonated`
  and `_real_admin` (`src/dependencies.py:113-127`).
- Every `/reviews` handler calls `_refuse_impersonation` → 403
  (`src/routers/reviews.py:48-52`, seven call sites). Template hides the
  write half under `{% set can_write = not impersonation_banner %}`
  (`_assessment_detail_body.html:548`) and shows a notice (`:571-576`).
- Tests pin the refusal: `test_reviews_router.py:360`
  (`test_an_impersonating_admin_is_refused`),
  `test_assessment_review_ui.py:268` (read-only card + notice),
  `test_reviewer_role.py:359`.
- **This reverses an operator decision recorded 2026-09-09** (design N1 and
  A15, `docs/plans/2026-09-09-reviewer-assessment-ui-design.md:53, :664`):
  "A review must be attributable to the real human". The plan records the
  reversal explicitly.
- `assessment_reviews.reviewer_user_id` and `assessment_review_events.
  actor_user_id`/`actor_name` (`src/models/review.py:78, :149-154`) have no
  "on behalf of" column.

### Design

1. Remove `_refuse_impersonation` from feedback submit/edit/delete and
   status; **keep it** on `assign`/`unassign` and on prompt-suggestion status
   (staff-management actions, not "review as the user"). Confirm this split.
2. Attribution: the review is recorded against the impersonated user (that
   is the request). Add nullable `assessment_reviews.recorded_by_user_id` and
   `assessment_review_events.recorded_by_user_id` (migration) set to
   `_real_admin.id` when impersonating, else NULL, plus a `logger.warning`
   naming both ids. Render "(entered by <admin> while impersonating)" next to
   the reviewer name. Without this the audit trail silently loses who typed.
3. Template: `can_write` becomes `True` for an impersonating admin; replace the
   notice with a short amber line "You are reviewing as {impersonation_banner.name}"
   — **not** `current_user.name`, which `manager.py:123`/`admin.py:154` swap
   to the real admin under impersonation.
   `get_review_user` must still pass: it does for an impersonated
   manager/reviewer/admin, and **403s for an impersonated PI** — that stays.
4. Tests: invert `test_an_impersonating_admin_is_refused` into "records
   reviewer_user_id = impersonated, recorded_by = admin" on **both** the
   review row and the `assessment_review_events` row (status action); update
   `test_impersonating_admin_sees_read_only_card`; keep assign/unassign 403.
5. Docs: N1/A15 amendment note in the 2026-09-09 design doc; CLAUDE.md line
   about review writes refusing impersonation (implementation-plan `:37`).

---

## F5 — Readability of the assessment detail page

### What exists (measured)

- Styling is Tailwind Play CDN with defaults (`base.html:9`): system sans
  stack, 16px root. The detail body uses `text-xs` (12px/16px) **62 times**
  and `text-sm` (14px/20px) 30 times; body copy including the pitch, key
  points, rationale and consult text is 14px or 12px.
- Content column is `max-w-5xl` (64rem = 1024px) on both wrappers
  (`templates/manager/assessment_detail.html:33`, admin twin). At 14px that
  is roughly 130-150 characters per line for the prose sections.
- Secondary text uses `text-gray-400` and `text-gray-500` on white in many
  places (`_assessment_detail_body.html:72, :527, :532, ...`).
- Sections: header → "In one minute" pitch → Key points → The ask → tool
  chips → verdict/score → panel state → `<details>` for gating, rationale,
  dimension scores → Human review → `<details>` timeline. Long sections are
  already collapsed.

### Literature findings

- Line length: 45-75 characters per line, ~66 optimal (Bringhurst);
  Dyson & Haselgrove 2001 found ~55 CPL best supported comprehension at
  normal and fast speeds.
- Size: Bernard & Mills 2000 found 12pt read reliably faster than 10pt;
  14pt better for older adults; 16px is the common accessible floor for body
  text. 12px (`text-xs`) body copy is below every guideline surveyed.
- Spacing: WCAG 1.4.12 requires content survive line-height 1.5, paragraph
  spacing 2×, letter spacing 0.12em; 1.5 line-height is the practical body
  default.
- Contrast: WCAG 1.4.3 needs 4.5:1 for normal text; Tailwind `gray-400` on
  white fails (≈2.5:1); `gray-500` ≈ 4.6:1 is borderline; use `gray-600+`
  for anything meant to be read.
- Reading behaviour: NN/g eye-tracking — users read ~20-28% of words, scan in
  an F-pattern; chunking, meaningful subheadings, one idea per paragraph,
  front-loaded conclusions and bullets improve pickup.

### Design

1. Prose measure: wrap the narrative sections (pitch, key points, the ask,
   rationale) in `max-w-prose`-class containers (~65ch), keep tables and the
   timeline at full width.
2. Sizes: body prose `text-base` (16px) with `leading-relaxed` (1.625);
   metadata/labels `text-sm`; reserve `text-xs` for chips only. This is a
   sweep of the 62 `text-xs` uses — mechanical, template-only.
3. Contrast: replace `text-gray-400` for readable text with `text-gray-600`;
   keep gray-400 only for decorative separators.
4. Structure: promote the "In one minute" pitch to the first thing under the
   title; keep the verdict chip visible above the fold; leave `<details>`
   collapsing as is.
5. Tests: `test_assessment_detail_page.py` ordering test keys on
   `html.index()` of section labels — labels unchanged, so it stays green;
   add no snapshot. Manual check by screenshot at 1280 and 390px widths.
6. Optional: a `prose` CSS block in `extra_head` of both wrappers rather than
   per-element classes, to keep the diff reviewable.

---

## F6 — External labels for every select; "Proposal merit" → "Overall rating"

### What exists (`templates/admin/_assessment_detail_body.html`)

- Add-feedback form `:731-747`: `<label>` "Proposal merit (1 = weak … 5 =
  strong)" (`:733`) **and** a placeholder `<option value="" disabled selected>
  Proposal merit</option>` inside the select (`:735`). Neither label carries
  `for`/`id`. `feedback_mode` select has a placeholder option "Mode" and **no
  label** (`:741-745`).
- Edit form `:696-710`: same label text (`:698`), no placeholder option;
  `feedback_mode` select unlabelled (`:705`).
- Dimension selects (macro `:514-545`): six `<select name="dim_{key}">` with
  the dimension title to the right as a div, not a `<label for>`.
- Assign form `:777-782`: `assignee_user_id` select, no label.
- Pinned text: `tests/integration/test_assessment_review_ui.py:133` asserts
  `"Proposal merit" in html`. Helper copy `:560-563` says "The overall
  proposal-merit score is required".
- The 2026-09-04 design (`docs/plans/2026-09-04-...design.md:122-163`) named
  the concept "proposal merit" and instructed avoiding certain substrings in
  helper text because `test_human_review_card_sits_between_...` keys on
  `html.index()` of "Dimension scores", "Human review", "Interview timeline".

### Design

1. One pattern everywhere: `<label for="{id}" class="...">Text</label>` then
   `<select id="{id}">`. The label text moves OUT of the option, but the
   score and mode selects **must keep an empty-valued first option**
   (`<option value="" disabled selected>—</option>` or similar) plus
   `required`: `reviews.py:166` declares `score: int = Form(...)`, and with
   no blank option the browser pre-selects `1`/`learn`, so an untouched form
   would record a rating of 1 and enqueue a review-bot LLM job. Blank stays
   a real answer for the dimension selects as today.
2. Rename: label "Overall rating (1 = weak … 5 = strong)"; remove the
   "Proposal merit" placeholder option; label `feedback_mode` "Feedback
   mode"; label each dimension select with `for` pointing at it (visually
   the existing title, made a `<label>`); label assignee select "Assign
   reviewer".
3. Ids must be unique per form: prefix with `add-`/`edit-{review.id}-`.
4. Update `test_assessment_review_ui.py:133` to "Overall rating"; add an
   assertion that no `<option>` text inside a select repeats its label and
   that the score select still has an empty-valued first option; keep
   the helper copy wording ("overall proposal-merit score") or change to
   "overall rating" — recommend changing for consistency, avoiding the three
   forbidden substrings.
5. Not in scope: the review-bot prompt (`prompts/review-bot.md`) still calls
   the number "proposal merit"; leave it, since that is the model-facing
   definition, unless you want the term retired everywhere.

---

## F7 — Login page: drop collaboration wording, add Blackbird Laboratories logo

### What exists

- `templates/login.html:9-11`: "CoPI" / "Research Collaboration Platform" /
  "Connect, collaborate, and discover synergies across Scripps Research";
  `:52` "Discover collaboration opportunities with other Scripps
  researchers"; `:2` title "Sign in — CoPI".
- `templates/base.html:6` title default "CoPI — Research Collaboration";
  `:49-50` nav brand "CoPI" + "Research Collaboration"; `:182` footer
  "CoPI — Research Collaboration Platform • Scripps Research".
- **No logo asset exists anywhere in the repo** (`static/` holds only
  `email/welcome_agent.png` and `js/markdown.js`). You must supply the file
  (SVG preferred; PNG @2x acceptable).
- Static is served by the app with no-cache headers (`src/main.py:366-378`),
  so no nginx change and no cache-bust — but `static/` is **baked into the
  image** (`Dockerfile:17` `COPY . .`; the app bind-mounts only `profiles/`
  and `prompts/`), so the logo needs `$DC build blackbird-app` + recreate.
- No test pins the login copy. `tests/unit/test_email_templates.py:31` pins
  the **email** footer tagline separately — untouched.
- No brand/site-name setting exists in `src/config.py`.

### Design

1. Add `static/img/blackbird-laboratories.svg` (you provide the artwork).
2. `login.html`: replace lines 9-11 with the logo `<img>` (alt "Blackbird
   Laboratories", explicit width/height) and a one-line subtitle without
   collaboration wording, e.g. "Sign in to the Blackbird review platform";
   replace `:52` with a non-collaboration step (e.g. "Your lab agent joins the
   Blackbird Slack workspace").
3. `base.html:6`, `:50` and `:182` ("Research Collaboration") render ON the
   login page, so removing collaboration wording from the login page
   necessarily includes them. Change all three; email footer stays
   (`tests/unit/test_email_templates.py:31` pins it separately).
4. Tests: add one assertion that `/login` contains the logo `alt` and that
   the full response does not contain "collaborat" (only valid with step 3).

---

## Open questions for the operator

1. F1: confirm the interpretation of "interview question type" (phase-4
   stage by role) and "specialist response type" (domain × verdict signal).
2. F2: confirm managers may also **activate** (not only provision), and that
   the D1 reversal is intended.
3. F4: confirm the attribution model (review under the impersonated user,
   admin recorded in a new `recorded_by` column) — this reverses N1/A15.
4. F6: retire "proposal merit" in the helper copy and the review-bot prompt
   too, or only in the label?
5. F7: logo file, and whether nav/footer/title branding changes with it.

---

## Audit log (2026-09-10)

Fresh-context reviewer verified every file:line in this document. Corrections
applied: F6 placeholder removal would have recorded score=1 by default;
F2 missed `test_manager_pi_writes.py:330`, the `current_user` template trap and
the three error redirects in the callback; F4 banner name source; F1 lacked a
coalesced "unclassified" bucket and the consult `log_meta` site; migration
pins under-specified (four preflight lists); revision numbering collision;
F7 needed the base.html change and the app image rebuild; F3 cited the wrong
enforcement line and an over-strong test claim. Verified as stated: 62
`text-xs` / 30 `text-sm` / 31 `text-gray-400` uses; `max-w-5xl` on both
wrappers; Tailwind Play CDN with no config override; `key_points` has two
readers only; session cookie `same_site="lax"` so the Slack callback GET keeps
the session; `test_json_none_as_null` unaffected.

---

## Decisions (operator, 2026-09-10)

1. F1: interpretation confirmed — interview stage (EXPLORE/DECIDE/CONCLUDE)
   by role, specialist domain × verdict signal, plus call-kind split.
2. F2: managers may provision AND activate agents (no override checkbox).
   D1 of the 2026-08-21 manager-PI-controls design is reversed.
3. F4: the review is recorded under the impersonated user; the real admin is
   recorded alongside in `recorded_by_user_id`. N1/A15 (2026-09-09) reversed.
4. F6: "proposal merit" is retired everywhere — label, helper copy, model
   docstring, and `prompts/review-bot.md` — in favour of "overall rating".
5. F7: logo located at https://blackbirdlab.org/wp-content/uploads/2025/09/Logo.svg
   (114×80, white fill; recoloured to `currentColor` and rendered in the brand
   dark `#353A41`, which is the site's header colour). Saved as
   `static/img/blackbird-laboratories.svg`. nav/footer/title branding change too.

---

## Execution notes (2026-09-10)

- **Migration numbering as executed**: 0044 = F4 (`assessment_reviews` /
  `assessment_review_events`.`recorded_by_user_id`), 0045 = F1
  (`llm_call_logs.thread_phase`, `.message_ordinal`), 0046 = F2
  (`slack_app_provisions.initiated_by_user_id`). The plan documents assigned
  0045 to F2 and 0046 to F1; the two were swapped at execution time. Single
  alembic head is `0046`.
- **Task 12 executed**: CLAUDE.md carries a combined `0044`/`0045`/`0046`
  deploy box and the corrected impersonation policy for review writes; the
  full `./scripts/ci.sh` gate was run on this branch.
