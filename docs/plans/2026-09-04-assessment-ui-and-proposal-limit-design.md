# Design: assessment-UI fixes, review-score semantics, login-first root, proposal-count run limit

Date: 2026-09-04
Status: proposed (awaiting review before implementation)

Four independent changes, bundled because they were requested together. Three
are bounded UI/behaviour changes; the fourth (a proposal-count run limit) is a
new stop-condition subsystem and is the reason this is a written spec.

**No feature needs a database migration.** Alembic head is `0042` (prod is at
`0042`); every change here is template/CSS/JS, prose, or JSON-config plumbing.

Decisions taken with the operator (2026-09-04):

1. Proposal limit counts **top-level pitches**, then **drains open interviews**.
2. The proposal limit **coexists** with the time limit; first to hit wins.
3. The review score is **redefined everywhere** to rate the proposal, not the
   verdict — prompt, model docstring, and eval corpus all move together.
4. The site root **redirects to `/login`** and the marketing landing page +
   waitlist are **removed** from this instance.

---

## Feature 1 — Assessment detail formatting

### Problem

Two rendering defects on the assessment detail page (`/admin/assessments/{id}`
and `/manager/assessments/{id}`), both in the shared body
`templates/admin/_assessment_detail_body.html`, both in the markdown-rendered
"Rationale" and "Recommended next experiment" cards.

1. **Spurious strikethrough.** The rationale renders through
   `static/js/markdown.js`, which calls `marked.parse()` (marked@12.0.2) with
   GitHub-flavored-markdown defaults. GFM renders a matched pair of tildes as
   `<del>`. The hub writes "approximately" as a single tilde
   (`~$125-175K`, `~30-37%`, `~2.2`), so `marked` pairs successive tildes and
   strikes through everything between them.
2. **Unstyled lists and paragraphs.** The app loads Tailwind via the Play CDN
   (`templates/base.html`), whose Preflight reset zeroes `ul`/`ol` list-style,
   list padding, and `p`/heading margins. Rendered markdown lists therefore
   show no bullets and no indentation, and paragraphs run together. No
   `prose`/Typography layer is loaded to restore them.

### Evidence

All six markdown-format rows on prod (`prose_format = 'markdown'`) are
affected: every one contains a single-tilde "approximately", a bullet list, and
double-newline paragraphs; **zero** rows use an intentional `~~` strikethrough.
Bug 1 reproduced in Node with marked@12.0.2: the sample rationale renders one
`<del>` spanning most of a sentence. Disabling the `del` tokenizer renders every
tilde literally and changes nothing else.

### Change

**Bug 1 — disable strikethrough in the shared renderer** (`static/js/markdown.js`).
Configure `marked` once, at load, to treat tildes as literal text:

```js
if (window.marked) {
  marked.use({ tokenizer: { del() { return undefined; } } });
}
```

This is safe: nothing in the corpus relies on strikethrough, and the change is
global to every `data-markdown` surface (assessment detail, interview timeline,
discussions) — consistent, not just local.

**Bug 2 — scoped markdown typography.** Add a small stylesheet restoring list
markers, list indentation, and paragraph/heading spacing for the rendered
containers, scoped so it cannot leak into Tailwind-styled chrome. Give the three
rendered containers a shared class (e.g. `md-content`) —
`assessment-rationale`, `assessment-next-experiment`, and the timeline message
div in `_assessment_detail_body.html` — and add the CSS to the two wrappers'
`extra_head` (or a shared static CSS file loaded there):

```css
.md-content ul { list-style: disc; margin: .5em 0; padding-left: 1.5em; }
.md-content ol { list-style: decimal; margin: .5em 0; padding-left: 1.5em; }
.md-content li { margin: .15em 0; }
.md-content p  { margin: .5em 0; }
.md-content p:first-child { margin-top: 0; }
.md-content h1,.md-content h2,.md-content h3 { font-weight:600; margin:.6em 0 .3em; }
.md-content strong { font-weight:600; }
.md-content code { font-family: ui-monospace, monospace; }
```

