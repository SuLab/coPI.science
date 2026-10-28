# Projects pages redesign, assignments and project chat — Implementation Plan

> **For agentic workers:** executed through `/lathe:plan-execution` (the owner's instruction,
> 2026-10-27: "write the plan, audit the plan, then fully execute the plan"). Each phase is one batch:
> its packages are implemented in parallel without building or testing, then verified together on the
> host, adversarially audited, fixed and committed. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remodel the Projects list and details pages on the Assessments pages, fix the defects the
analysis found, add quick reviews, project review assignments and an "Ask about this project" chat.

**Architecture:** Web tier only. `src/services/projects_view.py` builds richer read models; the two
project templates are rebuilt on the assessment pages' components and stylesheet; project reviews gain
return-state redirects; a new `hub_project_review_assignments` table (`0075`); the assessment chat's
tables, service and drawer take a project subject (`0076`) with a new record builder and router.

**Tech Stack:** FastAPI, SQLAlchemy 2 (async), Alembic, Jinja2, Tailwind (precompiled
`static/css/app.css`), vanilla JS, pytest (+ testcontainers Postgres), Playwright (workstation).

**Spec:** `docs/specs/2026-10-27-projects-pages-redesign-design.md` (revision 3, approved 2026-10-27;
decisions D42–D64). Every task's requirements include the spec section it names.

## Global Constraints

- No change under `src/agent/`, to `src/services/project_plan.py`, `src/services/project_research.py`,
  `prompts/roles/`, `prompts/projects/` or the interview prompt sets. `src/models/` gains only the
  `0075` model and the `0076` chat columns (written by the main session before fan-out; the engine
  and the worker import `src.models` but never read the new table or columns). No agent or worker
  rebuild is needed (spec §0, anti-goal 3).
- Read stored project data; never write a `hub_projects` row from the web tier (spec §3).
- Every new view-model key is registered in `src/web/presentation.py`: plain keys in `VIEW_FIELDS`,
  JSON kept whole in `JSON_FIELDS`, maps whose keys are data in `DYNAMIC_MAP_FIELDS`, admin-only keys
  in `ADMIN_KEYS` (spec §3, §6.5). An unregistered key silently renders as nothing.
- Templates: one `<main>`, one `<h1>`, no skipped heading level, every `<nav>` named
  (`aria-label`), no `text-gray-300`/`text-gray-400`, no inline `on…=` handler, every form control
  labelled; scripts only from files or nonce'd blocks; every form `method="post"` with a literal
  action path where Jinja fills only a path-parameter slot (`tests/unit/test_reachability.py`).
- Tailwind: use only classes already compiled into `static/css/app.css` (the assessment templates use
  everything this plan needs). `scripts/build_css.sh --check` must pass without rebuilding; a rebuilt
  `app.css` stales every help shot (spec §3).
- Model-written text is autoescaped or rendered through `data-markdown` (the shared sanitizer); never
  `| safe` (spec §3).
- Project review writes refuse impersonation; every review, assign and chat control is hidden then.
- D43: no score, band, recommendation, rating suggestion or fund suggestion anywhere a project shows.
  D55: no specialist `verdict_signal`, `confidence` or `raw_opinion` reaches a project page, the
  project chat record or the project chat prompt, and no order or colour is derived from them.
- D61: migrations `0075` and `0076` run only when no projects run is live; never stop a run for them.
- `templates/base.html` and `templates/_run_bar.html` are not edited (help shots, spec §3).
- `CLAUDE.md` stays below 200 lines: edit its "head is `0074`" sentence in place only.
- Host commands: run git, pytest and scripts on the host
  (`ssh -4 -o StrictHostKeyChecking=yes ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com`,
  `cd ~/blackbird-copi-science`); pytest as `.venv-test/bin/python -m pytest …`; CI as
  `ENGINE_REFUSAL_FALLBACK_ENABLED=false ./scripts/ci.sh`; never `pip install` from the client; never
  point `TEST_DATABASE_URL` at `copi`.
- Help content follows `docs/operations/help-content.md` (exact UI text, no numbers other than the
  1–5 rating and the 10,000-character comment limit, no real names, no repository internals).

## Review Focus

1. **Malformed stored JSON** — a project whose `choice.plan` is not a dict, whose `final.gaps` is a
   string, or whose `choice.sources` holds non-dicts must render both pages with empty sections, never
   a 500. Pinned in Task 1.5 (`test_malformed_project_json_renders`).
2. **next_id under every sort and tab** — the quick review's "land on the next card" must follow the
   rendered order of the current sort, tab and filters; the last card's `next_id` is "". Pinned in
   Task 1.1 (`test_next_id_follows_rendered_order`).
3. **A stale Unreviewed card** — a quick review posted for a project someone else reviewed meanwhile
   is saved (one per person), lands on the next card and flashes once. Pinned in Task 1.2
   (`test_quick_review_after_someone_else_reviewed`).
4. **A lab with no PI link** — an agent with no registry row or no linked user renders as plain text,
   never a broken link. Pinned in Task 1.5 (`test_lab_without_pi_user_is_plain_text`).
5. **A posting with no stored text** — `posting_ts` set but `posting_text` NULL falls back to the first
   logged chunk; neither present shows "Posting unavailable". Pinned in Task 1.1
   (`test_posting_falls_back_to_logged_chunk`).

---

## Contracts (read before any task)

### C1. List state (Phase 1, `src/services/projects_view.py`)

```python
LIST_REVIEW_TABS = ("unreviewed", "reviewed", "all"); LIST_REVIEW_DEFAULT = "unreviewed"
LIST_SORTS = ("finished", "lab", "outcome");          LIST_SORT_DEFAULT = "finished"
def project_list_state(*, run_id: object, lab: object, outcome: object, review: object,
                       sort: object, mine: object, assignment: object = None) -> dict[str, str]
def project_list_query(state: Mapping[str, str]) -> str   # "?a=b&…" via urlencode, "" when empty
```

`project_list_state` keeps only valid, non-default values, as strings, in this key order: `run_id`
(parses as a UUID), `lab` (non-empty after strip, at most 50 characters), `outcome` (a key of
`_OUTCOME_FILTERS` other than `""`), `review` (in `LIST_REVIEW_TABS`, not the default), `sort` (in
`LIST_SORTS`, not the default), `mine` (`"1"` only when the input is `"1"`), `assignment` (`"mine"`
only). Anything else is dropped, never echoed. Both routers and every template link use these two.

### C2. List view (Phase 1)

```python
async def build_projects_list(db, *, viewer: User, impersonating: bool, run_id: str | None,
    lab: str | None, outcome: str | None, review: str | None, sort: str | None, mine: bool,
    assignment: str | None = None) -> dict
```

