# Assessment-UI, review-score, login-first, proposal-limit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix assessment-detail markdown rendering, redefine the human-review score to rate the proposal (including the review-bot learning path), redirect the site root to login and remove the landing/waitlist, and add a proposal-count run limit to the simulation.

**Architecture:** Four independent changes in one branch, separate commits. Three are template/JS/route/prose edits; the fourth threads a `max_proposals` value through the CLI, supervisor, engine, and admin form, enforced at the single main-loop condition and a new posting gate. No database migration in any task.

**Tech Stack:** FastAPI, Jinja2, SQLAlchemy async, Typer, `marked`+DOMPurify (client), pytest (host `.venv-test`), Postgres via testcontainers.

**Spec:** `docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md`

## Global Constraints

- **No new migration.** Alembic head stays `0042`. Everything is JSON/JSONB config, prose, template, JS, or route code.
- **Run tests on the host, not in a container:** `.venv-test/bin/python -m pytest tests/ -v` (see CLAUDE.md; testcontainers spins its own Postgres). Never `pip install` against `.venv-test` from the sshfs client.
- **`prompts/` is bind-mounted** into app/worker/agent; edits to `prompts/review-bot.md` need no rebuild. `templates/`, `static/`, and `src/` are baked into the image — changes there need a rebuild of the affected service.
- **Never touch `docker-compose.prod.yml`** (uncommitted working-tree edits are load-bearing) and **never auto-start the simulation** (operator-only).
- **`ProposalReview` (`src/models/agent_registry.py`) is out of scope** — it is a PI's proposal review with a `rating` field, unrelated to `AssessmentReview`.
- Full gate before commit is `./scripts/ci.sh`; per-task steps run the narrower `pytest` selection shown.

---

## Task 1: Disable GFM strikethrough in the shared markdown renderer

**Files:**
- Modify: `static/js/markdown.js`
- Test: `tests/unit/test_markdown_renderer_config.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing importable; a source-contract guard only.

**Why a source test:** CI is Python-only (`scripts/ci.sh` runs pytest); there is no JS test runner. The durable guard is a source assertion, the same idiom `tests/unit/test_post_lane.py` uses with `inspect.getsource`. The behavioural proof (that a single tilde no longer becomes `<del>`) was done once in Node with marked@12.0.2 during design and is recorded in the spec.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_markdown_renderer_config.py
"""markdown.js must disable GFM strikethrough: the hub writes '~' for
'approximately' (e.g. '~30-37%'), and marked pairs tildes into <del> spans.
See docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md, Feature 1."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static" / "js" / "markdown.js").read_text()


def test_strikethrough_tokenizer_is_disabled():
    # The one durable signal that tildes are treated as literal text.
    assert "marked.use(" in JS
    assert "del(" in JS  # the tokenizer being overridden off
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_markdown_renderer_config.py -v`
Expected: FAIL (`marked.use(` not yet present).

- [ ] **Step 3: Add the tokenizer override**

In `static/js/markdown.js`, immediately inside the IIFE (before `renderAll` is defined/called), add:

```js
  // Disable GFM strikethrough: the corpus uses single tildes for
  // "approximately" (e.g. "~30-37%"), which marked otherwise pairs into a
  // <del> span. No content uses intentional ~~strikethrough~~. Returning
  // undefined from the del tokenizer makes marked treat every tilde as
  // literal text. Guarded because marked may be absent (fail-closed path).
  if (window.marked && typeof marked.use === "function") {
    marked.use({ tokenizer: { del() { return undefined; } } });
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_markdown_renderer_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/js/markdown.js tests/unit/test_markdown_renderer_config.py
git commit -m "fix(assessments): disable GFM strikethrough in shared markdown renderer"
```

---

## Task 2: Restore markdown list/paragraph styling on the assessment detail page

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html` (add `md-content` to the two markdown cards and the timeline message div)
- Modify: `templates/admin/assessment_detail.html` (add the scoped style block to `extra_head`)
- Modify: `templates/manager/assessment_detail.html` (same style block)
- Test: `tests/integration/test_assessment_detail_page.py` (add an assertion)

**Interfaces:**
- Consumes: nothing.
- Produces: a `.md-content` CSS scope both detail wrappers rely on.

**Context:** Tailwind's CDN Preflight zeroes `ul`/`ol` list-style and `p`/heading margins, so `marked`-rendered lists show no bullets and paragraphs run together. Scoped CSS restores them without affecting Tailwind chrome. The three rendered containers today are: the rationale div (`class="assessment-rationale ..." data-markdown=...`), the next-experiment div (`assessment-next-experiment`), and the timeline message div (`... data-markdown="{{ m.content | e }}"`).

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_assessment_detail_page.py`. Mirror the existing
`test_prose_format_markdown_renders_data_markdown_divs_on_both_surfaces` (it
seeds `prose_format="markdown"` with the `admin`/`manager` fixtures and reads
`resp.text`) — the `md-content` class must sit on the same three markdown divs:

```python
async def test_markdown_cards_carry_the_md_content_class(
    client, db_session, admin, manager
):
    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id=HUB,
        subject_agent_id=SUBJECT,
        channel_name=CHANNEL,
        recommendation="advance",
        rationale=RATIONALE_MARKDOWN_CONTENT,
        recommended_next_experiment=NEXT_EXPERIMENT_MARKDOWN_CONTENT,
        prose_format="markdown",
    )
    db_session.add(assessment)
    await db_session.flush()

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200
        html = resp.text
        # md-content must be on the same <div> that carries assessment-rationale
        rationale_at = html.index('class="assessment-rationale')
        tag_start = html.rindex("<div", 0, rationale_at)
        tag_end = html.index(">", rationale_at)
        assert "md-content" in html[tag_start:tag_end]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -k md_content -v`
Expected: FAIL (`md-content` not present).

- [ ] **Step 3: Add the class to the three containers**

In `templates/admin/_assessment_detail_body.html`, add `md-content` to each of the three markdown container class lists, e.g.:

```html
<div class="assessment-rationale md-content text-sm text-gray-700" data-markdown="{{ a.rationale | e }}"></div>
```
```html
<div class="assessment-next-experiment md-content text-sm text-gray-700" data-markdown="{{ a.recommended_next_experiment | e }}"></div>
```
```html
<div class="md-content text-sm text-gray-800 max-h-64 overflow-y-auto" data-markdown="{{ m.content | e }}"></div>
```

- [ ] **Step 4: Add the scoped style to both wrappers**

In `templates/admin/assessment_detail.html` and `templates/manager/assessment_detail.html`, inside `{% block extra_head %}` (after the three script tags), add:

```html
<style>
  .md-content ul { list-style: disc; margin: .5em 0; padding-left: 1.5em; }
  .md-content ol { list-style: decimal; margin: .5em 0; padding-left: 1.5em; }
  .md-content li { margin: .15em 0; }
  .md-content p  { margin: .5em 0; }
  .md-content p:first-child { margin-top: 0; }
  .md-content p:last-child  { margin-bottom: 0; }
  .md-content h1, .md-content h2, .md-content h3 { font-weight: 600; margin: .6em 0 .3em; }
  .md-content strong { font-weight: 600; }
  .md-content code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -k md_content -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add templates/admin/_assessment_detail_body.html templates/admin/assessment_detail.html templates/manager/assessment_detail.html tests/integration/test_assessment_detail_page.py
git commit -m "fix(assessments): restore markdown list/paragraph styling on the detail page"
```

---

## Task 3: Human-review card — descriptive text and proposal-merit score labels

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html` (the Human review card, ~lines 435-585)
- Test: `tests/integration/test_assessment_review_ui.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

**Constraint:** Do NOT put the literal substrings `Dimension scores`, `Human review`, or `Interview timeline` inside the new helper text (an ordering test in `test_assessment_detail_page.py` uses `html.index()` on those three). The card heading `Human review` already exists and stays.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_assessment_review_ui.py`. Use the module's real
helpers: `_seed_assessment` (imported there from `test_reviews_router`) and the
`admin` fixture, matching `test_feedback_and_status_render_on_all_three_surfaces`:

```python
async def test_card_explains_the_score_rates_the_proposal(client, db_session, admin):
    assessment = await _seed_assessment(db_session)
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "rates the proposal" in html.lower()
    assert "Proposal merit" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py -k rates_the_proposal -v`
Expected: FAIL.

- [ ] **Step 3: Add the helper paragraph under the heading**

Directly after `<h2 ...>Human review</h2>` in the card, add:

```html
<p class="text-xs text-gray-500 mb-3">
    Scores here rate the proposal's own scientific and strategic merit
    (1 = weak, 5 = strong), not how well the bot performed. Use the comment
    to explain the score and to flag anything the assessment got wrong.
</p>
```

- [ ] **Step 4: Add a label to the score selects**

In BOTH the add-feedback form and the author edit form, wrap/precede the `<select name="score" ...>` with a label. For the add-feedback form the score `<select>` starts with a disabled placeholder option "Score"; change that placeholder to "Proposal merit" and add a label before the select:

```html
<label class="block text-xs font-medium text-gray-600 mb-1">Proposal merit (1 = weak … 5 = strong)</label>
```

Keep the `<option value="" disabled selected>` present but relabel it `Proposal merit`. In the author edit form (which has no placeholder option), add the same `<label>` before its `<select name="score">`.

- [ ] **Step 5: Run the review-UI suite to verify it passes and nothing else broke**

Run: `.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py tests/integration/test_assessment_detail_page.py -v`
Expected: PASS (including the existing `Human review` / `4/5` / ordering assertions).

- [ ] **Step 6: Commit**

```bash
git add templates/admin/_assessment_detail_body.html tests/integration/test_assessment_review_ui.py
git commit -m "feat(reviews): label the human-review score as proposal merit, add helper text"
```

---

## Task 4: Redefine the score in the review-bot learning path (prompt + docstring + fallback)

**Files:**
- Modify: `prompts/review-bot.md` (FEEDBACK bullet + ASSESSMENT bullet)
- Modify: `src/services/review_bot.py` (`_DEFAULT_REVIEW_PROMPT`)
- Modify: `src/models/review.py` (`AssessmentReview` docstring, line ~58)
- Test: `tests/unit/test_review_bot_prompt_contract.py` (must stay green; add one assertion)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

**Hard constraints from `test_review_bot_prompt_contract.py` (verified):**
- The four `- **SECTION**` headings under `## What you will be given` must remain exactly `FEEDBACK`, `ASSESSMENT`, `INTERVIEW TRANSCRIPT`, `CURRENT PROMPT FILES` (reword INSIDE a bullet only).
- The prompt must still contain `` `learn` ``, `` `> ` ``, `prompts/rubric/blackbird-rubric.toml`, and the exact `"target": "scout_hub | pi_lab | specialist:<domain> | rubric | out_of_scope"` vocabulary line; and must NOT contain `agree/disagree`, `**RUBRIC**`, `five sections`, or `RUBRIC section`.
- `_DEFAULT_REVIEW_PROMPT` must keep the same `"target": ...` vocabulary line.

- [ ] **Step 1: Add the failing contract assertion**

Add to `tests/unit/test_review_bot_prompt_contract.py`:

```python
def test_feedback_bullet_defines_score_as_proposal_merit():
    given = PROMPT.split("## What you will be given", 1)[1]
    feedback = given.split("- **FEEDBACK**", 1)[1].split("- **ASSESSMENT**", 1)[0]
    low = feedback.lower()
    assert "proposal" in low and "merit" in low
    # It must NOT tell the model the score grades the assessment/verdict.
    assert "grade of the assessment" not in low
    assert "quality of the verdict" not in low
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_review_bot_prompt_contract.py -k score_as_proposal_merit -v`
Expected: FAIL.