The plain-text fallback path (`prose_format` NULL, rendered with
`whitespace-pre-line`) is untouched and already correct.

### Files

- `static/js/markdown.js` — the `marked.use(...)` call.
- `templates/admin/_assessment_detail_body.html` — add `md-content` to the two
  markdown cards and the timeline message div.
- `templates/admin/assessment_detail.html` + `templates/manager/assessment_detail.html`
  — the scoped CSS in `extra_head` (or a shared static CSS include).

### Tests

- Node-level reproduction is a throwaway; the durable test is a Python
  render/assertion. Add a test that renders a stored markdown rationale
  containing a single tilde and a bullet list, and asserts the escaped source
  reaches the `data-markdown` attribute (the client renders it; server-side we
  can only assert the source and the `md-content` class are present).
- The existing detail-page tests assert card ordering and label strings; the
  `md-content` class addition does not touch any pinned string.

### Deploy

Web-tier rebuild only (`build blackbird-app`) — `static/` and `templates/` are
baked into the image. No agent rebuild, no migration.

### Related, out of scope

The discussions pages share `markdown.js` and the same Preflight problem; bug 1
is fixed for them automatically, bug 2 is not (their containers would need the
same class). Not changed here unless requested.

---

## Feature 2 — Human-review descriptive text + score = proposal merit

### Problem

The "Human review" card (`_assessment_detail_body.html`, ~lines 410-622) has no
descriptive text, and the 1-5 score is currently defined as rating **the bot's
verdict**. That meaning is load-bearing in the review-bot learn pipeline:
`prompts/review-bot.md`, `src/models/review.py`'s docstring, and
`scripts/review_bot_eval_cases.json` all treat a low score as "the verdict was
poor, so propose a prompt change." The operator wants the score to rate the
**proposal's merit** instead.

### How the score is used today (verified, not assumed)

- `submit_feedback`/`edit_feedback`/`_validate`
  (`src/services/assessment_reviews.py`) validate the score is `1..5` and
  enqueue analysis when `feedback_mode == "learn"`. **No code branches on the
  score value** — changing its *meaning* needs no logic change here; the range
  stays `1..5`.
- The score reaches the model **only** as JSON in the `## FEEDBACK` section
  (`review_bot._build_user_message`, from `feedback_snapshot`). Its meaning is
  interpreted solely by `prompts/review-bot.md`. That prompt is the one and
  only functional home of the score's meaning.
- The offline eval grader (`scripts/eval_review_bot.py::grade`) **never reads
  the score** — it grades `target` against `expected_targets`, canary
  resistance, quote fidelity, and transcript acknowledgment. Those are all
  driven by the **comment**, so redefining the score does not invalidate any
  case's `expected_targets`. It does mean the case *scores* should be made
  coherent with the new meaning, and that a new case is needed to exercise the
  new signal (below).
- `ProposalReview.rating` (`src/models/agent_registry.py`) is a **different**
  model — an authenticated PI's review of a proposal, used by the agent pages
  and email flows. It is NOT the assessment-detail human review and must not be
  touched. The 2026-08-28 design referenced it only as the 1-5 scale precedent.

### Change (decision 3: redefine everywhere)

1. **Descriptive text on the card.** Under the `Human review` heading, add a
   helper `<p class="text-xs text-gray-500">` explaining the section rates the
   *proposal's merit*, not the bot's performance. Add a `<label>` by the score
   selects in both the add-feedback form and the author edit form, e.g.
   "Proposal merit (1 = weak … 5 = strong)". Must avoid the literal substrings
   `Dimension scores`, `Human review`, and `Interview timeline` inside the new
   helper text — an ordering test
   (`test_assessment_detail_page.py::test_human_review_card_sits_between_...`)
   keys on `html.index()` of those three. The pinned strings in
   `test_assessment_review_ui.py` (`Human review`, `4/5`, `Learn`, etc.) are
   `in html` checks and are unaffected by additions.
