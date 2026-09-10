# Seven-Feature Implementation Plan (2026-09-10)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the seven approved changes in `docs/plans/2026-09-10-seven-feature-investigation-plan.md`: login rebrand, external select labels, impersonated reviews, detail-page readability, structured key points, manager Slack provisioning/activation, and per-stage/per-specialist cost breakdown.

**Architecture:** FastAPI + SQLAlchemy async + Jinja templates. Read paths live in `src/services/*`, routes in `src/routers/*`, engine writes in `src/agent/simulation.py`. Two migrations (`0044`, `0045`) are additive and nullable; all deploys are migrate-before-serve; the agent image must be rebuilt for F1 and F3.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Jinja2, Tailwind Play CDN, pytest (`.venv-test`).

**Spec:** `docs/plans/2026-09-10-seven-feature-investigation-plan.md` (approved, with §Decisions).

## Global Constraints

- Run tests on the host with `.venv-test/bin/python -m pytest tests/... -v`; the full gate is `./scripts/ci.sh` before the final commit of each task group.
- Every new route must be linked from a template or allowlisted with a reason in `tests/unit/test_reachability.py`.
- Manager router POST paths are pinned by `tests/integration/test_manager_views.py:55-74`.
- Every new alembic revision updates `scripts/migrate/preflight.py` (`DEFAULT_TARGET` `:74`, `SUPPORTED_START_REVISIONS` `:128`, `PLANNED_OBJECTS` `:390`, `REVISION_ORDER` `:399`) and `tests/unit/test_migration_checks.py:236`.
- Any `prompts/` edit: run `.venv-test/bin/python scripts/sync_prompt_set_docs.py` and bump `prompts/roles/scout_hub/role.toml` `version` (`1.2.0` → `1.3.0`).
- Templates: gate per-user controls on `effective_user` / `impersonation_banner`, never `current_user` (`src/routers/manager.py:107-117`).
- Never touch `docker-compose.prod.yml`. Never start the simulation.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Revision numbering: Task 8 (F4) takes `0044`; Task 12 (F1) takes `0045`; Task 10 (F2) adds its column inside `0044` only if F4 has not yet been committed — otherwise `0046`. Default below assumes the order written here.

---

## Task 1: F7 — Login rebrand and Blackbird Laboratories logo

**Files:**
- Already added: `static/img/blackbird-laboratories.svg` (from blackbirdlab.org, recoloured `fill="currentColor"`)
- Modify: `templates/login.html:2-12, 45-54`
- Modify: `templates/base.html:6, 49-50, 182`
- Test: `tests/integration/test_login_page.py` (create)

**Interfaces:** none.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_login_page.py
"""The login page carries the Blackbird Laboratories brand and no
collaboration-platform wording (operator decision 2026-09-10, F7)."""
import pytest

pytestmark = pytest.mark.integration


async def test_login_page_shows_the_blackbird_logo_and_no_collaboration_copy(client):
    r = await client.get("/login")
    assert r.status_code == 200
    html = r.text
    assert 'src="/static/img/blackbird-laboratories.svg"' in html
    assert 'alt="Blackbird Laboratories"' in html
    assert "collaborat" not in html.lower()
    assert "Research Collaboration" not in html


async def test_logo_asset_is_served(client):
    r = await client.get("/static/img/blackbird-laboratories.svg")
    assert r.status_code == 200
    assert 'fill="currentColor"' in r.text
```

- [ ] **Step 2: Run it**: `.venv-test/bin/python -m pytest tests/integration/test_login_page.py -v` — expect FAIL on the `collaborat` assertion and the `src=` assertion.

- [ ] **Step 3: Edit `templates/login.html`** — replace lines 2 and 8-12 and line 52:

```html
{% block title %}Sign in — Blackbird{% endblock %}
...
        <!-- Logo / Header -->
        <div class="text-center mb-10">
            <img src="/static/img/blackbird-laboratories.svg" alt="Blackbird Laboratories"
                 width="171" height="120" class="mx-auto mb-4 text-[#353A41]">
            <p class="text-gray-600 text-lg">Translational research review platform</p>
        </div>
...
                <li>Your lab agent joins the Blackbird Slack workspace and starts interviewing ideas</li>
```

- [ ] **Step 4: Edit `templates/base.html`**:
  - `:6` → `<title>{% block title %}Blackbird — Review Platform{% endblock %}</title>`
  - `:49-50` → `<img src="/static/img/blackbird-laboratories.svg" alt="Blackbird Laboratories" width="46" height="32" class="text-[#353A41]">` and `<span class="ml-2 text-sm text-gray-500 hidden sm:block">Review Platform</span>`
  - `:182` → `Blackbird Laboratories &mdash; Review Platform`

- [ ] **Step 5: Run the test again plus the existing page tests**: `.venv-test/bin/python -m pytest tests/integration/test_login_page.py tests/unit/test_email_templates.py tests/unit/test_reachability.py -v` — expect PASS. (`test_email_templates.py:31` pins the email footer, which is separate and untouched.)

- [ ] **Step 6: Grep for other titles pinned in tests**: `grep -rn "CoPI" tests/integration tests/unit | grep -v email` — if any asserts a base-template string you changed, update that assertion in the same commit.

- [ ] **Step 7: Commit**: `git add static/img templates/login.html templates/base.html tests/integration/test_login_page.py && git commit -m "feat(login): Blackbird Laboratories logo, drop collaboration wording"`

---

## Task 2: F6 — External labels on every select; "Overall rating"

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html:514-545` (macro), `:550-563` (helper copy), `:696-710` (edit form), `:731-747` (add form), `:777-782` (assign form)
- Modify: `src/models/review.py:58-60` (docstring), `prompts/review-bot.md:19-31`
- Test: `tests/integration/test_assessment_review_ui.py:133` and new assertions

**Interfaces:** form field names (`score`, `feedback_mode`, `dim_{key}`, `assignee_user_id`) are unchanged — `src/routers/reviews.py` needs no edit.

- [ ] **Step 1: Write failing tests** (append to `tests/integration/test_assessment_review_ui.py`):

```python
import re

async def test_every_select_on_the_detail_page_has_an_external_label(client, db_session, manager):
    assessment = await _seed_assessment(db_session)
    html = (await client.get(f"/manager/assessments/{assessment.id}",
                             headers=auth_headers(manager.id))).text
    assert "Proposal merit" not in html
    assert "Overall rating (1 = weak … 5 = strong)" in html
    select_ids = re.findall(r'<select[^>]*\bid="([^"]+)"', html)
    selects_total = len(re.findall(r"<select\b", html))
    assert selects_total == len(select_ids), "every <select> must carry an id"
    for sid in select_ids:
        assert f'for="{sid}"' in html, f"no <label for> for select #{sid}"
    # The score select keeps a blank first option so an untouched form cannot
    # post score=1 (reviews.py declares score: int = Form(...)).
    m = re.search(r'<select[^>]*id="add-score"[^>]*>\s*<option value=""', html)
    assert m, "score select lost its blank first option"
```

