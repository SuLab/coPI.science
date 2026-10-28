# Projects pages modelled on the Assessments pages: list, details, quick reviews, assignments and chat — design

> Status: revision 3, approved by the owner 2026-10-27 ("approved, write the plan, audit the plan, then
> fully execute the plan"). Revision 2 folded in one adversarial design audit (21 findings, 13 builder
> defaults; §12); revision 3 records the owner's answers to its four questions (D61–D64). On 2026-10-28
> the owner ordered all phases executed together (D65, §1). Plan:
> `docs/plans/2026-10-27-projects-pages-redesign-plan.md`.
> Owner request (2026-10-27, verbatim): "visually analyze the projects web ui page and compare it to the
> assessments list page and assessments detail page. we need to model the new project page after the
> existing assessments pages with similar details, summaries, and separation of sections, and other
> helpful web ui details. do not assume anything and do not rely on your memories or training. confirm
> your findings with an adversarial audit"
> Evidence: the analysis and its audit (Appendix A). Builds on
> `docs/specs/2026-10-22-idea-pitching-prototype-design.md` (the projects prototype; D41–D55).

## 0. Summary

The Projects list and details pages show the right data in the wrong shape: one undivided column of
small text, nothing summarised, nothing collapsible, the review form at the bottom of a 20,000-pixel
page, and several defects (§5). This design gives them the Assessments pages' shape — count tiles,
review tabs, content-bearing cards with a quick review, a brief at the top of the details page, an
evidence summary, a card per section, a jump bar, the conversation as a collapsible timeline — without
anything the owner ruled out for projects: no score, no recommendation, no specialist label or
confidence (D43, D55).

Three phases, each committed, CI-green and deployed before the next:

1. **Pages** (no migration, `blackbird-app` only): the defects, the list, the details page, review
   tabs and quick reviews. Deployable while a projects run is live.
2. **Assignments** (migration `0075`, then `blackbird-app`): staff assign projects to reviewers.
3. **Chat** (migration `0076`, then `blackbird-app`): "Ask about this project", built on the assessment
   chat.

No phase changes the agent, the worker's code, a prompt the engine reads, or the interview pages. The
migrations of phases 2 and 3 run only between projects runs (D61).

## 1. Owner decisions

Carried from the projects prototype (§4 of its design): **D42** staff see "details similar to what is
seen on the assessment list and details pages", and project reviews stay separate from assessment
reviews; **D43** projects "should not carry scores and recommendations, staff and reviewers will
judge"; **D52** staff and reviewers only; **D53** a review is a 1–5 rating, a comment, and fund or
don't fund; **D55** the Projects page shows what each specialist found, never its
`blocking`/`gap`/`adequate` label or its confidence.