Keys: `run_bar` (entries with `rubric_version` set to None — F9), `selected_run_id`, `lab_options`,
`lab_filter`, `outcome_filter`, `review_filter` (one of `LIST_REVIEW_TABS`), `sort` (one of
`LIST_SORTS`), `mine` (bool), `assignment_filter` (`"all"` | `"mine"`), `list_state` (C1),
`review_counts` (`{"unreviewed","reviewed","all"}` → int, after run, lab, outcome, mine and assignment
filters, before the tab), `tiles` (list of `{"kind","label","count"}` for `total`, `ready`,
`not_agreed`, `not_finished`, `in_progress` labelled Total, Ready, Not agreed, Not finished, In
progress; after run, lab, tab, mine and assignment filters, not the outcome filter; `in_progress`
counts kinds `in_progress` and `paused`), `not_finished_reasons` (list of `{"label","count"}` over the
whole run, unfiltered, count descending then label), `summary` (as today: run-wide `counts`,
`review_count`, `mean_rating`, `fund_share`), `projects` (cards, all filters applied, sorted by
`sort`), `pi_user_ids` (`{agent_id: str(user_id)}` from `AgentRegistry` for the run's labs with a
linked user), `can_review_projects` (not impersonating), `project_total`.

A project is **reviewed** when it has at least one `HubProjectReview` (D56). Sorts: `finished` is
today's `_sort_key`; `lab` sorts by `agent_id`, then `_sort_key`; `outcome` sorts Ready, Not agreed,
Not finished, then In progress/Paused, then `_sort_key`. The query defers `HubProject.research` (F11).

Card keys: `id`, `anchor` (`"p-<id>"`), `lab`, `title`, `outcome_kind`, `outcome_label`,
`reason_label`, `is_finished` (kind in ready/not_agreed/not_finished), `requested_at`, `posted_at`,
`finished_at`, `summary` (`choice.summary` when a string, else ""), `key_points` (list of
`{"label","value"}` for Indication, Target or mechanism, Time and budget from the final plan, else the
opening plan, empty values omitted), `hub_position`, `lab_position` (C4 labels), `gap_count`,
`disagreement_count`, `reviewers` (list of `{"name","rating","fund"}` oldest first), `review_count`,
`mean_rating`, `fund_count`, `reviewed_by_me`, `my_review` (`{"rating","fund"}` or None), `stage`
(None when finished, else `requested`, `researching`, `ready_to_post`, `posted` or `refining`),
`stage_label` (Waiting for research, Researching, Ready to post, Posted, Refining), `stage_paused`
(kind `paused`), `stage_time`, `stage_time_label` (`requested` → `requested_at`, "requested";
`researching`/`ready_to_post` → `updated_at`, "last activity"; `posted` → `posted_at`, "posted";
`refining` → the lab reply's logged time, "lab replied"), `next_id` (the following card's id as a
string in rendered order, "" on the last), `assigned_names` (Phase 2; `[]` until then).

### C3. Detail view (Phase 1)

```python
async def build_project_detail(db, project_id, *, viewer: User | None, is_admin: bool,
                               impersonating: bool) -> dict | None   # {"project": {...}}
async def project_display_kind(db, project_id: uuid.UUID) -> str | None   # None when missing
def display_tool_marks(tools: Sequence[str], profile_items: Mapping[str, Sequence[str]],
                       anchors: Sequence[Mapping]) -> list[dict]   # [{"item","in_profile","matched"}]
```

`viewer=None` (the chat record, Phase 3) gives no viewer keys their viewer value: `can_edit` False,
`my_review` None, `can_review_projects` False.

`display_tool_marks` (F3, D62): candidates are the profile's items in `METHOD_MODEL_SECTIONS` plus
each anchor's `item`, all through `normalize_item`, keeping those of at least two words; a tool is in
the profile when its normalised text contains a candidate as whole words (regex word boundaries on
the normalised strings); the longest matching candidate wins and is returned (original text) as
`matched`; otherwise `in_profile` False, `matched` None. Profile items come from
`section_items(persona_file_text(agent_id))`, the lab's current public profile.

`project` keys: `id`, `lab`, `pi_user_id` (str or None), `title`, `summary`, `outcome_kind`,
`outcome_label`, `reason_label`, `is_finished`, `run_id`, `run_started_at`, `requested_at`,
`posted_at`, `finished_at`, `has_close` (`final` is a dict), `why_chosen`, `others`
(`[{"use","note"}]`), `anchors` (`[{"section","item"}]`), `plan_title` ("Plan" with a close, else
"Plan (as posted)"), `plan_rows` (every `TEMPLATE_FIELDS` field except `lab_tools`, as
`{"label","value","changed","opening_value"}`; with a close the final plan's value, `changed` when its
whitespace-normalised value differs from the opening plan's, `opening_value` the opening value when
changed, else None; without a close the opening plan, never changed), `tool_rows`
(`display_tool_marks` of the final plan's `lab_tools`, else the opening plan's), `hub_position`,
`lab_position` (C4), `gaps`, `disagreements`, `sources_by_kind` (non-empty groups in the order
papers, trials, patents, other: `{"kind","label","items"}` with labels Papers, Clinical trials,
Patents, Other sources and items `{"ident","title","url","specialist"}`), `evidence` (one entry per
consulted domain in order of its first consult: `{"domain","anchor","established","concerns",
"questions_to_ask","truncated","earlier_count"}` from the latest consult in that domain; `anchor` is
that consult's `consult-<k>`), `timeline` (C5), `thread_unavailable` (`reply_ts` set and no thread
row found — F8), `counts` (`{"sources","consults","posts","others"}`), `reviews`
(`{"id","reviewer_name","rating","comment","fund","created_at","edited","can_edit"}`, `edited` when
`updated_at` is later than `created_at` by more than a second), `my_review`, `can_review_projects`,
and for admins only `research_cost_usd`, `refinement_cost_usd`, `calls_url`, `raw_choice`
(`choice`), `raw_final` (`final`).

### C4. Position labels (F10)

With a close: `"Agreed"` (`agree`), `"Not agreed"` (`disagree`), else `"No position stated"`.
Without a close: `"No close recorded"`.

### C5. Timeline entries (F1, F4, F7, F8)

Time-ordered list; posts numbered `m-1…` and consults `consult-1…` in that order.
- Posting: `{"kind":"posting","anchor","speaker","at","text","partial"}` — `text` is
  `posting_text` (else the first logged chunk; neither: the entry is omitted and the page shows
  "Posting unavailable"), `at` the first logged chunk's time else `posted_at`, `partial` when fewer
  `hub_project` rows are logged for this posting than `split_for_wire(posting_text)` has chunks.
- Post: `{"kind":"post","anchor","speaker","at","text","closed_thread"}` — the lab's reply and the
  thread's posts merged by `merge_chunks`; `closed_thread` on BlackbirdBot's last post when the project
  has a close.
- Consult: `{"kind":"consult","anchor","domain","question","established","concerns",
  "questions_to_ask","concern_count","truncated","at"}` — `truncated` from
  `SpecialistConsult.truncated` (NULL reads False); never its signal, confidence or raw opinion.

### C6. Project review form contract (Phase 1, `src/routers/reviews.py`)

`POST /reviews/projects/{project_id}/review`, `/reviews/project-reviews/{review_id}/edit`,
`/reviews/project-reviews/{review_id}/delete` accept, besides today's fields, optional form fields
`surface` (`projects-list` | `project-detail`), the C1 state fields (`run_id`, `lab`, `outcome`,
`review`, `sort`, `mine`, `assignment`) and, on the new-review route, `next_id`. Redirects (303):
- `projects-list` (new review only): `/workspace/projects` + `project_list_query(state)` +
  `#p-<id>`; when the review is saved and the list's tab is Unreviewed (state `review` absent), the
  fragment is `#p-<next_id>` (none when `next_id` is "") and the flash is "Review saved; moved to
  Reviewed."; otherwise "Review saved.". A duplicate flashes only "You have already reviewed this
  project; edit your review on its page." and lands as a saved review would.