Change `:133` from `assert "Proposal merit" in html` to `assert "Overall rating" in html`.

- [ ] **Step 2: Run**: `.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py -v` — expect the new test to FAIL (no ids).

- [ ] **Step 3: Rewrite the macro** `dimension_score_rows(review_rubric, selected)` to take a third arg `prefix` and emit labelled selects:

```jinja
{% macro dimension_score_rows(review_rubric, selected, prefix) %}
<div class="review-dimension-scores space-y-2">
    {% for d in review_rubric.dimensions %}
    {% set sid = prefix ~ 'dim-' ~ d.key %}
    <div class="review-dimension-row review-dim-{{ d.key }} flex items-start gap-2">
        <select id="{{ sid }}" name="dim_{{ d.key }}" class="border rounded px-1 py-0.5 text-sm shrink-0">
            <option value="">&mdash;</option>
            {% for n in range(review_rubric.scale_min, review_rubric.scale_max + 1) %}
            <option value="{{ n }}" {% if selected.get(d.key) == n %}selected{% endif %}>{{ n }}</option>
            {% endfor %}
        </select>
        <div class="min-w-0">
            <label for="{{ sid }}" class="text-sm font-medium text-gray-700">
                {{ d.title }}
                <span class="font-normal text-gray-600">&middot; {{ d.weight }}% weight</span>
                {% if d.bot_score is not none %}
                <span class="review-bot-score font-normal text-gray-600">&middot; BlackbirdBot scored {{ "%g"|format(d.bot_score) }}</span>
                {% else %}
                <span class="review-bot-score font-normal text-gray-600">&middot; BlackbirdBot did not score this</span>
                {% endif %}
            </label>
            <details class="mt-0.5">
                <summary class="cursor-pointer text-sm text-indigo-600">What the scale means here</summary>
                <p class="mt-1 text-sm text-gray-600">{{ d.anchors }}</p>
            </details>
        </div>
    </div>
    {% endfor %}
</div>
{% endmacro %}
```

- [ ] **Step 4: Rewrite the add form** (`:731-747`):

```jinja
<form method="post" action="/reviews/assessments/{{ a.id }}/feedback" class="mt-2 space-y-2">
    <input type="hidden" name="surface" value="{{ 'admin' if admin_view else 'manager' }}">
    <label for="add-score" class="block text-sm font-medium text-gray-700 mb-1">Overall rating (1 = weak … 5 = strong)</label>
    <select id="add-score" name="score" required class="border rounded px-1 py-0.5 text-sm">
        <option value="" disabled selected>&mdash;</option>
        {% for n in range(1, 6) %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
    </select>
    <p class="block text-sm font-medium text-gray-700 mb-1 mt-3">Rubric dimensions (optional)</p>
    {{ dimension_score_rows(review_rubric, {}, 'add-') }}
    <label for="add-comment" class="block text-sm font-medium text-gray-700 mb-1 mt-3">Comment</label>
    <textarea id="add-comment" name="comment" rows="2" class="w-full border rounded px-2 py-1 text-sm"></textarea>
    <label for="add-mode" class="block text-sm font-medium text-gray-700 mb-1 mt-3">Feedback mode</label>
    <select id="add-mode" name="feedback_mode" required class="border rounded px-1 py-0.5 text-sm">
        <option value="" disabled selected>&mdash;</option>
        <option value="learn">Learn</option>
        <option value="log_only">Don't learn — log only</option>
    </select>
    <button type="submit" class="text-sm font-medium bg-indigo-600 text-white px-2 py-1 rounded hover:bg-indigo-700">Submit feedback</button>
</form>
```

- [ ] **Step 5: Rewrite the edit form** (`:696-710`) the same way with prefix `'edit-' ~ review.id ~ '-'`, ids `edit-{{ review.id }}-score`, `-comment`, `-mode`, no blank option on score (the stored value is selected), label text "Overall rating (1 = weak … 5 = strong)" and "Feedback mode".

- [ ] **Step 6: Assign form** (`:777-782`): add `<label for="assign-user" class="text-sm text-gray-700">Assign reviewer</label>` and `id="assign-user"` on the select.

- [ ] **Step 7: Helper copy** (`:550-563`): replace "The overall proposal-merit score is required" with "The overall rating is required" and "rate the proposal's own scientific and strategic merit" with "rate the proposal's own scientific and strategic quality". Do NOT introduce the substrings `Dimension scores`, `Human review`, `Interview timeline` (ordering test `test_assessment_detail_page.py::test_human_review_card_sits_between_...`).

- [ ] **Step 8: Retire the term elsewhere**: `src/models/review.py:58` → "One human reviewer's overall rating of a proposal (1–5) plus a free-text comment"; `prompts/review-bot.md:19,22,31` → replace "proposal-merit score"/"merit" phrasing with "overall rating (the PROPOSAL's own scientific and strategic quality on the rubric's 1–5 scale)". Check `grep -rn "review-bot.md" tests/ scripts/` — if `test_doc_prompt_sync` or another test embeds it, regenerate with the named script.

- [ ] **Step 9: Run**: `.venv-test/bin/python -m pytest tests/integration/test_assessment_review_ui.py tests/integration/test_assessment_detail_page.py tests/integration/test_reviews_router.py tests/integration/test_review_dimension_scores.py tests/unit/test_doc_prompt_sync.py -v` — expect PASS.

- [ ] **Step 10: Commit**: `git commit -am "feat(reviews): external labels on every select; rename proposal merit to overall rating"`

---

## Task 3: F5 — Readability sweep of the assessment detail body

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html` (whole file, class-only edits), `templates/admin/assessment_detail.html:15-33`, `templates/manager/assessment_detail.html:15-33`
- Test: `tests/integration/test_assessment_detail_page.py` (add one test)

**Interfaces:** none. Section label strings must not change.

- [ ] **Step 1: Write the failing test**:

```python
async def test_the_detail_body_uses_readable_type_sizes(client, db_session, manager):
    """F5: prose at 16px (`text-base`), metadata at 14px (`text-sm`), 12px
    (`text-xs`) reserved for chips; no `text-gray-400` on running text."""
    assessment = await _seed_assessment(db_session)
    html = (await client.get(f"/manager/assessments/{assessment.id}",
                             headers=auth_headers(manager.id))).text
    body = html.split("Assessment detail", 1)[1]
    assert body.count("text-xs") <= 12, body.count("text-xs")
    assert 'class="assessment-prose' in body
    assert "text-gray-400" not in body