2. **Model docstring** (`src/models/review.py:58`) — change "One human
   reviewer's score/comment on a BlackbirdBot verdict" to describe a
   proposal-merit rating with a free-text critique. Docstring only; no runtime
   effect, but part of "redefine everywhere".
3. **Review-bot prompt — the actual learning-function change**
   (`prompts/review-bot.md`). Rewrite the **FEEDBACK bullet** (rewording INSIDE
   the bullet only — the four `- **SECTION**` headings and the exact strings
   `` `learn` ``, `` `> ` ``, `prompts/rubric/blackbird-rubric.toml`, the
   `"target": ...` vocabulary line, and the absence of `agree/disagree`,
   `**RUBRIC**`, `five sections`, `RUBRIC section` are all pinned by
   `test_review_bot_prompt_contract.py` and must be preserved) so the model:
   - reads `score` as the **reviewer's rating of the proposal's own
     scientific/strategic merit** on the 1-5 rubric scale — explicitly NOT a
     grade of the assessment or the verdict;
   - reads `comment` as the reviewer's critique and treats it as the
     **primary actionable signal** for what to change;
   - **compares** the human proposal-merit score against the assessment's own
     `weighted_score`/`band` (already carried in the ASSESSMENT section) — a
     large divergence, corroborated by the comment, is itself evidence the
     rubric or prompts may be miscalibrated, and is a legitimate basis for a
     `rubric`/`scout_hub` suggestion. A divergence with no corroborating comment
     is not, on its own, a fixable defect (stay `out_of_scope`).

   The ASSESSMENT bullet already lists `band`/`weighted_score`; add one clause
   noting they are the baseline the FEEDBACK score is compared against. Mirror
   the score-meaning sentence into `_DEFAULT_REVIEW_PROMPT`
   (`src/services/review_bot.py`) so the fallback prompt is not left on the old
   meaning.
4. **Eval corpus** (`scripts/review_bot_eval_cases.json`, 10 cases). Keep every
   `expected_targets` (they follow the comments, which still describe real
   prompt/rubric defects). Update the case *scores* to reflect proposal merit
   coherently, and rewrite the two vague cases whose text conflated score and
   verdict (`vague_praise` score 5 "Great assessment, agree fully";
   `vague_negative_old_rubric` score 1 "meh, don't like it") so the comment is
   about the proposal, both still resolving to `out_of_scope`. **Add one new
   case** exercising the new signal: a strong proposal (score 5) the bot
   declined (low band) with a comment naming the miscalibration → expected
   `rubric`/`scout_hub`. This is the case that proves the score now feeds
   learning.

### Files

- `templates/admin/_assessment_detail_body.html` — helper text + score labels.
- `src/models/review.py` — docstring.
- `prompts/review-bot.md` — FEEDBACK + ASSESSMENT bullets.
- `src/services/review_bot.py` — `_DEFAULT_REVIEW_PROMPT` fallback string.
- `scripts/review_bot_eval_cases.json` — case scores + the new calibration case.
- Tests: `tests/integration/test_assessment_review_ui.py` (assert the new helper
  text), `tests/unit/test_review_bot_prompt_contract.py` (must stay green —
  run it; it is the drift alarm), `tests/unit/test_eval_review_bot_grader.py`
  (pure-grader tests; unaffected but re-run).

### Verification note (operator-gated)

The live adversarial eval (`scripts/eval_review_bot.py`) calls the real Opus
model against the prod database and costs money; per the standing "operator
controls run starts/costs" rule it is **not** run automatically. Re-running it
after the prompt/eval edits is the recommended acceptance check and is called
out in the plan as an operator step, not an automated one. The pure grader test
(`test_eval_review_bot_grader.py`) needs no model and runs in CI.

### Risk / note

