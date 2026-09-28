# Chat entry points, 250-word pitch, per-dimension rationale, key points 1.9.0 — Implementation Plan

> **For agentic workers:** this plan is executed through `/engineering:plan-execution`
> (the engineering policy's replacement for `subagent-driven-development` and
> `executing-plans`). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give staff two faster ways into the assessment chat, raise the
elevator pitch to 250 words, make every rubric dimension carry the hub's reason
for its score in the Evidence summary, and cut key points to six one-bullet
plain-language groups with a new path-to-clinic group.

**Architecture:** Four independent strands over the same two surfaces. The chat
strand is template + vanilla JS only (no route, no server change). The pitch
strand is a prompt bound plus one engine constant. The rationale strand is a
new sidecar field, migration `0052`, and a fourth bucket in the existing
read-path derivation. The key-points strand is a prompt-set bump to scout_hub
1.9.0 plus a retirement mechanism that keeps 14 stored rows rendering exactly
as they were written.

**Tech Stack:** FastAPI + SQLAlchemy 2 async + Alembic, Jinja2 templates,
Tailwind (Play CDN), vanilla JS, pytest + testcontainers Postgres.

**Spec:** `docs/specs/2026-09-28-assessment-chat-entry-and-key-points-design.md`

---

## How to run anything in this repo

The working tree is an **sshfs mount of the production host**. Running pytest
through the mount is 100–400× slower (CLAUDE.md). Run every test command ON THE
HOST:

```bash
ssh -4 -o StrictHostKeyChecking=yes ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com \
  'cd blackbird-copi-science && .venv-test/bin/python -m pytest <args>'
```

Every `pytest …` command below is shorthand for that. Never run
`pip install` against `.venv-test` from the client — it corrupts the venv's
shebangs.

The DB-backed suites start their own ephemeral Postgres via testcontainers; no
`TEST_DATABASE_URL`, no container, no manual database.

## Global Constraints

- **Never `git checkout`/`stash`/`restore` `docker-compose.prod.yml`** — the
  host's working-tree copy is uncommitted and load-bearing (CLAUDE.md).
- **Do not start the simulation.** Standing operator rule.
- **Do not push.** `origin` is public; this branch is not scrubbed.
- No absolute `/admin/` or `/manager/` URL may appear in
  `templates/admin/_assessments_body.html` or
  `templates/admin/_assessment_detail_body.html`; literal `/reviews/` and
  `/assessment-chat/` paths are the two recorded exceptions.
- Inside `<main>` on the detail page: no `text-gray-400`, no `text-gray-500`,
  and `text-xs` only on `rounded-full` chips (ceiling 13 occurrences).
- Sidecar shape violations are **warnings, never drops**; only a genuine type
  violation nulls a field, and `raw_verdict` always keeps the original.
- Every new nullable JSON/JSONB column must use `JSONB(none_as_null=True)`.
- Stored rows are never rewritten or backfilled.
- Key-point bullets: **at most 300 characters** (`_KEY_POINT_BULLET_CHARS`).
- Dimension rationale: **at most 200 characters**, one sentence.
- Elevator pitch: **at most 250 words**; elements 1–4 must end within
  approximately 550 characters; `PITCH_DISPLAY_CHARS` stays **600**.
- scout_hub prompt set → **1.9.0**; rubric stays **3.5.0** (no
  `revisions.toml` entry, no review-doc regeneration).

## Review Focus

Five inputs the spec implies that no task's happy path exercises. Each has a
test assigned to the task that owns the code.

1. **A `dimension_rationales` key that matches no dimension** (a typo, or a
   dimension renamed by a later rubric revision) must render nothing and raise
   nothing — never attach to the wrong dimension. → Task 6, Step 3.
2. **A row whose rubric revision is not in the registry.**
   `derive_strengths_and_risks` emits no dimension entries at all for it, while
   `build_assessment_detail` still lists the dimensions — so the rationale must
   still reach the page (via `#scores`) or the chat record breaks parity. →
   Task 7, Step 5.
3. **A stored `key_points` carrying BOTH `key_questions` and
   `path_to_clinic`** (a 1.8.0 row read after 1.9.0 lands, or a model emitting
   both) must render each exactly once, in the documented order. → Task 8,
   Step 7.
4. **`#chat` on a page where chat is unavailable** (kill switch or
   impersonation): the fragment must be inert — no drawer, no console error,
   no JS at all, because the script is not loaded. → Task 1, Step 9.
5. **Pitch word-count boundaries**: exactly 250 words must not warn, 251 must,
   and an empty or whitespace-only pitch must neither warn nor raise. →
   Task 3, Step 3.

---

## File map

| File | Responsibility | Task |
| --- | --- | --- |
| `templates/admin/_assessment_chat_drawer.html` | the bubble | 1 |
| `templates/admin/_assessment_detail_body.html` | nav button removed; rationale rows; Mid-scale section; `#scores` rationale | 1, 6 |
| `static/js/assessment_chat.js` | bubble hide/show; `#chat` auto-open | 1 |
| `templates/admin/assessments.html`, `templates/manager/assessments.html` | `assessment_chat_link` macro (literal paths) | 2 |
| `templates/admin/_assessments_body.html` | `card_chat_available`; render the chat link | 2 |
| `prompts/roles/scout_hub/phase4-thread-reply.md` | items 2, 7, 8 + skeleton | 3, 5, 9 |
| `prompts/roles/scout_hub/role.toml` | version 1.9.0 | 9 |
| `src/agent/simulation.py` | pitch word bound; rationale persist; key-point warnings | 3, 5, 9 |
| `alembic/versions/0052_assessment_dimension_rationales.py` | the column | 4 |
| `src/models/opportunity.py` | mapped column | 4 |
| `scripts/migrate/preflight.py` | target/order/starts/planned objects | 4 |
| `src/services/assessment_detail.py` | normalizer; `dimensions[].rationale`; `mid_scale`; key-point groups | 4, 6, 8 |
| `src/services/assessment_chat_record.py` | rationale in the dimension blocks | 7 |
| `tests/assessment_chat_support.py` | fixture shapes | 7 |
| `CLAUDE.md` | `0052` box, pitch and key-point wording | 10 |

---

## Task 1: Floating chat bubble and `#chat` auto-open

**Files:**
- Modify: `templates/admin/_assessment_chat_drawer.html` (add the bubble before the `<aside>` at `:20`)
- Modify: `templates/admin/_assessment_detail_body.html:84-88` (replace the nav control)
- Modify: `static/js/assessment_chat.js` (`openDrawer` `:907`, `closeDrawer` `:930`, end of IIFE `:984`)
- Test: `tests/integration/test_assessment_chat_templates.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the `data-chat-bubble` attribute and the `#chat` fragment
  convention, which Task 2's list link targets.

- [ ] **Step 1: Write the failing template tests**

Add to `tests/integration/test_assessment_chat_templates.py`:

```python
async def test_the_detail_page_offers_the_bubble_and_no_nav_button(client, db_session):
    _, _, body = await _page(client, db_session, USER_ROLE_ADMIN, "admin")
    assert "data-chat-bubble" in body
    assert "Ask about this assessment" in body          # the bubble's aria-label
    assert "<button" in body
    # The nav button is gone: the only chat opener left is the bubble.
    assert body.count("data-chat-open") == 1
    # A normal staff session must NOT be told chat is unavailable.
    assert "Chat unavailable" not in body


async def test_the_bubble_sits_under_the_drawer_and_is_not_printed(client, db_session):
    _, _, body = await _page(client, db_session, USER_ROLE_ADMIN, "admin")
    bubble = body[body.index("data-chat-bubble") - 400:body.index("data-chat-bubble") + 400]
    assert "z-30" in bubble
    assert "print:hidden" in bubble
    assert "text-gray-400" not in bubble and "text-gray-500" not in bubble
    assert "text-xs" not in bubble
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/integration/test_assessment_chat_templates.py -v -k "bubble"`
Expected: FAIL — `data-chat-bubble` is not in the page.

- [ ] **Step 3: Replace the nav control**

In `templates/admin/_assessment_detail_body.html`, replace lines 84-88 (the
`{% if chat_available %} … {% elif chat_enabled %} … {% endif %}` block) with:

```jinja
    {# The opener is the floating bubble in the drawer partial (2026-09-28).
       This branch is what a user sees when there is deliberately no control:
       `chat_enabled and not chat_available` is exactly "the feature is on but
       this session may not use it", i.e. impersonation. A bare
       `{% if chat_enabled %}` would print it beside a working bubble for every
       ordinary staff session. #}
    {% if chat_enabled and not chat_available %}
    <span class="text-sm text-gray-600">Chat unavailable while impersonating</span>
    {% endif %}
```

- [ ] **Step 4: Add the bubble**

In `templates/admin/_assessment_chat_drawer.html`, immediately before the
`<aside id="assessment-chat"` line:

```jinja
{# The one opener for the drawer (2026-09-28). It lives HERE, not in the page
   body, because this partial is included only under `chat_available` — so the
   kill switch and the impersonation refusal gate the control for free, and one
   place decides whether a chat control exists at all.
   * `z-30` against the drawer's `z-40`: on a narrow screen the drawer is
     `fixed inset-0`, and an equal or higher z-index would float this over the
     open panel.
   * The accessible name is the sentence the old nav button carried, so nothing
     a screen reader hears changed when the visible label went away.
   * No `text-xs`, no `text-gray-400/500`: this renders inside <main> and
     test_the_detail_body_uses_readable_type_sizes counts both. #}
<button type="button" data-chat-open data-chat-bubble
        aria-controls="assessment-chat" aria-expanded="false"
        title="Ask about this assessment" aria-label="Ask about this assessment"
        class="ph-no-capture print:hidden fixed bottom-6 right-6 z-30 flex h-14 w-14 items-center justify-center rounded-full bg-indigo-600 text-white shadow-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2">
  <svg class="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
  </svg>
</button>
```