```

- [ ] **Step 2: Run** and confirm FAIL.

- [ ] **Step 3: Add a prose block to both wrappers' `<style>`** (`admin/assessment_detail.html` and `manager/assessment_detail.html`, inside the existing `<style>`):

```css
.assessment-prose { max-width: 65ch; font-size: 1rem; line-height: 1.625; color: #1f2937; }
.assessment-prose p + p { margin-top: 1em; }
.assessment-prose li { margin: .35em 0; }
```

- [ ] **Step 4: Wrap the four narrative sections** in `_assessment_detail_body.html` — the "In one minute" pitch (`:79-89`), Key points (`:91-99`), The ask (`:109-120`), Full rationale body (`:417-445`) — each in `<div class="assessment-prose">…</div>` and drop their `text-sm`/`text-xs` classes.

- [ ] **Step 5: Mechanical class sweep** over the file:
  - `text-xs` → `text-sm` everywhere EXCEPT inside `rounded-full` chip spans (keep those `text-xs`).
  - `text-gray-400` → `text-gray-600` on all spans/paragraphs; leave `text-gray-400` only where the element is a `border`/separator (none currently carry text — so replace all).
  - `text-gray-500` on readable spans → `text-gray-600`.
  Command to check afterwards: `grep -c text-xs templates/admin/_assessment_detail_body.html` must be ≤ 12.

- [ ] **Step 6: Run**: `.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py tests/integration/test_assessment_review_ui.py tests/integration/test_assessment_pi_link_rendering.py -v` — PASS.

- [ ] **Step 7: Visual check** with the `run` skill or playwright at 1280px and 390px; confirm no horizontal overflow in the timeline table.

- [ ] **Step 8: Commit**: `git commit -am "style(assessments): readable measure, 16px prose, AA contrast on detail page"`

---

## Task 4: F4 migration `0044` — `recorded_by_user_id` on reviews and events

**Files:**
- Create: `alembic/versions/0044_review_recorded_by.py`
- Modify: `src/models/review.py` (two columns), `scripts/migrate/preflight.py:74,128,390,399`, `tests/unit/test_migration_checks.py:236`
- Test: `tests/unit/test_migration_checks.py` (existing assertions), `tests/integration/test_review_models.py` (add)

**Interfaces:**
- Produces: `AssessmentReview.recorded_by_user_id: uuid | None`, `AssessmentReviewEvent.recorded_by_user_id: uuid | None` (FK users SET NULL).

- [ ] **Step 1: Failing test** (append to `tests/integration/test_review_models.py`):

```python
async def test_recorded_by_columns_exist_and_default_null(db_session):
    from sqlalchemy import text
    cols = (await db_session.execute(text(
        "SELECT table_name FROM information_schema.columns "
        "WHERE column_name='recorded_by_user_id'"))).scalars().all()
    assert sorted(cols) == ["assessment_review_events", "assessment_reviews"]
```

- [ ] **Step 2: Run**: expect FAIL (empty list).

- [ ] **Step 3: Migration**:

```python
"""review rows record who physically entered them when impersonating

Revision ID: 0044
Revises: 0043
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("assessment_reviews", "assessment_review_events"):
        op.add_column(table, sa.Column(
            "recorded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_recorded_by_user_id_users", table, "users",
            ["recorded_by_user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    for table in ("assessment_review_events", "assessment_reviews"):
        op.drop_constraint(f"fk_{table}_recorded_by_user_id_users", table, type_="foreignkey")
        op.drop_column(table, "recorded_by_user_id")
```

- [ ] **Step 4: Model** — add to both classes in `src/models/review.py`:

```python
    #: Set ONLY when an admin entered this row while impersonating the user
    #: named in reviewer_user_id/actor_user_id (operator decision 2026-09-10,
    #: reversing N1/A15). NULL = the named user acted in person.
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
```

- [ ] **Step 5: Preflight pins** — `DEFAULT_TARGET = "0044"`; append `"0043"` to `SUPPORTED_START_REVISIONS` if not present and `"0044"` to `REVISION_ORDER`; add to `PLANNED_OBJECTS`:

```python
    # 0044_review_recorded_by
    PlannedObject("0044", "column", "recorded_by_user_id", "assessment_reviews"),
    PlannedObject("0044", "column", "recorded_by_user_id", "assessment_review_events"),
```
Update `tests/unit/test_migration_checks.py:236` to `"0044"`.

- [ ] **Step 6: Run**: `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py tests/integration/test_review_models.py tests/unit/test_json_none_as_null.py -v` — PASS.

- [ ] **Step 7: Commit**: `git commit -am "feat(reviews): 0044 recorded_by_user_id on reviews and events"`

---

## Task 5: F4 — Allow impersonated review writes, attributed to the impersonated user

**Files:**
- Modify: `src/routers/reviews.py:48-52, 173, 208, 244, 263`, `src/services/assessment_reviews.py:174-215, 218-265, 270-295`
- Modify: `templates/admin/_assessment_detail_body.html:548, 565-577, 655`
- Modify: `tests/integration/test_reviews_router.py:360-375`, `tests/integration/test_assessment_review_ui.py:268-300`
- Docs: `docs/plans/2026-09-09-reviewer-assessment-ui-design.md` (append amendment), CLAUDE.md is untouched (it does not state the rule)

**Interfaces:**
- Consumes: Task 4 columns.
- Produces: `submit_feedback(..., recorded_by: User | None = None)`, `edit_feedback(..., recorded_by=None)`, `record_status_event(..., recorded_by=None)`.

- [ ] **Step 1: Rewrite the router test** at `test_reviews_router.py:360`:

```python
async def test_an_impersonating_admin_reviews_as_the_impersonated_user(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"

    r = await client.post(f"/reviews/assessments/{assessment.id}/feedback",
                          data={"score": "3", "comment": "x", "feedback_mode": "log_only"},
                          headers=headers, follow_redirects=False)
    assert r.status_code == 302
    row = (await db_session.execute(select(AssessmentReview))).scalar_one()
    assert row.reviewer_user_id == mgr.id
    assert row.reviewer_name == mgr.name
    assert row.recorded_by_user_id == admin.id

    r = await client.post(f"/reviews/assessments/{assessment.id}/status",
                          data={"action": "approved"}, headers=headers, follow_redirects=False)
    assert r.status_code == 302
    ev = (await db_session.execute(select(AssessmentReviewEvent))).scalar_one()
    assert ev.actor_user_id == mgr.id and ev.recorded_by_user_id == admin.id


async def test_an_impersonating_admin_still_cannot_assign(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"
    r = await client.post(f"/reviews/assessments/{assessment.id}/assign",
                          data={"assignee_user_id": str(mgr.id)}, headers=headers,
                          follow_redirects=False)
    assert r.status_code == 403
```
(Import `AssessmentReviewEvent` from `src.models`.)

- [ ] **Step 2: Run** — expect FAIL (403).

- [ ] **Step 3: Services** — add `recorded_by: User | None = None` kwarg to `submit_feedback`, `edit_feedback`, `record_status_event`; set `recorded_by_user_id=recorded_by.id if recorded_by else None` on the created row (for `edit_feedback`, assign `review.recorded_by_user_id = recorded_by.id if recorded_by else None`).

- [ ] **Step 4: Router** — add helper and use it:

```python
def _recorded_by(current_user: User) -> User | None:
    """The real admin when the session is impersonating, else None. Review
    writes are attributed to the impersonated user (operator decision
    2026-09-10); this is the second signature on the row."""
    if getattr(current_user, "_is_impersonated", False):
        real = getattr(current_user, "_real_admin", None)
        logger.warning("Review action by admin %s while impersonating %s",
                       getattr(real, "id", None), current_user.id)
        return real
    return None
```
Remove `_refuse_impersonation(current_user)` from `submit_review_feedback`, `edit_review_feedback`, `delete_review_feedback`, `set_review_status`; pass `recorded_by=_recorded_by(current_user)` into the three service calls. KEEP the refusal on `assign_review`, `unassign_review`, `set_suggestion_status`. Update the module docstring line 3.

- [ ] **Step 5: Template** — `:548` → `{% set can_write = true %}`; replace the `{% if not can_write %}` notice (`:571-576`) with:

```jinja
{% if impersonation_banner %}
<p class="review-impersonation-notice mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
    You are reviewing as <strong>{{ impersonation_banner.name }}</strong>. The review is
    recorded under their name; your own account is recorded as the person who entered it.
</p>
{% endif %}
```
Keep `{% if can_write and effective_user.is_staff %}` for assign/unassign but add `and not impersonation_banner` (those routes still 403). At `:655` after `{{ review.reviewer_name }}` add `{% if review.recorded_by_user_id %}<span class="text-sm text-gray-600">(entered by an admin while impersonating)</span>{% endif %}`.

- [ ] **Step 6: Update UI test** `test_impersonating_admin_sees_read_only_card` → rename `test_impersonating_admin_sees_write_forms_and_the_reviewing_as_notice`: assert `'action="/reviews/assessments/'` IS in html, `"You are reviewing as"` in html, `f"/reviews/assessments/{assessment.id}/assign"` NOT in html. Check `tests/integration/test_reviewer_role.py:359` still passes (it covers staff forms on /manager/pis, not reviews).

- [ ] **Step 7: Run**: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py tests/integration/test_assessment_review_ui.py tests/integration/test_reviewer_role.py -v` — PASS.

- [ ] **Step 8: Docs** — append to `docs/plans/2026-09-09-reviewer-assessment-ui-design.md`: "## Amendment 2026-09-10 — N1/A15 reversed. Impersonated review writes are allowed and recorded under the impersonated user with `recorded_by_user_id` = the admin. Assign/unassign and suggestion status remain refused."

- [ ] **Step 9: Commit**: `git commit -am "feat(reviews): impersonating admin may review as the impersonated user (recorded_by)"`

---

## Task 6: F3 — Structured key points: prompt contract

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md:236-241, 294`, `prompts/roles/scout_hub/role.toml:7`
- Modify: `tests/unit/test_rubric_prompt_sync.py:270-280`
- Regenerate: `docs/specs/2026-08-07-hub-bot-prompts.md` via `scripts/sync_prompt_set_docs.py`

**Interfaces:**
- Produces the sidecar shape consumed by Task 7:
  `"key_points": {"significance": [], "innovation": [], "commercial_potential": []}`

- [ ] **Step 1: Update the test** at `test_rubric_prompt_sync.py:278`:

```python
    assert skeleton["key_points"] == {
        "significance": [], "innovation": [], "commercial_potential": []
    }
```
and add:
```python
def test_the_scout_hub_prompt_set_version_is_1_3_0_or_later():
    manifest = tomllib.loads(Path("prompts/roles/scout_hub/role.toml").read_text())
    assert tuple(int(x) for x in manifest["version"].split(".")) >= (1, 3, 0)
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Edit item 7** (`phase4-thread-reply.md:236-241`):

```
7. **Key points.** Three labelled groups, in this order, each holding ONE or
   TWO bullets of at most 160 characters, each a complete claim rather than a
   topic: `significance` (why the problem matters and for whom),
   `innovation` (what is genuinely new versus the state of the art), and
   `commercial_potential` (path to a product, IP, market or partner). Together
   they must let a reviewer who reads nothing else state what the idea is, what
   is established, and the deciding risk. Record them in `key_points` as an
   object with exactly those three keys, each an array of strings.
```
Skeleton `:294` → `"key_points": {"significance": [], "innovation": [], "commercial_potential": []},`

- [ ] **Step 4: Bump** `role.toml` `version = "1.3.0"`; run `.venv-test/bin/python scripts/sync_prompt_set_docs.py`.

- [ ] **Step 5: Run**: `.venv-test/bin/python -m pytest tests/unit/test_rubric_prompt_sync.py tests/unit/test_doc_prompt_sync.py tests/unit/test_claude_md_disclosure_sync.py -v` — PASS.

- [ ] **Step 6: Commit**: `git commit -am "feat(prompts): key_points becomes significance/innovation/commercial_potential (scout_hub 1.3.0)"`

---

## Task 7: F3 — Write gate and rendering for structured key points

**Files:**
- Modify: `src/agent/simulation.py:4491, 4516-4521, 4554-4555, 9115-9116`, `src/models/opportunity.py:69-73`
- Modify: `templates/admin/_assessments_body.html:294-298`, `templates/admin/_assessment_detail_body.html:91-99`
- Test: `tests/integration/test_assessment_narrative_fields.py`, `tests/integration/test_assessment_detail_page.py`

**Interfaces:**
- Consumes: sidecar shape from Task 6.
- Produces: `normalize_key_points(value) -> list | dict | None` in `src/services/assessment_detail.py` (pure, importable by engine and tests); `KEY_POINT_GROUPS = (("significance", "Significance"), ("innovation", "Innovation"), ("commercial_potential", "Commercial potential"))`.

- [ ] **Step 1: Failing tests** (append to `test_assessment_narrative_fields.py`):

```python
from src.services.assessment_detail import normalize_key_points

def test_normalize_accepts_legacy_list_and_the_three_group_object():
    assert normalize_key_points(["a", "b", "c"]) == ["a", "b", "c"]
    obj = {"significance": ["s"], "innovation": ["i1", "i2"], "commercial_potential": ["c"]}
    assert normalize_key_points(obj) == obj

def test_normalize_rejects_wrong_shapes():
    assert normalize_key_points("not a list") is None
    assert normalize_key_points({"significance": ["s"]}) is None            # missing keys
    assert normalize_key_points({"significance": "s", "innovation": [], "commercial_potential": []}) is None
    assert normalize_key_points({"significance": [], "innovation": [], "commercial_potential": [], "extra": []}) is None

async def test_a_grouped_key_points_sidecar_persists_and_renders(client, db_session, admin):
    # Drive _persist_assessment the same way the existing sidecar→column test
    # at :117-130 does, with key_points = the three-group object; then GET the
    # admin detail page and assert the three labels appear in order.
    ...
    assert row.key_points == obj
    html = (await client.get(f"/admin/assessments/{row.id}", headers=auth_headers(admin.id))).text
    i, j, k = html.index("Significance"), html.index("Innovation"), html.index("Commercial potential")
    assert i < j < k
```
(Copy the seeding pattern from `:117-130` verbatim for the elided part.)

- [ ] **Step 2: Run** — FAIL on import.

- [ ] **Step 3: Service**, in `src/services/assessment_detail.py`:

```python
KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("significance", "Significance"),
    ("innovation", "Innovation"),
    ("commercial_potential", "Commercial potential"),
)
_KEY_POINT_KEYS = frozenset(k for k, _ in KEY_POINT_GROUPS)


def normalize_key_points(value: object) -> list | dict | None:
    """Accept the legacy flat list (rows written under scout_hub <= 1.2.0) or
    the three-group object (>= 1.3.0). Anything else is None: a malformed
    narrative field never costs the verdict (A20); raw_verdict keeps it."""
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    if isinstance(value, dict) and set(value) == _KEY_POINT_KEYS and all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in value.values()
    ):
        return value
    return None
```

- [ ] **Step 4: Engine** — `simulation.py:4491` unchanged; replace `:4516-4521` cardinality warning with: for a list, keep the 3–5 check; for a dict, warn when any group has `len not in (1, 2)`. Replace `:4554-4555` with `key_points=normalize_key_points(key_points)`. Import from `src.services.assessment_detail` (check the engine does not already import that module cyclically; if it does, place `normalize_key_points` in `src/services/json_extract.py` instead and import from there). Model `:71` → `Mapped[list | dict | None]`.

- [ ] **Step 5: Templates** — both key-points loops become:

```jinja
{% if a.key_points is mapping %}
<div class="assessment-card-points mt-3 space-y-2">
    {% for key, label in key_point_groups %}
    {% if a.key_points.get(key) %}
    <div><span class="font-semibold text-gray-800">{{ label }}</span>
        <ul class="list-disc list-inside text-gray-700">
        {% for point in a.key_points[key] %}<li>{{ point }}</li>{% endfor %}
        </ul></div>
    {% endif %}
    {% endfor %}
</div>
{% elif a.key_points %}
<ul class="assessment-card-points mt-3 list-disc list-inside text-gray-700 space-y-0.5">
    {% for point in a.key_points %}<li>{{ point }}</li>{% endfor %}
</ul>
{% endif %}
```
`key_point_groups` must reach the templates WITHOUT a new context key on the admin assessments handler (`_assessments_body.html:215-221` forbids one): register it as a Jinja global in the shared templates setup (`grep -n "templates = Jinja2Templates\|templates.env.globals" src/` — add `templates.env.globals["key_point_groups"] = KEY_POINT_GROUPS` in that one place).

- [ ] **Step 6: Run**: `.venv-test/bin/python -m pytest tests/integration/test_assessment_narrative_fields.py tests/integration/test_assessment_detail_page.py tests/integration/test_opportunity_assessment_persistence.py tests/integration/test_assessment_queue_controls.py tests/unit/test_json_none_as_null.py -v` — PASS.

- [ ] **Step 7: Commit**: `git commit -am "feat(assessments): store and render grouped key points; legacy lists still render"`

---

## Task 8: F2 — Shared activation service

**Files:**
- Modify: `src/services/agent_activation.py` (add `activate_agent`), `src/routers/admin.py:1023-1092`
- Test: `tests/unit/test_agent_activation.py` or nearest existing (grep `activation_blockers` in tests/)

**Interfaces:**
- Produces: `async def activate_agent(db, agent, *, actor: User, override: bool) -> list[str]` — returns the blocker list when refused (agent untouched), `[]` when activated (status=active, approved_at, approved_by set; NOT committed).

- [ ] **Step 1: Failing test**:

```python
async def test_activate_agent_refuses_without_override_and_activates_with_it(db_session):
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=user, status="pending")  # no profile
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    blockers = await activate_agent(db_session, agent, actor=admin, override=False)
    assert blockers and agent.status == "pending"
    assert await activate_agent(db_session, agent, actor=admin, override=True) == []
    assert agent.status == "active" and agent.approved_by == admin.id
```
(Use the factory names that exist in `tests/factories.py`; grep `def make_agent`.)

- [ ] **Step 2: Implement**:

```python
async def activate_agent(db, agent, *, actor, override: bool) -> list[str]:
    blockers = await activation_blockers(db, agent)
    if blockers and not override:
        logger.warning("Refused activation of agent %s (%s): %s",
                       agent.agent_id, agent.id, "; ".join(blockers))
        return blockers
    if blockers:
        logger.warning("Activation OVERRIDE by %s for agent %s (%s) despite: %s",
                       actor.id, agent.agent_id, agent.id, "; ".join(blockers))
    agent.status = "active"
    agent.approved_at = datetime.now(UTC)
    agent.approved_by = actor.id
    return []
```
Refactor `admin_approve_agent` to call it for the pending→active and `agent_status == "active"` branches, preserving the existing redirect on refusal and the slug/name/token edits only on success.

- [ ] **Step 3: Run** admin agent tests: `grep -rl "agents/.*approve" tests/integration | xargs .venv-test/bin/python -m pytest -v` — PASS.

- [ ] **Step 4: Commit**: `git commit -am "refactor(agents): extract activate_agent for reuse by the manager surface"`

---

## Task 9: F2 — Manager provision + activate routes, callback re-gate

**Files:**
- Create: `alembic/versions/0045_slack_provision_initiated_by.py` (nullable `slack_app_provisions.initiated_by_user_id` UUID FK users SET NULL) + preflight/test pins as in Task 4 (target `0045`)
- Modify: `src/models/provisioning.py` (column), `src/services/admin_provisioning.py:33, 153-230, 233-280` (`start_provisioning(db, agent, *, initiated_by)`, `complete_provisioning(db, state, code, *, completing_user)`), `src/routers/admin.py:1116-1171`, `src/routers/manager.py` (two POST routes), `templates/manager/pi_detail.html:29-51`
- Modify tests: `tests/integration/test_manager_views.py:59-64`, `tests/integration/test_manager_pi_writes.py:330-371`
- Create: `tests/integration/test_manager_slack_provisioning.py`
- Docs: `docs/specs/2026-08-21-manager-pi-controls-design.md` (D1 amendment), CLAUDE.md "Manager" bullet ("Still cannot impersonate, set roles, or provision Slack bots" → remove "or provision Slack bots"; note activation via manager), `tests/unit/test_claude_md_disclosure_sync.py` if it pins that sentence.

**Interfaces:**
- Consumes: `activate_agent` (Task 8), `start_provisioning`, `complete_provisioning`.
- Produces routes: `POST /manager/pis/{user_id}/slack/provision`, `POST /manager/pis/{user_id}/activate`.

- [ ] **Step 1: Failing tests** (`test_manager_slack_provisioning.py`), monkeypatching `src.services.admin_provisioning.start_provisioning` to return `"https://slack.test/authorize?x=1"` and `complete_provisioning` to set the token:

```python
async def test_manager_can_start_provisioning_for_a_pending_pi(client, db_session, manager, monkeypatch):
    pi, agent = await _pending_pi(db_session)           # helper: make_user(role pi) + make_agent(status pending)
    async def fake_start(db, a, *, initiated_by): assert initiated_by.id == manager.id; return "https://slack.test/authorize"
    monkeypatch.setattr("src.routers.manager.start_provisioning", fake_start)
    r = await client.post(f"/manager/pis/{pi.id}/slack/provision", headers=auth_headers(manager.id), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://slack.test/")

async def test_reviewer_and_impersonating_admin_are_refused(client, db_session, admin, manager, reviewer): ...  # 403 both

async def test_callback_completes_for_the_initiating_manager_and_redirects_to_manager_surface(...):
    # seed SlackAppProvision(initiated_by_user_id=manager.id, state="s1"), monkeypatch exchange_code
    r = await client.get("/admin/agents/slack/callback?code=c&state=s1", headers=auth_headers(manager.id), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/manager/pis/{pi.id}?slack_ok=1"

async def test_callback_refuses_a_different_user(...):  # other staff user → 302 to their surface with slack_error

async def test_manager_activate_is_gated_and_has_no_override(client, db_session, manager):
    # pending agent, no profile → 302 back with ?activation_blocked=1, status still pending
    # then give it a grounded profile (copy the fixture used by admin approve tests) → status active, approved_by == manager.id
```

- [ ] **Step 2: Update pins**: allowlist in `test_manager_views.py:59-64` gains `"/pis/{user_id}/slack/provision"` and `"/pis/{user_id}/activate"`; rewrite `test_manager_pi_writes.py:330-371` to assert the manager sees a Provision button (`action="/manager/pis/{id}/slack/provision"`) and no "ask an admin" text.

- [ ] **Step 3: Run** — FAIL.

- [ ] **Step 4: Migration `0045`** (same shape as Task 4's, one column + FK on `slack_app_provisions`), model column, preflight pins (`DEFAULT_TARGET = "0045"`, `REVISION_ORDER`, `PLANNED_OBJECTS`, `SUPPORTED_START_REVISIONS` includes `"0044"`), `test_migration_checks.py:236` → `"0045"`.

- [ ] **Step 5: Service** — `start_provisioning(db, agent, *, initiated_by: User)` stores `initiated_by_user_id=initiated_by.id` on the bridge row. `complete_provisioning(db, state, code, *, completing_user: User)` raises `ProvisioningError("This install was started by a different account.")` when `prov.initiated_by_user_id not in (None, completing_user.id)` (None = pre-0045 row, allowed). Update the CLI/other callers: `grep -rn "start_provisioning\|complete_provisioning" src scripts tests`.

- [ ] **Step 6: Admin callback** (`admin.py:1143`): gate `Depends(get_staff_user)`; compute `surface_error(msg)` → `/admin/agents?slack_error=…` for admins, `/manager/pis?slack_error=…` for managers; on success redirect admin → `/admin/agents/{agent.id}?slack_ok=1`, manager → `/manager/pis/{agent.user_id}?slack_ok=1` (agent.user_id may be None → fall back to `/manager/pis?slack_ok=1`). Pass `completing_user=current_user`. Admin provision route passes `initiated_by=current_user`.

- [ ] **Step 7: Manager routes** (`manager.py`, after `manager_unmute_pi`):

```python
@router.post("/pis/{user_id}/slack/provision")
async def manager_provision_slack(user_id: uuid.UUID, request: Request,
                                  db: AsyncSession = _DB, current_user: User = _STAFF):
    if getattr(current_user, "_is_impersonated", False):
        raise HTTPException(status_code=403, detail="Disabled while impersonating.")
    agent = await _pending_pi_agent(db, user_id)      # 404 unless user_role == pi, agent exists, status == pending
    try:
        url = await start_provisioning(db, agent, initiated_by=current_user)
    except ProvisioningError as exc:
        return RedirectResponse(url=f"/manager/pis/{user_id}?slack_error={quote(str(exc)[:200])}", status_code=302)
    return RedirectResponse(url=url, status_code=302)


@router.post("/pis/{user_id}/activate")
async def manager_activate_agent(user_id: uuid.UUID, request: Request,
                                 db: AsyncSession = _DB, current_user: User = _STAFF):
    if getattr(current_user, "_is_impersonated", False):
        raise HTTPException(status_code=403, detail="Disabled while impersonating.")
    agent = await _pending_pi_agent(db, user_id)
    if not agent.slack_bot_token:
        return RedirectResponse(url=f"/manager/pis/{user_id}?slack_error=Install+the+Slack+bot+first", status_code=302)
    blockers = await activate_agent(db, agent, actor=current_user, override=False)
    if blockers:
        return RedirectResponse(url=f"/manager/pis/{user_id}?activation_blocked=1", status_code=302)
    await db.commit()
    return RedirectResponse(url=f"/manager/pis/{user_id}?activated=1", status_code=302)
```
`manager_pi_detail` passes `slack_ok`, `slack_error`, `activation_blocked`, `activated` query params and, when blocked, `blockers = await activation_blockers(db, agent)` into the context.

- [ ] **Step 8: Template** `pi_detail.html:43-51` — inside the existing `effective_user.is_staff and not impersonation_banner` block, replace the admin-link/else pair with:

```jinja
{% if not target_user.agent.slack_bot_token %}
<form method="post" action="/manager/pis/{{ target_user.id }}/slack/provision" class="mt-1">
    <button type="submit" class="text-sm px-3 py-1 rounded bg-indigo-600 text-white hover:opacity-90">Install Slack bot</button>
</form>
{% else %}
<form method="post" action="/manager/pis/{{ target_user.id }}/activate" class="mt-1">
    <button type="submit" class="text-sm px-3 py-1 rounded bg-green-600 text-white hover:opacity-90">Activate agent</button>
</form>
{% endif %}
```
Add banners near the top of the page for `slack_ok` / `slack_error` / `activation_blocked` (list `blockers`, "ask an admin to override") / `activated`, copying the admin markup at `templates/admin/agent_detail.html:18-27`.

- [ ] **Step 9: Run**: `.venv-test/bin/python -m pytest tests/integration/test_manager_slack_provisioning.py tests/integration/test_manager_views.py tests/integration/test_manager_pi_writes.py tests/integration/test_provisioning_loop.py tests/unit/test_slack_provisioning.py tests/unit/test_reachability.py tests/unit/test_migration_checks.py -v` — PASS.

- [ ] **Step 10: Docs** — D1 amendment in the 2026-08-21 spec; CLAUDE.md Manager bullet; run `.venv-test/bin/python -m pytest tests/unit/test_claude_md_disclosure_sync.py`.

- [ ] **Step 11: Commit**: `git commit -am "feat(manager): provision Slack bots and activate agents from the PI page (0045)"`

---

## Task 10: F1 — Migration `0046` and producers for `thread_phase` / `message_ordinal`

**Files:**
- Create: `alembic/versions/0046_llm_call_logs_thread_phase.py` (two nullable columns on `llm_call_logs`) + preflight/test pins (target `0046`)
- Modify: `src/models/agent_activity.py:197-204` (columns), `src/agent/simulation.py:2470-2475, 8166-8215`, `src/agent/tools.py:678-683` and the call path that hands `thread_ts` into `consult_specialist` (grep `thread_ts=` in `src/agent/tools.py` and `src/agent/agent.py`), `tests/factories.py:166-183`
- Test: `tests/unit/test_llm_log_channel.py` (pattern for log_meta → row), `tests/integration/test_llm_call_stats_storage.py`

**Interfaces:**
- Produces: `LlmCallLog.thread_phase: str | None` ∈ {`explore`,`decide`,`conclude`}, `LlmCallLog.message_ordinal: int | None`; `log_meta` keys `thread_phase`, `message_ordinal`.

- [ ] **Step 1: Failing test** (follow `test_llm_log_channel.py`'s pattern of driving `_llm_log_record`):

```python
def test_llm_log_record_maps_thread_phase_and_ordinal(engine_fixture):
    row = engine_fixture._llm_log_record({"agent_id": "hub", "phase": "thread_reply",
                                          "thread_phase": "decide", "message_ordinal": 7})
    assert row.thread_phase == "decide" and row.message_ordinal == 7

def test_llm_log_record_defaults_them_to_none(engine_fixture):
    row = engine_fixture._llm_log_record({"agent_id": "hub", "phase": "thread_reply"})
    assert row.thread_phase is None and row.message_ordinal is None
```

- [ ] **Step 2: Migration + model + pins** (as Tasks 4/9). Model:

```python
    #: Phase-4 interview stage of a thread_reply/consult turn (explore|decide|
    #: conclude) and the reply's ordinal in the thread, stamped by the producer
    #: from the same phase4_guidance() call that built the prompt. NULL on
    #: every pre-0046 row and on new_post/memory turns — never backfilled.
    thread_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
```
`_llm_log_record`: add `thread_phase=entry.get("thread_phase"), message_ordinal=entry.get("message_ordinal"),`.

- [ ] **Step 3: Producers** — at `simulation.py:2470` the enclosing function already computes `thread_phase` via `phase4_guidance(...)` (see `:2529`; hoist that call above the `log_meta` if it is below): add `"thread_phase": thread_phase.lower(), "message_ordinal": thread.message_count + 1`. For consults: find where `thread_ts` is threaded into `consult_specialist` (tools.py `:683`) and pass `thread_phase` the same way (add a `thread_phase: str | None = None` parameter defaulting None; include it in the consult `log_meta`).

- [ ] **Step 4: Factory** — no default change needed (nullable); add nothing.

- [ ] **Step 5: Run**: `.venv-test/bin/python -m pytest tests/unit/test_llm_log_channel.py tests/integration/test_llm_call_stats_storage.py tests/unit/test_migration_checks.py tests/integration/test_specialist_consult_capture.py -v` — PASS.

- [ ] **Step 6: Commit**: `git commit -am "feat(engine): stamp thread_phase and message_ordinal on llm_call_logs (0046)"`

---

## Task 11: F1 — Aggregates and admin panels

**Files:**
- Modify: `src/services/simulation_stats.py` (after `cost_summary`), `src/routers/admin.py:2251-2263, 2355-2374, 2510-2543`, `templates/admin/simulation.html:340-354`
- Test: `tests/unit/test_simulation_stats.py`, `tests/integration/test_admin_simulation_page.py`

**Interfaces:**
- Consumes: Task 10 columns; `_grouped_cost`, `hbar_list`, `cost_for_tokens`.
- Produces:

```python
@dataclass(frozen=True)
class StageCost:      # one row of "cost by interview stage"
    role: str         # 'scout_hub' | 'pi_lab' | 'unknown'
    thread_phase: str # 'explore'|'decide'|'conclude'|'unclassified'
    cost: Decimal
    call_count: int

@dataclass(frozen=True)
class SpecialistCost:
    domain: str       # phase suffix after 'consult_'
    verdict_signal: str  # 'blocking'|'gap'|'adequate'|'unmatched'
    cost: Decimal
    call_count: int

@dataclass(frozen=True)
class CallKindCost:
    kind: str         # round|final|forced_final|retry
    cost: Decimal
    call_count: int
    is_floor: bool    # any run row with call_stats IS NULL

async def cost_by_stage(db, run_id) -> list[StageCost]
async def cost_by_specialist(db, run_id) -> list[SpecialistCost]
async def cost_by_call_kind(db, run_id) -> list[CallKindCost]
```

- [ ] **Step 1: Failing tests** with hand-computed numbers (module docstring rule). Example:

```python
async def test_cost_by_stage_buckets_null_phase_as_unclassified(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(db_session, run=run, agent_id="blackbird", phase="thread_reply",
        thread_phase="decide", model="claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    await factories.make_llm_call_log(db_session, run=run, agent_id="blackbird", phase="thread_reply",
        thread_phase=None, model="claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    rows = await cost_by_stage(db_session, run.id)
    by = {(r.role, r.thread_phase): r for r in rows}
    # opus-5 input price from llm_pricing.PRICES → compute expected by hand here
    assert by[("scout_hub", "decide")].call_count == 1
    assert by[("scout_hub", "unclassified")].call_count == 1
```
Role comes from `AgentRegistry.role` joined on `agent_id` (LEFT JOIN; missing → `'unknown'`). Also tests: specialist split joins `specialist_consults` on `(simulation_run_id, thread_id == thread_ts, domain == suffix)` and labels unmatched rows `'unmatched'`; call-kind sums `jsonb_array_elements(call_stats)` tokens and sets `is_floor` when any row has NULL `call_stats`; empty run → three empty lists.

- [ ] **Step 2: Implement** the three functions with `func.coalesce(LlmCallLog.thread_phase, "unclassified")`, `func.coalesce(AgentRegistry.role, "unknown")`, and for specialists `LlmCallLog.phase.like("consult_%")` with `func.substr(LlmCallLog.phase, 9)` as domain, outer-joined to `SpecialistConsult`, `func.coalesce(SpecialistConsult.verdict_signal, "unmatched")`. Call-kind: a raw `select` over `jsonb_array_elements(LlmCallLog.call_stats)` using `func.jsonb_array_elements(...).table_valued("value")`; price with `cost_for_tokens(model, input_tokens=elem['input_tokens'], output_tokens=elem['output_tokens'] + elem['thinking_tokens'], cache_read=0, cache_creation=0)` and set `is_floor=True` always-with-note that per-call cache tokens are not recorded (label the panel "excl. cache").

- [ ] **Step 3: Router** `_live_tab_context`: call the three, build `cost_by_stage_html = hbar_list([(f"{r.role} · {r.thread_phase}", float(r.cost), f"${r.cost:.2f} ({r.call_count} turns)") ...])`, likewise `cost_by_specialist_html` (label `f"{domain} · {signal}"`) and `cost_by_call_kind_html`; add to the returned dict.

- [ ] **Step 4: Template** — add three panels after "Cost by phase" using the identical card markup at `simulation.html:340-354`, headings "Cost by interview stage", "Cost by specialist consult", "Cost by call kind (excl. cache)". Empty state text: "No classified turns yet — rows written before migration 0046 are unclassified."

- [ ] **Step 5: Run**: `.venv-test/bin/python -m pytest tests/unit/test_simulation_stats.py tests/integration/test_admin_simulation_page.py -v` — PASS (including `:442` empty-run render).

- [ ] **Step 6: Commit**: `git commit -am "feat(admin/simulation): cost by interview stage, specialist consult and call kind"`

---

## Task 12: Full gate, CLAUDE.md, deploy notes

- [ ] **Step 1**: `./scripts/ci.sh` on the host (not over sshfs if it appears to hang). Must be green: alembic single head `0046`, round-trip, ruff, coverage floor.
- [ ] **Step 2**: CLAUDE.md additions: deploy box for `0044`/`0045`/`0046` (all additive/nullable, migrate-before-serve, agent image rebuild required for `0046` producers and the F3 prompt+image pairing; scout_hub `1.3.0`); Manager bullet updated (Task 9); reviews bullet noting impersonated writes are allowed and `recorded_by_user_id`.
- [ ] **Step 3**: `.venv-test/bin/python -m pytest tests/unit/test_claude_md_disclosure_sync.py tests/unit/test_doc_prompt_sync.py -v`.
- [ ] **Step 4**: Commit `docs: CLAUDE.md deploy boxes for 0044-0046 and policy changes`.
- [ ] **Step 5**: Report to the operator: deploy sequence is build app+worker+agent → `alembic upgrade head` → `up -d blackbird-app worker` → `up -d agent` (supervisor returns IDLE; do not start a run). Flag that the agent process must be restarted by the operator to pick up F1 producers and the F3 prompt.

---

## Self-review

- Spec coverage: F1 → Tasks 10-11; F2 → 8-9; F3 → 6-7; F4 → 4-5; F5 → 3; F6 → 2; F7 → 1; deploy/docs → 12. Decisions 1-5 all reflected.
- Revision numbering is fixed in this plan as 0044 (F4), 0045 (F2), 0046 (F1); the Global Constraints note about reordering applies only if tasks are executed out of order.
- Names consistent: `normalize_key_points`, `KEY_POINT_GROUPS`, `activate_agent`, `start_provisioning(initiated_by=)`, `complete_provisioning(completing_user=)`, `StageCost`/`SpecialistCost`/`CallKindCost`, `_recorded_by`.
- Known soft spots for the executor: Task 7 Step 4 (possible import cycle — fallback location given), Task 10 Step 3 (locate how `thread_ts` reaches `consult_specialist`), Task 11 Step 2 (JSONB array pricing query — write the SQL, test with hand numbers).

---

## Execution notes (2026-09-10)

- **Migration numbering was renumbered at execution time.** This plan fixes the
  revisions as 0044 (F4), 0045 (F2), 0046 (F1). What actually landed is
  **0044 = F4** (`0044_review_recorded_by.py`), **0045 = F1**
  (`0045_llm_call_logs_thread_phase.py`), **0046 = F2**
  (`0046_slack_provision_initiated_by.py`) — F1 and F2 are swapped relative to
  the plan, because the tasks were executed in a different order than numbered.
  Single head is `0046`. Every reference elsewhere in this plan to "0045 (F2)"
  or "0046 (F1)" should be read against that mapping.
- **Task 12 executed**: combined `0044`/`0045`/`0046` deploy box added to
  CLAUDE.md after the `0043` box, the Reviewer bullet updated for
  impersonated review writes + `recorded_by_user_id`, and the full
  `./scripts/ci.sh` gate run. The Manager bullet was already updated by Task 9
  and was left alone.