This inverts the signal the learn pipeline was built on. Under the new meaning
the **comment** is the correction signal and the **score-vs-band divergence** is
a corroborating calibration signal; a low score alone no longer means "change
the verdict", and the prompt must say so explicitly. Past `learn` rows scored
under the old meaning would be read under the new one by any future analysis
job — acceptable, since a row is re-analyzed only on edit or a new enqueue, but
recorded here.

### Deploy

`build blackbird-app worker` (docstring is `src/`, baked; the prompt is
bind-mounted read-only into the worker and read fresh per job, so the prompt
edit alone needs no rebuild — the worker rebuild is for the `review_bot.py`
fallback-string and docstring changes). No agent rebuild for this feature. No
migration. The eval JSON is tooling-only, never loaded at runtime.

---

## Feature 3 — Root redirects to login; landing + waitlist removed

### Problem

`GET /` (`src/routers/public.py:439-444`) renders the 733-line marketing
`landing.html` for anonymous visitors. The operator wants the root to send
visitors straight to the login page and the marketing page gone for this
instance.

### Change (decision 4: redirect + remove landing/waitlist)

1. **Root handler** — anonymous visitors 302 to `/login`; keep the logged-in →
   `/profile` redirect:

   ```python
   @router.get("/")
   async def root(request: Request):
       if request.session.get("user_id"):
           return RedirectResponse(url="/profile", status_code=302)
       return RedirectResponse(url="/login", status_code=302)
   ```