- [ ] **Step 5: Run the template tests to verify they pass**

Run: `pytest tests/integration/test_assessment_chat_templates.py -v`
Expected: PASS, including the pre-existing impersonation and kill-switch tests.

- [ ] **Step 6: Check the type-size ceiling did not move**

Run: `pytest tests/integration/test_assessment_detail_page.py -v -k readable_type_sizes`
Expected: PASS.

- [ ] **Step 7: Hide the bubble while the drawer is open**

In `static/js/assessment_chat.js`, add near the other element lookups (after
`const openers = …`, `:101`):

```js
  const bubble = document.querySelector("[data-chat-bubble]");
```

In `openDrawer`, after the `openers.forEach(... "true")` line:

```js
    // Tailwind's own `hidden`, the class this partial already uses for the
    // drawer itself — not an invented name, and not the `aria-expanded:`
    // variant: the page loads Tailwind from the Play CDN, and a variant that
    // failed to compile would leave the bubble sitting over the open drawer
    // with nothing in the rendered HTML to catch it.
    if (bubble) { bubble.classList.add("hidden"); }
```

In `closeDrawer`, after the `openers.forEach(... "false")` line and **before**
the `state.opener.focus(...)` call at the end — focus must not land on a hidden
element:

```js
    if (bubble) { bubble.classList.remove("hidden"); }
```

- [ ] **Step 8: Open on the `#chat` fragment**

At the end of the IIFE in `static/js/assessment_chat.js`, after
`updateCounter();`:

```js
  // `#chat` is the list page's way in (2026-09-28): the card's Chat button
  // links to the detail page with this fragment and the drawer opens itself on
  // arrival. `openDrawer` is idempotent (`if (state.open) return`), so a
  // fragment plus a click cannot double-open or double-load history. The
  // fragment is deliberately never rewritten on open or close — it is a
  // permalink to "open with the chat up", and mutating history would make Back
  // ambiguous.
  function openFromHash() {
    if (window.location.hash === "#chat") {
      openDrawer(bubble);
    }
  }
  window.addEventListener("hashchange", openFromHash);
  openFromHash();
```

- [ ] **Step 9: Pin that `#chat` is inert when chat is unavailable (Review Focus 4)**

Add to `tests/integration/test_assessment_chat_templates.py`:

```python
async def test_the_fragment_is_inert_when_chat_is_unavailable(asgi_app, client, db_session):
    """The script that reads `#chat` ships with the drawer, and the drawer is
    not rendered when the chat is off — so the fragment is inert rather than
    broken. Nothing must reference the drawer or load the script."""
    asgi_app.state.assessment_chat_enabled = False
    _, _, body = await _page(client, db_session, USER_ROLE_ADMIN, "admin")
    assert "data-chat-bubble" not in body
    assert "assessment_chat.js" not in body
    assert "window.ASSESSMENT_CHAT" not in body
```

- [ ] **Step 10: Run the chat template suite**

Run: `pytest tests/integration/test_assessment_chat_templates.py tests/integration/test_assessment_detail_page.py -v`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add templates/admin/_assessment_chat_drawer.html \
        templates/admin/_assessment_detail_body.html \
        static/js/assessment_chat.js \
        tests/integration/test_assessment_chat_templates.py
git commit -m "feat(chat): floating bubble replaces the nav opener; #chat opens the drawer"
```

---

## Task 2: Chat button on the assessments list

**Files:**
- Modify: `templates/admin/assessments.html:207` (add a macro beside `assessment_link`)
- Modify: `templates/manager/assessments.html:192` (the twin macro)
- Modify: `templates/admin/_assessments_body.html` (`card_chat_available`; both call sites, `:380` and `:408`)
- Test: `tests/integration/test_assessment_queue_controls.py`

**Interfaces:**
- Consumes: the `#chat` fragment convention from Task 1.
- Produces: nothing later tasks use.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_assessment_queue_controls.py`:

```python
@pytest.mark.parametrize("surface,role_fixture", [("admin", "admin"), ("manager", "manager")])
async def test_each_card_offers_a_chat_button_into_the_open_drawer(
    client, db_session, request, surface, role_fixture
):
    user = request.getfixturevalue(role_fixture)
    run, assessment = await _seed_narrative_row(
        db_session, project="Chat Button Co",
        elevator_pitch="One minute of prose about the idea.",
        key_points=["a point the reviewer can read"],
    )
    html = (await client.get(
        f"/{surface}/assessments?run_id={run.id}", headers=auth_headers(user.id)
    )).text
    card = _row_slice(html, "Chat Button Co")
    assert card.count("assessment-chat-link") == 1
    assert f"/{surface}/assessments/{assessment.id}#chat" in card
    # The detail button is untouched: still exactly one.
    assert card.count("assessment-open-link") == 1


async def test_the_chat_button_is_absent_while_impersonating(client, db_session, admin, manager):
    run, _ = await _seed_narrative_row(db_session, project="No Chat Co")
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={manager.id}"
    html = (await client.get(
        f"/manager/assessments?run_id={run.id}", headers=headers
    )).text
    card = _row_slice(html, "No Chat Co")
    assert "assessment-chat-link" not in card
    assert card.count("assessment-open-link") == 1


async def test_the_chat_button_is_absent_when_the_chat_is_disabled(
    asgi_app, client, db_session, admin
):
    asgi_app.state.assessment_chat_enabled = False
    run, _ = await _seed_narrative_row(db_session, project="Disabled Chat Co")
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "assessment-chat-link" not in _row_slice(html, "Disabled Chat Co")
```

If `_seed_narrative_row` does not already return the assessment, read its
current signature at the top of that file and adapt the unpacking; do not
change the helper.

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/integration/test_assessment_queue_controls.py -v -k chat`
Expected: FAIL — `assessment-chat-link` is not rendered.

- [ ] **Step 3: Add the admin macro**

In `templates/admin/assessments.html`, immediately after the `assessment_link`
macro at `:207`:

```jinja
{# The card's second way through: the detail page with the chat drawer already
   open (2026-09-28). Literal path for the same reason assessment_link is one —
   test_reachability's _link_credits only accepts a Jinja expression in a
   {path_param} slot. `_normalize_link` strips the `#chat` fragment, so this
   credits the existing detail route and adds none. A DIFFERENT class from
   assessment-open-link: two tests assert exactly one of those per card. #}
{% macro assessment_chat_link(a) %}<a class="assessment-chat-link inline-flex items-center gap-1 rounded border border-indigo-300 px-2 py-1 text-sm font-medium text-indigo-700 hover:bg-indigo-50 whitespace-nowrap" href="/admin/assessments/{{ a.id }}#chat" title="Open this assessment and ask about it">chat</a>{% endmacro %}
```

- [ ] **Step 4: Add the manager macro**

In `templates/manager/assessments.html`, after `:192`, the same macro with
`href="/manager/assessments/{{ a.id }}#chat"` and a comment noting it points at
`/manager`, never `/admin`, for the same reason its `assessment_link` twin
does (`test_manager_assessments_never_links_into_admin`).

- [ ] **Step 5: Compute availability once in the shared body**

In `templates/admin/_assessments_body.html`, immediately after the header
comment block (before the gating legend at `:43`):

```jinja
{# Whether this session may open the assessment chat, computed the same way
   admin/_assessment_detail_body.html computes it: the app-state flag set by
   create_app from `settings.assessment_chat_enabled`, and no impersonation
   (every chat route 403s an impersonated session, so a live-looking control
   would be a dead one). Deliberately NOT a new context key — both list
   handlers render through their router's `_template_context`, which already
   supplies `request` and `impersonation_banner`. #}
{% set card_chat_available = request is defined and request.app is defined
     and request.app.state.assessment_chat_enabled is sameas true
     and not impersonation_banner %}
```

- [ ] **Step 6: Render it at both call sites**

Replace the contents of the two `assessment-card-open` divs (`:380` and
`:408`) so each reads:

```jinja
<div class="assessment-card-open mt-3 flex flex-wrap items-center gap-2">{{ assessment_link(a) }}{% if card_chat_available %}{{ assessment_chat_link(a) }}{% endif %}</div>
```

- [ ] **Step 7: Run the queue tests and the reachability gate**

Run: `pytest tests/integration/test_assessment_queue_controls.py tests/unit/test_reachability.py tests/integration/test_manager_views.py tests/integration/test_assessment_list_chrome.py -v`
Expected: PASS — including the two `assessment-open-link == 1` assertions and
`test_manager_assessments_never_links_into_admin`.

- [ ] **Step 8: Commit**

```bash
git add templates/admin/assessments.html templates/manager/assessments.html \
        templates/admin/_assessments_body.html \
        tests/integration/test_assessment_queue_controls.py
git commit -m "feat(assessments): chat button on each card, opening the drawer on arrival"
```

---

## Task 3: Elevator pitch bound → 250 words

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md:339-372` (item 8)
- Modify: `src/agent/simulation.py:9256-9262` (the constant) and `:4536-4542` (its use)
- Modify: `tests/unit/test_pitch_contract.py:37-38`
- Test: `tests/integration/test_assessment_narrative_fields.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_PITCH_WORD_LIMIT` (int, 250) in `src/agent/simulation.py`.

- [ ] **Step 1: Write the failing prompt-contract test**

Replace `test_the_pitch_sentence_count_is_four_to_six` in
`tests/unit/test_pitch_contract.py:37-38` with:

```python
def test_the_pitch_is_bounded_at_250_words():
    """Raised from "four to six sentences … at most 900 characters" on
    2026-09-28. The bound is mirrored by `_PITCH_WORD_LIMIT` on the write path,
    so the prose and the drift alarm cannot part company."""
    from src.agent.simulation import _PITCH_WORD_LIMIT

    item = _flat()
    assert "at most 250 words" in item
    assert _PITCH_WORD_LIMIT == 250
    assert "four to six sentences" not in item
```

- [ ] **Step 2: Write the failing write-path tests (Review Focus 5)**

Add to `tests/integration/test_assessment_narrative_fields.py`, following the
`_persist_assessment` pattern already used there (seed a run, build a stub
`SimulationEngine`, call `_persist_assessment` under `caplog`, assert on the
warning text, then `_delete_run`):

```python
@pytest.mark.parametrize("words,warns", [(250, False), (251, True)])
async def test_the_pitch_word_bound_warns_only_past_250(
    db_session_factory, caplog, words, warns
):
    """Boundary, both sides. 250 is the contract, so it must not warn; 251 must.
    A pitch is never dropped for length — the archive keeps what the hub
    wrote."""
    ...  # seed as the neighbouring tests do
    pitch = " ".join(["word"] * words)
    ...  # await SimulationEngine._persist_assessment(stub, "blackbird", "general", {...,
         #     "elevator_pitch": pitch, "recommendation": "pass", "scores": {}})
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert (f"elevator_pitch is {words} words" in warnings) is warns
    assert row.elevator_pitch == pitch          # stored either way


async def test_a_blank_pitch_neither_warns_nor_raises(db_session_factory, caplog):
    """`"   ".split()` is `[]`, so the count is 0 — the alarm must not fire and
    `_str_or_none` stores None."""
    ...  # same shape, "elevator_pitch": "   "
    assert "elevator_pitch is" not in warnings
```

- [ ] **Step 3: Run both to verify they fail**

Run: `pytest tests/unit/test_pitch_contract.py tests/integration/test_assessment_narrative_fields.py -v -k "250 or pitch_word or blank_pitch"`
Expected: FAIL — `_PITCH_WORD_LIMIT` does not exist.

- [ ] **Step 4: Replace the constant**

In `src/agent/simulation.py`, replace the `_PITCH_SOFT_LIMIT` block at
`:9256-9262` with:

```python
#: The pitch's own soft bound, raised from 900 CHARACTERS to 250 WORDS on
#: 2026-09-28 at the operator's request. Measured before the change: the five
#: verdicts written under scout_hub 1.8.0 carried 113-222-word pitches
#: (797-961 characters), so 900 characters was in practice a ~150-word bound.
#: The public excerpt did NOT move with it: PITCH_DISPLAY_CHARS (600,
#: src/services/assessment_headline.py) still clips what reaches
#: #assessments-summary, and item 8 still asks that sentences 1-4 END within
#: ~550 characters so the provenance citation completes inside that window.
#: A longer pitch makes that harder, not easier, which is why the
#: citation-loss alarm below is now the load-bearing check rather than this one.
_PITCH_WORD_LIMIT = 250
```

- [ ] **Step 5: Replace its use**

In `src/agent/simulation.py:4536-4542`:

```python
        if isinstance(verdict.get("elevator_pitch"), str) and len(
            verdict["elevator_pitch"].split()
        ) > _PITCH_WORD_LIMIT:
            logger.warning(
                "[%s] Assessment elevator_pitch is %d words (contract asks for <=%d)",
                agent_id, len(verdict["elevator_pitch"].split()), _PITCH_WORD_LIMIT,
            )
```

Leave the citation-loss alarm at `:4543-4572` exactly as it is.

- [ ] **Step 6: Rewrite item 8 in the prompt**

In `prompts/roles/scout_hub/phase4-thread-reply.md`, item 8:

- the opening becomes: "**Elevator pitch.** Plain language, for a
  scientifically literate reader who is not a specialist in this field, and
  **at most 250 words** — the first 600 characters are posted publicly to
  Blackbird's summary channel and the rest is app-only, so the opening
  sentences have to stand alone."
- **delete** "Where the two bounds conflict, cut a sentence rather than run
  over." (`:343-344`).
- keep the six numbered elements and their order verbatim.
- keep "Sentences 1-4 together must **end within approximately 550
  characters**…" verbatim.
- **replace** "If something has to go, cut from 5, which survives in the
  app-only tail. At four sentences, elements 4 and 6 are the two that must
  survive: merge 1 with 3 and 2 with 5 before dropping either." (`:366-368`)
  with: "At 250 words nothing has to be dropped: spend the extra room on
  elements 5 and 6, which are app-only, and keep elements 1-4 complete inside
  the first ~550 characters. Element 4, the citation, and element 6, the
  read-out that matters, are the two that must survive any cut."
- keep the jargon sentence and extend it: "Minimal jargon; spell out an
  abbreviation the first time, and apply the plain-language rule under item 7
  to this field too."

- [ ] **Step 7: Sync the prompt docs**

Run: `.venv-test/bin/python scripts/sync_prompt_set_docs.py`
Then: `pytest tests/unit/test_doc_prompt_sync.py -v`
Expected: PASS.

- [ ] **Step 8: Run the pitch tests**

Run: `pytest tests/unit/test_pitch_contract.py tests/unit/test_rubric_prompt_sync.py tests/integration/test_assessment_narrative_fields.py tests/unit/test_assessments_summary_post.py tests/unit/test_assessment_headline_render.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add prompts/roles/scout_hub/phase4-thread-reply.md src/agent/simulation.py \
        tests/unit/test_pitch_contract.py tests/integration/test_assessment_narrative_fields.py \
        docs/specs/2026-08-07-hub-bot-prompts.md
git commit -m "feat(hub): elevator pitch bound becomes 250 words; Slack excerpt unchanged"
```

---

## Task 4: Migration `0052`, the column, and the normalizer

**Files:**
- Create: `alembic/versions/0052_assessment_dimension_rationales.py`
- Modify: `src/models/opportunity.py:96` (after `score_rationale`)
- Modify: `src/services/assessment_detail.py` (after `normalize_bullets`, `:250`)
- Modify: `scripts/migrate/preflight.py:78`, `:138-145`, `:205`, `:451`, `:456-460`, and `PLANNED_OBJECTS`
- Modify: `tests/unit/test_migration_checks.py:274-281`
- Test: `tests/unit/test_dimension_rationales.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `OpportunityAssessment.dimension_rationales: Mapped[dict | None]`
  - `normalize_dimension_rationales(value: object) -> dict[str, str] | None`
    in `src/services/assessment_detail.py`

- [ ] **Step 1: Write the failing normalizer tests**

Create `tests/unit/test_dimension_rationales.py`:

```python
"""`normalize_dimension_rationales` — the write-time shape check for sidecar
item 2's companion field (migration 0052)."""
from src.services.assessment_detail import normalize_dimension_rationales


def test_a_well_formed_map_is_kept_and_stripped():
    assert normalize_dimension_rationales(
        {"scientific_credibility": "  Genetics and rescue data are believable.  "}
    ) == {"scientific_credibility": "Genetics and rescue data are believable."}


def test_keys_are_lowercased_and_stripped_like_the_scores_map():
    """`build_assessment_detail` normalizes `scores` keys with
    `.strip().lower()`; a rationale keyed `Scientific_Credibility` would
    otherwise score fine and silently render nothing."""
    assert normalize_dimension_rationales(
        {" Scientific_Credibility ": "why"}
    ) == {"scientific_credibility": "why"}


def test_blank_values_and_non_strings_reject_the_whole_field():
    for bad in ({"a": ""}, {"a": "   "}, {"a": None}, {"a": ["x"]}, {"a": 3}):
        assert normalize_dimension_rationales(bad) is None


def test_non_dicts_and_empties_are_none():
    for bad in (None, [], {}, "text", 3, ["a"]):
        assert normalize_dimension_rationales(bad) is None


def test_an_oversized_map_or_key_is_rejected():
    assert normalize_dimension_rationales({str(i): "why" for i in range(21)}) is None
    assert normalize_dimension_rationales({"k" * 51: "why"}) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/test_dimension_rationales.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement the normalizer**

In `src/services/assessment_detail.py`, after `normalize_bullets` (`:250`):

