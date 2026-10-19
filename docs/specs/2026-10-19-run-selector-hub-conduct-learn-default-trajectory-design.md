# Run selector, hub conduct rule, Learn default, interview trajectory page — design

- **Status:** APPROVED by the owner 2026-10-19 (§6), after two adversarial audits the same day
  (one on §2, one on §1/§3/§4; every finding folded in) and a builder check. Uncommitted.
- **Requested by:** the owner, 2026-10-19 (four changes, quoted in each section).
- **Measured:** every production number below was read from the `copi-blackbird` Postgres on
  2026-10-19 with read-only `SELECT`s. Code references are to `blackbird@819b4348`.

The four changes are independent and can ship separately. Recommended order, smallest and
least risky first: §3 (Learn default) → §1 (run selector) → §2 (hub conduct rule) → §4
(trajectory page). §1 and §3 are small template changes; §2 is a prompt-set release under the
prompt freeze; §4 is a project with a migration.

---

## 1. Run selector on the Assessments list

> "improve visibility and visual appeal of the run selector on the assessments list page"

### 1.1 What exists

- `templates/workspace/assessments.html:11-25`: one GET form at the top right of the header
  holds **Run**, **Sort**, **Assignment** and **Lab** and one **Apply** button. The run label
  is `text-sm text-gray-500` "Run:"; each option reads
  `{started_at|ts} ({status}, {n} stored across all labs and assignments, rubric X) — current`.
  On the help screenshot (`help_assets/shots/assessments-list.png`) the select is a faint,
  borderless 700-pixel line wedged beside Sort, and the one run-scope sentence sits two lines
  lower ("Showing the latest run only — verdict rows survive restarts. view all runs.").
- Data: `list_assessments` (`src/services/directory.py:836`) returns `runs` (every run, newest
  first, `runs_ordered`), `assessment_counts_by_run` (all stored rows, across labs and
  assignments), `selected_run_id`, `show_all_runs`. `_resolve_run_selection`
  (`directory.py:513`) falls back silently to the newest run.
- Production: **25 runs; 14 have 0 stored assessments**; three rows from 2026-07-24, 08-07 and
  08-14 still carry `status = 'running'` with no `ended_at` although nothing runs. The menu
  therefore shows 26 options, most of them empty, three of them mislabelled.
- Reviewers see this page. `presentation.REVIEWER_SCHEMAS["SimulationRun"]` admits only
  `id, started_at, ended_at, status, config`, and `config` is cut to `rubric_version`
  (`src/web/presentation.py:257`); `finalized_at` and `held_at` are not reviewer-visible.
  `tests/integration/test_workspace_data_contract.py::test_reviewer_run_config_is_projected_before_template_and_absent_from_html`
  pins that.
- Pinned text: `test_workspace_data_contract.py:80,86` assert
  `'4 stored across all labs and assignments'`; `tests/e2e/ui_audit/journeys_phase5.py:144`
  reads `#assessments-run-select option:checked` and that same phrase.
- Help: `templates/help/pages/assessments.html` "Filters" describes the Run menu; shot
  `assessments-list` (and every shot whose hashed sources include the page) goes stale.
- The Discussions page has its own, plainer run select (`templates/workspace/discussions.html:6`).
- **Why it looks faint:** Tailwind's preflight sets `border-width: 0`, no forms plugin is
  installed (`tailwind.config.js`), and every select on the page (and on Discussions) carries
  `border-gray-300` without `border`, so none of them has a border at all.
- "Stored" is load-bearing: the per-run count is "the dropdown's honesty device"
  (`directory.py:896-898`) that tells a run with no rows from one whose rows exist only in the
  offline backup after the 2026-08-27 purge.
- Liveness is readable: `simulation_process_status` (`src/models/simulation_control.py:51-67`)
  holds the engine's `simulation_run_id` and an `updated_at` heartbeat (one-row read).
  `held_at` is projected for nobody (absent from `SCHEMAS["SimulationRun"]`); `finalized_at`
  is staff-only.

### 1.2 Proposal (bordered run bar, decided §6 D5)

1. **Its own row.** Run leaves the right-hand filter cluster and becomes a full-width "run
   bar" directly under the title, above the description line: a white bordered card with
   the selected run's summary — start date and time, a status badge, the assessment count,
   the rubric version and a **Current** badge on the newest run — and the run control
   itself, larger and clearly bordered. Sort, Assignment and Lab stay where they are.