- `project-detail`: `/workspace/projects/<id>` + `project_list_query(state)` + `#reviews`, with
  today's flashes.
- No surface: today's behaviour.

### C7. Phase 2 contracts

Model `HubProjectReviewAssignment` (`src/models/hub_project.py`, table
`hub_project_review_assignments`, spec §7.1, unique `uq_hub_project_review_assignment_once`,
indexes `ix_hub_project_review_assignments_project_id`, `ix_hub_project_review_assignments_assignee`).
Service (`src/services/project_reviews.py`):
`assign_project_reviewer(db, *, project, assignee, assigned_by) -> None` (idempotent,
`on_conflict_do_nothing(constraint=…)`), `unassign_project_reviewer(db, *, project, assignee_user_id)
-> None`, `project_assignments(db, project_ids) -> dict[uuid.UUID, list[HubProjectReviewAssignment]]`
(oldest first). Routes: `POST /reviews/projects/{project_id}/assign` and `/unassign` (form
`assignee_user_id`, `surface=project-detail`, C1 state), staff only (`_STAFF`), refuse impersonation,
assignee validated by `_load_assignee`, redirect as C6 `project-detail`. List: `assignment="mine"`
keeps projects assigned to the viewer; cards' `assigned_names`. Detail: `assignments`
(`[{"assignee_user_id","assignee_name"}]`), `review_capable_users` (`[{"id","name"}]`, staff viewers
only, staff or reviewer accounts with allowed access, by name).

### C8. Phase 3 contracts

- Constants (`src/models/assessment_chat.py`): `CHAT_SUBJECT_ASSESSMENT = "assessment"`,
  `CHAT_SUBJECT_PROJECT = "project"`, `CHAT_SUBJECTS`. Checks `ck_assessment_chat_turns_one_subject`
  (`num_nonnulls(assessment_id, project_id) = 1`), `ck_assessment_chat_usage_subject`,
  `ck_assessment_chat_opens_subject` (`subject IN (…)`, NULL allowed). Indexes
  `ix_assessment_chat_turns_project_conversation` (project_id, user_id, context_tier, created_at),
  `ix_assessment_chat_usage_project_id`, `ix_assessment_chat_opens_project_id`.
- Service (`src/services/assessment_chat.py`): `ChatSubject(kind: str, id: uuid.UUID)` (frozen;
  raises ValueError for an unknown kind or a None id); `PROJECT_PROMPT_PATH =
  Path("prompts/project-chat.md")`; `load_system_prompt(path: Path = PROMPT_PATH)`. `prepare_turn`,
  `list_history`, `clear_history`, `record_open`, `_conversation`, `_refuse_over_caps`,
  `_own_turn_is_stale`, `_new_rows` take `assessment_id=` (unchanged) or `project_id=` (new),
  exactly one (else ValueError); `PreparedTurn` gains `project_id: uuid.UUID | None = None` and its
  `assessment_id` becomes `uuid.UUID | None`. New ledger and opens rows record `subject`.
- Record (`src/services/project_chat_record.py`, new): `build_project_chat_record(detail: dict, *,
  tier: str) -> ChatRecord`; `async load_project_chat_record(db, project_id, *, tier) ->
  tuple[ChatRecord, HubProject] | None`; `project_template_suggestions(detail: dict) ->
  list[Suggestion]`; `project_page_suggestions(detail: dict, user, *, chat_enabled: bool) ->
  list[Suggestion]` ([] when off or impersonating).
- `INLINE_SECTIONS` (`src/services/assessment_chat_suggestions.py`) gains `evidence`, `plan`,
  `positions`.
- Settings: `project_chat_enabled: bool = False` (`src/config.py`); `create_app` sets
  `application.state.project_chat_enabled`. Router `src/routers/project_chat.py` at `/project-chat`.