```python
#: Bounds on the sidecar's `dimension_rationales`. A rubric has six dimensions;
#: 20 leaves room for a future revision without letting an unbounded object
#: into a JSONB column, and 50 characters is longer than any dimension key this
#: repo has ever used (`differentiation_unmet_need` is 26).
_MAX_DIMENSION_RATIONALES = 20
_MAX_DIMENSION_KEY_CHARS = 50


def normalize_dimension_rationales(value: object) -> dict[str, str] | None:
    """The hub's one-sentence reason per dimension (sidecar item 2, migration
    0052).

    Accepts a non-empty dict of `str -> non-blank str`, at most
    `_MAX_DIMENSION_RATIONALES` entries. Keys are `.strip().lower()`-normalized
    the same way `build_assessment_detail` normalizes the `scores` map, so a
    sidecar emitting `"Scientific_Credibility"` still matches the dimension it
    is about — without this the rationale stores fine and renders nowhere,
    which is indistinguishable from the hub not writing one.

    Keys are deliberately NOT validated against the live rubric's dimension
    keys: a row is rendered against the revision that SCORED it, and a later
    revision renaming a dimension must not make an older row's reasons
    unstorable. An unknown key simply matches no rendered dimension.

    Anything else is None: a malformed narrative field never costs the verdict
    (A20), and `raw_verdict` keeps the original either way.
    """
    if not isinstance(value, dict) or not value:
        return None
    if len(value) > _MAX_DIMENSION_RATIONALES:
        return None
    out: dict[str, str] = {}
    for key, text in value.items():
        if not isinstance(key, str) or not isinstance(text, str):
            return None
        slug = key.strip().lower()
        if not slug or len(slug) > _MAX_DIMENSION_KEY_CHARS or not text.strip():
            return None
        out[slug] = text.strip()
    return out or None
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/unit/test_dimension_rationales.py -v`
Expected: PASS.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0052_assessment_dimension_rationales.py`:

```python
"""opportunity_assessments.dimension_rationales

One additive nullable JSONB column: the hub's one-sentence reason for each
dimension score (sidecar item 2 of scout_hub 1.9.0), so the Evidence summary can
show WHY a dimension scored what it did rather than only the number.

Purely additive — OLD CODE AGAINST THE NEW SCHEMA IS SAFE. The reverse is not:
the new code maps the column, so every `select(OpportunityAssessment)` raises
UndefinedColumn against a pre-0052 database (both assessment list pages, both
detail pages, src/services/review_bot.py on the worker, src/routers/reviews.py
and src/services/directory.py), and `_persist_assessment` names it in the
INSERT — a best-effort write, so every verdict of a running simulation would be
lost to one ERROR line while the Slack replies kept looking normal. Migrate
BEFORE the new code serves.

NULL on every pre-0052 row and deliberately never backfilled: those verdicts
were never asked for per-dimension reasons, and a generated one would be
indistinguishable from one the hub wrote. Both assessment surfaces render
nothing when it is NULL.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("dimension_rationales", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "dimension_rationales")
```

- [ ] **Step 6: Map the column**

In `src/models/opportunity.py`, immediately after `score_rationale` (`:96`):

```python
    # Sidecar item 2's companion (scout_hub 1.9.0, migration 0052): the hub's
    # own one-sentence reason for EACH dimension score, keyed by dimension key
    # (lower-cased by `normalize_dimension_rationales`, the same way
    # `build_assessment_detail` normalizes the `scores` map). NULL means the row
    # predates 0052 or the hub emitted a malformed value; `raw_verdict` keeps
    # what was emitted either way.
    #
    # Unlike `strengths`/`risks`/`competitive_landscape`/`evidence_maturity`,
    # this is NOT staff-only: it renders for the reviewer tier too, like
    # `score_rationale`, because the Evidence summary is the reviewer's surface
    # and six scores with the reasons blanked out would defeat the field
    # (operator decision 2026-09-28). The prompt tells the model so; do not add
    # it to STAFF_ONLY_VERDICT_FIELDS.
    dimension_rationales: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
```

- [ ] **Step 7: Update the preflight registry**

In `scripts/migrate/preflight.py`:
- `:78` — `DEFAULT_TARGET = "0052"`
- `:141-145` — append `"0051"` to `SUPPORTED_START_REVISIONS`, and extend the
  comment above it in the file's existing style: production is stamped 0051, so
  with `DEFAULT_TARGET` at 0052 it is the starting point and must be a
  supported start, not a BLOCK.
- `PLANNED_OBJECTS` — after the `0051` entries:
  ```python
      # 0052_assessment_dimension_rationales
      PlannedObject("0052", "column", "dimension_rationales", "opportunity_assessments"),
  ```
- `REVISION_ORDER` (`:456-460`) — append `"0052"`.
- Re-date the revision-keyed prose at `:140`, `:205` and `:451`
  ("0036-0051" → "0036-0052", "0019-0051" → "0019-0052").

- [ ] **Step 8: Update the pinned migration test**

In `tests/unit/test_migration_checks.py:274-281`, append `"0051"` to the
`SUPPORTED_START_REVISIONS` tuple and change the last line to
`assert pf.DEFAULT_TARGET == "0052"`.

- [ ] **Step 9: Run the migration gates**

Run: `pytest tests/unit/test_migration_checks.py tests/unit/test_json_none_as_null.py -v`
Then the alembic sanity + round trip, which is what `scripts/ci.sh` steps 1-2
do:
Run: `.venv-test/bin/python -m alembic heads` — expected: a single head, `0052`.
Expected: PASS / one head.

- [ ] **Step 10: Commit**

```bash
git add alembic/versions/0052_assessment_dimension_rationales.py \
        src/models/opportunity.py src/services/assessment_detail.py \
        scripts/migrate/preflight.py tests/unit/test_migration_checks.py \
        tests/unit/test_dimension_rationales.py
git commit -m "feat(0052): add opportunity_assessments.dimension_rationales"
```

---

## Task 5: The hub emits and the engine stores `dimension_rationales`

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md:194-200` (item 2) and the skeleton at `:483-511`
- Modify: `src/agent/simulation.py` (import, warnings near `:4536`, `assessment_kwargs` near `:4729`, constants near `:9298`)
- Modify: `tests/unit/test_rubric_prompt_sync.py`
- Test: `tests/integration/test_assessment_narrative_fields.py`

**Interfaces:**
- Consumes: `normalize_dimension_rationales` and the column from Task 4.
- Produces: `dimension_rationales` populated on new rows; the constant
  `_DIMENSION_RATIONALE_CHARS = 200`.

- [ ] **Step 1: Write the failing prompt-sync tests**

Add to `tests/unit/test_rubric_prompt_sync.py`:

```python
def test_the_skeleton_carries_a_rationale_for_every_scored_dimension():
    """Sidecar item 2's companion (0052). The two maps must name the SAME
    dimensions: a key in one and not the other stores a rationale that matches
    no dimension (renders nothing) or a score with no reason. `scores` is
    already pinned against the live rubric's weights above, so pinning
    `dimension_rationales` against `scores` transitively pins it to the rubric."""
    skeleton = _skeleton()
    assert set(skeleton["dimension_rationales"]) == set(skeleton["scores"])
    assert all(v == "" for v in skeleton["dimension_rationales"].values())


def test_item_two_bounds_the_rationale_and_names_its_audience():
    """D5: this field is NOT staff-only — a reviewer reads it — so the prompt
    must say so and must bind it to the no-unpublished-disclosure rule instead
    of the staff-only promise the 0049/0050 bullets carry."""
    text = _phase4_text()
    item = text[text.index("2. **The six dimension scores.**"):text.index("3. **Red flags.**")]
    body = _norm(item)
    assert "dimension_rationales" in body
    assert "at most 200 characters" in body
    assert "never posted to Slack" in body
    assert "reviewers" in body
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/test_rubric_prompt_sync.py -v -k "rationale or item_two"`
Expected: FAIL — the skeleton has no `dimension_rationales`.

- [ ] **Step 3: Extend item 2 in the prompt**

Append to item 2 of `prompts/roles/scout_hub/phase4-thread-reply.md`:

> For each of the six, also record **one sentence of at most 200 characters**
> in `dimension_rationales`, under the same key, saying what drove THAT score:
> the evidence that set it, and what would move it. Never state a number for
> the weighted score or the band — those are computed server-side. Like
> `score_rationale`, this field is **never posted to Slack**, so it may reason
> about the score freely. Unlike the staff-only fields below, it **is read by
> Blackbird reviewers as well as staff**, so the confidentiality rule above
> applies to it exactly as it applies to the elevator pitch: do not restate a
> PI's unpublished result, unfiled construct, undisclosed compound or
> volunteered limitation in it.

- [ ] **Step 4: Extend the skeleton**

In the `<assessment_json>` block, immediately after the `"scores"` object:

```json
  "dimension_rationales": {
    "differentiation_unmet_need": "", "scientific_credibility": "",
    "translational_path": "", "fundable_experiment": "",
    "venture_potential": "", "team_executability": ""
  },
```

- [ ] **Step 5: Write the failing persistence test**

Add to `tests/integration/test_assessment_narrative_fields.py`, in the style of
the neighbouring `_persist_assessment` tests:

```python
async def test_dimension_rationales_are_stored_and_overlong_ones_warned(
    db_session_factory, caplog
):
    """Warnings, never drops (A4): an over-long rationale is still the
    archive's copy of what the hub wrote."""
    rationales = {
        "scientific_credibility": "Rescue data in two models, published.",
        "venture_potential": "x" * 201,
    }
    ...  # persist with "scores": {"scientific_credibility": 4, "venture_potential": 2}
    assert row.dimension_rationales == rationales
    assert "dimension_rationales.venture_potential is 201 chars" in warnings


async def test_a_malformed_rationale_map_is_dropped_to_null_and_named(
    db_session_factory, caplog
):
    ...  # persist with "dimension_rationales": {"a": 3}
    assert row.dimension_rationales is None
    assert "dimension_rationales was DROPPED" in warnings
    assert row.raw_verdict["dimension_rationales"] == {"a": 3}