- [ ] **Step 3: Rewrite the FEEDBACK bullet in `prompts/review-bot.md`**

Replace the current FEEDBACK bullet body with (keeping the `- **FEEDBACK**` heading):

```markdown
- **FEEDBACK** — one or more human reviewer notes about this specific assessment, as a
  JSON list: a numeric `score` (1–5), a `mode` (always `learn` — rows a reviewer marked
  `log_only` are never shown to you), and a free-text `comment`. **The score rates the
  PROPOSAL's own scientific and strategic merit on the same 1–5 scale the rubric uses —
  it is NOT a grade of the assessment or the verdict.** The `comment` is the reviewer's
  critique and is your primary, actionable signal for what to change. Treat a large gap
  between the reviewer's proposal-merit score and the assessment's own `weighted_score` /
  `band` (see ASSESSMENT) as a calibration signal: when the comment corroborates it, that
  gap is legitimate grounds for a `rubric` or `scout_hub` suggestion. A gap with no
  corroborating comment is not, on its own, a fixable defect — say so and stay
  `out_of_scope`. Ground your suggestion in what the feedback actually says, never in a
  generic sense that the prompt could be better.
```

- [ ] **Step 4: Add one clause to the ASSESSMENT bullet**

In the ASSESSMENT bullet, after it lists "recommendation, band, gating status, red flags, the rationale text", add:

```markdown
  Its `band` and `weighted_score` are the baseline the FEEDBACK score is compared
  against.
```

- [ ] **Step 5: Update `_DEFAULT_REVIEW_PROMPT` in `src/services/review_bot.py`**

In the fallback string, change the opening two sentences so the score meaning matches. Replace:

```
You will be given FEEDBACK, ASSESSMENT, INTERVIEW TRANSCRIPT (which may say it is
unavailable — do not invent one) and CURRENT PROMPT FILES.
```

with:

```
You will be given FEEDBACK, ASSESSMENT, INTERVIEW TRANSCRIPT (which may say it is
unavailable — do not invent one) and CURRENT PROMPT FILES. In FEEDBACK the numeric
score rates the PROPOSAL's merit on a 1–5 scale, not the assessment; the comment is
your actionable signal, and a score that diverges sharply from the assessment's own
band is a calibration signal only when the comment corroborates it.
```

(Leave the `"target": ...` line unchanged — the contract test checks it.)

- [ ] **Step 6: Update the model docstring in `src/models/review.py`**

Change `AssessmentReview`'s docstring first line from:

```
"""One human reviewer's score/comment on a BlackbirdBot verdict.
```

to:

```
"""One human reviewer's rating of a proposal's merit (1–5) plus a free-text
    comment, attached to one BlackbirdBot assessment. The score rates the
    PROPOSAL, not the bot's performance; the comment is the actionable critique.
```

- [ ] **Step 7: Run the contract suite**

Run: `.venv-test/bin/python -m pytest tests/unit/test_review_bot_prompt_contract.py -v`
Expected: PASS (all pre-existing assertions plus the new one).

- [ ] **Step 8: Commit**

```bash
git add prompts/review-bot.md src/services/review_bot.py src/models/review.py tests/unit/test_review_bot_prompt_contract.py
git commit -m "feat(reviews): redefine review score as proposal merit in the learning path"
```

---

## Task 5: Update the review-bot eval corpus for the new score meaning

**Files:**
- Modify: `scripts/review_bot_eval_cases.json`
- Test: `tests/unit/test_eval_review_bot_grader.py` (add a cases-file sanity test)

**Interfaces:**
- Consumes: `scripts/eval_review_bot.py` grader (unchanged).
- Produces: the eval cases the operator-run live eval consumes.

**Context (verified):** the grader (`scripts/eval_review_bot.py::grade`) never reads the score — it grades `target`, canary, quotes, and transcript-ack, all driven by the comment. So every existing `expected_targets` stays valid. Two vague cases conflate score and verdict and must be rewritten; one new case exercises the score-vs-band calibration signal.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_eval_review_bot_grader.py`:

```python
import json