- Drawer: `templates/assessments/_chat_drawer.html` reads `chat_kind` (default `"assessment"`),
  `chat_subject_id` (default `a.id`) and string variables with today's text as defaults;
  `window.ASSESSMENT_CHAT` gains optional `errors` (merged over the script's table) and `subject`.

---

## Phase 1 — pages (no migration)

### Task 1.1: Project read models

**Files:**
- Modify: `src/services/projects_view.py`
- Create: `tests/unit/test_projects_list_state.py`, `tests/unit/test_project_tool_marks.py`,
  `tests/integration/test_projects_view.py`

**Interfaces:** Produces C1, C2, C3, C4, C5. Consumes `normalize_item`, `METHOD_MODEL_SECTIONS`,
`TEMPLATE_FIELDS`, `display_id` (`project_plan`, read-only), `split_for_wire`
(`src/agent/slack_client.py`, read-only import), `merge_chunks`, `section_items`, `persona_file_text`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_projects_list_state.py`:
- `test_state_drops_junk_and_defaults`: `project_list_state(run_id="x", lab="", outcome="bogus",
  review="unreviewed", sort="finished", mine="yes")` == `{}`.
- `test_state_keeps_valid_values_in_order`: valid UUID, `lab="lab1"`, `outcome="ready"`,
  `review="all"`, `sort="lab"`, `mine="1"`, `assignment="mine"` → those seven keys in C1 order.
- `test_query_is_urlencoded`: `project_list_query({"lab": "a&b\r\nX: y"})` ==
  `"?lab=a%26b%0D%0AX%3A+y"`; `project_list_query({})` == `""`.
- `test_long_lab_dropped`: a 51-character lab is dropped.

`tests/unit/test_project_tool_marks.py` (profile `{"Key Methods and Technologies": ["DNase I
protection assays of NET DNA"], "Model Systems": ["primary human neutrophils"], "Key Molecular
Targets": ["STING"], "Keywords": ["lupus nephritis"]}`):
- `test_method_contained_in_tool_is_in_profile`: tool "DNase I protection assays of NET DNA
  (published, gel-based)" → `in_profile` True, `matched` "DNase I protection assays of NET DNA".
- `test_target_only_tool_is_stated`: "STING agonist diABZI in monocytes" → False.
- `test_keyword_only_tool_is_stated`: "biopsies from lupus nephritis cohorts" → False.
- `test_single_word_items_never_match`: profile method "ELISA", tool "ELISA kits" → False.
- `test_anchor_counts`: anchors `[{"section": "Model Systems", "item": "in vitro reconstituted
  NET-ALR protein systems"}]`, tool containing it → True.
- `test_longest_match_wins`: two candidates contained, the longer is `matched`.
- `test_whole_words_only`: method "cell lines" does not match "cell linesman".

`tests/integration/test_projects_view.py` (seed with the helpers of
`tests/integration/test_projects_pages.py`, copied or imported):
- `test_review_counts_and_tiles`: three closed projects, one reviewed → `review_counts ==
  {"unreviewed": 2, "reviewed": 1, "all": 3}` on the default outcome; tiles ignore the outcome filter.
- `test_tabs_filter_cards`: `review="reviewed"` lists only the reviewed project.
- `test_not_finished_reasons_are_run_wide`: two `thread_timeout`, one `research_failed`, a lab
  filter set → reasons `[("The thread ended without a close", 2), ("Research failed", 1)]`.
- `test_sorts`: `lab` orders by agent id; `outcome` orders Ready, Not agreed, Not finished.
- `test_next_id_follows_rendered_order` (Review Focus 2): each card's `next_id` is the next card's id
  under `sort="lab"`, the last is "".
- `test_stage_labels_and_times`: a `refining` project's `stage_time` is the lab reply's logged time
  and its label "lab replied"; `researching` reads "last activity".
- `test_run_bar_has_no_rubric_version`: every `run_bar` entry's `rubric_version` is None.
- `test_detail_posting_is_full_text_with_partial_note`: posting_text "A\nB" with only the first chunk
  logged and `split_for_wire` producing two → posting entry text "A\nB", `partial` True.
- `test_posting_falls_back_to_logged_chunk` (Review Focus 5).
- `test_plan_once_with_change_marks`: with a close, the indication row has `changed` True and
  `opening_value` "OPENING INDICATION"; without a close `plan_title == "Plan (as posted)"` and no row
  changed; no row for `lab_tools`.
- `test_positions_labels`: close with `lab_position` None → "No position stated"; no close → "No
  close recorded".
- `test_timeline_interleaves_consults_with_anchors`: posting, reply, a consult made between two
  thread posts → order posting, reply, post, consult, post; anchors `m-1, m-2, m-3, consult-1, m-4`.
- `test_closed_thread_marks_last_hub_post`.
- `test_truncated_consult_flagged`.
- `test_thread_unavailable`: `reply_ts` set, no rows → True.
- `test_evidence_latest_per_domain`: two scientific consults → one entry, `earlier_count` 1, anchor
  of the latest.
- `test_viewer_none`: `viewer=None` → `my_review` None, every review `can_edit` False.
- `test_admin_only_keys`: `raw_choice`/`raw_final`/costs present only with `is_admin=True`.
- `test_no_signal_or_confidence_anywhere`: seed `verdict_signal="blocking"`, `confidence="moderate"`,
  `raw_opinion="RAW OPINION TEXT"`; none appears in `repr()` of the detail.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_projects_list_state.py tests/unit/test_project_tool_marks.py tests/integration/test_projects_view.py -q`
Expected: FAIL (missing names).

- [ ] **Step 3: Implement C1–C5 in `src/services/projects_view.py`**

Keep `display_outcome`, `OUTCOME_LABELS`, `REASON_LABELS` and `project_thread_roots` unchanged (other
modules import them). Load lab replies for `refining` stage times in one query per list. Batch every
per-project query by run.

- [ ] **Step 4: Run them to verify they pass** (same command; expected PASS).

### Task 1.2: Routers, projection and review redirects

**Files:**
- Modify: `src/routers/workspace/projects.py`, `src/web/presentation.py`, `src/routers/reviews.py`
- Modify: `tests/integration/test_project_reviews.py`

**Interfaces:** Consumes C1–C3. Produces C6 and the template context: list → C2 keys; detail →
`project` (C3) plus `list_state` (C1, validated from the detail URL's query).

- [ ] **Step 1: Write the failing tests** in `tests/integration/test_project_reviews.py`:
- `test_quick_review_returns_to_card_anchor`: `surface=projects-list`, `review=all` → 303 to
  `/workspace/projects?review=all#p-<id>`, flash "Review saved.".
- `test_quick_review_moves_to_next_card_on_unreviewed`: no `review` field, `next_id=<id2>` →
  `…#p-<id2>`, flash "Review saved; moved to Reviewed.".
- `test_quick_review_last_card_has_no_fragment`: `next_id=""` → no fragment.
- `test_quick_review_after_someone_else_reviewed` (Review Focus 3): another user's review exists;
  the post saves, lands on next, one flash.
- `test_quick_review_duplicate`: second submit by the same user → only the duplicate flash, lands
  like a save.
- `test_junk_state_dropped`: `run_id=x`, `lab=` 60 chars, `sort=bogus`, `lab` with CR/LF → none
  echoed in `Location`.
- `test_detail_surface_keeps_state`: new, edit and delete with `surface=project-detail` and
  `lab=lab1` → `/workspace/projects/<id>?lab=lab1#reviews`.
- `test_no_surface_behaves_as_before`.
- `test_impersonated_quick_review_refused` → 403.

- [ ] **Step 2: Run to fail**: `.venv-test/bin/python -m pytest tests/integration/test_project_reviews.py -q`

- [ ] **Step 3: Implement**
- `src/routers/workspace/projects.py`: list route reads `review`, `sort`, `mine` (`"1"`) and
  `assignment` (ignored until Phase 2) and passes them to `build_projects_list`; detail route reads
  the C1 fields, puts `list_state = project_list_state(...)` in the context, calls
  `build_project_detail(..., viewer=current_user, ...)`.
- `src/web/presentation.py`: register every C2/C3/C5 key not already registered (`VIEW_FIELDS`);
  `raw_choice`, `raw_final` in `JSON_FIELDS` and `ADMIN_KEYS`; `list_state` and `pi_user_ids` in
  `DYNAMIC_MAP_FIELDS` (`pi_user_ids` already is; `review_counts` already is).
- `src/routers/reviews.py`: C6 for the three project review routes (read the form once; reuse
  `_form_str`).

- [ ] **Step 4: Run to pass** (same command).

### Task 1.3: The list template

**Files:** Modify `templates/workspace/projects.html`.

**Interfaces:** Consumes C1, C2, C6 (form fields). Spec §6.2, §6.3.

- [ ] **Step 1: Rebuild the page** (no separate failing test: Task 1.5 owns the page tests):
1. Title row like `templates/workspace/assessments.html:7-23`: h1 "Projects" + help link left; GET
   form right with Sort (Recently finished / Lab / Outcome), Lab, Outcome (today's options),
   "Not reviewed by me" checkbox, hidden `run_id` and `review`, Apply.
2. Run bar macro call as today (entries already carry no rubric).
3. One scope line: "BlackbirdBot's projects: new uses of each lab's own work, researched and refined
   with the lab in Slack." plus the help link sentence "Help explains each outcome."
4. Tabs nav (`aria-label="Review status"`) like `assessments.html:29-32`: Unreviewed (n), Reviewed
   (n), All (n), each a link carrying `list_state` with its own `review`; note "Tab counts follow the
   selected run, lab, outcome and filters. Reviewed means anyone has reviewed it."
5. Not-finished banner when `not_finished_reasons`: amber box like `_body.html:38-87`: "{n} project(s)
   in this run did not finish:" then each "{label} × {count}", "Counted across the whole run, whatever
   the filters.", a link "Show not finished projects" (`outcome=not_finished`, same run).
6. Tiles: five boxes like `_body.html:147-176` from `tiles`; under them today's summary line (run-wide
   reviews, mean rating, fund share).
7. Cards: `id="{{ p.anchor }}"`; finished card per spec §6.2(7) — headline title, "Lab: <PI link or
   plain>" · posted · finished, outcome chip and reason right; "In one minute" box (summary,
   `whitespace-pre-line`) and "Key points" box (`key_points` rows, then "Positions: BlackbirdBot …
   · The lab …", then "{gap_count} gaps · {disagreement_count} disagreements"); "detail →" button
   (detail link with `list_state`); footer "Reviewed by: Name (4/5, Fund), …" or "—", "reviewed by
   you", "Assigned: …" only when `assigned_names` is non-empty.
   Quick review (§6.3): `<details open>` unless `p.my_review`; under impersonation the sentence
   "Reviews can't be written while impersonating."; with `my_review` "Your review: R/5 · Fund|Don't
   fund" and a link "edit it on the project's page" (detail + `list_state` + `#reviews`); form
   `method="post" action="/reviews/projects/{{ p.id }}/review"`, hidden `surface=projects-list`,
   every `list_state` field, `next_id`; Rating select with `<option value="" disabled selected>—`,
   Funding radios (required), Comment (maxlength 10000), button "Submit review", note "One review per
   person; you can edit yours on the project's page." Copy from the assessments' Quick scoring:
   "Rates the project itself, not how the bots performed."
   Open card: compact — title or "Untitled project", lab, stage chip ("Paused · " prefix when
   `stage_paused`), "{stage_time_label} {stage_time | ts}"; no review controls.
8. Empty states: no project in the run at all → "No projects in this run yet — the run menu shows
   each run's count."; Reviewed tab empty → "No reviewed projects match these filters — {n} still
   await a review on the Unreviewed tab."; Unreviewed tab empty with `review_counts.all > 0` → "No
   unreviewed projects match these filters — all {n} have a review."; otherwise "No projects match
   these filters in this run."

- [ ] **Step 2: Render check** happens in Task 1.5 / integrated verification.

### Task 1.4: The details template

**Files:** Modify `templates/workspace/project_detail.html`.

**Interfaces:** Consumes C3, C4, C5, C6, `list_state`. Spec §6.4.

- [ ] **Step 1: Rebuild the page** as spec §6.4 items 1–12, in a `max-w-5xl mx-auto` column, with
`{% block extra_head %}{% include "assessments/_head.html" %}{% endblock %}` and
`<script src="/static/js/assessment_timeline.js"></script>` at the end of the content; element ids
exactly `brief`, `evidence`, `plan`, `positions`, `others`, `research`, `reviews`, `timeline`, and
each timeline entry's `anchor`; `scroll-mt-16 max-md:scroll-mt-28` on each anchored card like
`_detail_body.html`. Specifics:
- Back link "← Projects" to the list with `list_state`; h1 "Project detail"; help link
  `help_link(request, effective_user, 'projects', 'detail')`.
- Admin: a button-styled link "Model calls for this project" when `calls_url`, and the cost line
  "Cost: research $X.XX, refinement $Y.YY".
- Jump nav (`aria-label="On this page"`, sticky) with the seven links and the two
  `data-details-toggle` buttons "Expand all" / "Collapse all".
- Brief: `<p>` headline (no second h1), meta line "Lab: <PI link or plain> · run started … · posted …
  · finished …", outcome chip + reason; left "In one minute" (summary) and "Why this use" in the
  amber box style of `_detail_body.html:156-169`; right "Key points": "Positions: BlackbirdBot <chip>
  · The lab <chip>", "Built on" list "{section}: {item}", and "{n} sources · {n} specialist consults ·
  {n} posts · {n} other uses considered", each count linking to its section.
- Evidence summary (h2, never collapsed): three boxes — "Established" (green), "Concerns" (slate),
  "Questions to ask" (slate) — each listing every domain that has items: bold domain, a
  `line-clamp-2` preview of the first item, a `<details>` "show" with the full list, and "{n}
  earlier consult(s) in this domain are in the conversation." when `earlier_count`; "reply cut off"
  chip when `truncated`. Empty: "No specialist was consulted in this project's thread." Legend: "Each
  row is the latest consult in that specialist's domain; “show” opens its full text." Provenance:
  "Quoted from the specialists' stored replies; the specialists are AI personas BlackbirdBot
  consulted while refining the plan."
- Plan card (h2 `plan_title`): rows as a two-column grid (`sm:w-56` label column), values in
  `assessment-prose`; a changed row shows a chip "changed since posting" and `<details>` "As first
  posted" with `opening_value`; then h3 "The lab's tools and models": each tool, then "in the lab's
  profile" (green) with "matches: {matched}" or "stated by the lab" (grey).
- Positions card (h2): chips; Not agreed → amber card with h3 "Gaps" and "Disagreements" (open);
  Ready → h3 "Acknowledged limits" (neutral) and Disagreements if any; no close → "No close
  recorded."
- "Also considered ({n})" as a closed `<details>` card (`id="others"`).
- Research card: h2 "Research ({n} sources)", h3 per group, items as today (links unchanged).
- Reviews card (`id="reviews"`): h2 "Reviews"; instructions: "Your review rates the project itself:
  whether the idea and its plan are worth pursuing, not how well the bots performed. Rate it from 1
  (weak) to 5 (strong), and say whether you would fund the project as planned."; reviews as today
  plus a Fund/Don't fund chip and "(edited)"; every review form carries `surface=project-detail` and
  `list_state`; the new-review rating starts on a disabled, selected "—" (F2); no form under
  impersonation (as today).
- Conversation: closed `<details id="timeline">` card, h2 "Conversation"; intro "The posting, the
  lab's reply and the thread in Slack, in order, with each specialist consult at the time it was
  made."; posting card ("BlackbirdBot · {at} · posting", `whitespace-pre-line` text, "Part of this
  posting may not have reached Slack." when `partial`); "Posting unavailable." when the posting is
  missing; post cards with the `timeline-message-clamp … data-clamp` wrapper, a
  `md-content` div with `data-markdown="{{ e.text | e }}"`, the "Show full message" button
  (`data-unclamp`) and a "closed the thread" chip; consult cards indented and dashed: "specialist ·
  {domain}", "{n} concerns", "reply cut off" chip plus "This specialist's reply stopped before it
  finished, so what follows is partial.", then Asked / Established / Concerns / Questions to ask;
  `thread_unavailable` → box "Thread messages unavailable. The posts of this project's thread are not
  in the message log."
- Admin: closed `<details>` card "Raw choice and close" with two `<pre>` blocks
  (`raw_choice | tojson(indent=2)`, `raw_final | tojson(indent=2)`).
- Update the template's header comment: replies render through the sanitizer; its links follow the
  sanitizer's allowlist.

### Task 1.5: Page tests

**Files:** Modify `tests/integration/test_projects_pages.py`; modify
`tests/unit/test_projects_projection.py` if its key sets change.

**Interfaces:** Consumes the pages from Tasks 1.3–1.4 (copy, ids and classes named there).

- [ ] **Step 1: Rewrite the tests** keeping `test_access_matrix`,
`test_projects_nav_item_for_reviewers_and_staff`, the impersonation and the cost tests (update their
string expectations), and adding:
- List: `test_default_tab_is_unreviewed_with_counts` ("Unreviewed (2)" etc.),
  `test_tiles_render_five_counts`, `test_not_finished_banner_and_link`,
  `test_card_shows_summary_key_points_and_reviewers`, `test_lab_without_pi_user_is_plain_text`
  (Review Focus 4), `test_lab_links_to_pi_page`, `test_open_card_shows_stage_not_review_form`,
  `test_quick_review_form_fields` (hidden `surface`, `next_id`, the "—" option, `required` radios),
  `test_my_review_replaces_form`, `test_impersonation_hides_quick_review`,
  `test_detail_links_carry_list_state`, `test_empty_states_per_tab`, `test_no_rubric_in_run_bar`.
- Detail: `test_sections_and_ids` (h1 "Project detail" and each id),
  `test_plan_change_marks_and_tools`, `test_positions_labels`,
  `test_evidence_summary_groups_by_domain`, `test_conversation_is_collapsed_and_ordered`,
  `test_replies_use_data_markdown_and_posting_is_plain`, `test_full_posting_text_shown`,
  `test_truncated_consult_chip`, `test_thread_unavailable_box`, `test_new_review_rating_placeholder`,
  `test_edited_marker`, `test_back_link_keeps_state`,
  `test_admin_raw_json_only_for_admin`, `test_malformed_project_json_renders` (Review Focus 1),
  `test_no_label_or_confidence_for_any_role` (sentinels "blocking", "moderate", "RAW OPINION TEXT"
  absent as admin, manager and reviewer).
- Update `_titles` to the new card markup.

- [ ] **Step 2** runs in the integrated verification.

### Task 1.6: Help

**Files:** Modify `templates/help/pages/projects.html`, `templates/help/pages/glossary.html`,
`templates/help/pages/troubleshooting.html`; check `templates/help/pages/getting-started.html`,
`templates/help/tutorials/reviewer/01-welcome.html`, `templates/help/tutorials/manager/01-your-role.html`,
`templates/help/tutorials/manager/02-workspace.html` and change any sentence that no longer matches.

**Interfaces:** Consumes the copy of Tasks 1.3–1.4.

- [ ] **Step 1:** Rewrite the `list`, `detail` and `reviewing` sections for the new pages (the tabs
and what "reviewed" means, "Not reviewed by me", tiles, banner, cards, Quick review, the open-project
cards' stages; the details page's sections in order; tool marks "in the lab's profile" / "stated by
the lab" with what counts — a listed method or model, or one of the project's anchors; Acknowledged
limits vs Gaps). Glossary: "Acknowledged limits", "stated by the lab", the stage names, "Quick
review". Troubleshooting: the three new flashes and "Reviews can't be written while impersonating."
Follow the content rules (no numbers but 1–5 and 10,000; no real names; exact UI text).

### Task 1.7: Harness seed

**Files:** Modify `tests/e2e/ui_audit/seed.py`.

- [ ] **Step 1:** Add `_seed_projects(s, *, run, now)` (called from `main()`), all invented: a Ready
project (two-chunk posting logged as two `hub_project` rows, the second with `thread_ts` =
`posting_ts`; a lab reply; four thread posts with Markdown; three consults in two domains, one
`truncated`; one review), a Not agreed project with gaps and disagreements, a Not finished
`thread_timeout` project, and a `researching` project. Use agent ids already seeded for labs.

### Phase 1 verification, audit, commit and deploy (main session)

- [ ] Integrate the packages; `git diff --stat` touches only the files above.
- [ ] Focused: `.venv-test/bin/python -m pytest tests/unit/test_projects_list_state.py tests/unit/test_project_tool_marks.py tests/integration/test_projects_view.py tests/integration/test_projects_pages.py tests/integration/test_project_reviews.py tests/integration/test_rendered_page_gate.py tests/integration/test_help_links_render.py tests/unit/test_reachability.py tests/integration/test_workspace_permissions_matrix.py -q` → all pass.
- [ ] `scripts/build_css.sh --check` → no drift.
- [ ] Full CI → `CI passed`.
- [ ] Browser (`/lathe:browser-testing`, harness on the workstation): the list's tabs, filters,
  sort and a quick review landing on the next card; the details page's jump bar, Expand all, Show
  full message and a review.
- [ ] Audit: `lathe:plan-auditor` (Phase 1 tasks + diff), `lathe:semantic-reviewer`,
  `lathe:security-reviewer` (redirect state, markdown rendering). Fix confirmed findings; rerun the
  affected checks.
- [ ] Commit (spec + plan + Phase 1) on `blackbird`, with the session's attribution lines.
- [ ] Deploy `blackbird-app` only: porcelain check; image tag `rollback-pre-projui` and git tag
  `rollback-pre-projui-git`; `$DC build blackbird-app`; `.build_info.json` commit; `$DC up -d
  --no-deps blackbird-app`; health 200; copi-edge IP unchanged; read-only render of the list and the
  ready project as admin and manager → 200, new sections present.

## Phase 2 — assignments (`0075`)

### Task 2.1: Schema

**Files:** Create `alembic/versions/0075_project_review_assignments.py`; modify
`src/models/hub_project.py` (and `src/models/__init__.py` if the model must be exported there),
`scripts/migrate/preflight.py`, `scripts/migrate/postflight.py`,
`tests/unit/test_migration_checks.py`, `tests/integration/test_harness_smoke.py`,
`docs/operations/migration-deploy-notes.md`, `CLAUDE.md` (in place).

**Interfaces:** Produces C7's model and names.

- [ ] **Step 1: Tests first**: in `test_migration_checks.py`, `DEFAULT_TARGET == "0075"`,
  `test_0075_plans_the_assignment_table` (`planned_objects_between("0074", "0075")` names the table,
  its unique, its indexes and FKs as 0073's tests do); in `test_harness_smoke.py`, head `0075`.
- [ ] **Step 2: Migration** per spec §7.1, written like `0073_hub_projects.py` (module docstring with
  OLD/NEW code statements; downgrade drops the table). Model mirrors `AssessmentReviewAssignment`.
- [ ] **Step 3: Chores**: preflight `DEFAULT_TARGET="0075"`, `REVISION_ORDER` + `"0075"`,
  `SUPPORTED_START_REVISIONS` + `"0074"`, `PLANNED_OBJECTS` entries; postflight pins the table's
  columns, unique, indexes and FKs (and `VERIFIED_REVISIONS` + `"0075"`); deploy box "Deploy order for
  `0075_project_review_assignments`" (migrate before the new image serves; only between projects runs,
  D61; worker idle; no agent rebuild; old code safe; new code on the old schema raises
  `UndefinedTable` on both project pages; rollback = rollback images then `alembic downgrade 0074`);
  `CLAUDE.md` "head is `0074`" → "`0075`" in place.

### Task 2.2: Assignments in the service, routes and pages

**Files:** Modify `src/services/project_reviews.py`, `src/routers/reviews.py`,
`src/services/projects_view.py`, `src/routers/workspace/projects.py`, `src/web/presentation.py`,
`templates/workspace/projects.html`, `templates/workspace/project_detail.html`; create
`tests/integration/test_project_assignments.py`.

**Interfaces:** Consumes C1–C7. Produces C7's service, routes and view keys.

- [ ] **Step 1: Tests**: `test_staff_assign_and_unassign` (manager assigns a reviewer → row; again →
  still one; unassign → none; redirect keeps state, `#reviews`), `test_reviewer_cannot_assign` (403),
  `test_impersonated_assign_refused` (403), `test_assignee_must_be_allowed_staff_or_reviewer` (PI →
  400), `test_assigned_to_me_filter_and_counts`, `test_card_and_detail_show_assigned_names`,
  `test_assign_form_only_for_staff`, `test_run_deletion_cascades` (delete the run in a test DB → the
  assignment row is gone), `test_user_deletion_cascades`.
- [ ] **Step 2: Implement** C7; list title row gains "Assignment: All / Assigned to me"; card footer
  "Assigned: names"; detail Reviews card "Assigned (n)", "No one assigned.", Remove per row and
  "Assign to" select + "Assign" for staff (not under impersonation), forms carrying
  `surface=project-detail` and `list_state`.

### Task 2.3: Help for assignments

**Files:** Modify `templates/help/pages/projects.html`, `templates/help/pages/glossary.html`.

- [ ] **Step 1:** A section "Assigning projects" (`{% if permissions.staff %}` for the how-to; a
  reviewer reads "A manager can assign you projects; they appear under Assigned to me."), with a
  `careful()` callout for assigning (visible to the assignee, undone with Remove). Register the new
  anchor in `src/web/help_pages.py` only if the page's anchors list must name it (check the
  registry's `anchors` for `projects`).

### Phase 2 verification, audit, commit (main session)

- [ ] Focused tests for Tasks 2.1–2.3 plus the Phase 1 page tests; full CI; plan-auditor +
  semantic-reviewer; fix; commit. Not deployed until a between-runs window (Release R2).

## Phase 3 — project chat (`0076`)

### Task 3.1: Schema

**Files:** Create `alembic/versions/0076_project_chat.py`; modify `src/models/assessment_chat.py`,
`scripts/migrate/preflight.py`, `scripts/migrate/postflight.py`,
`tests/unit/test_migration_checks.py`, `tests/integration/test_harness_smoke.py`,
`tests/integration/test_assessment_chat_schema.py` (its exact `CHECKS` and index sets),
`docs/operations/migration-deploy-notes.md`, `CLAUDE.md` (in place).

**Interfaces:** Produces C8's columns, checks, indexes and constants.

- [ ] **Step 1: Tests first**: schema test expects the three new checks and indexes;
  `DEFAULT_TARGET == "0076"`; `test_0076_plans_project_chat_objects`; harness head `0076`.
- [ ] **Step 2: Migration** per spec §8.2/§8.8: add columns, FKs, indexes and checks; `ALTER COLUMN
  assessment_id DROP NOT NULL`; downgrade: `DELETE FROM assessment_chat_turns WHERE project_id IS NOT
  NULL`, drop the new checks, indexes and columns (usage and opens rows kept), restore NOT NULL. The
  ORM maps `AssessmentChatTurn.assessment_id` as `Mapped[uuid.UUID | None]`.
- [ ] **Step 3: Chores** as Task 2.1 Step 3 for `0076` (deploy box: only between projects runs; the
  chat tables' brief ACCESS EXCLUSIVE; old code safe; new code on the old schema raises
  `UndefinedColumn` on every chat request; the downgrade keeps ledger rows).

### Task 3.2: The chat service takes a project subject

**Files:** Modify `src/services/assessment_chat.py`; create
`tests/integration/test_project_chat_service.py`.

**Interfaces:** Consumes C8 (model), `load_project_chat_record` (Task 3.3). Produces C8 service API.

- [ ] **Step 1: Tests** (`test_project_chat_service.py`, with the assessment chat tests' fakes):
  `test_project_turn_round_trip` (prepare → run with the fake client → history shows it),
  `test_conversations_do_not_mix` (project A's turns absent from project B's history and from an
  assessment's), `test_turn_cap_per_project`, `test_clear_is_per_subject` (Clear on project A leaves
  project B and assessment turns), `test_one_answer_in_flight_across_subjects` (a streaming
  assessment turn refuses a project ask with 409 `answer_in_progress`),
  `test_limits_are_shared` (assessment spend counts toward the project ask's per-user ceiling),
  `test_exactly_one_subject_required` (both or neither → ValueError), `test_ledger_and_opens_record_subject`,
  `test_project_prompt_path_used`, `test_missing_project_prompt_is_503`.
- [ ] **Step 2: Implement** C8 without changing any assessment-path behaviour, refusal order or the
  lock order `tests/integration/test_chat_spend_lock.py` pins (one `prepare_turn`, one
  `clear_history`, one `build_request(` call after the lock). Per-kind hooks: record loader
  (`load_chat_record` / `load_project_chat_record`), revision (`_assessment_revision` / always 1),
  may-change (`verdict_may_change` / always False), prompt path. Never build a filter with a None id.
  `_has_streaming_turn`, `_questions_in_window`, `spend_24h` and `sweep_stale` stay unfiltered by
  subject. Log lines say "Chat" or name the subject kind.

### Task 3.3: The project record and opening questions

**Files:** Create `src/services/project_chat_record.py`, `tests/unit/test_project_chat_record.py`,
`tests/integration/test_project_chat_parity.py`; modify `src/services/assessment_chat_suggestions.py`
(`INLINE_SECTIONS` only).

**Interfaces:** Consumes C3 (`build_project_detail(viewer=None, is_admin=False,
impersonating=False)`, `project_display_kind`), `ChatRecord`, `BlockTarget`, `_Doc`, `url_tokens_in`,
`quoted_lines`, `_label_safe`, `tier_for` (`assessment_chat_record`), `Suggestion`, `KIND_TEMPLATE`.
Produces C8 record API.

- [ ] **Step 1: Tests**: unit — five documents titled Project, Conversation, Specialists, Research,
  Reviews; anchors exactly as spec §8.4; `url_tokens` hold the PubMed/ClinicalTrials links and reply
  URLs only; legends contain none of "blocking", "adequate", "confidence", "signal"; the record of a
  detail with costs carries no cost; template suggestions: Not agreed → a `positions` question about
  unresolved points, Ready → one about acknowledged limits, always ≤ 4, never containing "fund",
  "score" or "rate"; `Suggestion(anchor="evidence").section == "evidence"`. Parity (modelled on
  `tests/integration/test_assessment_chat_parity.py`): for admin, manager and reviewer, every quoted
  token appears on that role's `/workspace/projects/{id}` page (corpus inside `<main>` without
  `<aside id="assessment-chat">`), every anchor is an element id there, and sentinels for a consult's
  `verdict_signal`, `confidence`, `raw_opinion`, the costs, `research` JSON and another project's
  title are in no record.
- [ ] **Step 2: Implement** C8 record API and the `INLINE_SECTIONS` addition. Template questions:
  positions — Not agreed "What did BlackbirdBot and the lab leave unresolved, and why?", Ready "Which
  of the acknowledged limits matter most for this plan?"; evidence "Which specialist concerns does the
  final plan not address?"; brief "How does this project build on the lab's own work?"; plan "What do
  the plan's time and budget cover, and what is left out?"; generic "What is being proposed, in plain
  terms?".

### Task 3.4: Router, settings and the details route

**Files:** Create `src/routers/project_chat.py`, `tests/integration/test_project_chat_routes.py`;
modify `src/main.py`, `src/config.py`, `src/routers/workspace/projects.py`,
`tests/integration/test_rendered_page_gate.py` (exemption for `/project-chat/{project_id}`).

**Interfaces:** Consumes C8 service and record APIs. Produces the four routes and the detail
context keys `chat_enabled` (bool), `chat_available` (bool), `chat_suggestions`.

- [ ] **Step 1: Tests**: refusal order — impersonating 403, PI 403 `forbidden`, either switch off 503
  `disabled`, bad id 404, unknown project 404, open project 409 `project_open`, POST without JSON 415;
  history/ask/clear/opened happy paths with the fake client; `Cache-Control: no-store`; SSE headers;
  the details page shows the drawer only for a finished project with both switches on and not
  impersonating.
- [ ] **Step 2: Implement**: the router mirrors `src/routers/assessment_chat.py` (import its `_error`,
  `_is_json`, `_body`, `_NO_STORE`, `_STREAM_HEADERS`); `_refused` adds
  `app.state.project_chat_enabled`; finished = `project_display_kind` in ready, not_agreed,
  not_finished. `src/config.py`: `project_chat_enabled: bool = False`; `src/main.py`: set
  `application.state.project_chat_enabled` and include the router at `/project-chat` beside the
  assessment chat's. Details route: `chat_enabled = app flags`, `chat_available = chat_enabled and not
  impersonating and finished`, `chat_suggestions = project_page_suggestions(...)` (a pure function).

### Task 3.5: Drawer and page wiring

**Files:** Modify `templates/assessments/_chat_drawer.html`, `static/js/assessment_chat.js`,
`templates/workspace/project_detail.html`, `templates/workspace/projects.html`; create
`tests/integration/test_project_chat_templates.py`; adjust `tests/integration/test_assessment_chat_templates.py`
only where it pins a literal this task parameterises.

**Interfaces:** Consumes C8 drawer variables and Task 3.4's context keys.

- [ ] **Step 1: Tests**: the assessment detail page's drawer markup is unchanged (same strings, ids,
  URLs); the project page's drawer has `/project-chat/<id>` URLs in the nonce'd block, "Ask about this
  project", `subject: "project"`, the project error strings; the JS still names no route; the card's
  "chat" button links to `#chat` only for finished projects with the chat available.
- [ ] **Step 2: Implement**: partial branches on `chat_kind` for the four literal URLs and strings;
  JS merges `cfg.errors` over its table, adds `project_open`, and treats `verdict_revision` as absent
  for projects (no divider); project page includes the partial (`chat_kind="project"`,
  `chat_subject_id=project.id`) under `chat_available`, an inline-asks macro like
  `_detail_body.html:25-38` at the end of Brief, Evidence, Plan and Positions, and the nav sentence
  "Chat unavailable while impersonating" when the chat is on but impersonating; list cards get the
  "chat" button.

### Task 3.6: Prompt, operations doc and help

**Files:** Create `prompts/project-chat.md`, `tests/unit/test_project_chat_prompt.py`; modify
`docs/operations/assessment-chat.md`, `templates/help/pages/projects.html`, `src/web/help_pages.py`
(anchor `chat` on `projects` if the registry needs it).

- [ ] **Step 1: Prompt** per spec §8.5, reusing `prompts/assessment-chat.md`'s generic rules
  verbatim; pinned sentences (tested): the record is one project; the posts are between AI agents;
  "Do not rate the project, recommend whether to fund it, or write the user's review."; the link rule.
- [ ] **Step 2: Docs**: a "Project chat" section (routes, switch `PROJECT_CHAT_ENABLED`, finished
  projects only, one budget with the assessment chat, record documents, `subject` in the operator SQL);
  correct the stale `/admin/…` and `/manager/…` paths at the top. Help: "Asking about a project"
  (where it appears, what it can and cannot see, that it never rates or recommends; no limits stated).

### Phase 3 verification, audit, shots, commit (main session)

- [ ] Focused: every `tests/*/test_*chat*` file plus the project page tests; full CI.
- [ ] Audit: plan-auditor, semantic-reviewer, security-reviewer (subject branching, router, drawer);
  fix; rerun.
- [ ] Help shots (workstation): `capture --check`; recapture each stale shot with `--only`; commit the
  PNGs with `manifest.json`.
- [ ] Commit Phase 3.

## Release R2 — phases 2 and 3 (main session, D61)

- [ ] Only when no projects run is live (`simulation_process_status` not `running` for a run with a
  Projects session) and no `jobs` row is `processing`; otherwise stop here and report readiness.
- [ ] Rollback tags `rollback-pre-0075` (images) and `rollback-pre-0075-git`; build `blackbird-app`;
  `run_migration.sh` rehearsal to `0076`, then `--apply`; postflight VERIFIED; `$DC up -d --no-deps
  blackbird-app`; health; smoke the assign forms as admin (read-only render).
- [ ] Project chat smoke: one real question on a finished project from a one-off container with
  `PROJECT_CHAT_ENABLED=true`; on a complete answer set `PROJECT_CHAT_ENABLED=true` in `.env` and
  `$DC up -d --force-recreate --no-deps blackbird-app`.

## Plan audit (2026-10-28) and execution amendments

One adversarial audit (Opus) found 9 blockers, 18 medium and 11 low findings. All phases were then
executed in one batch (D65), so the dead-code finding on `project_display_kind` (B7) no longer
applies. The amendments sent to the packages, and binding on this plan:

- **C1:** a `lab` containing a control character is dropped; `next_id` must parse as a UUID.
- **C2:** `review`, `sort`, `mine` and `assignment` take defaults. A not-finished project with no
  reason reads "No reason recorded". `not_finished_reasons` holds dicts.
- **C3:** tool-mark candidates are the method and model items plus the anchors from those sections,
  matched with `(?<!\w)…(?!\w)` lookarounds.
- **C6:** the "moved off the tab" rule also applies with `mine=1`.
- **C8:**
  - `load_system_prompt(path: Path | None = None)` resolves its path at call time.
  - `assessment_chat.py` imports `project_chat_record` inside the project hook only (an import cycle
    otherwise).
  - `_persist`, `_log_completion` and `run_turn` branch on the subject, and the assessment log text
    is unchanged.
  - `/project-chat/` joins the JSON-error prefixes (`src/web/errors.py`) and the login-redirect
    exclusion (`src/dependencies.py`).
  - The drawer resolves `chat_kind` and `chat_subject_id` with `is defined`, never
    `default(a.id)`.
- **Projection:** `raw_choice` and `raw_final` go in `VIEW_FIELDS`, `JSON_FIELDS` and `ADMIN_KEYS`;
  `list_state` in `VIEW_FIELDS` and `DYNAMIC_MAP_FIELDS`; `chat_available` and
  `card_chat_available` in `VIEW_FIELDS`.
- **Markup pins:**
  - card title `<h2 class="project-card-title …"><a …>{{ title }}</a></h2>` inside the
    `id="p-<uuid>"` card;
  - tiles `project-tile` / `project-tile-count`;
  - the rating placeholder `<option value="" disabled selected>—</option>`, on a `required`
    select.
  - Tailwind classes not compiled (avoid them): `bg-slate-100`, `text-slate-600`, `border-l-4`,
    `pl-4`, `pl-6`.
- **Tests added to ownership:**
  - `tests/integration/test_migration_0073.py`: the new table, and downgrading through 0076/0075.
  - `tests/integration/test_reviews_router.py`: the POST allowlist grows to 13.
  - The `SUPPORTED_START_REVISIONS` pin.
  - `tests/unit/test_reachability.py`: the unresolvable same-page link count. Main session, at
    integration.
  - `tests/integration/test_rendered_page_gate.py`: seed an unreviewed project with consults, gaps,
    thread posts and an open project. Main session.
  - `tests/integration/test_migration_0073.py` downgrade order.
- **Release R2:** a real-model smoke question through a one-off script in the scratchpad (not in the
  repository), then `PROJECT_CHAT_ENABLED=true`. Phase 2's assign and unassign routes join the
  security review.