async def test_a_scored_dimension_with_no_rationale_is_warned(db_session_factory, caplog):
    ...  # scores name two dimensions, dimension_rationales names one
    assert "scored dimension(s) with no rationale: venture_potential" in warnings
```

- [ ] **Step 6: Run to verify they fail**

Run: `pytest tests/integration/test_assessment_narrative_fields.py -v -k rationale`
Expected: FAIL.

- [ ] **Step 7: Implement the engine side**

In `src/agent/simulation.py`:

Add to the `src.services.assessment_detail` import block (near `:66-70`):
`normalize_dimension_rationales,`

Add beside `_HUB_BULLET_CHARS` (`:9298`):

```python
#: Sidecar item 2's companion (scout_hub >= 1.9.0, migration 0052): one
#: sentence per dimension. Warnings only, like every other shape check here.
_DIMENSION_RATIONALE_CHARS = 200
```

After the `strengths`/`risks`/`competitive_landscape`/`evidence_maturity` loop
(`:4660-4690`):

```python
        # Sidecar item 2's companion (0052). Three warnings, no drops beyond
        # what the normalizer already refuses: an over-long sentence, a
        # malformed map, and a dimension that was SCORED but not explained —
        # the last is the one a reader of the Evidence summary actually feels,
        # because the row renders a score with no reason beside it.
        _raw_rationales = verdict.get("dimension_rationales")
        _rationales = normalize_dimension_rationales(_raw_rationales)
        if _raw_rationales is not None and _rationales is None:
            logger.warning(
                "[%s] Assessment dimension_rationales was DROPPED (stored NULL; "
                "the value survives only in raw_verdict): not a non-empty map of "
                "dimension key to non-blank sentence",
                agent_id,
            )
        for _key, _text in (_rationales or {}).items():
            if len(_text) > _DIMENSION_RATIONALE_CHARS:
                logger.warning(
                    "[%s] Assessment dimension_rationales.%s is %d chars "
                    "(contract asks for <=%d)",
                    agent_id, _key, len(_text), _DIMENSION_RATIONALE_CHARS,
                )
        _unexplained = sorted(
            k for k, v in (scores or {}).items()
            if isinstance(k, str) and v is not None
            and k.strip().lower() not in (_rationales or {})
        )
        if _unexplained:
            logger.warning(
                "[%s] Assessment has %d scored dimension(s) with no rationale: %s",
                agent_id, len(_unexplained), ", ".join(_unexplained),
            )
```

In `assessment_kwargs`, immediately after `score_rationale=…` (`:4729`):

```python
            # Sidecar item 2's companion (0052): the per-dimension reasons.
            # Degrades to None on a wrong shape like its narrative siblings;
            # raw_verdict keeps the original either way.
            dimension_rationales=_rationales,
```

- [ ] **Step 8: Run the tests**

Run: `pytest tests/unit/test_rubric_prompt_sync.py tests/integration/test_assessment_narrative_fields.py tests/integration/test_opportunity_assessment_persistence.py -v`
Expected: PASS.

- [ ] **Step 9: Sync the prompt docs and commit**

```bash
.venv-test/bin/python scripts/sync_prompt_set_docs.py
git add prompts/roles/scout_hub/phase4-thread-reply.md src/agent/simulation.py \
        tests/unit/test_rubric_prompt_sync.py \
        tests/integration/test_assessment_narrative_fields.py \
        docs/specs/2026-08-07-hub-bot-prompts.md
git commit -m "feat(hub): sidecar carries a one-sentence rationale per dimension"
```

---

## Task 6: Evidence summary shows every dimension with its reason

**Files:**
- Modify: `src/services/assessment_detail.py:1211-1241` (`dimensions`), `:806-1082` (`derive_strengths_and_risks`)
- Modify: `templates/admin/_assessment_detail_body.html:249-294` (`signal_row`), `:295-417` (the card), `:804-834` (`#scores`)
- Modify: `tests/unit/test_assessment_strength_risk_derivation.py:117-120`, `:134`
- Modify: `tests/integration/test_assessment_detail_page.py:1948-1965`, `:2426-2428`, `:2448-2450`

**Interfaces:**
- Consumes: the column and normalizer from Task 4.
- Produces:
  - `build_assessment_detail(...)["dimensions"][i]["rationale"]: str | None`
  - `derive_strengths_and_risks(...)["mid_scale"]: list[dict]`
  - every entry in every bucket gains `"rationale": str | None`

- [ ] **Step 1: Update the two pinned contract assertions**

In `tests/unit/test_assessment_strength_risk_derivation.py`:

```python
    assert set(result) == {
        "strengths", "risks", "unestablished", "mid_scale", "scale_known",
        "thresholds", "mid_scale_count", "scored_dimension_count",
    }
```

and

```python
        assert set(entry) == {
            "source", "label", "detail", "body", "preview", "note", "rationale",
        }
```

Extend the second test's `entries` to include `result["mid_scale"]`, and seed a
mid-scale dimension so that bucket is non-empty.

- [ ] **Step 2: Write the failing derivation tests**

Add to the same file:

```python
def test_a_mid_scale_dimension_lands_in_its_own_bucket_not_in_neither():
    """A 3 of 5 is a real, neutral answer. It was counted and then dropped;
    now it is listed, so all six dimensions appear on the page."""
    result = _derive(dimensions=[_dimension("translational_path", 3)])
    assert result["strengths"] == result["risks"] == []
    assert [e["label"] for e in result["mid_scale"]] == ["translational_path"]
    assert result["mid_scale_count"] == 1


def test_every_dimension_entry_carries_its_stored_rationale():
    a = _assessment(dimension_rationales={"translational_path": "No route named."})
    result = _derive(a, dimensions=[_dimension("translational_path", 3)])
    assert result["mid_scale"][0]["rationale"] == "No route named."


def test_non_dimension_entries_carry_a_null_rationale():
    result = _derive(_assessment(red_flags=["No IP position"]))
    assert all(e["rationale"] is None for e in result["risks"])
```

- [ ] **Step 3: Write the mismatched-key test (Review Focus 1)**

```python
def test_a_rationale_key_matching_no_dimension_is_rendered_nowhere():
    """A typo, or a dimension renamed by a later revision. It must attach to
    nothing and raise nothing — never to the wrong dimension."""
    a = _assessment(dimension_rationales={"not_a_dimension": "orphan"})
    result = _derive(a, dimensions=[_dimension("translational_path", 3)])
    assert result["mid_scale"][0]["rationale"] is None
    assert all(
        e["rationale"] != "orphan"
        for bucket in ("strengths", "risks", "unestablished", "mid_scale")
        for e in result[bucket]
    )
```

- [ ] **Step 4: Run to verify they fail**

Run: `pytest tests/unit/test_assessment_strength_risk_derivation.py -v`
Expected: FAIL — no `mid_scale` key.

- [ ] **Step 5: Thread the rationale into `dimensions`**

In `src/services/assessment_detail.py`, before the `dimensions = []` line
(`:1211`):

```python
    # The hub's per-dimension reasons (0052), keyed the SAME way the scores map
    # is normalized above — `normalize_dimension_rationales` lower-cases on
    # write, and this lookup must match it or a stored reason renders nowhere.
    stored_rationales = assessment.dimension_rationales
    rationales = (
        {k.strip().lower(): v for k, v in stored_rationales.items() if isinstance(k, str)}
        if isinstance(stored_rationales, dict)
        else {}
    )
```

Add `"rationale": rationales.get(dim.key),` to the first loop's dict and
`"rationale": rationales.get(key),` to the fallback loop's dict.

- [ ] **Step 6: Add the bucket and the rationale to the derivation**

In `derive_strengths_and_risks`:

- add `mid_scale: list[dict[str, Any]] = []` beside the other buckets;
- give `_add` a `rationale: str | None = None` parameter and put
  `"rationale": rationale` in the dict it appends;
- build the same `rationales` map from `assessment.dimension_rationales` (the
  function takes the assessment, so read it there rather than plumbing it in);
- pass `rationale=rationales.get(dim.get("key"))` on the three dimension
  `_add` calls (strength, risk, not-scored);
- replace the bare `mid_scale_count += 1` at `:994` with:
  ```python
                mid_scale_count += 1
                _add(mid_scale, "dimension", label, detail, body=weight_body,
                     rationale=rationales.get(dim.get("key")))
  ```
- return `"mid_scale": mid_scale,` in the result dict;
- update the docstring's bucket table: add the row "dimension score between →
  `mid_scale`: a real, neutral answer, listed but bucketed as neither", and
  document `rationale` in the entry-shape paragraph (always present, `None` for
  every non-dimension source and for a dimension with no stored reason).

- [ ] **Step 7: Run the derivation tests**

Run: `pytest tests/unit/test_assessment_strength_risk_derivation.py -v`
Expected: PASS.

- [ ] **Step 8: Teach `signal_row` the new kind and the rationale**

In `templates/admin/_assessment_detail_body.html:250`, extend BOTH maps — a
missing key yields `Undefined` and `glyph[1]` then raises, 500ing the page:

```jinja
{% set glyph = {'strengths': ('&#10003;', 'text-green-700', 'Strength'), 'risks': ('&#10007;', 'text-red-700', 'Risk'), 'mid_scale': ('&ndash;', 'text-slate-700', 'Mid-scale'), 'unestablished': ('?', 'text-slate-700', 'Not established')}[kind] %}
```

and at `:252`:

```jinja
<li class="signal-entry signal-{{ {'strengths': 'strength', 'risks': 'risk', 'mid_scale': 'midscale', 'unestablished': 'unestablished'}[kind] }} signal-source-{{ item.source }} …"
```

In the non-collapsible branch (`:286-291`), after the label/detail span, and in
the collapsible branch's `<summary>` as a `basis-full` line:

```jinja
{% if item.rationale %}<span class="signal-rationale basis-full ml-6 text-sm text-gray-600">{{ item.rationale }}</span>{% endif %}
```

- [ ] **Step 9: Add the Mid-scale section and reword the count line**

After the Risks `</section>` (`:367`) and before the landscape section:

```jinja
        {# Mid-scale (2026-09-28). A dimension between the two thresholds is a
           real, neutral answer, and until now it appeared in NO column — so a
           reader could not see all six dimensions and their reasons in one
           place. Class `assessment-signals-neutral`, deliberately not
           `…-midscale`: `signals-midscale` is already the count paragraph's
           class above, and one class name being a substring of another makes
           both the tests and future greps ambiguous. #}
        {% if vs.get('mid_scale') %}
        <section class="assessment-signals-neutral rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
            <div class="text-sm font-semibold text-slate-900">Mid-scale</div>
            <p class="text-sm text-gray-600">Neither a strength nor a risk at this revision's thresholds.</p>
            <ul class="signal-derived mt-1 text-base leading-relaxed text-gray-700">
                {% for item in vs['mid_scale'] %}{{ signal_row(item, 'mid_scale') }}{% endfor %}
            </ul>
        </section>
        {% endif %}
```

Reword the four variants of the count paragraph (`:300-309`) so none of them
claims the dimensions are unlisted when they now are: replace each trailing
"so neither column lists it/them" with "listed under Mid-scale below" (keep the
counts and the threshold numbers exactly as they are).

- [ ] **Step 10: Render the rationale in the `#scores` disclosure**

In the dimension loop at `:808-825`, after the closing `</span>` of the weight
column, add a full-width line:

```jinja
            {% if d.rationale %}<p class="score-rationale-line w-full text-sm text-gray-600 sm:pl-[15.5rem]">{{ d.rationale }}</p>{% endif %}
```

This is not decoration: it is what makes the chat record's `scores` anchor land
on the sentence it quotes, and what keeps the rationale on the page for a row
whose rubric revision is unknown (the derivation contributes no dimension
entries at all for those).

- [ ] **Step 11: Update the three test anchors**

- `tests/integration/test_assessment_detail_page.py:1948-1965`: add
  `"assessment-signals-neutral"` to the `risks_end` tuple, and extend the
  helper's docstring with the same reasoning it already gives for
  landscape/maturity.
- `:2426-2428` and `:2448-2450`: update the expected paragraph text to the
  new wording.

- [ ] **Step 12: Write the page tests**

```python
async def test_a_midscale_dimension_is_listed_with_its_reason(client, db_session, admin):
    scores = {"translational_path": 3, "scientific_credibility": 4}
    assessment = await _seed_live_stamped(
        db_session, scores=scores,
        dimension_rationales={"translational_path": "MIDSCALE-REASON"},
    )
    card = _signals_card(_main((await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text))
    assert "assessment-signals-neutral" in card
    assert "MIDSCALE-REASON" in card


async def test_a_reviewer_sees_the_dimension_reasons(client, db_session):
    """D5: unlike the hub's strengths/risks bullets, these are NOT staff-only."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_live_stamped(
        db_session, scores={"translational_path": 3},
        dimension_rationales={"translational_path": "REVIEWER-VISIBLE-REASON"},
    )
    body = _main((await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
    )).text)
    assert "REVIEWER-VISIBLE-REASON" in body
```

Extend `_seed_live_stamped` with a `dimension_rationales=None` keyword.

- [ ] **Step 13: Run the detail-page suite**

Run: `pytest tests/integration/test_assessment_detail_page.py tests/unit/test_assessment_strength_risk_derivation.py tests/unit/test_panel_state.py -v`
Expected: PASS.

- [ ] **Step 14: Commit**

```bash
git add src/services/assessment_detail.py templates/admin/_assessment_detail_body.html \
        tests/unit/test_assessment_strength_risk_derivation.py \
        tests/integration/test_assessment_detail_page.py
git commit -m "feat(assessments): every dimension shows its score reason; mid-scale gets a section"
```

---

## Task 7: The chat record quotes the dimension reasons

**Files:**
- Modify: `src/services/assessment_chat_record.py:491-515`
- Modify: `tests/assessment_chat_support.py:177-222`
- Test: `tests/unit/test_assessment_chat_record.py`, `tests/integration/test_assessment_chat_parity.py`

**Interfaces:**
- Consumes: `dimensions[].rationale` and the `mid_scale` bucket from Task 6.
- Produces: nothing later tasks use.

- [ ] **Step 1: Update the shared fixture**

In `tests/assessment_chat_support.py`, add `"rationale": "DIM-REASON-SCIENCE"`
to the first `dimensions` entry and `"rationale": None` to the second; add
`"rationale": None` to every entry in `verdict_signals`' three buckets; and add
`"mid_scale": []` beside `"unestablished"`.

- [ ] **Step 2: Write the failing record test**

Add to `tests/unit/test_assessment_chat_record.py`:

```python
def test_a_dimension_block_quotes_its_stored_reason():
    """The chat must be able to answer "why did this dimension score 4?" — the
    reason is on the page, so the record may carry it (parity is record ⊆ page)."""
    doc = _verdict_doc(synthetic_detail(), tier="staff")
    block = next(b for b in _blocks(doc) if "Scientific credibility" in b)
    assert "DIM-REASON-SCIENCE" in block
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/unit/test_assessment_chat_record.py -v -k reason`
Expected: FAIL.

- [ ] **Step 4: Quote the rationale**

In `src/services/assessment_chat_record.py`, inside the dimension loop
(`:498-513`), collect the quoted lines instead of passing a single value:

```python
        # The hub's own reason for THIS dimension (0052). Quotable because the
        # page renders it in two places — the Evidence summary row and the
        # `#scores` disclosure — and the anchor below points at the second, so
        # "Show in page" lands on the sentence being quoted. Without the
        # `#scores` rendering this would breach parity for any row whose rubric
        # revision is unknown: `derive_strengths_and_risks` contributes no
        # dimension entries at all for those, so the Evidence summary shows
        # nothing to contain the quote.
        lines = [] if score is None else [f"{score:g}"]
        if d.get("rationale"):
            lines.append(str(d["rationale"]))
        doc.add(label, lines, anchor="scores")
```

replacing the current `if score is None: … doc.add(label, anchor="scores") else: doc.add(label, [f"{score:g}"], …)`
pair — keep the `label +=` "not scored…" branch exactly as it is.

- [ ] **Step 5: Write the unknown-revision parity test (Review Focus 2)**

In `tests/integration/test_assessment_chat_parity.py`, add a case that stamps
the assessment with a rubric version absent from the registry, gives it
`dimension_rationales`, and asserts the existing containment check still passes
— i.e. the sentence the record quotes is somewhere in the page's `<main>`.
Follow the existing `_seed`/`_parse_page` helpers; the assertion is the same
`missing` loop the file already runs.

- [ ] **Step 6: Run the chat suites**

Run: `pytest tests/unit/test_assessment_chat_record.py tests/unit/test_assessment_chat_service.py tests/unit/test_assessment_chat_stream.py tests/integration/test_assessment_chat_parity.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/services/assessment_chat_record.py tests/assessment_chat_support.py \
        tests/unit/test_assessment_chat_record.py \
        tests/integration/test_assessment_chat_parity.py
git commit -m "feat(chat): the record carries each dimension's stored reason"
```

---

## Task 8: Key-point groups 1.9.0 — read path

**Files:**
- Modify: `src/services/assessment_detail.py:112-205`
- Modify: `tests/unit/test_key_point_sections.py:16-45`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `KEY_POINT_GROUPS` — the six 1.9.0 groups
  - `RETIRED_KEY_POINT_GROUPS: tuple[tuple[str, str], ...]`
  - `KEY_POINT_ACCEPTED_KEYS` — current ∪ retired ∪ legacy
  - `key_point_sections` render order (documented below), consumed by both
    assessment templates and `assessment_chat_record`

- [ ] **Step 1: Update the pinned tuples and add the new ones**

In `tests/unit/test_key_point_sections.py:16-32`:

```python
def test_the_current_retired_and_legacy_sets_are_pinned():
    assert KEY_POINT_GROUPS == (
        ("indication_audience", "Indication / Audience"),
        ("lab_background", "Lab Background"),
        ("proposal", "Proposal"),
        ("clinical_actionability", "Clinical Actionability"),
        ("path_to_clinic", "Path to Clinic / Commercialization"),
        ("commercial_opportunity", "Commercial Opportunity"),
    )
    # Retired by 1.9.0, still rendered for the 5 rows written under 1.8.0 —
    # under 1.8.0's label AND in 1.8.0's slot.
    assert RETIRED_KEY_POINT_GROUPS == (("key_questions", "Key Questions/Experiment"),)
    assert LEGACY_KEY_POINT_GROUPS == (
        ("significance", "Significance"),
        ("innovation", "Innovation"),
        ("clinical_actionability", "Clinical actionability"),
        ("key_questions", "Key questions / experiments"),
        ("commercial_potential", "Commercial potential"),
    )
    assert KEY_POINT_ACCEPTED_KEYS == (
        frozenset(CURRENT) | frozenset(RETIRED) | frozenset(LEGACY)
    )