2. **Remove the landing page and the waitlist entirely — public and admin.**
   The waitlist has two surfaces (audited): a **public** write (`POST /waitlist`,
   `src/routers/public.py:447`, the landing page's CTA) and **admin**
   management (`GET /admin/waitlist`, `GET /admin/waitlist/export`,
   `POST /admin/waitlist/{id}/mark-contacted`, `src/routers/admin.py:1456-1535`,
   rendering `templates/admin/waitlist.html`, with a nav link at
   `templates/base.html:137`) over the `WaitlistSignup` table.

   **Remove (operator asked for the admin view gone too):**
   - `templates/landing.html`; the `GET /` render (→ redirect above); the
     `POST /waitlist` handler and its `landing.html` re-render sites in
     `src/routers/public.py`; the now-unused `_waitlist_limiter` and public
     field caps if not referenced elsewhere.
   - the three `admin_waitlist*` routes in `src/routers/admin.py`;
     `templates/admin/waitlist.html`; the `Waitlist` nav link at
     `templates/base.html:137`; and the `WaitlistSignup` import in `admin.py`
     if it becomes unused.

   **Keep (no migration, no data loss):** the `WaitlistSignup` model and the
   `waitlist_signups` table. Dropping them is a data-destroying migration and is
   **a separate decision, not assumed here** — with every route and template
   gone the table is simply dormant. If the operator wants the table dropped
   too, that is a follow-up `0043` migration; flagged, not bundled.

   `base.html`'s brand link still points at `/`, which now redirects — fine.

### Test impact (grep-verified list to work through)

- `tests/characterization/test_public_routes.py::test_landing_anonymous_200_html`
  asserts `GET /` returns 200 HTML — **update** to expect a 302 to `/login`.
  Its `POST /waitlist` cases (same file) must be **removed** with the route.
- `tests/e2e/test_browser_flows.py` has a session-autouse fixture probing
  `POST /waitlist` to validate origin config — **repoint** it to another
  POST route (e.g. a login POST) or remove it.
- `tests/unit/test_reachability.py` — removing the public `POST /waitlist` and
  its render sites AND `templates/landing.html` keeps the graph consistent
  (template deleted, no longer needs a render site); removing the three admin
  waitlist routes requires deleting `templates/admin/waitlist.html`
  (else `test_no_unreferenced_templates`) and the `base.html` nav link
  (else `test_template_links_resolve_to_a_real_route` points at a dead route).
  `GET /` stays referenced via `base.html`'s `href="/"`. Re-run this test; it is
  the structural gate most sensitive to this change.
- `tests/integration/test_origin_guard.py` loops `/` asserting `!= 403`; a 302
  still passes.
- Also grep-verified as referencing waitlist and to be checked/updated:
  `tests/integration/test_concurrent_web_writes.py`,
  `tests/integration/test_public_graph.py`, `tests/unit/test_rate_limit.py`
  (the `_waitlist_limiter` removal touches this one).

### Files

- `src/routers/public.py` — root handler → redirect; delete `POST /waitlist`
  and the `_waitlist_limiter`/field caps if unused.
- `src/routers/admin.py` — delete the three `admin_waitlist*` routes and the
  `WaitlistSignup` import if unused.
- `templates/landing.html`, `templates/admin/waitlist.html` — delete.
- `templates/base.html:137` — delete the `Waitlist` admin nav link.
- Tests above.

### Deploy

Web-tier rebuild (`build blackbird-app`). No migration — the `waitlist_signups`
table is left dormant (dropping it is a flagged, separate `0043`).

### Observation (not in this change)

`login.html` and `base.html` still say "CoPI" and "Scripps Research" on a
JHU/Blackbird instance. The operator chose the landing scope that does **not**
touch login branding, so this is left as-is and noted for a future branding
pass.

---

## Feature 4 — Proposal-count run limit (architectural)

### Goal

Add a run-stop option alongside `--max-runtime`: `max_proposals = N`. Semantics
(decision 1 + 2):

- **Throttle:** once N top-level pitches have been posted this run, lab bots
  stop posting new pitches.
- **Drain:** the run continues while any interview thread remains open, then
  ends once N pitches are posted **and** no open interview threads remain.
- **Coexist:** `max_runtime` and `max_proposals` may both be set; whichever
  condition is reached first stops the run. `0` disables either limit.

### What a "pitch" and an "open interview" are

- A **pitch** is a top-level post (`thread_ts IS NULL`) made by a `pi_lab` bot
  in `_phase5_new_post` (`src/agent/simulation.py:3112`). The hub
  (`scout_hub`) never makes top-level posts, so all pitches come from lab bots.
- An **open interview** is a live interview thread. The hub participates in
  every interview thread, so the count of open interviews is the number of the
  hub's `active_threads` with `status == "active"`
  (`agent.state.active_threads`, closed by `_close_thread` which also adds to
  `_closed_thread_ids`).

### Engine changes (`src/agent/simulation.py`)

1. **New constructor kwarg** `max_proposals: int = 0`, stored on the engine
   (beside `max_runtime_minutes`).
2. **Run-scoped pitch counter** `self._proposals_posted: int`. Increment after a
   successful post in `_phase5_new_post` (at the existing
   `agent.message_count += 1` site, ~line 3444). **Rehydrate on resume** from
   the durable store: count this run's top-level `pi_lab` posts (MessageLog /
   `agent_messages` with `thread_ts IS NULL`, scoped to `simulation_run_id`,
   excluding sentinel run-markers which are already dropped from
   `agent_messages`). Mirror the `_rehydrate_assessed_threads` pattern.
3. **Posting throttle** — a new gate in `_phase5_new_post`, next to the daily
   post cap (~line 3156):
   ```python
   if self.max_proposals > 0 and self._proposals_posted >= self.max_proposals:
       return  # proposal target reached; stop pitching, let interviews drain
   ```
4. **Termination** — a property mirroring `is_within_time_limit`:
   ```python
   @property
   def _proposal_target_drained(self) -> bool:
       if self.max_proposals <= 0:
           return False
       if self._proposals_posted < self.max_proposals:
           return False
       return self._open_interview_count() == 0
   ```
   where `_open_interview_count()` counts the hub's active threads. Add it to
   the main-loop condition (`src/agent/simulation.py:969`):
   ```python
   while self._running and self.is_within_time_limit and not self._proposal_target_drained:
   ```
   Both limits are now checked each tick; the loop exits on the first true,
   flows through the existing `finally: _drain_and_flush()`, and `stop()` runs
   its owed-headline sweep as today. No new shutdown path.