def test_cases_file_has_a_calibration_divergence_case():
    cases = json.loads((ROOT / "scripts" / "review_bot_eval_cases.json").read_text())
    names = {c["name"] for c in cases}
    assert "score_band_divergence" in names
    case = next(c for c in cases if c["name"] == "score_band_divergence")
    assert case["feedback"][0]["score"] == 5
    assert set(case["expected_targets"]) & {"rubric", "scout_hub"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_eval_review_bot_grader.py -k calibration_divergence -v`
Expected: FAIL.

- [ ] **Step 3: Rewrite the two vague cases**

In `scripts/review_bot_eval_cases.json`, change `vague_praise`'s comment to be about the proposal, keeping `score` 5 and `expected_targets` `["out_of_scope"]`:

```json
"comment": "Strong proposal and the assessment handled it well — no prompt or rubric change is warranted."
```

Change `vague_negative_old_rubric`'s comment to be a vague proposal-merit judgment, keeping `score` 1 and `expected_targets` `["out_of_scope"]`:

```json
"comment": "Weak proposal in my view, but I can't point to anything the assessment did wrong."
```

- [ ] **Step 4: Add the calibration case**

Append this object to the array (reuse a real `assessment_id` already used by another case, e.g. `ab30ded3-4048-4ce7-9f16-a1c99122cd26`):

```json
{
  "name": "score_band_divergence",
  "assessment_id": "ab30ded3-4048-4ce7-9f16-a1c99122cd26",
  "feedback": [{"reviewer_name": "Eval Reviewer", "score": 5, "comment": "This is a strong, fundable proposal, but the assessment banded it as a decline. The rubric is under-weighting translational potential relative to how this program should score; the hub also never credited the de-risking already done. Raise the weight on translational potential or add guidance to credit completed de-risking."}],
  "force_no_transcript": false,
  "inject_transcript_message": null,
  "expected_targets": ["rubric", "scout_hub"],
  "canary": null,
  "expect_transcript_ack": false
}
```

- [ ] **Step 5: Run the grader tests + JSON validity**

Run: `.venv-test/bin/python -m pytest tests/unit/test_eval_review_bot_grader.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/review_bot_eval_cases.json tests/unit/test_eval_review_bot_grader.py
git commit -m "test(reviews): update eval corpus for proposal-merit score + calibration case"
```

**Operator step (not automated):** after deploy, re-run the live eval to confirm behaviour under the new prompt:
`$DC run --rm --no-deps -v "$PWD/src:/app/src:ro" -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/docs:/app/docs" blackbird-app python scripts/eval_review_bot.py --cases scripts/review_bot_eval_cases.json --out docs/audits/2026-09-04-review-score/eval-results.json` — it calls the real Opus model and costs money, so it is operator-gated.

---

## Task 6: Redirect the site root to login; remove the public waitlist

**Files:**
- Modify: `src/routers/public.py` (the `GET /` handler; delete `POST /waitlist` and `_waitlist_limiter`/field caps if unused)
- Delete: `templates/landing.html`
- Test: `tests/characterization/test_public_routes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GET /` now returns a redirect (302) for anonymous users.

- [ ] **Step 1: Update the failing characterization test**

In `tests/characterization/test_public_routes.py`, replace `test_landing_anonymous_200_html` with:

```python
async def test_root_anonymous_redirects_to_login(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
```

Delete the `POST /waitlist` test cases in the same file (they exercise a route being removed).

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/characterization/test_public_routes.py -k root_anonymous -v`
Expected: FAIL (root still renders 200).

- [ ] **Step 3: Rewrite the `GET /` handler**

In `src/routers/public.py`, replace the `landing` handler body:

```python
@router.get("/")
async def root(request: Request):
    """Site root: signed-in users go to their profile, everyone else to login.
    This instance has no marketing landing page."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/profile", status_code=302)
    return RedirectResponse(url="/login", status_code=302)
```

- [ ] **Step 4: Delete the public waitlist route and its helpers**

Remove the `POST /waitlist` handler (`waitlist_submit`), the `_waitlist_limiter` definition, and the public field-cap constants that only it used. Remove the now-unused `WaitlistSignup` import from `public.py` if nothing else in the file references it (leave `ProposalVote`/`User` etc.). Then delete `templates/landing.html`.

- [ ] **Step 4b: Repoint the e2e origin probe off `POST /waitlist`**

`tests/e2e/test_browser_flows.py` has a session-autouse fixture
(`_the_server_agrees_about_its_own_origin`, ~line 76) that POSTs to `/waitlist`
with an invalid address to confirm the live server accepts a same-origin POST.
That route is gone. Repoint the probe to an origin-guarded POST that survives —
`POST /logout` is the safe choice (it exists, is origin-guarded, and a probe
with no session just 302s to `/login` / 403s cross-origin). Update the fixture's
path and its expected status accordingly. (E2E runs only when `E2E_BASE_URL` is
set, so this is not part of the host `pytest`/`ci.sh` gate, but leaving it
pointed at a 404 would silently break the next real e2e run.)

- [ ] **Step 5: Run to verify it passes, plus reachability and origin guard**

Run: `.venv-test/bin/python -m pytest tests/characterization/test_public_routes.py tests/unit/test_reachability.py tests/integration/test_origin_guard.py tests/unit/test_rate_limit.py -v`
Expected: PASS. If `test_rate_limit.py` references `_waitlist_limiter`, update it to drop that reference.

- [ ] **Step 6: Commit**

```bash
git add src/routers/public.py templates/landing.html tests/characterization/test_public_routes.py tests/unit/test_rate_limit.py
git commit -m "feat(site): redirect root to login and remove the public waitlist/landing"
```

---

## Task 7: Remove the admin waitlist views

**Files:**
- Modify: `src/routers/admin.py` (delete the three `admin_waitlist*` routes; drop the `WaitlistSignup` import if unused)
- Delete: `templates/admin/waitlist.html`
- Modify: `templates/base.html:137` (delete the `Waitlist` admin nav link)
- Test: `tests/unit/test_reachability.py` plus any admin test referencing `/admin/waitlist`

**Interfaces:**
- Consumes: nothing.
- Produces: `/admin/waitlist*` routes no longer exist.

**Note:** keep the `WaitlistSignup` model and `waitlist_signups` table (dropping them is a separate, data-destroying `0043` migration, deliberately not in this plan).

- [ ] **Step 1: Write the failing test**

Add to the admin test module that owns simulation/admin GET checks (e.g. `tests/integration/test_admin_simulation_page.py` or the admin routes characterization file):

```python
async def test_admin_waitlist_route_is_gone(client, db_session):
    admin = await _admin(db_session, "no-waitlist@example.org")
    r = await client.get("/admin/waitlist", headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest -k admin_waitlist_route_is_gone -v`
Expected: FAIL (route still returns 200).

- [ ] **Step 3: Delete the routes, template, nav link**

- In `src/routers/admin.py`, delete `admin_waitlist`, `admin_waitlist_export`, and `admin_waitlist_mark_contacted`. Remove `WaitlistSignup` from the imports if nothing else in `admin.py` uses it.
- Delete `templates/admin/waitlist.html`.
- In `templates/base.html`, delete the line: `<a href="/admin/waitlist" ...>Waitlist</a>` (line ~137).

- [ ] **Step 4: Run to verify it passes + reachability**

Run: `.venv-test/bin/python -m pytest -k "admin_waitlist_route_is_gone" tests/unit/test_reachability.py -v`
Expected: PASS (no unreferenced template `admin/waitlist.html`, no nav link pointing at a dead route).

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin.py templates/admin/waitlist.html templates/base.html tests/integration/test_admin_simulation_page.py
git commit -m "feat(admin): remove the admin waitlist views"
```

---

## Task 8: Engine — proposal counter, posting throttle, and resume rehydration

**Files:**
- Modify: `src/agent/simulation.py` (`__init__`, `_phase5_new_post`, `start()`, new helper)
- Test: `tests/unit/test_post_lane.py`, `tests/unit/test_proposal_limit.py` (create)

**Interfaces:**
- Consumes: `SimulationEngine(..., max_proposals: int = 0)`.
- Produces (used by Task 9): `self.max_proposals: int`, `self._proposals_posted: int`, `self._open_interview_count() -> int`.

- [ ] **Step 1: Write the failing throttle test**

```python
# tests/unit/test_proposal_limit.py
"""Proposal-count run limit: stop pitching at N, drain interviews, then stop.
See docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md, Feature 4."""

import pytest
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


def _engine(max_proposals):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    return SimulationEngine(
        agents=[agent],
        slack_clients={"wang": FakeSlackClient(agent_id="wang")},
        max_proposals=max_proposals,
    ), agent


@pytest.mark.asyncio
async def test_phase5_is_throttled_once_the_proposal_cap_is_reached(monkeypatch):
    eng, agent = _engine(max_proposals=2)
    eng._proposals_posted = 2  # cap already reached

    posted = []
    async def _post(*a, **k):
        posted.append(a)
        return "ts-1"
    monkeypatch.setattr(eng, "_post_message", _post)

    await eng._phase5_new_post(agent)
    assert posted == [], "no new pitch may be posted at/over the cap"


@pytest.mark.asyncio
async def test_zero_max_proposals_never_throttles():
    eng, _ = _engine(max_proposals=0)
    assert eng.max_proposals == 0
    assert eng._proposals_posted == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_proposal_limit.py -v`
Expected: FAIL (`max_proposals` kwarg unknown).

- [ ] **Step 3: Add engine state**

In `SimulationEngine.__init__` add the kwarg after `fresh_start: bool = False`:

```python
        max_proposals: int = 0,
```

and in the body, near `self.max_runtime_minutes = ...`:

```python
        # 0 = off. When >0, the engine stops opening NEW pitches once this many
        # top-level posts exist this run, then ends the run when every opened
        # interview has drained (see _proposal_target_drained). Rehydrated on
        # resume from agent_messages (phase='new_post') so a restart cannot
        # re-pitch past the cap.
        self.max_proposals = max_proposals
        self._proposals_posted = 0
```

- [ ] **Step 4: Add the throttle gate in `_phase5_new_post`**

Immediately AFTER the daily-post-cap block (the `if today_posts >= cap:` return), add:

```python
            # Proposal-count limit: once the run has posted its target number of
            # pitches, stop opening new ones and let interviews drain. Checked
            # here, beside the daily cap, so it costs no LLM call.
            if self.max_proposals > 0 and self._proposals_posted >= self.max_proposals:
                logger.info(
                    "[%s] Phase 5: Skipped (proposal cap %d/%d reached)",
                    agent.agent_id, self._proposals_posted, self.max_proposals,
                )
                return
```

- [ ] **Step 5: Increment the counter on a successful post**

In `_phase5_new_post`, in the `else:` branch after `posted` is truthy (where `agent.message_count += 1` is), add right after that line:

```python
                    self._proposals_posted += 1
```

- [ ] **Step 6: Add the rehydration helper and call it from `start()`**

Add a method (near `_rehydrate_assessed_threads`):

```python
    async def _rehydrate_proposal_count(self) -> None:
        """Recover this run's pitch count from the durable store on resume.

        A pitch is a BOT ``agent_messages`` row with ``phase == 'new_post'``
        for this run — the only top-level post kind (the hub is gated out of
        Phase 5, ``pi_lab`` is the only other role, and the two other
        _post_message callers write 'thread_reply' or the panel-note phase).
        The ``is_bot``/``agent_id`` filters keep parity with the live counter,
        which only increments in ``_phase5_new_post``: they exclude the rare
        mirrored human top-level post (``agent_id`` NULL) the Slack poller can
        record in a seeded channel. Without rehydration a resumed run restarts
        the in-memory counter at 0 and could pitch a second full batch past the
        cap.
        """
        if not self.session_factory or self.simulation_run_id is None:
            return
        from sqlalchemy import func, select
        from src.models import AgentMessage
        async with self.session_factory() as db:
            count = (
                await db.execute(
                    select(func.count(AgentMessage.id)).where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        AgentMessage.phase == "new_post",
                        AgentMessage.is_bot.is_(True),
                        AgentMessage.agent_id.isnot(None),
                    )
                )
            ).scalar_one()
        self._proposals_posted = int(count or 0)
        logger.info("Rehydrated proposal count: %d pitch(es) this run", self._proposals_posted)
```

Call it in `start()` right after `await self._rehydrate_assessed_threads()`:

```python
        await self._rehydrate_proposal_count()
```

- [ ] **Step 7: Write and run the rehydration test**

Add to `tests/unit/test_proposal_limit.py` a DB-backed test (mirror the fixture style of `tests/unit/test_engine_control_poll.py` — `_seed_run`, an `async_sessionmaker`): insert two `AgentMessage` rows with `phase="new_post"` for a run, construct an engine with that `session_factory`/`simulation_run_id`, call `await eng._rehydrate_proposal_count()`, assert `eng._proposals_posted == 2`. Insert one `phase="thread_reply"` row and assert it is not counted.

Run: `.venv-test/bin/python -m pytest tests/unit/test_proposal_limit.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_proposal_limit.py
git commit -m "feat(sim): proposal counter, posting throttle, resume rehydration"
```

---

## Task 9: Engine — drain-and-terminate on the proposal target

**Files:**
- Modify: `src/agent/simulation.py` (`__init__` add a settle counter; new `_open_interview_count`, `_proposal_target_drained`; the main-loop `while`)
- Test: `tests/unit/test_proposal_limit.py`

**Interfaces:**
- Consumes: `self.max_proposals`, `self._proposals_posted` (Task 8).
- Produces: loop exits when the target is drained.

**Design:** open interviews = distinct thread ids with `status == "active"` across all agents (the hub participates in every interview; a distinct-set is robust if the hub is absent). To avoid the tail race where the last pitch is posted but the hub has not yet opened its thread, the drain must hold for `PROPOSAL_DRAIN_SETTLE_TICKS` consecutive loop iterations before the loop exits. `max_runtime` remains an independent backstop.

- [ ] **Step 1: Write the failing termination tests**

Add to `tests/unit/test_proposal_limit.py`:

```python
def test_not_drained_before_cap_reached():
    eng, _ = _engine(max_proposals=2)
    eng._proposals_posted = 1
    assert eng._proposal_target_drained() is False


def test_not_drained_while_interviews_open(monkeypatch):
    eng, _ = _engine(max_proposals=2)
    eng._proposals_posted = 2
    monkeypatch.setattr(eng, "_open_interview_count", lambda: 1)
    # Even called enough times to exhaust the settle window, an open interview
    # blocks the drain.
    for _ in range(10):
        assert eng._proposal_target_drained() is False


def test_drains_after_settle_window_once_cap_reached_and_no_open(monkeypatch):
    eng, _ = _engine(max_proposals=2)
    eng._proposals_posted = 2
    monkeypatch.setattr(eng, "_open_interview_count", lambda: 0)
    from src.agent.simulation import PROPOSAL_DRAIN_SETTLE_TICKS
    results = [eng._proposal_target_drained() for _ in range(PROPOSAL_DRAIN_SETTLE_TICKS)]
    assert results[-1] is True
    assert results[0] is False  # not on the first drained tick
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_proposal_limit.py -k "drain or drained" -v`
Expected: FAIL (`_proposal_target_drained` undefined).

- [ ] **Step 3: Add the constant and settle counter**

Near the other module constants (e.g. beside `CONTROL_POLL_INTERVAL`):

```python
# How many consecutive main-loop iterations the proposal target must read
# "cap reached AND no open interviews" before the run ends. Bridges the brief
# window between the last pitch and the hub opening its interview thread; a
# single zero-read is not enough. max_runtime is the independent backstop.
PROPOSAL_DRAIN_SETTLE_TICKS = 3
```

In `__init__`, beside `self._proposals_posted = 0`:

```python
        self._proposal_drain_streak = 0
```

- [ ] **Step 4: Add the two methods**

```python
    def _open_interview_count(self) -> int:
        """Distinct interview threads still open across all agents. The hub is
        in every interview thread; a distinct-set is robust even if it is not."""
        open_ids: set[str] = set()
        for agent in self.agents.values():
            for tid, t in agent.state.active_threads.items():
                if t.status == "active":
                    open_ids.add(tid)
        return len(open_ids)

    def _proposal_target_drained(self) -> bool:
        """True once the pitch cap is reached and no interview has been open
        for PROPOSAL_DRAIN_SETTLE_TICKS consecutive checks. A METHOD, not a
        property, because it MUTATES ``_proposal_drain_streak`` — call it
        exactly once per loop iteration (the main-loop condition does)."""
        if self.max_proposals <= 0 or self._proposals_posted < self.max_proposals:
            self._proposal_drain_streak = 0
            return False
        if self._open_interview_count() > 0:
            self._proposal_drain_streak = 0
            return False
        self._proposal_drain_streak += 1
        return self._proposal_drain_streak >= PROPOSAL_DRAIN_SETTLE_TICKS
```

- [ ] **Step 5: Add it to the main-loop condition**

Change `src/agent/simulation.py` line ~969 (short-circuit order matters: the
method mutates the streak, so it must run once per iteration — it sits last, and
`is_within_time_limit`/`_running` short-circuit before it only when the run is
already ending for another reason, where the streak no longer matters):

```python
        while self._running and self.is_within_time_limit and not self._proposal_target_drained():
```

- [ ] **Step 6: Run the unit tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_proposal_limit.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_proposal_limit.py
git commit -m "feat(sim): end the run when the proposal target drains"
```

---

## Task 10: Thread `max_proposals` through the CLI and run config

**Files:**
- Modify: `src/agent/main.py` (`main` CLI, `_run_simulation` signature/callers, `run_config`, engine ctor, `runtime_label`)
- Test: `tests/unit/test_proposal_limit.py` (a run-config assertion) or the existing main test if present

**Interfaces:**
- Consumes: nothing new.
- Produces (used by Task 11): `_run_simulation(..., max_proposals: int = 0)` as the trailing positional param; `SimulationRun.config["max_proposals"]`.

- [ ] **Step 1: Add the CLI option**

In `main(...)`, after the `all_agents` option, add:

```python
    max_proposals: int = typer.Option(0, "--max-proposals", help="Stop opening new pitches after this many top-level posts, then drain interviews (0 = no limit)"),
```

and change the invocation to pass it (append, preserving order):

```python
    asyncio.run(_run_simulation(max_runtime, budget, mock, no_db, fresh, reset_cursors, all_agents, max_proposals))
```

- [ ] **Step 2: Extend `_run_simulation`**

Append the trailing param:

```python
async def _run_simulation(
    max_runtime: int,
    budget: int,
    mock: bool,
    no_db: bool,
    fresh: bool,
    reset_cursors: bool = False,
    all_agents: bool = False,
    max_proposals: int = 0,
) -> None:
```

Add to `run_config`:

```python
            "max_proposals": max_proposals,
```

Pass to the engine constructor (add beside `fresh_start=fresh`):

```python
        max_proposals=max_proposals,
```

Update the label line so the banner is honest:

```python
    runtime_label = f"{max_runtime}m" if max_runtime > 0 else "indefinite"
    if max_proposals > 0:
        runtime_label += f", {max_proposals} proposals"
```

- [ ] **Step 3: Add a config-stamp test**

Add to `tests/unit/test_proposal_limit.py` (or the main-module test if it exists) an assertion that `run_config` carries the key. The lightest form is a direct check that the engine received it:

```python
def test_engine_accepts_and_stores_max_proposals():
    eng, _ = _engine(max_proposals=5)
    assert eng.max_proposals == 5
```

- [ ] **Step 4: Run**

Run: `.venv-test/bin/python -m pytest tests/unit/test_proposal_limit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/main.py tests/unit/test_proposal_limit.py
git commit -m "feat(sim): --max-proposals CLI option and run-config stamp"
```

---

## Task 11: Thread `max_proposals` through the supervisor (positional contract)

**Files:**
- Modify: `src/agent/supervisor.py` (payload read, `upsert_status` detail, positional `run_fn` call)
- Test: `tests/unit/test_supervisor.py` (update the pinned positional tuple)

**Interfaces:**
- Consumes: `SimulationCommand.payload["max_proposals"]`.
- Produces: `run_fn(max_runtime, 0, False, False, fresh, False, False, max_proposals)`.

- [ ] **Step 1: Update the pinned supervisor test**

In `tests/unit/test_supervisor.py::test_start_enqueued_after_boot_runs_positionally_and_the_loop_exits`:
- change the seeded command payload to include the key:

```python
            cmd = SimulationCommand(command="start", payload={"fresh": True, "max_runtime": 60, "max_proposals": 0})
```

- change the assertion to the 8-tuple:

```python
        assert calls == [(60, 0, False, False, True, False, False, 0)]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_supervisor.py -k positionally -v`
Expected: FAIL (call is still a 7-tuple).

- [ ] **Step 3: Read the payload and pass it positionally**

In `src/agent/supervisor.py`, where `fresh`/`max_runtime` are read from the payload:

```python
                    payload = cmd.payload or {}
                    fresh = bool(payload.get("fresh", False))
                    max_runtime = int(payload.get("max_runtime", 0))
                    max_proposals = int(payload.get("max_proposals", 0))
                    await upsert_status(db, state="starting",
                                        detail={"fresh": fresh, "max_runtime": max_runtime,
                                                "max_proposals": max_proposals})
```

and the run call (append the arg):

```python
                    await run_fn(max_runtime, 0, False, False, fresh, False, False, max_proposals)
```

- [ ] **Step 4: Run**

Run: `.venv-test/bin/python -m pytest tests/unit/test_supervisor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/supervisor.py tests/unit/test_supervisor.py
git commit -m "feat(sim): supervisor forwards max_proposals to the run"
```

---

## Task 12: Admin start form + heartbeat display for `max_proposals`

**Files:**
- Modify: `src/routers/admin.py` (`admin_simulation_start` form field + payload)
- Modify: `templates/admin/simulation.html` (start form input + heartbeat `<dl>`)
- Test: `tests/integration/test_admin_simulation_page.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: the start command payload now carries `max_proposals`.

- [ ] **Step 1: Update the pinned admin test**

In `tests/integration/test_admin_simulation_page.py::test_post_start_creates_pending_command_with_payload_and_audit_row`:
- add `max_proposals` to the POST data:

```python
        data={"fresh": "true", "max_runtime": "30", "max_proposals": "4"},
```

- update the payload assertion:

```python
    assert cmd.payload == {"fresh": True, "max_runtime": 30, "max_proposals": 4}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_simulation_page.py -k creates_pending_command -v`
Expected: FAIL.

- [ ] **Step 3: Add the form field to the handler**

In `admin_simulation_start`, add the parameter and payload key:

```python
    max_proposals: int = Form(0),
```
```python
    payload = {"fresh": fresh, "max_runtime": max_runtime, "max_proposals": max_proposals}
```

- [ ] **Step 4: Add the input and heartbeat row to the template**

In `templates/admin/simulation.html`, in the start form after the `max_runtime` block:

```html
            <div>
                <label class="block text-xs font-medium text-gray-600 mb-1">Max proposals (0 = no limit)</label>
                <input type="number" name="max_proposals" value="0" min="0"
                       class="border border-gray-300 rounded px-2 py-1 text-sm w-40">
            </div>
```

and in the heartbeat `<dl>`, after the `max_runtime` cell:

```html
        {% if status_row.detail.max_proposals is defined %}
        <div><dt class="text-gray-500">Max proposals</dt><dd class="font-medium">{{ status_row.detail.max_proposals }}</dd></div>
        {% endif %}
```

- [ ] **Step 5: Run the admin page suite**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_simulation_page.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/routers/admin.py templates/admin/simulation.html tests/integration/test_admin_simulation_page.py
git commit -m "feat(sim): admin start form + heartbeat surface max_proposals"
```

---

## Task 13: Full gate + deploy notes

**Files:** none (verification).

- [ ] **Step 1: Run the whole gate**

Run: `./scripts/ci.sh`
Expected: PASS (alembic single-head/round-trip — trivially green with no new migration — ruff, full pytest with coverage floor).

- [ ] **Step 2: Record the deploy matrix (no migration)**

Rebuild matrix by feature: Task 1-2 → `blackbird-app`. Task 3-5 → `blackbird-app` + `worker` (docstring/fallback are `src/`; the prompt is bind-mounted). Task 6-7 → `blackbird-app`. Task 8-12 → `blackbird-app` + `worker` + `agent` (engine/main/supervisor are `src/`, baked into the agent image). Deploy is build → (no `alembic upgrade` needed) → `up -d`; **do not auto-start the simulation** — the operator starts a run from `/admin/simulation`, which now shows the Max proposals field.

- [ ] **Step 3: Commit any final doc updates**

```bash
git add -A && git commit -m "docs: deploy notes for the assessment-UI/review-score/login/proposal-limit change"
```

---

## Self-Review

**Spec coverage:**
- F1 strikethrough → Task 1. F1 unstyled lists → Task 2.
- F2 helper text/labels → Task 3. F2 learning path (prompt/docstring/fallback) → Task 4. F2 eval corpus + calibration case → Task 5.
- F3 root redirect + public waitlist removal → Task 6. F3 admin waitlist removal → Task 7.
- F4 counter/throttle/rehydration → Task 8. F4 drain/terminate → Task 9. F4 CLI/run-config → Task 10. F4 supervisor → Task 11. F4 admin form/heartbeat → Task 12. Optional Live-tab progress display was cut (YAGNI — the heartbeat `<dl>` shows the target; the progress bar is not required for the stop behaviour).
- Gate/deploy → Task 13.

**Type consistency:** `max_proposals: int` everywhere; `_run_simulation` and `run_fn` both take it as the trailing (8th) positional; engine kwarg `max_proposals`; state `self._proposals_posted: int`, `self._proposal_drain_streak: int`; helpers `_open_interview_count() -> int`, `_proposal_target_drained` (property, bool), `_rehydrate_proposal_count()` (async). Payload key `"max_proposals"` consistent across admin handler, supervisor, and both pinned tests.

**Placeholder scan:** every code step carries real code; test steps carry real assertions. The two DB-backed tests (Task 2 render helper, Task 8 rehydration) reference the existing fixture idioms in their modules rather than inlining a full Postgres fixture, which is the honest instruction for this suite.

---

## Adversarial audit (2026-09-04, code-verified)

Every claim below was checked against the working tree, not memory. Findings that
changed the plan are marked FIXED; the plan text above already reflects them.

**A1 — Does the hub actually track interview threads? CONFIRMED SAFE.**
`_open_interview_count` relies on the scout_hub populating `active_threads`.
`src/agent/simulation.py:~2102` has an explicit `if agent.role == "scout_hub":`
branch that adds discovered interview threads to `agent.state.active_threads`,
and `_close_thread` (`:2739-2745`) pops the thread from BOTH participants under
lock, so hub and lab stay in sync. Counting distinct active thread ids across
agents is therefore a valid open-interview count.

**A2 — Do engine-posted summary headlines corrupt the rehydration count? CONFIRMED SAFE.**
`_post_assessment_summary` posts via the transport's `client.apost_message(...)`
(`:3758`), NOT `self._post_message`, so it writes no `agent_messages` row; and
`#assessments-summary` is never polled/subscribed, so it is never mirrored in.
No `phase='new_post'` row is produced by the headline path. The only
`_post_message` caller that omits `thread_ts` and a phase override is
`_phase5_new_post` (`:3431`); `:2552` passes `thread_ts`, `:5394` passes the
panel-note phase. So `phase='new_post'` == pitches.

**A3 — Only `pi_lab` posts top-level? CONFIRMED.** Roles are `pi_lab` and
`scout_hub` only (`src/agent/roles.py`); `grantbot` "has cohort memberships but
no AgentRegistry row" (`simulation.py:5623`) so it is not an engine agent. The
hub is Phase-5-gated. FIXED: rehydration filters `is_bot IS TRUE` and
`agent_id IS NOT NULL` to exclude a rare mirrored human top-level post and keep
parity with the live counter.

**A4 — Positional-contract blast radius. CONFIRMED BOUNDED.** The only callers of
`_run_simulation` are `main.py:66` and `supervisor.py:146` (via
`run_fn = run_fn or _run_simulation`). `test_api_call_accounting.py:202` and
`test_shutdown_flush_tasks.py:58` are source scans / comments, not calls.
Appending `max_proposals=0` (defaulted) touches exactly those two call sites and
the one pinned tuple in `test_supervisor.py`.

**A5 — Invented test helpers. FIXED (was a real plan defect).** The first draft
referenced `_get_admin_assessment_detail_html` and `_render_assessment_detail`,
which do not exist. The real idioms are: `test_assessment_detail_page.py` uses a
`_seed(...)` fixture plus `admin`/`manager` fixtures and `resp.text` (and already
has `test_prose_format_markdown_renders_data_markdown_divs_on_both_surfaces`
seeding `prose_format="markdown"`); `test_assessment_review_ui.py` uses
`_seed_assessment` (imported from `test_reviews_router`) plus `admin`/`manager`/
`reviewer` fixtures. Tasks 2 and 3 now use these.

**A6 — e2e waitlist probe. FIXED.** `tests/e2e/test_browser_flows.py`'s
session-autouse origin probe POSTs `/waitlist`; Task 6 Step 4b repoints it to
`POST /logout`.

**A7 — Reachability after landing/waitlist deletion. CONFIRMED SAFE.**
`test_reachability.py`'s only `landing.html`/`waitlist` references are synthetic
fixture inputs to `compute_reachable_templates` (`:984-986`), not assertions that
the template must exist. Deleting `templates/landing.html` and
`templates/admin/waitlist.html` (with their routes and the `base.html` nav link)
leaves the graph consistent. Re-run the test to confirm.

**A8 — Stateful drain check. FIXED (quality).** `_proposal_target_drained` was a
property that mutated `_proposal_drain_streak` — a surprising side effect in a
`while` condition. It is now a METHOD, called once per loop iteration, with tests
updated to call it.

**A9 — `_open_interview_count` scope (accepted, noted).** It counts distinct
active threads across all agents. In this star topology every open thread is a
hub↔lab interview (spokes do not talk; grantbot is not an agent), so this equals
"open interviews". If a non-interview thread ever existed and stayed open, it
would only DELAY proposal-drain termination (never end early), and `max_runtime`
is the independent backstop (design decision 2). Accepted as the safe direction;
counting only the hub's threads is a possible future tightening.

**A10 — Backwards compatibility with `max_proposals=0`. CONFIRMED.** Default 0
means the throttle gate short-circuits and `_proposal_target_drained()` returns
False immediately (resetting the streak), so existing runs and the whole engine
test suite are unaffected. The new heartbeat `<dl>` row is guarded by
`{% if status_row.detail.max_proposals is defined %}`, so a running-state
heartbeat (which carries the engine's own detail shape, without that key) renders
unchanged — identical behaviour to the existing `max_runtime`/`fresh` rows.