New, 2026-10-27 (answers to the analysis's five questions, verbatim):

- **D56** (what "reviewed" means for the list's tabs) "reviewed by anyone"
- **D57** (a review form on the list's cards, like Quick scoring) "add quick reviews"
- **D58** (an "All runs" option) "no"
- **D59** (assignments for projects) "yes"
- **D60** (a chat on projects) "yes"

Answers to the design audit's owner questions, 2026-10-27 (the option each chose, verbatim):

- **D61** (when `0075` and `0076` may run) "Only between runs (Recommended)"
- **D62** (when a tool counts as in the lab's profile) "Methods and models only (Recommended)"
- **D63** (the specialists' Concerns on a Ready project) "Neutral (Recommended)"
- **D64** (the project chat's limits) "Share one budget (Recommended)"

2026-10-28: **D65** (how to run the phases) "proceed with executing all phases in parallel with the
investigations, do not start any new simulations" — the three phases are implemented, verified and
deployed together; the migrations `0075` and `0076` run in one window with no projects run live
(D61), which supersedes §0's and §4's one-phase-at-a-time order.

## 2. Goals, non-goals, anti-goals

Goals:
- PG1. A reviewer can judge a finished project from its list card and record a review there.
- PG2. The details page reads top-down like an assessment: what it is, what was established and what
  was questioned, the plan, the positions, the reasons, the sources, the reviews, then the
  conversation.
- PG3. Nothing a project row stores is lost or garbled on the way to the page.
- PG4. Staff can assign projects; anyone who can open a finished project can ask the chat about it.

Non-goals: scores, recommendations, bands or rubric dimensions for projects (D43); an "All runs" view
(D58); generated opening questions for the project chat (§8.6); changing how projects are researched,
posted, refined or closed; the interview prompt sets; the Assessments pages' behaviour.

Anti-goals:
- A specialist's label or confidence reaching a project page or the project chat, directly or through
  how rows are sorted or coloured (D55).
- Anything on a Ready project styled as a verdict against it (D43): its gaps are the plan's
  acknowledged limits.
- A deploy step that stops or recreates the agent or the worker while a projects run is live.

## 3. Constraints

- **Web tier only.** The engine computes and stores every project field; this design reads them.
  Where a stored value reads badly (the tool marks, F3), the page derives its display from stored data
  and the lab's current public profile; it never rewrites a row.
- **The workspace projection** (`src/web/presentation.py`) keeps a dict key only if it is in
  `VIEW_FIELDS`, or the field is in `JSON_FIELDS` (kept whole) or `DYNAMIC_MAP_FIELDS` (keys kept);
  `ADMIN_KEYS` are dropped for non-admins. Every new key is registered accordingly (§6.5); a missing
  registration renders as nothing, with no error.
- **Templates** keep the rendered-page gate (one `<main>`, one `<h1>`, no skipped heading level, every
  `<nav>` named, no `text-gray-300`/`-400`, no inline `on…=` handler, named form controls) and the CSP
  (a script runs only from a file or as a nonce'd block, like the chat drawer's URL configuration),
  and every action path stays a literal (`tests/unit/test_reachability.py`).
- **Model-written text** renders autoescaped or through the shared sanitizer (`static/js/markdown.js`),
  never `| safe`.
- **Project review writes** keep refusing an impersonated session (prototype §5.11); every review form
  is hidden then.
- **Help** changes in the same commit as the pages it describes, under
  `docs/operations/help-content.md`: the `projects` page (`list`, `detail`, `reviewing`, and a new
  section per new feature, each registered as an anchor in `src/web/help_pages.py`),
  `getting-started#navigation`, reviewer step 1, manager step 2, the glossary and troubleshooting.
- **Help screenshots.** No shot shows a Projects page. Phase 1 must not edit `templates/_run_bar.html`
  (a source of the `assessments-list` and `discussions` shots): F9 is done in the view model.
  Phase 3 edits `templates/assessments/_chat_drawer.html` (the `chat-drawer` shot's source), so the
  shot is recaptured on a workstation as the last step before that deploy. Any phase whose templates
  add a Tailwind class not already in `static/css/app.css` changes `app.css` and stales every shot: a
  full recapture before that deploy.
- **`CLAUDE.md`** is at 199 of its 200 lines; its "head is `0074`" sentence is updated in place when
  `0075` and `0076` land, adding no line.

## 4. Phases and deploys

| Phase | Schema | Deploy | During a live projects run |
|---|---|---|---|
| 1 Pages | none | build and recreate `blackbird-app` | yes |
| 2 Assignments | `0075`: one new table | migrate, then recreate `blackbird-app` | no: between runs (D61) |
| 3 Chat | `0076`: columns on the three chat tables | migrate, recapture the drawer shot, recreate `blackbird-app` | no: between runs (D61) |

Migrations follow `alembic/CLAUDE.md` and `docs/production-migration.md`: committed before the build,
rehearsed with `run_migration.sh` without `--apply`, applied from a one-off container off the new
image. Both new revisions create foreign keys to `users` (and `hub_projects`), which lock those tables
briefly; like `0059` they need the worker idle — no `jobs` row `processing` — when `--apply` runs, and
preflight BLOCKs while any session is idle in a transaction or has one open longer than 5 s. D61: they
run only when no projects run is live (`/workspace/simulation` shows none), so no run is ever stopped
for them; the chain's 10 s `lock_timeout` still makes a blocked apply fail cleanly.

## 5. Phase 1: defects

| # | Defect (Appendix A) | Fix |
|---|---|---|
| F1 | The Conversation shows only the first of the posting's two Slack messages: `_timeline` selects the row whose `message_ts` is the posting's, and the continuation is logged with `thread_ts = posting_ts` (`src/agent/engine/slack_io.py`). The sources and the closing line are lost on every project, a plan field on some. | The posting entry shows `HubProject.posting_text` (the text the engine posted, chunks joined), timed by its first logged row, else `posted_at`. If fewer chunks were logged than `split_for_wire(posting_text)` produces, a line says part of the posting may not have reached Slack (a later chunk's send can fail after the first one posts). The lab's reply needs no change: the engine refuses a reply that would not fit one message. |
| F2 | The new-review rating select has no blank option, so it opens on 1. | Every new-review form (details and list) opens on a disabled, selected "—". The edit form keeps the stored rating. |
| F3 | Tool marks: a closed project's tools are exact-matched against profile items, so the close's wording reads "not found in the lab's profile" in amber while "Built on" lists the same item. | The page derives each mark: a tool is "in the lab's profile" when, after `normalize_item` on both sides, it contains as whole words an item of at least two words from the profile's Key Methods and Technologies or Model Systems (`METHOD_MODEL_SECTIONS`) or one of the project's own stored anchors; the longest match wins and is named ("matches: …"). Targets and keywords never mark a tool (a tool that names a target the lab studies is not a tool the lab uses). Otherwise the tool reads "stated by the lab" (the prototype spec's words), in neutral grey. One list: the plan rows drop the `lab_tools` row (D62). |
| F4 | Model text is printed raw: Markdown (`**`, `- ` lists) shows literally. | The lab's reply and the thread's posts render through the shared sanitizer (`data-markdown`), as the interview timeline does. The posting stays plain text with its line breaks: it is assembled by code with single newlines, which the sanitizer would join into one paragraph. Consult text stays plain. |
| F5 | List → details → "Back to Projects" drops the run, lab and outcome. | The list's state travels both ways (§6.1); the back link reads "← Projects". |
| F6 | The plan is shown three times (Plan, Opening plan, the posting) and, with no close, Plan and Opening plan are identical. | One plan card (§6.4): on a closed project the final plan, each field the close changed marked "changed since posting" with its opening value one click down; with no close, the opening plan once, titled "Plan (as posted)". The posting stays inside the collapsed conversation. |
| F7 | A specialist reply that was cut off looks complete. | Consults also select `SpecialistConsult.truncated`; a cut-off reply gets a "reply cut off" chip and a project sentence: "This specialist's reply stopped before it finished, so what follows is partial." No word about signals or the specialist floor (D55). |
| F8 | With `posting_ts` set but no log rows, the page says "Nothing has been posted yet." beside a Posted time. | The posting always shows (F1). With `reply_ts` set but no thread rows, the page says "Thread messages unavailable", worded like the interview timeline's state. |
| F9 | The projects run bar prints "rubric x" though projects carry no rubric scores. | The projects view sets each run-bar entry's `rubric_version` to None; the macro already hides a falsy value. No template change. |
| F10 | A closed project can store no lab position, and its chip then reads "No close recorded" beside a close. | With a close, a missing position reads "No position stated"; "No close recorded" only without one. |
| F11 | The list loads every project's `research` JSON, which no card shows. | The list query defers the column. |

## 6. Phase 1: pages

### 6.1 State in URLs

The list reads `run_id`, `lab`, `outcome`, `review`, `sort`, `mine` (and `assignment` from Phase 2).
Each link from the list to a project carries them; the details page validates them by the list's
rules and puts them on its back link and in a hidden block in every review form (new, edit, delete).
`src/routers/reviews.py` gets a projects version of `_list_filter_query`: `run_id` a UUID, `lab`
opaque, the rest known values only, everything through `urlencode`, a junk value dropped, never
echoed. Two surfaces: `projects-list` returns to the list at the card's anchor; `project-detail`
returns to the details page's `#reviews` with the state. A post with neither (an old open tab)
behaves as today.

### 6.2 The list

1. **Title row** like Assessments: "Projects" and the help link on the left; on the right **Sort**
   (Recently finished, the default and today's order; Lab: by lab, then recently finished; Outcome:
   Ready, Not agreed, Not finished, In progress, then recently finished), **Lab**, **Outcome** (today's
   five options), **Not reviewed by me** (kept: with D56 tabs one review moves a project off everyone's
   Unreviewed tab, and this is how a reviewer still finds what they have not reviewed) and **Apply**.
2. **Run bar**, one run at a time (D58), without a rubric label (F9).
3. **One line of scope**: what a project is, and that the help page explains each outcome.
4. **Review tabs** (D56): **Unreviewed (n) · Reviewed (n) · All (n)**, Unreviewed by default; a
   project is reviewed once anyone has reviewed it. Counts follow the run, lab, outcome, "Not reviewed
   by me" (and assignment) filters, like the Assessments tabs.
5. **Not-finished banner** (the "verdicts lost" banner's counterpart): when projects of the run ended
   Not finished, an amber box names each reason in words with its count, counted across the run
   whatever the filters (it says so), with a link to the Not finished filter.
6. **Count tiles**: Total · Ready · Not agreed · Not finished · In progress (In progress includes
   Paused), over the run under the lab, tab, "Not reviewed by me" and assignment filters but not the
   outcome filter, so a filtered view never shows four zeros. Not links. Under them, today's
   run-wide line: reviews, mean rating, share that said fund.
7. **Cards**, one per project, `id="p-<project id>"`, for finished projects:
   - **Header**: the title as headline; the lab linked to its PI page (a `pi_user_ids` map, as the
     Assessments list does) · posted · finished; on the right the outcome chip with its reason.
   - **Two boxes** like "In one minute" / "Key points": the summary (`choice.summary`); and Indication,
     Target or mechanism, Time and budget (final plan, else opening plan), the two positions (F10),
     and how many gaps and disagreements the close recorded.
   - **Buttons**: "detail →" (and "chat" from Phase 3).
   - **Footer**: Reviewed by — each reviewer with rating and Fund/Don't fund, or "—"; "reviewed by
     you"; Assigned (Phase 2).
   - **Quick review** (§6.3).
   A project still open gets a compact card: title or "Untitled project", the lab, a stage chip and a
   time: Waiting for research (requested at), Researching and Ready to post (last activity, labelled
   so), Posted (posted at), Refining (the lab's reply's time); "Paused" before the stage when its run
   is not live. No review controls on the list; its details page keeps "Your review", as today.
8. **Empty state** per tab, worded like the Assessments list's.

### 6.3 Quick review (D57)

- On each finished project's card (Ready, Not agreed, Not finished), a **Quick review** disclosure,
  open by default while the viewer has not reviewed the project: Rating ("—", 1–5), Fund / Don't fund,
  Comment, **Submit review**, and "One review per person; you can edit yours on the project's page."
- Once the viewer has reviewed it: "Your review: 4/5 · Fund" and a link to edit it on the details page.
- Under impersonation the disclosure is replaced by "Reviews can't be written while impersonating."
- The form posts to the existing `POST /reviews/projects/{id}/review` with `surface=projects-list`,
  the list's state and `next_id` (the following card, "" on the last). The route returns to the list
  at `#p-<id>`; when the review takes the card off the Unreviewed tab it lands at `next_id` instead.
  One flash per outcome: "Review saved." where the card stays; "Review saved; moved to Reviewed." where
  it leaves; on a duplicate only "You have already reviewed this project; edit your review on its
  page.", landing as a success would.

### 6.4 The details page

Inside a centred `max-w-5xl` column, with the assessment pages' prose measure (`assessment.css`):

1. **Back link** "← Projects" (with the state), **H1 "Project detail"**, the help link; for admins a
   **"Model calls for this project"** button (today's calls link) and the cost line.
2. **Jump bar** ("On this page", sticky): Brief · Evidence · Plan · Positions · Research · Reviews ·
   Conversation, with **Expand all** / **Collapse all**, driven by `assessment_timeline.js`.
3. **Brief** (card): the title as headline; Lab (PI link) · run start time · posted · finished; the
   outcome chip and reason on the right. Left: **In one minute** (the summary) and, under it, **Why
   this use** in the "Why this score" box style (reasons in words). Right: **Key points** — the two
   positions, **Built on** (anchors with their profile sections), and counts of sources, specialist
   consults, posts in the thread and other uses considered, each linking to its section.
4. **Evidence summary** (card, never collapsed): per specialist domain, from the latest consult in
   that domain, **Established** and **Concerns** rows ("domain — n concerns", a two-line preview,
   **show**), then **Questions to ask** in the same shape; a domain with earlier consults says they are
   in the conversation. Grouped by content and domain only: no label, no confidence, no order or
   colour derived from either (D55). Established is green; Concerns and Questions to ask are neutral
   slate on every outcome (D63). A legend and a provenance sentence like the Assessments'.
5. **Plan** (card): label/value rows Indication … Time and budget, with F6's change marks; then **The
   lab's tools and models**, one list with F3's marks.
6. **Positions** (card): a chip per bot (Agreed, Not agreed, No position stated, No close recorded).
   On a Not agreed project, **Gaps** and **Disagreements** as an always-open amber card, like Red
   flags. On a Ready project the same lists read **Acknowledged limits**, open, neutral.
7. **Also considered (n)** (collapsed card): the other uses with their notes.
8. **Research** (card): sources grouped as Papers, Clinical trials and Patents, each linked as today,
   with the specialist that found it; counts in the heading.
9. **Reviews** (card, before the conversation): what the rating and Fund mean (the help page's
   words); the reviews (name, rating, Fund chip, date, "(edited)" when `updated_at` is later than
   `created_at`, comment, Edit for the author, Delete for an admin); **Your review** (F2); Assigned
   (Phase 2).
10. **Conversation** (collapsed card, like Interview timeline): an intro sentence; the posting (F1,
    plain), the lab's reply and the thread's posts in time order, markdown-rendered (F4), clamped
    with **Show full message**; each consult interleaved at its time as an indented dashed card with
    the specialist's domain, the question, Established, Concerns, Questions to ask, the concern count
    and F7's chip; on a closed project BlackbirdBot's last post in the thread is marked "closed the
    thread" (no post follows a close).
11. **Raw choice and close** (admin only, collapsed card): `choice` and `final` as JSON, like "Raw
    verdict JSON"; `research` is not shown.
12. **Print**: the assessment pages' print rules (open every disclosure, lift clamps, hide nav and
    forms), by including `assessments/_head.html`.

Element ids, for the jump bar now and the chat's citations in Phase 3: `brief`, `evidence`, `plan`,
`positions`, `others`, `research`, `reviews` (kept: the review routes return to it), `timeline`,
`m-<n>` (posts, numbered in timeline order) and `consult-<k>` (consults, numbered in timeline order).

### 6.5 Shared assets and code

- Project pages include `assessments/_head.html` (sanitizer, `assessment.css`, print script) and
  `assessment_timeline.js`, unchanged.
- `src/services/projects_view.py` builds the new fields. Registration in `src/web/presentation.py`:
  plain keys in `VIEW_FIELDS`; `raw_choice` and `raw_final` in `JSON_FIELDS` and `ADMIN_KEYS` (kept
  whole, admins only); the banner's reason counts and the tiles' counts as lists of pairs or as maps
  under names in `DYNAMIC_MAP_FIELDS` (today's summary `counts` survives only because `counts` is
  listed there); the lab-to-PI map as `pi_user_ids`, already listed.
- No change to `src/services/assessment_*`, `directory.py` or the assessment templates.

### 6.6 Verification

- Integration tests (`tests/integration/test_projects_pages.py`, `test_project_reviews.py`) rewritten
  for the new defaults and sections: tabs and counts, tiles, banner, cards, the quick review's
  redirects (anchor, next card, duplicate, junk state dropped, impersonation refused), state through
  every review write, F1–F11 each with a regression test (F3: a target-only tool and a keyword-only
  tool read "stated by the lab", a parenthesised method reads "in the lab's profile"), the D55 guard
  as today's tests do it (specific words plus seeded sentinel signal and confidence values, absent
  from every role's page), admin-only keys absent for managers and reviewers, an admin seeing every
  stored key of the raw choice and close.
- The permissions matrix, rendered-page gate, security headers, reachability and help-links tests
  pass with the new templates.
- Browser, through `/lathe:browser-testing` on the harness: the list (each tab and filter; a quick
  review from the Unreviewed tab landing on the next card) and the details page (jump bar, Expand all,
  Show full message, a review). The harness seeds no project today (`tests/e2e/ui_audit/seed.py`); it
  gains a Ready, a Not agreed, a Not finished and an in-progress project with a two-message posting, a
  thread, consults (one cut off) and reviews, all invented.

## 7. Phase 2: assignments (D59)

### 7.1 Table (`0075`)

`hub_project_review_assignments`, mirroring `assessment_review_assignments`: `id`; `project_id` →
`hub_projects.id` ON DELETE CASCADE (a run's deletion removes it); `assignee_user_id` → `users.id`
ON DELETE CASCADE (user deletion needs no code change); `assignee_name`; `assigned_by_user_id` →
`users.id` ON DELETE SET NULL; `assigned_by_name`; `created_at`; unique `(project_id,
assignee_user_id)`; indexes on `project_id` and `assignee_user_id`. Additive: old code ignores it.

### 7.2 Writes

`POST /reviews/projects/{id}/assign` and `/unassign`: staff only (admin, manager), refuse
impersonation, the assignee must be staff or a reviewer with allowed access (the assessments'
`_load_assignee`), idempotent (`on_conflict_do_nothing` naming the constraint / delete if present),
logged, returning to the details page's Reviews card with the state (§6.1). No assign form on the
list.

### 7.3 Pages

- List: **Assignment** (All / Assigned to me) in the title row, applied before the tabs and tiles;
  **Assigned: names** in each card's footer.
- Details: in the Reviews card, **Assigned (n)** with **Remove** for staff, and **Assign to** (the
  review-capable users) for staff; hidden under impersonation.

### 7.4 Migration and deploy

Deploy box "Deploy order for `0075`" in `docs/operations/migration-deploy-notes.md` (old code on the
new schema: safe; new code on the old schema: the details and list pages raise `UndefinedTable`; no
agent rebuild). `scripts/migrate/preflight.py` (`DEFAULT_TARGET`, `REVISION_ORDER`,
`SUPPORTED_START_REVISIONS`, `PLANNED_OBJECTS`), `scripts/migrate/postflight.py` (the table's pinned
columns, constraints and indexes) and the pins in `tests/unit/test_migration_checks.py` and
`tests/integration/test_harness_smoke.py` move with it; `CLAUDE.md`'s head sentence in place. It runs
only between projects runs (D61, §4).

## 8. Phase 3: chat (D60)

"Ask about this project": the assessment chat's drawer, streaming, citations, limits and refusals,
answering from a record of the project page. `docs/operations/assessment-chat.md` holds unless this
section says otherwise.

### 8.1 Where it appears

- A **finished** project's details page — finished as the page shows it (`display_outcome`: Ready,
  Not agreed, Not finished, including an open project of a finalized run): the bubble and drawer,
  inline "Ask:" buttons at the end of the Brief, Evidence summary, Plan and Positions cards, and a
  "chat" button on the list card (the `#chat` fragment, as on Assessments).
- Not on an open project, whose record would change under the conversation: the routes answer 409
  `project_open` (after the 404 and before the 415).
- Never under impersonation (every route refuses it; the page says so instead of a control), never
  for a PI (D52).

### 8.2 Storage: the chat tables take a project subject

The assessment chat's guarantees live in its tables: one answer in flight per user (a partial unique
index on `user_id`), the per-user and total daily limits (summed from the ledger), and the spend lock
around the limit checks and the insert. Parallel project tables would split all three. Instead:

- `assessment_chat_turns`: `project_id` → `hub_projects.id` ON DELETE CASCADE, with an index on
  `(project_id, user_id, context_tier, created_at)`; `assessment_id` becomes nullable; a check that
  exactly one of the two is set.
- `assessment_chat_usage` and `assessment_chat_opens`: `project_id` → `hub_projects.id` ON DELETE SET
  NULL, indexed; `subject` (`assessment` | `project`; NULL on rows written before `0076`, all
  assessments; not backfilled).
- No suggestions-table change (§8.6). Table names keep their `assessment_` prefix.

Which queries span subjects: `_has_streaming_turn`, `_questions_in_window`, `spend_24h` and
`sweep_stale` read across every turn or ledger row by design, so one answer in flight and the limits
cover both chats: one budget (D64). Every other read and write — the conversation, history, the
turn cap, Clear, persistence, the stale-turn check, opens — branches on the subject's kind and filters
by that kind's column; a subject id is required and is never passed as None (SQLAlchemy would render
`IS NULL` and match every row of the other kind). The ORM maps `assessment_id` as optional.

### 8.3 Service and routes

- `src/services/assessment_chat.py` takes a subject (kind and id) where it takes an assessment id
  today, with per-kind hooks: record loader, prompt path, revision (projects: always 1), "may change"
  (projects: never, §8.1), existence check. `prepare_turn` and `clear_history` stay single functions
  with today's lock order (`tests/integration/test_chat_spend_lock.py` pins it); the assessment path's
  behaviour and refusal order do not change.
- `src/routers/project_chat.py`, mounted at `/project-chat`: `GET /{id}`, `POST /{id}/messages`,
  `/clear`, `/opened`, on `get_current_user` with the same refusal order (impersonating, forbidden,
  disabled, then 404, 409 `project_open`, 415). The JSON `GET` joins the rendered-page gate's exemption
  list beside `/assessment-chat/{assessment_id}`.
- `PROJECT_CHAT_ENABLED`, default **false**: switched on in `.env` after the smoke question (§8.8), by
  recreating `blackbird-app` only (the worker does not read it). Either it or the assessment chat's
  switch off turns the project chat off. Model, effort and limits are the assessment chat's settings.
- Turns take `tier_for(user)` (the column is NOT NULL with a check); origins are the existing
  `typed`, `drawer_template` and `inline_template`.

### 8.4 The record

`src/services/project_chat_record.py` builds five citation documents from a viewer-free variant of
`build_project_detail` (no admin view, no viewer keys), reusing `ChatRecord`, `_Doc` and the URL-token
rules, with legends of its own that use no label or confidence word:

1. **Project**: title, outcome and reason, lab, dates, summary, why this use, other uses considered,
   anchors, positions, gaps or acknowledged limits, disagreements, the plan with each changed field's
   opening value, and the tools with their marks. Anchors `brief`, `plan`, `positions`, `others`.
2. **Conversation**: the full posting, the lab's reply and the thread's posts, one block each,
   anchored `m-<n>`.
3. **Specialists**: each consult — domain, question, established, concerns, questions to ask, cut off
   or not — anchored `consult-<k>`. Never its signal, confidence or raw opinion: the read model does
   not select them.
4. **Research**: each source with identifier, title, kind, the specialist that found it and the page's
   link. Anchor `research`.
5. **Reviews**: the project reviews (name, rating, fund, comment, date). Anchor `reviews`.

No tier differs: the page shows reviewers what it shows staff, apart from the admin-only cost and
calls link, which no record holds. Links: a URL becomes clickable in an answer only if the record
quotes it (D20); the replies' URLs are links on the page too (F4), the posting's are not and none was
seen. `tests/integration/test_project_chat_parity.py`, modelled on the assessment parity test: for an
admin, a manager and a reviewer, every quoted token of the record appears on that role's page, every
anchor is an element id there, and sentinel values for a consult's signal, confidence and raw
opinion, the costs, the research JSON and another project appear in no record.

### 8.5 Prompt

`prompts/project-chat.md`, read per question like `prompts/assessment-chat.md` (an edit applies to the
next question; a missing file answers 503). It keeps that prompt's generic rules — confidentiality,
general background, labels and quotes as data, the answer format, the link rule — drops its
assessment sentences (gates, "adequate", "pass", scores, the rubric), and states the project's own:
the record is one project; the posts are between AI agents, BlackbirdBot and the lab's agent,
refining a plan; the specialists are AI personas; the chat does not rate the project, recommend fund
or don't fund, or draft the user's review (D43). It is in no role's prompt set: no version bump; the
interview sets stay frozen.

### 8.6 Opening questions

A template set, at most four, each anchored to its card: the open points of a Not agreed project or
the acknowledged limits of a Ready one (`positions`), the specialists' concerns (`evidence`), how the
project builds on the lab's own work (`brief`), what the plan's time and budget cover (`plan`);
padded with generic questions. Generated sets would need a worker deploy (which interrupts project
research during a live run) and a ceiling of their own: not in this design.

### 8.7 Drawer

`templates/assessments/_chat_drawer.html` takes its URLs (literal `/project-chat/{{ project.id }}…`
in the nonce'd configuration block, as today), strings ("Ask about this project", "Questions about
this project", the error sentences) and subject kind from the including page; the assessment page
passes today's values, so its drawer renders as before. `static/js/assessment_chat.js` reads those
instead of its literals and names no route. The drawer keeps `id="assessment-chat"` (the script, the
tests, the help shot and the parity corpus key on it) and one width preference per browser. The
`chat-drawer` help shot is recaptured before the deploy (§3).

### 8.8 Migration, deploy, verification

- `0076`: the columns, check and indexes of §8.2. `ALTER TABLE` takes a brief ACCESS EXCLUSIVE lock
  on each chat table and the foreign keys lock `hub_projects` briefly; the chat tables are small
  (seven ledger rows on 2026-10-27). Old code on the new schema is safe (it never writes a NULL
  `assessment_id` and selects turns by `assessment_id`); new code on the old schema raises
  `UndefinedColumn` on every chat request. The downgrade deletes project turns only, then drops the
  columns and the check and restores NOT NULL; ledger and opens rows stay (the ledger outlives its
  subject). Deploy box, preflight, postflight and test pins as §7.4; only between projects runs (D61).
- Tests: the assessment chat suites pass unchanged except the schema test's exact check set and the
  drawer's literal pins; new route, service (project A's history, turn cap and Clear leave project B
  and every assessment untouched, and the reverse), record, parity, prompt and drawer tests; a
  security review of the subject change, the router and the drawer; a real-model smoke question on a
  finished project before the switch goes on.
- `docs/operations/assessment-chat.md` gains a "Project chat" section, corrects its stale `/admin`
  and `/manager` paths, and its operator SQL gains `subject`. The help page gains "Asking about a
  project", stating no limit (help content rule 3).

## 9. What is not copied, and why

| Assessment element | Why not |
|---|---|
| Score, band, recommendation chip, score and recommendation sorts, Dimension scores, Dimension distribution, Gating criteria and legend, the band sentence, "Why this score" as a score account | D43 |
| Rubric dimensions and "BlackbirdBot scored N" in the review form; Feedback mode; Approval status | D43; assessment-only workflow |
| Specialist signal chips, the panel banner's states, rows ordered by signal, consult confidence, raw opinions, the cut-off sentence's "signal"/"floor" wording | D55 |
| An "All runs" option | D58 |

D55 still allows, and this design uses: grouping consults by domain, a concern count, and "reply cut
off".

## 10. Risks

- **R1 Label leakage through layout.** Copying the assessment evidence derivation would place
  consults by signal. §6.4(4) groups by content only; the tests seed sentinel labels and assert them
  absent.
- **R2 Markdown links.** Rendering replies through the sanitizer turns their URLs into links, as the
  interview timeline does (DOMPurify allowlist: `http`, `https`, `mailto`). The template's link-rule
  comment changes with it; a security review covers it.
- **R3 Subject mix-ups in the chat.** A query given the wrong kind's column, or None, would mix
  conversations or clear the wrong rows. §8.2's rule and the cross-subject tests are the guard.
- **R4 Tab semantics.** With D56, one review moves a project off every reviewer's default tab; "Not
  reviewed by me" and the All tab remain.
- **R5 Migrations with live writers.** Avoided: phases 2 and 3 migrate between runs (D61).
- **R6 Help drift and stale shots.** Help in the same commit; the drawer shot recaptured before the
  Phase 3 deploy; a new Tailwind class means a full recapture.

## 11. Calls made in this draft (check while reading)

1. Tiles count across outcomes under the other filters, so they never read four zeros; not links.
2. "Not reviewed by me" stays beside the D56 tabs.
3. Sorts: Recently finished (default), Lab, Outcome.
4. The posting stays plain text; replies render through the sanitizer; consults stay plain.
5. H1 "Project detail" with the title in the Brief, like "Assessment detail".
6. Gaps on a Ready project read "Acknowledged limits", neutral; amber only on Not agreed.
7. Quick reviews on Not finished projects too, as the details page allows today.
8. The chat extends the assessment chat's tables with a project subject rather than adding parallel
   tables.
9. The chat is offered on finished projects only.
10. `PROJECT_CHAT_ENABLED`, off until the smoke question passes; model and effort shared (limits
    shared by D64).
11. Opening questions are a template set.
12. The project chat never rates a project, recommends fund or don't fund, or drafts a review.

## 12. Revision history

- Revision 1 (2026-10-27): first draft.
- Revision 2 (2026-10-27): one adversarial design audit (Opus). Folded in: the tool-mark rule limited
  to method and model items of two or more words plus the project's anchors (F-01); projection
  registration for raw JSON and count maps (F-02); the chat's subject branching and the corrected
  list of cross-subject queries (F-03); F9 moved out of the shared run-bar template and the help-shot
  consequences stated (F-04, F-18); the live-run migration procedure and an owner question (F-05);
  the `0076` migration section (F-06); state through every review write (F-07); F1's partial-posting
  note and the dropped reply branch (F-08); "closed the thread" from the last post (F-09); per-stage
  times (F-10); the D55 test method (F-11); a project cut-off sentence (F-12); the list's flash and
  impersonation wording (F-13); the chat switch off by default (F-14); Concerns neutral pending the
  owner (F-15); a downgrade that keeps ledger rows (F-16); "No position stated" (F-17); the CSP
  wording (F-19); "current profile" provenance (F-20); the list deferring `research` (F-21); and the
  audit's 12 repo answers and 13 defaults.
- Revision 3 (2026-10-27): the owner's answers D61–D64 (migrations only between runs; tool marks from
  methods and models only; neutral Concerns; one chat budget).

## 13. Open questions for the owner

None. Revision 2's four questions were answered as D61–D64 (§1).

## Appendix A. Evidence

The analysis (2026-10-27): read-only renders of the production pages (run 1abf809c, projects; run
652a6102, assessments) as an admin and a manager, 19 screenshots at 1,280 and 390 pixels, compared
with the templates; an Opus adversarial audit confirmed 21 findings and corrected 13 in part,
refuting none. Key measurements: the ready project's details page is 20,440 px tall at 1,280 px with
no disclosure (the assessment details page: 11,893 px as a manager, 20 disclosures); the three
postings each lost their second message (763, 1,206 and 1,181 characters; chunk + newline + chunk =
`posting_text`); the ready project's 13 tools all read "not found in the lab's profile"; 114 literal
`**` markers on its page; "Indication:" three times on every project page; on the in-progress view,
18 "Untitled project · In progress" cards that differ only in the lab's name. Production chat use to
2026-10-27: seven questions, six answered by the `claude-opus-5` fallback, no refusal recorded.