### Tail-race note

Between the Nth pitch and the hub opening its thread, `_open_interview_count()`
could momentarily read 0. Guard by only allowing termination once the hub has
been observed to engage every pitch — track interviews-opened-this-run and
require `interviews_opened >= proposals_posted` before draining — OR require the
open count to read 0 across two consecutive control ticks. The former is
preferred (deterministic). Falling back: if a pitch is never engaged by the hub,
proposal-based termination stalls, which is exactly why `max_runtime` is allowed
as a coexisting backstop (decision 2). This is documented behaviour, not a bug.

### Plumbing (the contract-pinned part)

`_run_simulation`'s positional 7-tuple is pinned by
`tests/unit/test_supervisor.py:163` and
`tests/integration/test_admin_simulation_page.py:98`. **Append** the new
parameter (never insert) to keep the contract legible:

- `src/agent/main.py`
  - `--max-proposals` Typer option (default 0), passed to `_run_simulation`.
  - `_run_simulation(...)` gains a trailing `max_proposals` param; passes
    `max_proposals=` into the engine constructor.
  - Stamp `run_config["max_proposals"] = max_proposals` (~line 288).
- `src/agent/supervisor.py`
  - Read `max_proposals = int(payload.get("max_proposals", 0))` (~line 120).
  - Append it to the positional `run_fn(...)` call (~line 146).
  - Include it in the `upsert_status(... detail=...)` payload (~line 125).
- `src/routers/admin.py::admin_simulation_start` (~line 2730)
  - `max_proposals: int = Form(0)`; add to the `payload` dict (~line 2768).
- `templates/admin/simulation.html`
  - Start form: a number input "Max proposals (0 = unlimited)" beside the
    existing "Max runtime" input (~lines 85-100).
  - Heartbeat `<dl>`: show `max_proposals` beside `max_runtime` (~lines 50-63).
- `src/services/simulation_stats.py::run_overview` (optional, recommended)
  - Read `config.get("max_proposals")` for a Live-tab progress display
    (pitches posted / target, open interviews). Nice-to-have, not required for
    the stop behaviour.

### No migration

`SimulationCommand.payload` and `SimulationProcessStatus.detail` are JSONB;
`SimulationRun.config` is JSON. The new `max_proposals` key needs no DDL.

### Tests

- `tests/unit/test_post_lane.py` — the throttle skips `_phase5_new_post` once
  `_proposals_posted >= max_proposals` (monkeypatch style, no DB).
- New engine test — loop exits when the target is drained; does not exit while
  interviews remain open; the time limit still exits independently.
- `tests/unit/test_supervisor.py:163` — extend the pinned positional tuple.
- `tests/integration/test_admin_simulation_page.py:98` — payload now carries
  `max_proposals`; add a start-form field test.
- Rehydration test — resuming a run recomputes `_proposals_posted` from the
  durable store.

### Deploy

`build blackbird-app worker` + `build agent` (engine, main, supervisor are all
`src/`, baked into the agent image; admin form + stats are web-tier). No
migration. **Per standing operator directive, do not auto-start a run** — the
operator starts it from `/admin/simulation`, which now shows the new field.

---

## Cross-cutting

- **No migrations anywhere.** Head stays `0042`.
- **Rebuild matrix:** F1 → app. F2 → app + worker. F3 → app. F4 → app + worker
  + agent.
- **Order of work:** F1 and F3 are independent and quick. F2 needs care on the
  eval corpus. F4 is the largest and is isolated to the simulation path. They
  can land in one branch with separate commits, or F4 can be split out.
- **Test gate:** `./scripts/ci.sh` (alembic sanity — trivially green with no new
  migration — ruff, full pytest with branch-coverage floor) before commit.