2. **Readable options.** Each option shrinks to `16 Oct 2026 · 12:48 UTC — 3 stored ·
   rubric 3.5.0 (current)`. The scope disclosure the old label carried ("across all labs and
   assignments") moves to one line under the control, said once; "stored" stays, so the
   empty-state sentences at `_body.html:648,651` stay true.
3. **Empty runs out of the way.** Two `<optgroup>`s: "Runs with stored assessments" and "Runs
   with no stored assessments". Nothing is hidden or deleted; every run stays selectable.
4. **Two forms, each carrying the other's state.** The run bar is its own GET form with its
   own **Show** button and hidden `sort`, `lab`, `review`, `assignment`; the Sort/Assignment/Lab
   form gains hidden `run_id` and keeps `review` (without it, Apply would silently fall back to
   the newest run, `directory.py:530-531`). The fields are written out in each form: the
   `qs_filters()` macro is local to `_body.html`, which is included after the header, and it
   also emits `surface` and a duplicate `run_id`. A pending change in the *other* form is
   discarded on submit; help says so. No auto-submit on change: navigating on a select's
   `change` is a change of context (WCAG 2.2 SC 3.2.2, confirmed against the W3C
   Understanding page), and the CSP forbids inline handlers.
5. **Status (decided, §6 D6).** The badge says **Live** for the run `simulation_process_status`
   names while its heartbeat is fresh, and otherwise the stored status, with **Finalized** or
   **Held** where `finalized_at` / `held_at` is set.
6. **Reviewers see the same badges (decided, §6 D6).** `finalized_at` and `held_at` join
   `REVIEWER_SCHEMAS["SimulationRun"]`, and `held_at` joins `SCHEMAS["SimulationRun"]`, which
   lacks it today. The Live flag is computed in the service and passed as a plain value.
   `test_reviewer_run_config_is_projected_before_template_and_absent_from_html` keeps pinning
   the `config` cut.
6a. **Repair the three stale `running` rows (decided, §6 D6).** Runs `2fd6e53a` (07-24),
   `2485863a` (08-07) and `e2b5a03e` (08-14) have `status = 'running'`, no `ended_at`, no
   `agent_messages`, and a last `llm_call_logs` row on their start day. A one-off script,
   dry-run by default, sets `status = 'stopped'` and `ended_at` = the run's last
   `llm_call_logs.created_at` (else `started_at`), only for rows still `running` that are not
   the run `simulation_process_status` names and whose last activity is older than a day. It
   refuses while an engine holds the engine lock, never touches `finalized_at`, `held_at` or
   any other table, and never deletes. Readers of `status == 'running'` checked:
   `simulation_view.py:455,533` (caching and live stats of a non-running run — correct once
   stopped), `assessment_chat.py:426` (whether an assessment's run can still supersede it —
   the three runs have no assessments, so nothing changes). The status enum is
   `running/completed/stopped`.

### 1.3 The same run bar on Discussions (decided, §6 D5)

- One shared partial (e.g. `templates/_run_bar.html`) used by Assessments, Discussions and
  the §4 Interviews page, parameterised by route, the per-run count and its noun (stored
  assessments; interviews), whether "All runs" is offered, and the hidden fields to carry.
- Discussions (`templates/workspace/discussions.html:6`): today "All Runs" is the first
  option and the filter form already carries `run_id`. The bar keeps "All Runs" (still refused
  above `DISCUSSIONS_ALL_RUNS_MAX` threads), counts threads per run, and carries
  `channel_filter`, `status_filter` and `agent_filter` (multi-valued) as hidden fields; the
  pager and the admin Export buttons keep working from the filter form.
- Help: `discussions-activity` (`discussions` section) and manager tutorial step 10, with
  their shots.
7. **Every select on the page gets `border`** (Sort, Assignment and Lab too), so the controls
   read as controls; the run select keeps `min-w-0 max-w-full` (the 375 px no-scroll check,
   `journeys_phase5.py:150`).

Constraints the build must keep: `id="assessments-run-select"` and a `<label for>` (the
rendered-page gate wants named controls; the e2e journeys read the id and
`form:has(#assessments-sort-select)`); no `text-gray-300/400`; no inline script; badge colour
classes as literal strings (a variable-built class must join `CONSTRUCTED_SITES` in
`tests/unit/test_compiled_css.py` and the safelist); new utility classes need
`scripts/build_css.sh` and the rebuilt `static/css/app.css` committed (CI's drift check), and
any CSS rebuild marks every help shot stale, so plan a full recapture on a workstation as the
last step before deploy. Rewrite the pins at `test_workspace_data_contract.py:80,86-87` and
`journeys_phase5.py:145` explicitly. Help: the Filters list and the figure caption ("the
filters at the top right") in `templates/help/pages/assessments.html`, reviewer tutorial step 3
(`03-assessments-list.html`). Deploy is app-only: no migration, no agent rebuild.

---

## 2. BlackbirdBot must not assess the PI bot's personality or behaviour

> "Hub bot shouldn't be assessing the PI bot personality or behavior — example: 'a PI who
> surfaces her own design flaws unprompted'; 'a PI who revised his own pre-specified effect
> size when the statistical unit was challenged'"

### 2.1 Where the examples come from

- "a PI who surfaces her own design flaws unprompted": `score_rationale` of assessment
  `ff90409f` (deutsch, run `e91c35df`, 2026-10-16), copied from the **talent** specialist's
  opinion ("Behavioral evidence of a PI who surfaces her own design flaws unprompted…"), which
  answered the hub's consult question "How do I read this PI's execution probability and
  candour?".
- "a PI who revised his own pre-specified effect size…": `score_rationale` and
  `dimension_rationales.scientific_credibility` of `00ef76db` (gray, same run).

### 2.2 How widespread (pattern match; a few false positives are possible)

- **59 of 64** rubric-3.x assessments carry interview-conduct language in some sidecar field:
  `strengths` 38, `rationale` 36, `score_rationale` 7, `dimension_rationales` 3,
  `key_points` 2. Every run from 08-27 to 10-16 is affected (10-08: 16 of 19).
- Typical strengths: "The lab volunteered every weakness a reviewer would find…";
  "Exceptional intellectual honesty under questioning… raises confidence in everything it
  did assert"; "candour materially raised confidence in the record".
- It reaches the visible Slack replies too: 26 of 252 hub interview replies since 09-20, e.g.
  "That candour is why I can score the evidence at all."; "two corrections volunteered
  unprompted is worth more to a screen than any extra figure."
- It reaches the panel: 15 of 149 talent consult *questions* since 09-20 ask about candour or
  concessions, and talent and scientific opinions answer in kind ("That candor is itself
  evidence of rigor").

### 2.3 Root cause

1. **The behaviour is manufactured by the PI bot's own prompt.** `pi_lab` is told to
   "Volunteer the limitations before you are asked" (`src/agent/thread_guidance.py:65`,
   `_PI_LAB[DECIDE]`), "Volunteer the limitation before it is found" (`prompts/phase4-thread-reply.md:31`)
   and the same in `prompts/agent-system.md:285`. Every lab bot runs that one prompt set, so
   the hub is crediting the prompt, uniformly, as if it were a trait of each PI.
   The hub's own guidance asks for the second example: `_SCOUT_HUB[DECIDE]` tells it to
   "refine and re-scope the experiment across a turn or two" with the PI
   (`thread_guidance.py:143-147`), and then the hub credits the agent for doing so.
2. **Nothing in the hub's prompts separates what the agent said from how it said it.**
   Sidecar item 11 invites "a fact about the team" (`prompts/roles/scout_hub/phase4-thread-reply.md:494-497`);
   the rubric's `team_executability` anchor and `[stage_bar.talent]` say "PI credibility"
   (`prompts/rubric/blackbird-rubric.toml:313,408`); other anchors reward acknowledgement —
   "contradictory literature is unacknowledged" (`:270`), "ignorance of the risk scores low"
   (`:284`, `:388`), "a claim whose reach is stated honestly" (`:404`), and
   `prompts/specialists/technologic.md:20-21` "would the team recognize a fundamental
   limitation"; `agent-system.md:123-124` says "A PI who labels their own work
   *[Speculative]* is being useful, not weak", and `:157` calls a pitch "a strong starting
   signal" — both credit behaviour every lab bot is instructed to perform.
3. **Everything says "the PI".** `identity.md:3`, `agent-system.md:5,146`,
   `thread_guidance.py:98`, the consult tool description (`src/agent/tool_definitions.py:96-107,135-136`,
   "'talent' (execution probability…" — the source of the hub's "How do I read this PI's
   execution probability and candour?"), and the code-built consult header every specialist
   receives, "## What the PI has said" (`src/agent/tools.py:747`, pinned by
   `tests/unit/test_specialist_no_anchoring.py:133` and C27(b)). The models are never told
   that "the PI" in the thread is an agent speaking on shared instructions.
4. **The panel is asked to judge it.** The hub puts candour questions to `talent`, and
   `prompts/specialists/talent.md` gives no reason to decline them.

The effect is not only tone: conduct is used to *raise confidence in the science*, so it biases
scores upward uniformly across labs.

### 2.4 Proposal: scout_hub 1.11.0 → 1.12.0 plus the eight personas

Revised after the §2 audit (2026-10-19). Prompt files only: no code-built prompt text changes,
so no agent image rebuild and no C27(b) code exception.

**a. Hub system prompt** (`prompts/roles/scout_hub/agent-system.md`, Core Principles, new 8):

> 8. **Judge the work, not the agent's conduct.** The PI is represented in every interview by
>    the lab's AI agent; wherever these instructions, your tools or the panel say "the PI
>    said" or "ask the PI", they mean that agent. How it conducts itself — volunteering or
>    conceding a limitation, correcting or re-scoping its own plan when challenged, its
>    candour or tone — comes from instructions every lab shares. It is not evidence about the
>    PI, the team or the science: never credit or penalise it, never describe the PI's
>    personality, temperament or character, and never ask a specialist to judge it.
>
>    What the agent says *is* evidence: a limitation, a conflict of interest, another
>    commitment or a team member it names is a fact about the work, weighed the same whether
>    it was volunteered or drawn out by your question. It is still something the PI told you,
>    so the confidentiality rules for a volunteered limitation still decide where you may
>    write it. Where the rubric asks whether risks are *identified* or contradictory
>    literature *acknowledged*, judge what the lab knows and plans to test, not whether it
>    raised the point before you asked. In `team_executability`, "PI credibility" means the
>    PI's and lab's record of executing comparable work.

Same file: `:123-124` → "A *[Speculative]* label is a claim about the evidence, not a weakness;
a *[High]* label is a checkable claim about replication, so check it."; `:157` "a strong
starting signal" → "A pitch tells you which idea the lab wants screened — not a reason to be
softer on it."

**b. Phase-4 prompt** (`prompts/roles/scout_hub/phase4-thread-reply.md`):
- "The evaluation panel": add "Ask them about the work, never about how the lab agent has
  conducted the interview (Core Principle 8)."
- Before the sidecar items: "No field below credits or criticises how the lab agent conducted
  the interview (Core Principle 8). Recording what it did not answer — a question deferred to
  its PI, a point left open — is a fact for staff, not a judgement of its conduct."
- Item 11: "a fact about the team or market" → "a fact about the team or market, never how
  the lab agent behaved in this interview" (as built 2026-10-19: the first draft's "the team's
  record" was dropped after the audit, because Principle 8 counts a named team member as a
  fact about the work).
- The reply rules ("Replies are 2-4 sentences…", `:127`): add "Respond to what the agent said,
  not to how it said it: no praise or criticism of its candour or concessions."

**c. All eight specialist personas** (`prompts/specialists/*.md`; the acknowledgement anchors
reach chemistry and technologic through their stage bars too), one paragraph:

> The section headed "What the PI has said" quotes the lab's AI agent, which speaks for the
> PI on instructions every lab shares. What it says — a limitation, a conflict of interest,
> another commitment, who is on the team — is evidence like any other. That it volunteered or
> conceded it, and its candour or tone, are not: they say nothing about this PI or team, so
> never credit or penalise them.

`talent.md` only, also: "In your stage bar, 'PI credibility' means the PI's and lab's record of
executing comparable work."; and `:60-61` "a hiring manager or program officer" → "a program
officer".

**d. Not changed:** the rubric (the Principle-8 and talent glosses carry "PI credibility" and
the acknowledgement anchors without a rubric exception — decided, §6 D7); every code-built prompt string
(`tools.py:747` header, `tool_definitions.py`, `thread_guidance.py` — andrewsu's sign-off rule
covers those strings); every `pi_lab` prompt (volunteering limitations is useful *content*, and
`_PI_LAB` is pinned by `test_agent_turn_gm.ambr`); the assessment-chat and review-bot prompts
(they already say "the lab's agent" and invite no conduct judgement); stored assessments (stamp
and keep — the 59 rows stay as written).

### 2.5 Mechanics

- **Freeze.** B22/B24/C27 (`docs/specs/2026-09-29-audit-remediation-design.md`) forbid any
  prompt change except by owner-approved exception, recorded as a numbered decision
  (precedents: O1/B25 for scout_hub 1.10.0, D1/§8 of the 2026-10-05 spec for 1.11.0, Q17 for
  pi_lab 1.2.0). B25 keeps the personas frozen, so the exception must name: the scout_hub
  prompt set, the eight persona files, and one reviewed regeneration of the
  `test_prompt_freeze_gm` entries that embed them — `test_hub_reply_final_text_truncation_retry_gm`,
  `test_hub_reply_forced_final_and_its_retry_gm`,
  `test_hub_reply_tool_error_and_truncated_variants_gm`,
  `test_hub_reply_with_a_gathered_consult_round_gm`,
  `test_review_bot_request_over_a_superseded_verdict_gm` (the only one with the role.toml
  version), `test_run_start_marker_gm` (the only one with the hub hash) and, because every
  persona changes, `test_specialist_consult_request_gm` (clinical). Each `.ambr` diff must be
  exactly the prompt diff plus version and hash stamps, checked line by line.
  `test_agent_turn_gm.ambr` and `test_profile_pipeline_gm.ambr` do not change.
- `prompts/roles/scout_hub/role.toml` `version = "1.12.0"`; `tests/unit/test_headline_contract.py:89`
  pins `'version = "1.11.0"'` and moves with it; run `scripts/sync_prompt_set_docs.py` (it
  syncs the hub files and all eight personas into `docs/specs/2026-08-07-hub-bot-prompts.md`);
  the header of `test_prompt_freeze_gm.py` records the exception; a "scout_hub 1.12.0"
  section in `docs/operations/blackbird-hub.md`.
- **Persona edits are not stamped.** `prompt_set_stamp` hashes only `prompts/roles/<role>`
  (`src/agent/roles.py:203-219`); persona hashes are only drift-checked
  (`src/agent/prompt_snapshot.py:111-115`). Commit the persona edits with the 1.12.0 bump and
  say in the release notes that 1.12.0 implies the new persona text.
- **Deploy.** `prompts/` is bind-mounted into app, worker and agent, so no image rebuild.
  The worker's review bot reads prompt files per job and sees 1.12.0 as soon as the files
  land, while judging 1.11.0 assessments. The engine applies it at its next start, which
  must be **FRESH**: the version bump, and FRESH also archives working memory, which already
  carries conduct wording (`profiles/memory/blackbird/public.md`, "PI conceded …") that a
  resume would inject into every hub prompt. Never start a run as part of the deploy.

### 2.6 Verification

- Deterministic: `./scripts/ci.sh` (prompt-sync, doc-sync, the headline contract, the freeze
  suite after the reviewed regeneration).
- **Panel calibration ladder** — the repo's required check on any persona change
  (`scripts/panel_calibration_ladder.py:12`; acceptance criteria in §7.2 of
  `docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md`), about 48 real Opus calls,
  not in CI. Run per domain, talent first, so a shift can be attributed; talent's STRONG-tier
  `adequate` count must not fall. Spend approved (§6 D4).
- Behavioural (spend approved for (i), §6 D4): (i) the cheapest sharp test — replay the 15 stored candour consults
  to talent from `llm_call_logs` against the new persona; (ii) replaying a concluding turn is
  partial, since its history already holds 1.11.0 replies that praise candour; (iii) measure
  the next live run's sidecars, replies and consults with a recorded query (the §2.2 terms
  plus "forthright", "transparent", "self-critical", "intellectual honesty"), read the hits
  by hand, and compare with today's baseline (59/64 rows, 26/252 replies, 15/149 talent
  questions). The same run is checked for the opposite failure: volunteered limitations
  leaking into `<slack_message>` or reviewer-visible fields.
- **Pass bars and the stop rule.** The replay passes when none of the 15 talent opinions
  credits or penalises candour, concessions or volunteering, read by hand, while each still
  answers the domain question it was asked. The ladder passes on §7.2 of the vocabulary spec,
  with talent's STRONG-tier `adequate` count not lower than before. On a failure the persona
  paragraph is revised once and only the failing check or domain is re-run, within D4; a
  second failure stops §2 and goes back to the owner, who decides whether scout_hub 1.12.0
  ships without the persona edits.

---

## 3. Feedback mode defaults to Learn

> "make the default for human review entries on assessments 'learning'"

- The two *add* forms render the mode select with a disabled "—" placeholder preselected, so a
  reviewer must choose: the list page's Quick scoring
  (`templates/assessments/_body.html:627`) and the detail page's add form
  (`templates/assessments/_detail_body.html:1238-1242`).
- Change: drop the mode placeholder in both and preselect **Learn**; keep `required`. The
  overall-rating placeholder stays (a score must still be chosen). The *edit* form
  (`_detail_body.html:1196-1198`) keeps showing the stored mode.
- Server unchanged: `feedback_mode` stays `Form(...)` and is validated against
  `("learn", "log_only")` (`src/services/assessment_reviews.py:88`); a client that omits it
  still gets an error rather than a silent default.
- Effect: more rows become eligible for the manual **Generate suggestions** press; nothing
  spends on submit (no automatic enqueue since 2026-09-14). All 8 production reviews to date
  are already `learn`.
- Help: `templates/help/pages/assessments.html:252` ("Feedback mode, required: …") and
  `templates/help/tutorials/reviewer/07-recording-a-review.html` say the default; stale shots
  recaptured. Tests: assert both add forms render `value="learn" selected` and no mode
  placeholder.
- Deploy: app-only.

---

## 4. Interview trajectory page

> "add a new workspace page showing a summary of the interview trajectory, approximately one
> line per post and visible to managers and admins"

### 4.1 What exists

- An interview is one Slack thread: the lab's `:bulb:` pitch (`agent_messages.phase =
  'new_post'`, the root) and its `thread_reply` rows, plus the hub's `panel_note` rows (one
  per posted specialist consult: "🧪 Panel · scientific — ⚠️ gap — asked: …").
  `thread_decisions` records how it ended (`outcome`, `closed_by_role`; Discussions uses the
  last by `decided_at`); `opportunity_assessments.thread_id` and `hub_declines.thread_id` hold
  the root's ts. Pre-`0036` assessments have a NULL `thread_id` and link through `slack_ts`
  (as `load_interview_thread` does); pre-`0071` declines have no `hub_declines` row.
- Scale, run `652a6102` (10-08): 20 interviews, ~50 rows each — 1 pitch, ~11 replies (mean
  2,279 characters, max 3,977), ~30 panel notes. A reply over 4,000 escaped characters is
  stored as several rows, so one turn can be two rows (seen in run `e91c35df`).
- **Where post text is readable today:** only the assessment detail page's **Interview
  timeline** (interviews with an assessment; reviewers included) and the PI-scoped
  `/agent/{id}/conversations` views. **Discussions renders no posts**: its rows show status,
  poster, reply count, outcome, the decision's `summary_text` and panel cards, and the run
  detail page loads rows without `content`. So for an interview without an assessment no
  staff page shows any post today.
- Interviews with no `thread_decisions` row: 0 in every run since 09-09; 3 in 08-31 and 2 in
  the two 08-27 runs (measured). They are the interviews still open when a run stopped.

### 4.2 Proposal

- **Route and access.** `GET /workspace/interviews` (`workspace_interviews`) behind
  `get_staff_user` — admins and managers; reviewers, PIs and an admin impersonating a PI get
  403. A `staff_only` nav item "Interviews" in `src/web/navigation.py`. GET only, so the
  manager router's write allowlist is untouched.
- **Summaries only (decided, §6 D2).** The page shows managers one-line summaries of what
  lab agents and the hub said, including interviews with no assessment, which no staff page
  shows today; it never shows a post's full text. Lines can carry a PI's unpublished
  disclosures. Never shown to reviewers, never posted to Slack.
- **What "an interview" is.** A root lab post in the run with at least one `thread_reply`
  from a *different* author in its thread (a split root keeps its own continuation chunks in
  its thread, `src/agent/slack_client.py:871-874`). Unanswered pitches are counted in the
  header, not listed. Measured: 121 interviews, 7 unanswered pitches, 0 pitches whose thread
  holds only their own continuations.
- **What "a post" is.** Consecutive non-panel rows of one thread by the same author less than
  2 seconds apart are one post (no column links split chunks, and recording one would be an
  engine change, which B24 forbids). Measured over every thread on record: all 74
  consecutive same-author pairs are under 2 s apart, and of 957 different-author pairs one is
  2–10 s apart and the rest 10 s or more, so the rule matches every split on record.
- **Page.** A run bar like §1's, but counting **interviews** per run (14 of 25 runs have no
  stored assessments yet may have interviews), with no "All runs" option (Discussions refuses
  "All runs" above 2,000 threads; this page costs ~50 rows per interview). Then the run's
  interviews, newest first: lab, start time, outcome, links to the assessment or decline,
  each a `<details>` holding its trajectory, one numbered line per post, `#n · Speaker ·
  line`. No full text. Rows of one split turn are merged into one post. An assessed
  interview links to its assessment, where the timeline already shows the full posts.
- **Outcomes shown:** verdict (band), hub decline, timeout, abandoned, lab ⏸️, verdict
  dropped (`assessment_drops`), a NULL `closed_by_role` on old rows shown as "closed", **open**
  only for the live run (`simulation_process_status`), and "run ended before a close" for a
  decision-less interview in any other run.
- **Panel lines.** Consults fold into one line per hub turn ("Panel: scientific ⚠️ gap ·
  commercial ⚠️ gap · budget ☑️ adequate"), built from `specialist_consults` (structured,
  includes consults whose note was never posted and the "reply cut off" state, as
  `thread_panel.py:136-158` does for Discussions), queried per run with no 500-row cap;
  parsing `panel_note` text is the fallback only for interviews older than that table.
- **The one line** — model-written, decided (§6 D3):
  - *A — deterministic:* the post's first sentence, clipped; reads `left(content, N)`, not
    whole posts. Free and instant, but often empty ("Thanks — I've read the WSI-vs-glass
    concordance paper…").
  - *B — model-written, with A as the per-post fallback:* one call per **done**
    interview — it has a decision, or its run is no longer live (`finalized_at` set, or
    `ended_at` set and not the run `simulation_process_status` names). The ordered posts go
    out with short ids `P1…Pn` (the precedent's `V1…Vn`), mapped back to each post's first
    row id; the model returns JSON `{"P1": line, …}`, each at most ~160 characters, about
    content only (the §2 rule: no remarks on the agent's conduct). Panel notes are not sent.
    Each line is cleaned as `clean_question` does (invisible and private-use characters
    stripped, links and markup rejected); a valid line is kept per post and a missing or bad
    one falls back to A for that post only.
  - Generation in the worker's idle work on the `assessment_chat_suggestions` precedent: row
    written before the call, at most 3 attempts, 429/5xx retried without spending one, a
    refusal never retried, a daily USD ceiling, at most one generator call per idle tick
    (the worker docstring's promise), its own on/off setting (not tied to
    `assessment_chat_enabled`).
  - Model: `claude-opus-5`, already priced in `llm_pricing.py`, as the chat-suggestions
    generator uses: `src/config.py:402-409` records that Opus 5.5's biology classifier declined
    36 of 37 hub requests in the 2026-09-28 replay, and a refusal is never retried.
  - Migration `0072_interview_trajectories`, additive: `id`, `simulation_run_id` (FK, `ON
    DELETE CASCADE`, index led by it as `tests/unit/test_run_scoped_tables.py` requires),
    `thread_id`, `UNIQUE (simulation_run_id, thread_id)`, `status`, `attempts`, `error_code`,
    `model`, `served_by_model`, `usage`, `latency_ms`, `prompt_sha256_12`,
    `posts_sha256_12` (hash of the post-id set; a mismatch regenerates — at shutdown a
    decision can land before the last posts are flushed), `lines` JSONB, timestamps. Plus the
    alembic chores: `scripts/migrate/preflight.py` (`DEFAULT_TARGET`, `REVISION_ORDER`,
    `SUPPORTED_START_REVISIONS`, `PLANNED_OBJECTS`), the pins in `test_migration_checks.py`
    and `test_harness_smoke.py`, a deploy box in `migration-deploy-notes.md`.
  - New prompt file `prompts/interview-trajectory.md` (a new feature's prompt, outside the
    frozen prompt sets); new settings for the model, the ceiling (added to the finite-value
    check in `config.py`) and the switch.
- **Projection.** The service returns plain dicts and lists only, with joins done in the
  service (`OpportunityAssessment.thread_id` is admin-only in the projection): lines as a
  list of `{n, speaker, line, source}` with every key in `VIEW_FIELDS`. An ORM row or a map
  keyed by ids would raise or project to `{}` (`presentation.py:292,325`).
- **Engine untouched** (B24): generation reads `agent_messages`, `thread_decisions`,
  `specialist_consults` and `simulation_process_status` only.
- **Help and hand-kept lists.** A help section and registry entry with the page's Help link;
  the tab lists in `help/pages/getting-started.html` and `tutorials/manager/02-workspace.html`;
  manager tutorial step 10; the page table in `docs/operations/help-content.md`; the glossary;
  `LINKED_PAGES` in `test_help_links_render.py`; `PAGES` in `test_rendered_page_gate.py`; a
  special case in `test_workspace_permissions_matrix.py` (it expects a `/manager/...` alias
  for every path); links built with `page_url` (`test_reachability.py` caps variable-built
  links). A new nav item lives in Python, which shot hashing does not see, so staff shots that
  show the sub-nav must be recaptured by hand.
- **Back-fill every past run (decided, §6 D3).** Every done interview is a candidate, so the
  worker back-fills past runs by itself, newest first, under the daily ceiling; no separate
  script. Measured: 121 interviews in 16 runs, 1,152 posts (panel notes excluded), 2.49 M
  characters, at most 32.5 K per interview. At `claude-opus-5` list prices ($5 / $25 per M
  tokens) that is roughly $5–15 for the whole back-fill, depending on thinking; the ceiling
  default must let it finish in a day or two.
- **Deploy.** Migration, then app and worker; agent untouched.

---

## 5. Verification for every section

- `./scripts/ci.sh` green on the host (alembic sanity and round trip for `0072`, CSS drift,
  ruff, full pytest with the coverage floor).
- New tests: §1 run bar renders for a reviewer with only projected fields; optgroups split by
  stored count; changing Sort on an older run keeps the run; changing the run keeps sort, lab,
  review and assignment. §3 both add forms preselect Learn with no mode placeholder. §4 access
  matrix (admin 200, manager 200, reviewer 403, PI 403, admin-as-PI 403); split turns merged;
  panel rounds folded from `specialist_consults`; a decision-less thread of a stopped run is a
  generation candidate; a `ready` row's line reaches a manager's HTML; per-post fallback; a
  changed post set regenerates; the ceiling stops generation (B).
- §2: the panel calibration ladder and the behavioural check in §2.6.
- Browser: one journey per page change through `/lathe:browser-testing` on a seeded instance.
- Help shots recaptured last, on a workstation.

## 6. Owner decisions (2026-10-19)

- **D2** (§4): the trajectory page shows one-line summaries only, never a post's full text;
  managers and admins may read them for every interview, assessed or not.
- **D3** (§4): lines are model-written, with the first sentence as the per-post fallback;
  every past run is back-filled.
- **D4** (§2.6): spend approved for the panel calibration ladder and for replaying the 15
  stored talent candour consults against the new persona text.
- **D5** (§1): the bordered run bar, on Assessments and on Discussions.
- **D6** (§1): the badge shows **Live** from the engine heartbeat; the three stale `running`
  rows are repaired; reviewers see **Finalized** and **Held**.
- **D7** (§2.4d): the Principle-8 and `talent.md` glosses are enough; the rubric is not
  changed now, and further changes are evaluated after the next run.
- **D8** (§4): panel consults fold into one line per hub turn.

- **D1** (§2.5), the prompt-freeze exception, granted by the owner 2026-10-19. On 2026-09-29
  the owner froze every prompt file and every prompt string sent to a model (B22/B24/C27 of
  the audit-remediation spec); each prompt change since went through a written exception
  (O1/B25 for scout_hub 1.10.0, D1 of the 2026-10-05 spec for 1.11.0, Q17 for pi_lab 1.2.0).
  D1 licenses exactly: the scout_hub prompt set 1.11.0 → 1.12.0 (§2.4a-b), the eight
  specialist personas (§2.4c), and one reviewed regeneration of the seven
  `test_prompt_freeze_gm` entries listed in §2.5, whose `.ambr` diff must be exactly the
  prompt diff plus version and hash stamps. It licenses no other prompt, rubric or code-built
  prompt string change, and `test_agent_turn_gm.ambr` is not touched. §1, §3 and §4 change no
  existing prompt (§4 adds a new prompt file for a new feature).
- **Approval:** the owner approved this design as written on 2026-10-19.

## 7. Calls made in this draft (check while reading)

- Learn default is UI-only; the server still requires the field.
- No auto-submit on the run select; two forms that carry each other's state.
- "Stored" stays in the run labels and groups.
- The trajectory tab is named "Interviews"; it lists interviews with at least one reply,
  assessed or not, one run at a time.
- One model call per done interview, short post ids, per-post fallback, `claude-opus-5`.
- The hub conduct fix is prompt-file only (hub prompt set and all eight personas); `pi_lab`,
  the rubric and every code-built prompt string are not touched.
- The stale-run repair is a dry-run-by-default script that sets `stopped` and takes
  `ended_at` from the run's last `llm_call_logs` row; it touches nothing else.
- One shared run-bar partial for Assessments, Discussions and Interviews; Discussions counts
  threads per run and keeps "All Runs".
- The back-fill is the worker's normal generation, newest first, under the daily ceiling —
  no separate back-fill script.
- A post is consecutive same-author rows under 2 s apart; an interview needs a reply from a
  different author (both measured against every thread on record, §4.2).
- §2's pass bars and its one-revision stop rule (§2.6).