```

with `RETIRED = [k for k, _ in RETIRED_KEY_POINT_GROUPS]` beside the existing
`CURRENT`/`LEGACY` lists, and fix the `# shared key only` comment at `:39`:
`key_questions` is now evidence of NEITHER shape, so
`key_point_shape({"key_questions": ["q"]}) == "current"` for a different
reason — say so.

- [ ] **Step 2: Write the failing rendering tests**

```python
def test_a_1_8_0_row_keeps_key_questions_label_and_slot():
    """D3: a stored row renders under the labels AND in the order it was
    written with. All five 1.8.0 production rows carry this group."""
    sections = key_point_sections({
        "indication_audience": ["i"], "lab_background": ["l"], "proposal": ["p"],
        "clinical_actionability": ["c"], "key_questions": ["q"],
        "commercial_opportunity": ["o"],
    })
    assert [label for label, _ in sections] == [
        "Indication / Audience", "Lab Background", "Proposal",
        "Clinical Actionability", "Key Questions/Experiment", "Commercial Opportunity",
    ]


def test_a_1_9_0_row_renders_the_six_new_groups_in_order():
    sections = key_point_sections({
        "indication_audience": ["i"], "lab_background": ["l"], "proposal": ["p"],
        "clinical_actionability": ["c"], "path_to_clinic": ["t"],
        "commercial_opportunity": ["o"],
    })
    assert [label for label, _ in sections] == [
        "Indication / Audience", "Lab Background", "Proposal",
        "Clinical Actionability", "Path to Clinic / Commercialization",
        "Commercial Opportunity",
    ]


def test_a_legacy_row_still_uses_the_legacy_labels():
    sections = key_point_sections({"significance": ["s"], "key_questions": ["q"]})
    assert [label for label, _ in sections] == [
        "Significance", "Key questions / experiments",
    ]
```

- [ ] **Step 3: Write the both-keys test (Review Focus 3)**

```python
def test_a_row_carrying_both_key_questions_and_path_to_clinic_renders_each_once():
    """A 1.8.0 row read after 1.9.0 lands, or a model emitting both. Neither
    may be hidden and neither may render twice."""
    sections = key_point_sections({
        "clinical_actionability": ["c"], "key_questions": ["q"], "path_to_clinic": ["t"],
    })
    assert [label for label, _ in sections] == [
        "Clinical Actionability", "Key Questions/Experiment",
        "Path to Clinic / Commercialization",
    ]
    assert len(sections) == len({label for label, _ in sections})
```

- [ ] **Step 4: Run to verify they fail**

Run: `pytest tests/unit/test_key_point_sections.py -v`
Expected: FAIL — `RETIRED_KEY_POINT_GROUPS` does not exist.

- [ ] **Step 5: Implement the groups**

In `src/services/assessment_detail.py`, replace the `KEY_POINT_GROUPS` block
(`:112-145`) with:

```python
#: scout_hub >= 1.9.0 (2026-09-28): six groups, ONE bullet each. The (key,
#: label) order is the render order on both assessment surfaces and in the
#: assessment-chat record, all of which render through `key_point_sections`.
KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("indication_audience", "Indication / Audience"),
    ("lab_background", "Lab Background"),
    ("proposal", "Proposal"),
    ("clinical_actionability", "Clinical Actionability"),
    ("path_to_clinic", "Path to Clinic / Commercialization"),
    ("commercial_opportunity", "Commercial Opportunity"),
)

#: Retired from the WRITE contract by 1.9.0 and still rendered: five production
#: rows were written under 1.8.0 with this group, and nine legacy rows carry the
#: key too. The label here is 1.8.0's, and `_RENDER_GROUPS` below puts it back
#: in 1.8.0's fifth slot — a stored row renders under the labels AND in the
#: order it was written with, which is why this is a tuple of its own rather
#: than a deletion.
RETIRED_KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("key_questions", "Key Questions/Experiment"),
)

#: scout_hub 1.3.0-1.7.1 …  (unchanged LEGACY_KEY_POINT_GROUPS block)

#: Render order for any non-legacy row: the current six with the retired group
#: restored to the slot 1.8.0 gave it (fifth, between clinical actionability
#: and what follows it). Only groups the stored value actually carries render,
#: so a 1.9.0 row simply has nothing there.
_RENDER_GROUPS: tuple[tuple[str, str], ...] = (
    KEY_POINT_GROUPS[:4] + RETIRED_KEY_POINT_GROUPS + KEY_POINT_GROUPS[4:]
)

_CURRENT_KEY_POINT_KEYS = frozenset(k for k, _ in KEY_POINT_GROUPS)
_RETIRED_KEY_POINT_KEYS = frozenset(k for k, _ in RETIRED_KEY_POINT_GROUPS)
_LEGACY_KEY_POINT_KEYS = frozenset(k for k, _ in LEGACY_KEY_POINT_GROUPS)
#: A RETIRED key is evidence of neither shape — it was written by 1.8.0 (a
#: "current" prompt at the time) and by the legacy sets alike — so it is
#: subtracted here. Without this a 1.8.0 row classifies "mixed" and takes the
#: stale-prompt warning branch it has no business on.
_LEGACY_ONLY_KEY_POINT_KEYS = (
    _LEGACY_KEY_POINT_KEYS - _CURRENT_KEY_POINT_KEYS - _RETIRED_KEY_POINT_KEYS
)
_CURRENT_ONLY_KEY_POINT_KEYS = _CURRENT_KEY_POINT_KEYS - _LEGACY_KEY_POINT_KEYS

#: Write-time acceptance: the THREE-way union. Accepting only the current keys
#: would drop `key_points` in both skew directions — a 1.9.0 prompt on an old
#: image, and a stale 1.8.0 prompt on this one.
KEY_POINT_ACCEPTED_KEYS: frozenset[str] = (
    _CURRENT_KEY_POINT_KEYS | _RETIRED_KEY_POINT_KEYS | _LEGACY_KEY_POINT_KEYS
)
```

In `key_point_sections`, replace the non-legacy branch's `groups = …` with:

```python
        groups = _RENDER_GROUPS + tuple(
            (key, label) for key, label in LEGACY_KEY_POINT_GROUPS
            if key in _LEGACY_ONLY_KEY_POINT_KEYS
        )
```

and update the docstring to describe the retired slot.

- [ ] **Step 6: Run the section tests**

Run: `pytest tests/unit/test_key_point_sections.py -v`
Expected: PASS.

- [ ] **Step 7: Run every consumer of the groups**

Run: `pytest tests/integration/test_assessment_detail_page.py tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_chat_parity.py tests/unit/test_assessment_chat_record.py tests/integration/test_assessment_narrative_fields.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/services/assessment_detail.py tests/unit/test_key_point_sections.py
git commit -m "feat(key-points): 1.9.0 groups with path_to_clinic; key_questions retired, not hidden"
```

---

## Task 9: Key points 1.9.0 — prompt, warnings, version

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md:303-338` (item 7) and the skeleton
- Modify: `prompts/roles/scout_hub/role.toml` (version)
- Modify: `src/agent/simulation.py:4609-4654`, `:9269-9276`
- Modify: `tests/unit/test_rubric_prompt_sync.py:287-302`
- Modify: `tests/integration/test_assessment_narrative_fields.py:419`, `:474`

**Interfaces:**
- Consumes: `KEY_POINT_GROUPS` / `RETIRED_KEY_POINT_GROUPS` from Task 8.
- Produces: `_KEY_POINT_GROUP_BULLETS` — the six 1.9.0 keys, each `1`.

- [ ] **Step 1: Update the pinned skeleton test**

In `tests/unit/test_rubric_prompt_sync.py:287-302`, replace both the dict and
the order list with the six 1.9.0 keys (`indication_audience`,
`lab_background`, `proposal`, `clinical_actionability`, `path_to_clinic`,
`commercial_opportunity`), and add:

```python
    assert "key_questions" not in skeleton["key_points"], (
        "key_questions is retired from the WRITE contract by 1.9.0; it stays "
        "renderable for stored rows, but the hub must not be asked for it"
    )
```

- [ ] **Step 2: Re-point the two engine-warning assertions**

In `tests/integration/test_assessment_narrative_fields.py`:
- `:474` — `"key_points.key_questions carries 2 bullets (contract asks for 1)"`
  can no longer fire (the key has no count expectation). Re-point the fixture
  and the assertion at `path_to_clinic` with two bullets against an expectation
  of one.
- `:419` — `"key_points.lab_background carries 1 bullets (contract asks for 2)"`
  is now correct input. Change the fixture so `lab_background` carries two
  bullets and assert `"carries 2 bullets (contract asks for 1)"`.
- Add: a sidecar carrying `key_questions` stores it and logs the retired-key
  warning.

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/unit/test_rubric_prompt_sync.py tests/integration/test_assessment_narrative_fields.py -v -k "key_point or skeleton"`
Expected: FAIL.

- [ ] **Step 4: Rewrite item 7 in the prompt**

Replace the group list in item 7 with the six groups, each `— **one bullet**`,
in `KEY_POINT_GROUPS` order, with the content each asks for per spec §6.1:
`indication_audience` (condition, rough size, what goes wrong, what patients
get today), `lab_background` (the PI and the specific established work this
builds on — keeping the sentence "State only what the PI's public profile,
their publications or the lab in this interview established; leave out anything
not on that record rather than supplying it from general knowledge." verbatim,
because `test_item_seven_confines_lab_background_to_the_record` asserts it),
`proposal` (what it is, how it works, why it differs), `clinical_actionability`
(what patients get today, the nearest clinical-stage alternative with its
stage, how this differs), `path_to_clinic` (the development pathway: the animal
or disease model the next step runs in, the validation or IND-enabling work
between now and a first-in-human or first-clinical-use study, the regulatory
route or precedent, and who carries it forward), `commercial_opportunity` (the
realistic shape, the closest deal comparable or funding signal with its date,
any novelty or IP caveat).

Keep "each bullet is a complete claim of at most 300 characters, not a topic"
and re-derive the closing sentence for the new set (who this is for, who is
behind it, what it is, how it would change care, how it reaches patients, why
it is worth building).

- [ ] **Step 5: Add the plain-language rule**

Immediately after the group list:

> **Write every one of these six for a non-specialist.** An intelligent reader
> who does not work in this field must be able to read each bullet once and
> understand it. Expand every abbreviation on first use, and gloss every gene,
> protein, receptor or pathway symbol in plain words the first time it appears
> ("IGHV4-34" → "a particular antibody gene segment, IGHV4-34"). No
> colon-stacked noun phrases and no slash-separated alternatives — the same two
> rules item 6 applies to the headline. One main clause per bullet. At most 300
> characters.
>
>     Not: "Systemic lupus erythematosus: on the order of 200,000-300,000 US
>     patients, with a refractory fraction in the tens of thousands driven by
>     autoreactive B cells and autoantibody (anti-dsDNA, anti-Sm/RNP). Today
>     they get steroids, mycophenolate, belimumab, anti-CD20
>     rituximab/obinutuzumab off-label, or trial CD19 CAR-T."
>
>     Write: "Lupus, an autoimmune disease affecting roughly 250,000 people in
>     the US; for the tens of thousands whose disease resists treatment, the
>     immune cells driving it survive today's drugs, which suppress the whole
>     immune system rather than removing those cells."
>
> The `Not:` example is a real bullet this prompt produced under 1.8.0.

- [ ] **Step 6: Update the skeleton and the version**

Skeleton `key_points` becomes:

```json
  "key_points": {"indication_audience": [], "lab_background": [], "proposal": [], "clinical_actionability": [], "path_to_clinic": [], "commercial_opportunity": []},
```

`prompts/roles/scout_hub/role.toml`: `version = "1.9.0"`.

- [ ] **Step 7: Update the engine warnings**

In `src/agent/simulation.py`:

`_KEY_POINT_GROUP_BULLETS` (`:9269`) becomes the six 1.9.0 keys, each `1`, with
its comment re-dated to 1.9.0.

Inside the `if shape in ("current", "mixed"):` branch (`:4625`), before the
per-group loop, add the retired-key warning — it needs its OWN site, because
with Task 8's classification fix a 1.8.0-shaped sidecar classifies `"current"`
and the legacy branch at `:4614` never sees it:

```python
                retired = sorted(set(checked_key_points) & _RETIRED_KEY_POINT_KEYS)
                if retired:
                    logger.warning(
                        "[%s] Assessment key_points carries group(s) %s retired "
                        "by scout_hub 1.9.0; stored and rendered under the 1.8.0 "
                        "label. Is prompts/roles/scout_hub at 1.9.0 on this host?",
                        agent_id, retired,
                    )
```

importing `_RETIRED_KEY_POINT_KEYS`' public twin (export
`RETIRED_KEY_POINT_GROUPS` and derive the key set locally, matching how
`LEGACY_KEY_POINT_GROUPS` is already imported and used at `:4612`).

Leave the `legacy_only` computation and its warning otherwise as they are.

- [ ] **Step 8: Run the prompt and engine tests**

Run: `pytest tests/unit/test_rubric_prompt_sync.py tests/integration/test_assessment_narrative_fields.py tests/integration/test_opportunity_assessment_persistence.py -v`
Expected: PASS.

- [ ] **Step 9: Sync the prompt docs**

Run: `.venv-test/bin/python scripts/sync_prompt_set_docs.py`
Then: `pytest tests/unit/test_doc_prompt_sync.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add prompts/roles/scout_hub/phase4-thread-reply.md prompts/roles/scout_hub/role.toml \
        src/agent/simulation.py tests/unit/test_rubric_prompt_sync.py \
        tests/integration/test_assessment_narrative_fields.py \
        docs/specs/2026-08-07-hub-bot-prompts.md
git commit -m "feat(hub): scout_hub 1.9.0 — six one-bullet key points, path to clinic, plain language"
```

---

## Task 10: CLAUDE.md, the full gate, and the deploy

**Files:**
- Modify: `CLAUDE.md`
- Test: the whole suite

**Interfaces:**
- Consumes: everything above.
- Produces: a deployable tree.

- [ ] **Step 1: Add the `0052` deploy box to CLAUDE.md**

After the `0051` box, in the same style as the `0048`/`0049`/`0050` boxes:
migrate BEFORE the new code serves; name the READ surfaces that raise
(`UndefinedColumn` on both list pages, both detail pages,
`src/services/review_bot.py` on the worker, `src/routers/reviews.py`,
`src/services/directory.py`) and the WRITE consequence (`_persist_assessment`
names it, best-effort, so every verdict is lost to one ERROR line); state that
the agent rebuild is required and that **prompt-without-image is the hazardous
half** (a 1.9.0 sidecar's `path_to_clinic` is an unknown key to an old image's
`normalize_key_points`, so `key_points` stores NULL); give the command block
from spec §8 including the `git status --porcelain --untracked-files=all`
check; state that NULL is never backfilled.

- [ ] **Step 2: Update the two prose claims CLAUDE.md already makes**

- the assessment-archive / sidecar bullet: the elevator pitch bound is now 250
  words (the `#assessments-summary` excerpt is unchanged at 600 characters);
- the key-points description: six one-bullet groups under scout_hub 1.9.0, with
  `key_questions` retired but still rendered for rows that carry it.

- [ ] **Step 3: Run the CLAUDE.md sync test**

Run: `pytest tests/unit/test_claude_md_disclosure_sync.py -v`
Expected: PASS. (The reply-only bullet must keep "verdict inline",
"thread_guidance" and "unpublished"; no new clause containing "a PI or another
lab sees" may name gating, recommendation, red flags or confidence.)

- [ ] **Step 4: Run the whole gate**

Run, on the host: `./scripts/ci.sh`
Expected: PASS — single alembic head, upgrade→downgrade→upgrade round trip,
ruff clean on tests, `src/` under the ceiling, full pytest over the coverage
floor. Capture the exact failing command and the smallest useful output if it
does not.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): 0052 deploy box; 250-word pitch; key points 1.9.0"
```

- [ ] **Step 6: Confirm the tree is clean enough to build**

Run: `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini`
Expected: NO OUTPUT. The builder stage runs `git clean -ffdx`, so an untracked
`0052` migration would be dropped from the image while the tracked preflight
change targeting it survives — the deploy then fails after the dump.

- [ ] **Step 7: Confirm no run is live, then deploy**

Open `/admin/simulation` and confirm the engine is not running. `prompts/` is
the host working tree read per use, so a 1.9.0 prompt reaches a live agent the
moment the tree lands — before any build.

Then run the command block from spec §8 (rollback tags, build all three, the
guarded migration rehearsal and apply, `alembic current` == `0052`, `up -d`
web + worker, `up -d agent`).

- [ ] **Step 8: Do NOT start the simulation**

Standing operator rule. Report the deploy as complete with the supervisor IDLE,
and note that the next run should be started FRESH.

---

## Self-review

**Spec coverage.** §3.1 → Task 1; §3.2 → Task 1 steps 7-8; §3.3 → Task 2; §4 →
Task 3; §5.1-5.3 → Task 4; §5.2 prompt + persist → Task 5; §5.4 → Task 6; §5.5
→ Task 7; §6.1-6.2 → Tasks 8 and 9 step 4; §6.3 → Task 9 step 5; §6.4 → Task 9
step 7; §6.5 → Task 9 step 6; §7 → every task's test steps plus Task 10 step 4;
§8 → Task 4 step 7 and Task 10 steps 6-8; §9 → no task (it is the accepted-risk
register).

**Type consistency.** `normalize_dimension_rationales` (Task 4) is the only
producer of the stored map; Task 5 calls it, Task 6 reads the column directly
and re-applies the same `.strip().lower()` normalization for the lookup, Task 7
reads `dimensions[i]["rationale"]`. The bucket is `mid_scale` (underscore) in
Python and `mid_scale` as `signal_row`'s `kind` argument, rendering the CSS
class `signal-midscale` and the section class `assessment-signals-neutral` —
three distinct names for three distinct things, each used consistently.
`RETIRED_KEY_POINT_GROUPS` (Task 8) is consumed by name in Task 9 step 7.

**Review Focus coverage.** 1 → Task 6 step 3; 2 → Task 7 step 5; 3 → Task 8
step 3; 4 → Task 1 step 9; 5 → Task 3 step 2.

**Known softness, deliberately left to the implementer.** Three test steps
(Task 3 step 2, Task 5 step 5, Task 7 step 5) give the assertions and the shape
but elide the seeding boilerplate, because those files already carry a
seeding helper whose exact signature the implementer will be reading anyway
(`_seed_narrative_row`, `_seed_live_stamped`, `_seed`); copying a stale
signature into the plan would be worse than naming the helper. Every assertion
that matters is written out.
