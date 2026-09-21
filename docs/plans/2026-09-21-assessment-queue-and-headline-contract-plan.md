# Assessment Queue, Brief and Headline Contract — Implementation Plan

> **For agentic workers:** this repo's governing policy routes plan execution
> through `/engineering:plan-execution`, which replaces
> `superpowers:executing-plans` and `superpowers:subagent-driven-development`.
> Note the caveat in "Execution notes" below: these tasks are **not**
> parallelisable into disjoint-file packages. Steps use `- [ ]` checkboxes.

**Goal:** Ship seven operator-requested changes to the assessment queue, the
assessment detail page and the `scout_hub` headline contract, with no schema
migration.

**Architecture:** Six of the seven changes are template and service work on two
shared Jinja partials (`templates/admin/_assessments_body.html`,
`templates/admin/_assessment_detail_body.html`) plus their four wrappers; the
seventh is a prompt-set edit. One new pure module,
`src/services/prose_citations.py`, does the URL→"cited paper" rewriting and is
registered as a Jinja global on **both** router `Jinja2Templates` instances.

**Tech Stack:** FastAPI + Jinja2 + SQLAlchemy (async) + Postgres; pytest with
testcontainers; Tailwind Play CDN; marked 12.0.2 + DOMPurify 3.1.6 client-side.

**Spec:** `docs/specs/2026-09-21-assessment-queue-and-headline-contract-design.md`
— read it alongside this plan; every task argues from a numbered section there.

## Global Constraints

Copied verbatim from the spec's §9. Every task's requirements include these.

- **No absolute `/admin/` or `/manager/` path may appear in either shared
  body.** Links live in the four wrappers as literal strings. `/reviews/...`
  action paths are the one recorded exception. Verify with
  `grep -nE '"/(admin|manager)/' templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html` — it must print nothing.
- **The two list wrappers stay identical to each other, and the two detail
  wrappers stay identical to each other**, in their `extra_head` script and
  style blocks. No test compares them; `tests/integration/test_assessment_list_chrome.py`
  only asserts named substrings on both surfaces.
- **No new top-level context key may reach only one surface.**
  `src/routers/admin.py::admin_assessments` allowlists every key it forwards;
  `src/routers/manager.py::manager_assessments` splats `**view`.
- **Whitespace control inside the per-card disclosures is load-bearing.** Use
  `{%-` / `-%}` inside anything rendered once per card; the page renders up to
  `ASSESSMENTS_LIMIT = 500` cards.
- **`list_surface` is a bare token (`admin-list` / `manager-list`), never a path.**
- **Reviewer comments never become markdown or HTML.**
- **Any edit under `prompts/` requires the `role.toml` version bump and
  `scripts/sync_prompt_set_docs.py`.**
- **Never regenerate `.ambr` snapshots.** Nothing here touches
  `src/agent/thread_guidance.py`; a moved snapshot is a finding.
- **Never `git checkout` / `stash` / `restore` `docker-compose.prod.yml`.** The
  working-tree edit is load-bearing and uncommitted.
- Run pytest as `.venv-test/bin/python -m pytest …` on the host, never in a
  container.

## Execution notes

Tasks 1→9 are **ordered and must run sequentially**: tasks 2, 4, 5, 6, 7 and 8
all edit `templates/admin/_assessments_body.html`, and tasks 3, 4 and 5 all edit
`templates/admin/_assessment_detail_body.html`. Task 4 must precede task 8
because both change the same assertions in
`test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces`. Task 9
(prompt) is independent of 1–8 and may run at any point.

---

### Task 1: The citation helpers (pure module, no UI)

Spec §7. A pure module with no I/O, so it is fully testable before any template
moves.

**Files:**
- Create: `src/services/prose_citations.py`
- Test: `tests/unit/test_prose_citations.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CITATION_LABEL: str = "cited paper"`
  - `markdown_with_citation_links(text: str | None) -> str | None`
  - `plain_with_citation_links(text: str | None) -> markupsafe.Markup | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_prose_citations.py`:

```python
"""URL -> "cited paper" rewriting for assessment prose (spec 2026-09-21 §7).

Two functions, one per render path. The plain one returns Markup and is the
only place in this change that produces HTML from a string, so its escaping is
tested harder than anything else here: the ONE ordering defect this module
exists to avoid is escape-then-match, which turns `&` into `&amp;` before the
URL matcher runs and yields `&amp;amp;` in the href — a dead link.
"""

from __future__ import annotations

import pytest
from markupsafe import Markup

from src.services.prose_citations import (
    CITATION_LABEL,
    markdown_with_citation_links,
    plain_with_citation_links,
)

pytestmark = pytest.mark.unit

DOI = "https://doi.org/10.1021/acsmedchemlett.5c00623"
DOI2 = "https://doi.org/10.64898/2026.06.29.735215"


# --- both functions, the total contract ------------------------------------

@pytest.mark.parametrize("fn", [markdown_with_citation_links, plain_with_citation_links])
def test_none_in_none_out(fn):
    assert fn(None) is None


@pytest.mark.parametrize("fn", [markdown_with_citation_links, plain_with_citation_links])
def test_empty_string_survives(fn):
    assert fn("") == ""


def test_markdown_without_a_url_is_returned_unchanged():
    text = "Three sentences of plain prose, no citation at all."
    assert markdown_with_citation_links(text) == text


def test_plain_without_a_url_is_escaped_and_nothing_else():
    assert plain_with_citation_links("a < b & c") == Markup("a &lt; b &amp; c")


# --- markdown path ----------------------------------------------------------

def test_markdown_rewrites_a_bare_url_to_an_angle_bracketed_link():
    assert markdown_with_citation_links(f"See {DOI} for details.") == (
        f'See [{CITATION_LABEL}](<{DOI}> "{DOI}") for details.'
    )


def test_markdown_rewrites_two_urls_in_one_paragraph_and_keeps_the_parens():
    """The real production shape: two parenthesised DOIs in one pitch. The
    closing bracket belongs to the sentence, not to the URL."""
    out = markdown_with_citation_links(
        f"in ACS Med Chem Lett 2026 ({DOI}), with modelling on bioRxiv ({DOI2})."
    )
    assert out == (
        f'in ACS Med Chem Lett 2026 ([{CITATION_LABEL}](<{DOI}> "{DOI}")), '
        f'with modelling on bioRxiv ([{CITATION_LABEL}](<{DOI2}> "{DOI2}")).'
    )


def test_a_balanced_paren_inside_a_doi_is_not_stripped():
    """`10.1002/(SICI)…` is a real DOI shape. Only an UNBALANCED trailing `)`
    is sentence punctuation."""
    url = "https://doi.org/10.1002/(SICI)1521-3773(19980316)37:5"
    out = markdown_with_citation_links(f"see {url} here")
    assert f"(<{url}>" in out
    assert out.endswith(" here")


def test_markdown_is_idempotent():
    once = markdown_with_citation_links(f"See {DOI}.")
    assert markdown_with_citation_links(once) == once


def test_a_markdown_link_with_real_text_is_left_alone():
    text = f"the [Slusher lab paper]({DOI}) reports"
    assert markdown_with_citation_links(text) == text


def test_a_markdown_link_whose_text_is_its_own_url_is_rewritten():
    assert markdown_with_citation_links(f"see [{DOI}]({DOI}) now") == (
        f'see [{CITATION_LABEL}](<{DOI}> "{DOI}") now'
    )


def test_a_url_inside_a_code_span_is_left_alone():
    text = f"call `curl {DOI}` first"
    assert markdown_with_citation_links(text) == text


def test_a_url_inside_a_fenced_block_is_left_alone():
    text = f"```\ncurl {DOI}\n```"
    assert markdown_with_citation_links(text) == text


def test_an_autolink_is_left_alone():
    text = f"see <{DOI}> now"
    assert markdown_with_citation_links(text) == text


def test_a_double_quote_terminates_the_url_rather_than_entering_the_title():
    """The URL charset excludes `"`, so a quote ends the match. The half
    before it is still linkified; what matters is that no `"` ever reaches the
    title attribute, where it would break the link syntax and leave marked
    rendering the raw markdown to the reader."""
    out = markdown_with_citation_links('see https://x.test/a"b now')
    assert '(<https://x.test/a> "https://x.test/a")' in out
    assert '"b now' in out


# --- plain path -------------------------------------------------------------

def test_plain_linkifies_and_escapes_around_the_link():
    out = plain_with_citation_links(f"a <b> & {DOI} end")
    assert out == Markup(
        "a &lt;b&gt; &amp; "
        f'<a class="citation-link" href="{DOI}" title="{DOI}">{CITATION_LABEL}</a>'
        " end"
    )


def test_plain_does_not_double_escape_an_ampersand_bearing_url():
    """The defect this ordering exists to prevent: escaping the whole string
    first would put `&amp;amp;` in the href."""
    url = "https://clinicaltrials.gov/search?cond=A&term=B"
    out = plain_with_citation_links(f"see {url}")
    assert f'href="{url.replace("&", "&amp;")}"' in out
    assert "&amp;amp;" not in out


def test_plain_escapes_a_script_tag_in_the_surrounding_prose():
    out = plain_with_citation_links(f"<script>alert(1)</script> {DOI}")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_plain_keeps_the_sentence_paren_outside_the_anchor():
    """The closing bracket and the comma belong to the sentence, not the URL."""
    out = plain_with_citation_links(f"in eLife 2025 ({DOI}), which reports")
    assert f'href="{DOI}"' in out
    assert ">cited paper</a>), which reports" in out


def test_plain_leaves_a_mailto_or_scheme_less_doi_alone():
    text = "write to a@b.test or see doi:10.1021/x"
    assert plain_with_citation_links(text) == Markup(text)
```

- [ ] **Step 2: Run the tests and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_prose_citations.py -q
```
Expected: collection error — `ModuleNotFoundError: No module named 'src.services.prose_citations'`.

- [ ] **Step 3: Write the module**

Create `src/services/prose_citations.py`:

```python
"""Render-time rewriting of URLs in assessment prose into "cited paper" links.

Why render-time and not write-time: `opportunity_assessments` stores what the
hub wrote, and the Slack headline
(`src/services/assessment_headline.py::render_assessment_headline`) publishes
the RAW `elevator_pitch` clipped at a sentence boundary. Rewriting the stored
text would move that clip boundary and change what is posted to a channel this
change is not allowed to touch.

Two functions because the app has two render paths, chosen per row by the
write-time `prose_format` stamp:

  * `markdown_with_citation_links` feeds `data-markdown`, which
    `static/js/markdown.js` parses with marked and sanitises with DOMPurify.
    It emits MARKDOWN, not HTML.
  * `plain_with_citation_links` feeds a `whitespace-pre-line` block and is the
    only place here that produces HTML. It returns `Markup`, so a template
    renders it unescaped — which is safe only because this function escapes
    every non-URL span itself.

The anchor carries `href`, `title` and `class` and NOTHING else. `target` is
absent deliberately: DOMPurify 3.1.6's default `ALLOWED_ATTR` does not include
it (measured 2026-09-21 against the pinned CDN bundle), so a `target` would
survive on the plain path and be stripped on the markdown path — the two
renderings would disagree. Adding it would mean widening the shared sanitiser
for every markdown surface in the app, including the interview transcript.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

#: The visible text of every rewritten link. Identical for every URL: two
#: citations in one paragraph are told apart by the `title` (the full URL),
#: not by a number this module would have to invent.
CITATION_LABEL = "cited paper"

#: Conservative on purpose. `http`/`https` only — a false link on a staff
#: reviewing surface is worse than a visible URL, so `mailto:`, bare `www.`
#: and scheme-less DOIs are left as text. The excluded characters are the ones
#: that would break the markdown destination (`<`, `>`), the title (`"`), or a
#: code span (backtick).
_URL_RE = re.compile(r'https?://[^\s<>"`]+')

#: Spans the markdown rewriter must not touch: fenced code, code spans,
#: autolinks, and inline links/images. Ordered longest-construct-first so a
#: fence is not eaten as a code span.
_MD_PROTECTED_RE = re.compile(
    r"```.*?```"
    r"|`[^`]*`"
    r"|<https?://[^>\s]+>"
    r"|!?\[[^\]]*\]\(\s*<[^>]*>\s*(?:\"[^\"]*\")?\s*\)"
    r"|!?\[[^\]]*\]\([^()\s]*(?:\s+\"[^\"]*\")?\)",
    re.S,
)

#: An inline link, for the one protected shape that IS rewritten: a link whose
#: visible text is its own destination.
_MD_LINK_RE = re.compile(
    r"^(!?)\[([^\]]*)\]\(\s*<?([^()\s>]*)>?\s*(?:\"[^\"]*\")?\s*\)$"
)

#: Trailing characters that belong to the sentence, not the URL.
_SENTENCE_TRAILING = ".,;:!?'"


def _split_trailing(url: str) -> tuple[str, str]:
    """Peel sentence punctuation off the end of a matched URL.

    A `)` is peeled ONLY when it is unbalanced, which is marked's own GFM rule
    and the reason `https://doi.org/10.1002/(SICI)…` survives intact while
    `(https://doi.org/x)` does not swallow its bracket.
    """
    trail = ""
    while url:
        last = url[-1]
        if last in _SENTENCE_TRAILING:
            url, trail = url[:-1], last + trail
        elif last == ")" and url.count(")") > url.count("("):
            url, trail = url[:-1], last + trail
        else:
            break
    return url, trail


def _markdown_link(url: str) -> str:
    """`[cited paper](<url> "url")`.

    Angle-bracketed because a bare destination terminates at the first `)`,
    and a real DOI can contain one — marked then renders the literal text
    `[cited paper](https://…` to the reader. `_URL_RE` already guarantees the
    URL holds no `<`, `>` or `"`, so no further escaping is needed inside
    either the destination or the title.
    """
    return f'[{CITATION_LABEL}](<{url}> "{url}")'


def markdown_with_citation_links(text: str | None) -> str | None:
    """Rewrite every bare URL in `text` as a `cited paper` markdown link.

    Idempotent: a second pass sees only links whose visible text is
    `cited paper`, which are left alone.
    """
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for protected in _MD_PROTECTED_RE.finditer(text):
        out.append(_link_free_span(text[pos : protected.start()]))
        out.append(_rewrite_protected(protected.group()))
        pos = protected.end()
    out.append(_link_free_span(text[pos:]))
    return "".join(out)


def _rewrite_protected(span: str) -> str:
    """A protected span is returned verbatim, EXCEPT an inline link whose
    visible text is its own destination — the hub wrote no link text there, so
    there is nothing deliberate to preserve."""
    match = _MD_LINK_RE.match(span)
    if match and not match.group(1) and match.group(2) == match.group(3):
        return _markdown_link(match.group(3))
    return span


def _link_free_span(span: str) -> str:
    if not span:
        return span
    out: list[str] = []
    pos = 0
    for match in _URL_RE.finditer(span):
        url, trail = _split_trailing(match.group())
        if not url:
            continue
        out.append(span[pos : match.start()])
        out.append(_markdown_link(url))
        out.append(trail)
        pos = match.end()
    out.append(span[pos:])
    return "".join(out)


def plain_with_citation_links(text: str | None) -> Markup | None:
    """Escape `text` and rewrite every URL in it as a `cited paper` anchor.

    Matching runs on the RAW string and escaping is applied per span. The
    reverse order is the defect this ordering exists to prevent: `escape()`
    turns `&` into `&amp;`, the matcher then captures the entity, and the
    href ends up holding `&amp;amp;` — a dead link.

    Apply exactly ONCE per value. The result is `Markup`; feeding it back in
    would match the URL inside the `href` it just produced.
    """
    if not text:
        return Markup(text) if text == "" else None
    parts: list[Markup] = []
    pos = 0
    for match in _URL_RE.finditer(text):
        url, trail = _split_trailing(match.group())
        if not url:
            continue
        parts.append(escape(text[pos : match.start()]))
        parts.append(
            Markup('<a class="citation-link" href="{}" title="{}">{}</a>').format(
                url, url, CITATION_LABEL
            )
        )
        parts.append(escape(trail))
        pos = match.end()
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)
```

- [ ] **Step 4: Run the tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_prose_citations.py -q
.venv-test/bin/python -m ruff check src/services/prose_citations.py tests/unit/test_prose_citations.py
```
Expected: all pass, zero ruff findings.

- [ ] **Step 5: Commit**

```bash
git add src/services/prose_citations.py tests/unit/test_prose_citations.py
git commit -m "feat(assessments): cited-paper link helpers for assessment prose

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire the helpers into the list card

Spec §7. The list card renders exactly three of the narrative fields —
`elevator_pitch`, `key_points`, `score_rationale`. `rationale`,
`recommended_next_experiment`, `strengths` and `risks` are deliberately absent
from this file (see its comment at `:405-406`).

**Files:**
- Modify: `src/routers/admin.py` (beside the `key_point_groups` registration, `:143`)
- Modify: `src/routers/manager.py` (beside the same registration, `:97`)
- Modify: `templates/admin/_assessments_body.html:368`, `:370`, `:383`, `:393`, `:412`, `:414`
- Modify: `templates/admin/assessments.html` and `templates/manager/assessments.html` (style block)
- Modify: `tests/integration/test_assessment_list_chrome.py`
- Test: `tests/integration/test_assessment_queue_controls.py`

**Interfaces:**
- Consumes: `markdown_with_citation_links`, `plain_with_citation_links`, `CITATION_LABEL` from Task 1.
- Produces: the Jinja globals `md_citations` and `plain_citations`, available to
  every template rendered by either router. Task 3 reuses both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_queue_controls.py`:

```python
CITED_DOI = "https://doi.org/10.7554/eLife.94488"


async def test_the_card_pitch_renders_a_doi_as_a_cited_paper_link(
    client, db_session, admin
):
    """Spec §7. A markdown row's pitch reaches the browser as `data-markdown`,
    so the assertion is on the MARKDOWN we hand the client, not on an anchor:
    marked builds the anchor at render time."""
    run, _ = await _seed_narrative_row(
        db_session,
        project="Cited Co",
        prose_format="markdown",
        elevator_pitch=f"Built on the lab's eLife 2025 paper ({CITED_DOI}), which reports.",
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Cited Co")

    assert "cited paper" in card
    assert f"&lt;{CITED_DOI}&gt;" in card, "destination must be angle-bracketed"
    assert f">{CITED_DOI}<" not in card, "the raw URL must not be visible text"


async def test_a_plain_text_card_pitch_renders_a_real_anchor(
    client, db_session, admin
):
    """A `prose_format IS NULL` row never reaches marked, so the anchor has to
    be built server-side."""
    run, _ = await _seed_narrative_row(
        db_session,
        project="Plain Cited Co",
        prose_format=None,
        elevator_pitch=f"See {CITED_DOI} for the paper.",
    )

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Plain Cited Co")

    assert f'<a class="citation-link" href="{CITED_DOI}"' in card
    assert ">cited paper</a>" in card
    assert "target=" not in card.split("citation-link")[1][:200]


async def test_the_manager_surface_renders_citation_links_too(client, db_session):
    """Both routers own their own Jinja2Templates instance and their own
    globals; registering the helpers on one leaves the other raising
    UndefinedError. This is the test that catches a single registration."""
    manager = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="cite-mgr@example.org"
    )
    run, _ = await _seed_narrative_row(
        db_session,
        project="Manager Cited Co",
        prose_format=None,
        elevator_pitch=f"See {CITED_DOI} for the paper.",
    )

    resp = await client.get(
        f"/manager/assessments?run_id={run.id}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 200
    assert ">cited paper</a>" in _row_slice(resp.text, "Manager Cited Co")
```

Modify `tests/integration/test_assessment_list_chrome.py` — add the new rule to
the substring list it asserts on **both** list surfaces:

```python
    assert ".citation-link" in html, (
        "the citation link rule must be in BOTH list wrappers; no test compares "
        "the two wrappers to each other, so a one-sided edit passes everything else"
    )
```

- [ ] **Step 2: Run the tests and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k "cited or citation" tests/integration/test_assessment_list_chrome.py -q
```
Expected: FAIL — the raw URL is still the link text; `.citation-link` absent.

- [ ] **Step 3: Register the globals on both routers**

In `src/routers/admin.py`, directly below the `key_point_groups` registration:

```python
# Render-time URL -> "cited paper" rewriting (spec 2026-09-21 §7). Registered
# as globals for the same reason `key_point_groups` is: the admin assessments
# handler allowlists its context keys and forbids a new one, and BOTH routers
# include the same two partials while each `Jinja2Templates` keeps its own
# globals. src/routers/manager.py carries the identical two lines.
templates.env.globals["md_citations"] = markdown_with_citation_links
templates.env.globals["plain_citations"] = plain_with_citation_links
```

with `from src.services.prose_citations import (markdown_with_citation_links,
plain_with_citation_links)` added to the imports. Add the **same two lines and
the same import** to `src/routers/manager.py`, below its
`key_point_groups` registration, with a comment pointing back at admin.py.

- [ ] **Step 4: Apply the helpers at the card's three render sites**

`templates/admin/_assessments_body.html` — pitch (`:368`/`:370`):

```jinja
                {% if a.prose_format == 'markdown' %}
                <div class="assessment-card-pitch-body md-content" data-markdown="{{ md_citations(a.elevator_pitch) | e }}"></div>
                {% else %}
                <p class="assessment-card-pitch-body whitespace-pre-line">{{ plain_citations(a.elevator_pitch) }}</p>
                {% endif %}
```

key points, both branches (`:383`, `:393`) — bullets have no `prose_format`
branch, so they always take the plain helper:

```jinja
                        {% for point in a.key_points[key] %}<li>{{ plain_citations(point) }}</li>{% endfor %}
```
```jinja
                    {% for point in a.key_points %}<li>{{ plain_citations(point) }}</li>{% endfor %}
```

score rationale (`:412`/`:414`), same shape as the pitch:

```jinja
            {% if a.prose_format == 'markdown' %}
            <div class="assessment-card-score-rationale-body md-content" data-markdown="{{ md_citations(a.score_rationale) | e }}"></div>
            {% else %}
            <p class="assessment-card-score-rationale-body whitespace-pre-line">{{ plain_citations(a.score_rationale) }}</p>
            {% endif %}
```

- [ ] **Step 5: Add the style rule to BOTH list wrappers**

In the `<style>` block of `templates/admin/assessments.html` **and**
`templates/manager/assessments.html`, immediately after the `.md-content code`
rule (keep the two blocks byte-identical):

```css
  /* Links inside rendered prose. Tailwind's preflight sets
     `a{color:inherit;text-decoration:inherit}`, so without this an anchor is
     indistinguishable from body text. `.citation-link` is the server-rendered
     plain-text path; `.md-content a` covers everything marked builds,
     including the interview transcript on the detail page — a link should
     look like a link everywhere on the page. */
  .md-content a, .citation-link { color: #2563eb; text-decoration: underline; }
```

- [ ] **Step 6: Run the tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_list_chrome.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/routers/admin.py src/routers/manager.py templates/admin/_assessments_body.html templates/admin/assessments.html templates/manager/assessments.html tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_list_chrome.py
git commit -m "feat(assessments): render cited-paper links on the queue cards

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire the helpers into the detail body

Spec §7 "Render sites". The detail body renders **more** fields than the card,
and two of them are not a simple two-branch pair.

**Files:**
- Modify: `templates/admin/_assessment_detail_body.html` at `:108`, `:110`,
  `:129`, `:131`, `:182`, `:184`, `:298`, `:323`, `:396`, `:399`, `:402`,
  `:404`, `:736`, `:742`
- Modify: `templates/admin/assessment_detail.html`, `templates/manager/assessment_detail.html` (style block)
- Test: `tests/integration/test_assessment_detail_page.py`

**Interfaces:**
- Consumes: the `md_citations` / `plain_citations` globals from Task 2.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_assessment_detail_page.py`:

```python
DETAIL_DOI = "https://doi.org/10.1021/acsmedchemlett.5c00623"


async def test_the_detail_brief_renders_a_cited_paper_link(client, db_session, admin):
    """Spec §7. Same rewrite as the card, on the detail page's own pitch."""
    assessment = await _seed_assessment(
        db_session,
        prose_format="markdown",
        elevator_pitch=f"Published in ACS Med Chem Lett 2026 ({DETAIL_DOI}).",
    )
    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert "cited paper" in html
    assert f"&lt;{DETAIL_DOI}&gt;" in html


async def test_a_plain_rationale_paragraph_keeps_its_paragraphs_and_gains_a_link(
    client, db_session, admin
):
    """The plain `rationale` path is NOT one block: it splits on a blank line
    and emits one <p> per paragraph, so the helper runs per paragraph. A single
    whole-field call would collapse the paragraphs."""
    assessment = await _seed_assessment(
        db_session,
        prose_format=None,
        rationale=f"First paragraph, see {DETAIL_DOI}.\n\nSecond paragraph.",
    )
    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert html.count('<p class="whitespace-pre-line">') >= 2
    assert f'<a class="citation-link" href="{DETAIL_DOI}"' in html


async def test_a_plain_next_experiment_url_becomes_a_link(client, db_session, admin):
    """One production row has a URL in a PLAIN `recommended_next_experiment`;
    that field has four render branches and all four take the rewrite."""
    assessment = await _seed_assessment(
        db_session,
        prose_format=None,
        recommended_next_experiment=f"Run the panel; protocol at {DETAIL_DOI}.",
    )
    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    assert ">cited paper</a>" in html


async def test_the_manager_detail_page_renders_citation_links(client, db_session):
    manager = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="cite-detail-mgr@example.org"
    )
    assessment = await _seed_assessment(
        db_session,
        prose_format=None,
        elevator_pitch=f"See {DETAIL_DOI}.",
    )
    resp = await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 200
    assert ">cited paper</a>" in resp.text
```

If `_seed_assessment` in that file does not already accept these keyword
overrides, extend it to pass `**overrides` into `OpportunityAssessment(...)`
rather than adding a second seeding helper.

- [ ] **Step 2: Run the tests and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -k "cited or citation" -q
```
Expected: FAIL.

- [ ] **Step 3: Apply the helpers at every detail render site**

- pitch `:108` / `:110` — `md_citations(a.elevator_pitch) | e` and
  `plain_citations(a.elevator_pitch)`.
- score rationale, **both** copies, `:129`/`:131` and `:182`/`:184` — same pair
  on `a.score_rationale`.
- next experiment, all four branches:
  `:396` `data-markdown="{{ md_citations(ask_parts[0]) | e }}"`,
  `:399` `data-markdown="{{ md_citations(ask_parts[1]) | e }}"`,
  `:402` `data-markdown="{{ md_citations(ask_text) | e }}"`,
  `:404` `<p class="assessment-next-experiment whitespace-pre-line">{{ plain_citations(ask_text) }}</p>`.
- rationale `:736` markdown — `md_citations(a.rationale) | e`.
- rationale `:742` plain — **per paragraph**:

```jinja
        {% for para in a.rationale.split('\n\n') %}<p class="whitespace-pre-line">{{ plain_citations(para) }}</p>{% endfor %}
```

- hub strengths `:298` and hub risks `:323` — `{{ plain_citations(bullet) }}`.
  These stay behind the existing `viewer_is_staff` gate (`:216-217`); do not
  touch that condition.

- [ ] **Step 4: Add the same style rule to BOTH detail wrappers**

Paste the identical CSS block from Task 2 Step 5 into the `<style>` block of
`templates/admin/assessment_detail.html` and
`templates/manager/assessment_detail.html`.

- [ ] **Step 5: Run the tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_detail_page.py -q
```
Expected: PASS, including the pre-existing `test_sidecar_prose_stays_plain_text`
and `test_prose_format_markdown_renders_data_markdown_divs_on_both_surfaces`.

- [ ] **Step 6: Prove the Slack headline is untouched**

```bash
.venv-test/bin/python -m pytest tests/unit/test_assessment_headline_render.py -q
```
Expected: PASS unchanged — `render_assessment_headline` must still clip the RAW
pitch.

- [ ] **Step 7: Commit**

```bash
git add templates/admin/_assessment_detail_body.html templates/admin/assessment_detail.html templates/manager/assessment_detail.html tests/integration/test_assessment_detail_page.py
git commit -m "feat(assessments): render cited-paper links on the detail page

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Remove the approve/disapprove controls

Spec §3.

**Files:**
- Modify: `templates/admin/_assessments_body.html` — delete `:563-568`, delete the orphaned comment `:534-538`
- Modify: `templates/admin/_assessment_detail_body.html` — delete `:948-953`
- Modify: `tests/unit/test_reachability.py` (`ROUTE_ALLOWLIST`)
- Modify: `src/routers/reviews.py:136-142` (docstring only)
- Test: `tests/integration/test_assessment_queue_controls.py`, `tests/integration/test_assessment_review_ui.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a card and a review panel with no status form; the quick-scoring
  disclosure now holds exactly ONE form, which Task 8 relies on when it changes
  the hidden-input counts.

- [ ] **Step 1: Update the two tests that pin the removed markup, so they fail**

In `tests/integration/test_assessment_queue_controls.py::test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces`,
replace the status assertions with absence assertions and drop the counts to one
form:

```python
    assert f'action="/reviews/assessments/{assessment.id}/feedback"' in html
    assert f'action="/reviews/assessments/{assessment.id}/status"' not in html
    assert 'method="post"' in html
    # ONE form now (the status form was removed 2026-09-21 on operator
    # request); the four hidden inputs appear once each, not twice.
    quickscore = _details_slice(html, "assessment-card-quickscore")
    assert quickscore.count(f'name="surface" value="{surface}"') == 1
    assert quickscore.count('name="run_id"') == 1
    assert quickscore.count('name="sort"') == 1
    assert quickscore.count('name="lab"') == 1
    # No status control of any kind survives on the card.
    assert 'name="action"' not in quickscore
    assert "Approved" not in quickscore and "Disapproved" not in quickscore
```

In `tests/integration/test_assessment_review_ui.py::test_the_forms_post_to_literal_review_paths`
(`:187`), change the status line to an absence assertion on all three surfaces:

```python
        assert f"/reviews/assessments/{assessment.id}/feedback" in html
        assert f"/reviews/assessments/{assessment.id}/status" not in html
```

- [ ] **Step 2: Run them and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py::test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces tests/integration/test_assessment_review_ui.py -q
```
Expected: FAIL — the forms are still rendered.

- [ ] **Step 3: Delete both forms and the orphaned comment**

Delete `templates/admin/_assessments_body.html:563-568` in full (the
`<form … /status">` element and its three buttons). Delete the now-orphaned
comment at `:534-538` that begins "The status control is BUTTONS, not a
`<select>`", and move its surviving constraint onto the chip's own comment
above `:446`:

```jinja
            {# The chip is the only place a card may print the words
               "Approved"/"Disapproved" (test_list_pages_show_reviewer_columns
               row-scopes that). The control that used to set it was removed
               2026-09-21; this renders history only, and nothing in the app
               can now write a new status. #}
```

Delete `templates/admin/_assessment_detail_body.html:948-953` in full — the
whole `{% if can_write %}` … `{% endif %}` wrapper around the status form goes
with it, since it guards nothing else.

- [ ] **Step 4: Add the reachability allowlist entry**

In `tests/unit/test_reachability.py`, inside `ROUTE_ALLOWLIST`:

```python
    ("POST", "/reviews/assessments/{assessment_id}/status"): (
        "Deliberately caller-less, which is a NEW category on this list: every "
        "other entry names a real external caller. The approve/disapprove/clear "
        "buttons were removed from both the queue card and the detail page on "
        "operator request 2026-09-21 (docs/specs/2026-09-21-assessment-queue-"
        "and-headline-contract-design.md, D2/D3). The handler, "
        "VALID_STATUS_ACTIONS and the assessment_review_events table are kept "
        "so the capability can be restored without a migration, and the read-"
        "only Status line and chip still render the history. Do not go looking "
        "for a caller: there is none. Re-adding a button makes this entry stale "
        "and test_route_allowlist_has_no_stale_entries will say so."
    ),
```

- [ ] **Step 5: Correct the stale docstring**

In `src/routers/reviews.py`, in `_assessments_redirect`'s docstring, amend the
sentence naming the two callers that pass `run_id`/`sort`/`lab`:

```
    ``run_id``/``sort``/``lab`` are only passed by ``submit_review_feedback``
    and ``set_review_status`` — and since 2026-09-21 the second has no UI
    caller at all (its buttons were removed; see the ROUTE_ALLOWLIST entry in
    tests/unit/test_reachability.py), so in practice the filtered redirect is
    the feedback path's.
```

- [ ] **Step 6: Run the tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_reachability.py tests/integration/test_assessment_review_ui.py tests/integration/test_assessment_queue_controls.py tests/integration/test_reviews_router.py -q
grep -rn 'name="action"' templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html
```
Expected: tests PASS; the grep prints nothing.

- [ ] **Step 7: Commit**

```bash
git add templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html tests/unit/test_reachability.py src/routers/reviews.py tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_review_ui.py
git commit -m "feat(assessments): remove the approve/disapprove controls

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Quick review and Add feedback open by default

Spec §5.

**Files:**
- Modify: `templates/admin/_assessments_body.html:540`
- Modify: `templates/admin/_assessment_detail_body.html:1048`
- Test: `tests/integration/test_assessment_queue_controls.py`, `tests/integration/test_assessment_detail_page.py`

- [ ] **Step 1: Invert the existing test and add the detail-page one**

Rename `test_quick_scoring_is_collapsed_and_labelled` to
`test_quick_scoring_is_expanded_and_labelled` and replace its body:

```python
async def test_quick_scoring_is_expanded_and_labelled(client, db_session, admin):
    """OPEN by default. The 2026-09-09 card list made this disclosure closed on
    the grounds that a permanently-expanded form per card is what made the old
    table unreadable; the operator reversed that call on 2026-09-21 — the
    review form is the point of the queue. Still a <details>, so a reader can
    collapse it and the print handler still works on it."""
    run, _ = await _seed_narrative_row(db_session, project="Quickscore Co")

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "assessment-card-quickscore" in html
    assert "Quick scoring" in html
    assert " open" in _details_open_tag(html, "assessment-card-quickscore"), (
        "the quick-scoring disclosure must start open"
    )
```

Append to `tests/integration/test_assessment_detail_page.py`:

```python
async def test_add_feedback_is_open_by_default(client, db_session, admin):
    """Matches the queue card's quick review (spec §5): the review form is
    visible without a click on both surfaces."""
    assessment = await _seed_assessment(db_session)
    html = (await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )).text
    at = html.index("Add feedback")
    start = html.rfind("<details", 0, at)
    assert " open" in html[start : html.index(">", start) + 1]
```

- [ ] **Step 2: Run them and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k expanded_and_labelled tests/integration/test_assessment_detail_page.py -k add_feedback_is_open -q
```
Expected: FAIL — no `open` attribute.

- [ ] **Step 3: Add the attributes**

`templates/admin/_assessments_body.html:540`:

```jinja
        <details class="assessment-card-quickscore mt-3 border-t border-gray-100 pt-3" open>
```

`templates/admin/_assessment_detail_body.html:1048`:

```jinja
    <details class="mb-4" open>
```

- [ ] **Step 4: Run the tests, including the size ceiling**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_detail_page.py -q
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k size_ceiling -q
```
Expected: PASS. ` open` adds 5 bytes per card (~2.5 KB at 500 rows), which is
inside the existing headroom. **If** the ceiling fails, raise `CEILING` to the
newly measured number and record the measurement in that test's docstring table
— no speculative headroom.

- [ ] **Step 5: Commit**

```bash
git add templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_detail_page.py
git commit -m "feat(assessments): open the quick review and add-feedback forms by default

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The detail link becomes a button in the pitch box

Spec §6.

**Files:**
- Modify: `templates/admin/assessments.html` and `templates/manager/assessments.html` (the `assessment_link` macro)
- Modify: `templates/admin/_assessments_body.html` — remove the call at `:457`, add it inside the pitch box and in a fallback row
- Test: `tests/integration/test_assessment_queue_controls.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_detail_button_sits_in_the_pitch_box(client, db_session, admin):
    """Spec §6/D7. One control per card, at the foot of "In one minute"."""
    run, _ = await _seed_narrative_row(
        db_session, project="Pitch Button Co",
        elevator_pitch="One minute of prose about the idea.",
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Pitch Button Co")

    assert card.count("assessment-open-link") == 1
    pitch_box = card[card.index("assessment-card-pitch") :]
    pitch_box = pitch_box[: pitch_box.index("assessment-card-points")]
    assert "assessment-open-link" in pitch_box


async def test_a_card_with_no_pitch_still_has_exactly_one_detail_button(
    client, db_session, admin
):
    """12 of 22 production rows have no pitch, so the fallback is the common
    case, not an edge one."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="No Pitch Button Co",
        recommendation="pass", weighted_score=1.2, band="pass",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "No Pitch Button Co")

    assert card.count("assessment-open-link") == 1
    assert f"/admin/assessments/" in card
```

- [ ] **Step 2: Run them and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k detail_button -q
```
Expected: FAIL — the link is in the footer, not the pitch box.

- [ ] **Step 3: Restyle the macro in BOTH wrappers**

`templates/admin/assessments.html` (and the `/manager` twin, changing only the
path):

```jinja
{% macro assessment_link(a) %}<a class="assessment-open-link inline-flex items-center gap-1 rounded bg-indigo-600 px-2 py-1 text-sm font-medium text-white hover:bg-indigo-700 whitespace-nowrap" href="/admin/assessments/{{ a.id }}" title="Full verdict and interview timeline">detail &rarr;</a>{% endmacro %}
```

It stays an `<a>`: it navigates, so middle-click, copy-link and screen-reader
semantics must remain those of a link. (Side effect, unchanged from today: the
`@media print` rule hides `button`, not `a`, so a printed card keeps its URL.)

- [ ] **Step 4: Move the call site**

In `templates/admin/_assessments_body.html`, delete
`<span class="ml-auto">{{ assessment_link(a) }}</span>` from the footer row
(`:457`). Inside the pitch box, after the prose `</div>`:

```jinja
                <div class="assessment-card-open mt-3">{{ assessment_link(a) }}</div>
```

And immediately after the pitch/key-points grid's `{% endif %}`, the fallback:

```jinja
        {% if not a.elevator_pitch %}
        {# The button lives at the foot of the pitch box (D7). 12 of 22
           production rows have no pitch, so without this branch the commonest
           card on the page would have no way through to the verdict. #}
        <div class="assessment-card-open mt-3">{{ assessment_link(a) }}</div>
        {% endif %}
```

- [ ] **Step 5: Run the tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_detail_page.py -k "detail_button or link" -q
.venv-test/bin/python -m pytest tests/integration/test_manager_views.py -q
```
Expected: PASS, including `test_both_assessment_lists_link_to_the_detail_page`
and `test_the_manager_controls_never_point_into_admin`.

- [ ] **Step 6: Commit**

```bash
git add templates/admin/assessments.html templates/manager/assessments.html templates/admin/_assessments_body.html tests/integration/test_assessment_queue_controls.py
git commit -m "feat(assessments): move the detail control into the pitch box as a button

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Score and band into a collapsed "Why this score"

Spec §8. Includes the required correction to the band-as-text test, which would
otherwise keep passing while the property it protects is gone.

**Files:**
- Modify: `templates/admin/_assessments_body.html:307-333` (top-right block) and `:407-418` (rationale box)
- Test: `tests/integration/test_assessment_queue_controls.py`,
  `tests/integration/test_opportunity_assessment_persistence.py`

- [ ] **Step 1: Write the failing tests and fix the two that would lie**

New tests:

```python
async def test_the_score_and_band_live_inside_the_collapsed_why_this_score_box(
    client, db_session, admin
):
    """Spec §8/D8. The card face keeps the recommendation chip only."""
    run, _ = await _seed_narrative_row(
        db_session, project="Score Box Co", score_rationale="Because of the cohort.",
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    card = _row_slice(html, "Score Box Co")

    assert " open" not in _details_open_tag(html, "assessment-card-score-rationale")
    box = _details_slice(html, "assessment-card-score-rationale")
    assert "3.05" in box
    assert "band-label" in box
    # Nothing outside the box carries the number or the band word.
    face = card[: card.index("assessment-card-score-rationale")]
    assert "3.05" not in face
    assert "band-label" not in face
    assert "conditional" in face, "the recommendation chip stays on the face"


async def test_a_row_with_no_score_rationale_still_shows_its_score_in_the_box(
    client, db_session, admin
):
    """D9: 12 of 22 production rows are pre-0048 and have score_rationale NULL.
    The box is unconditional so none of them loses its score."""
    run, _ = await _seed_narrative_row(
        db_session, project="No Rationale Co", score_rationale=None,
    )
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    box = _details_slice(html, "assessment-card-score-rationale")
    assert "3.05" in box
    assert "no score rationale" in box.lower()


async def test_a_row_with_no_weighted_score_renders_the_em_dash_in_the_box(
    client, db_session, admin
):
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="No Score Co",
        recommendation="pass",
    ))
    await db_session.flush()
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    box = _details_slice(html, "assessment-card-score-rationale")
    assert "&mdash;" in box or "—" in box
```

Update `test_a_row_with_no_pitch_or_points_renders_neither_box` (`:1171`) —
the score-rationale box is now unconditional:

```python
        assert "assessment-card-pitch" not in card, marker
        assert "assessment-card-points" not in card, marker
        # The score-rationale box is UNCONDITIONAL as of 2026-09-21 (D9): it
        # now carries the score itself, so a row that omitted it would show no
        # score anywhere.
        assert "assessment-card-score-rationale" in card, marker
```

Correct `test_admin_assessments_page_renders_band_as_text_not_just_colour` in
`tests/integration/test_opportunity_assessment_persistence.py:1356` — append to
its docstring and add one assertion, so it stops implying a guarantee it no
longer gives:

```python
    """… (existing docstring) …

    UPDATED 2026-09-21: the band label moved inside the collapsed "Why this
    score" disclosure with the score (spec §8/D8, an accepted accessibility
    regression the operator signed off). `_band_label` is a page-wide regex, so
    this test kept passing on its own — which is exactly why the assertion
    below was added: the band must still be TEXT, and it must be findable where
    the page actually puts it now.
    """
    ...
    assert _band_label(html) == "decline"
    assert _band_label(html) != "route-to-incubation"
    # The label is inside the collapsed disclosure, not on the card face.
    assert "assessment-card-score-rationale" in html
    box_at = html.index("assessment-card-score-rationale")
    assert html.index('class="band-label') > box_at
```

- [ ] **Step 2: Run them and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k "score_box or no_score_rationale or no_weighted_score or neither_box" tests/integration/test_opportunity_assessment_persistence.py -k band -q
```
Expected: FAIL.

- [ ] **Step 3: Strip the score and band from the card face**

`templates/admin/_assessments_body.html`, replace the right-hand block
(`:307-333`) with the chip alone:

```jinja
            <div class="text-right shrink-0">
                {# Score and band moved into the collapsed "Why this score"
                   disclosure below (2026-09-21, operator request). The
                   recommendation — the model's own call — stays here; the
                   computed band is one click down, which is an accepted
                   accessibility regression recorded in the design doc. #}
                {% if a.recommendation %}
                    {% set rec_chip = {
                        'advance': 'bg-green-100 text-green-700',
                        'conditional': 'bg-amber-100 text-amber-700',
                        'route-to-incubation': 'bg-teal-100 text-teal-700',
                        'pass': 'bg-gray-100 text-gray-600',
                    } %}
                    <span class="px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap {{ rec_chip.get(a.recommendation, 'bg-gray-100 text-gray-500') }}">{{ banding.pass_label if a.recommendation == 'pass' else a.recommendation }}</span>
                {% else %}<span class="text-xs text-gray-400">no recommendation</span>{% endif %}
            </div>
```

- [ ] **Step 4: Turn the rationale box into an unconditional disclosure**

Replace `:407-418` (keep the explanatory comment above it, updated) with — note
the trailing space after the class name, which `_details_open_tag` and
`_details_slice` match literally, and the `{%-` trims, which are load-bearing at
500 cards:

```jinja
        <details class="assessment-card-score-rationale rounded-lg border border-amber-200 bg-amber-50 p-3 mt-3">
            <summary class="cursor-pointer text-xs font-semibold uppercase tracking-wide text-amber-800">Why this score</summary>
            {%- if a.weighted_score is not none %}
            <div class="mt-2"><span class="text-xl font-bold {% if a.band == 'advance' %}text-green-600{% elif a.band == 'conditional' %}text-amber-600{% else %}text-gray-400{% endif %}">{{ "%.2f"|format(a.weighted_score) }}</span><span class="band-label ml-1.5 text-xs font-medium uppercase tracking-wide align-middle {% if a.band == 'advance' %}text-green-600{% elif a.band == 'conditional' %}text-amber-600{% else %}text-gray-400{% endif %}">{{ (banding.pass_label if a.band == 'pass' else a.band) or '—' }}</span></div>
            {%- else %}
            <div class="mt-2"><span class="text-xl font-bold text-gray-400">&mdash;</span><span class="band-label ml-1.5 text-xs font-medium uppercase tracking-wide align-middle text-gray-400">{{ (banding.pass_label if a.band == 'pass' else a.band) or '—' }}</span></div>
            {%- endif %}
            {%- if a.score_rationale %}
            <div class="assessment-prose max-w-none mt-2">
            {% if a.prose_format == 'markdown' %}
            <div class="assessment-card-score-rationale-body md-content" data-markdown="{{ md_citations(a.score_rationale) | e }}"></div>
            {% else %}
            <p class="assessment-card-score-rationale-body whitespace-pre-line">{{ plain_citations(a.score_rationale) }}</p>
            {% endif %}
            </div>
            {%- else %}
            <p class="mt-2 text-xs text-gray-600">The hub recorded no score rationale for this verdict. Rows written before migration 0048 were never asked for one, and none was backfilled.</p>
            {%- endif %}
        </details>
```

The `{% if a.score_rationale %}` wrapper that used to guard the whole block is
gone; make sure the box is emitted for every card.

- [ ] **Step 5: Run the tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py tests/integration/test_opportunity_assessment_persistence.py tests/integration/test_manager_views.py -q
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k size_ceiling -q
```
Expected: PASS. The box now renders on every card; if the ceiling fails, raise
it to the measured number and record the measurement in the docstring table.

- [ ] **Step 6: Commit**

```bash
git add templates/admin/_assessments_body.html tests/integration/test_assessment_queue_controls.py tests/integration/test_opportunity_assessment_persistence.py
git commit -m "feat(assessments): fold the score and band into a collapsed Why-this-score box

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Reviewed / unreviewed sub-tabs

Spec §4. The largest task: one service predicate, two handlers, two wrappers,
three pieces of false copy, and the redirect round-trip.

**Files:**
- Modify: `src/services/assessment_detail.py` (new `has_review_filter()` beside `unvetted_panel_filter()`, `:501`)
- Modify: `src/services/directory.py::list_assessments` (`:340-700`)
- Modify: `src/routers/admin.py:816` and `src/routers/manager.py:624`
- Modify: `src/routers/reviews.py::_list_filter_query` (`:81`) and `submit_review_feedback`
- Modify: `templates/admin/assessments.html`, `templates/manager/assessments.html`
- Modify: `templates/admin/_assessments_body.html` (`qs_filters` `:257`, header comment `:246-250`, empty state `:573-578`)
- Test: `tests/unit/test_directory_assessment_sorting.py`, `tests/integration/test_assessment_queue_controls.py`, `tests/integration/test_reviewer_role.py`, `tests/integration/test_reviews_router.py`

**Interfaces:**
- Consumes: nothing from earlier tasks except the one-form quick-scoring
  disclosure Task 4 produced.
- Produces:
  - `src.services.assessment_detail.has_review_filter() -> ColumnElement[bool]`
  - `list_assessments(db, run_id, *, sort=None, lab=None, review=None)` returning
    two additional keys: `review: str` and
    `review_counts: dict[str, int]` with keys `unreviewed`, `reviewed`, `all`.
  - `ASSESSMENT_REVIEW_FILTERS: tuple[str, ...] = ("unreviewed", "reviewed", "all")`
    and `ASSESSMENT_REVIEW_DEFAULT = "unreviewed"` in `src/services/directory.py`.

- [ ] **Step 1: Write the failing service tests**

Append to `tests/unit/test_directory_assessment_sorting.py`:

```python
async def test_the_review_filter_splits_the_queue(db_session):
    """Spec §4/D4. Reviewed == at least one assessment_reviews row."""
    run = await factories.make_simulation_run(db_session)
    reviewed = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Reviewed One",
    )
    unreviewed = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Unreviewed One",
    )
    db_session.add_all([reviewed, unreviewed])
    await db_session.flush()
    reviewer = await factories.make_user(db_session)
    db_session.add(AssessmentReview(
        assessment_id=reviewed.id, reviewer_user_id=reviewer.id,
        reviewer_name="R", score=4, comment="ok", feedback_mode="log_only",
    ))
    await db_session.flush()

    only_unreviewed = await list_assessments(db_session, str(run.id), review="unreviewed")
    only_reviewed = await list_assessments(db_session, str(run.id), review="reviewed")
    everything = await list_assessments(db_session, str(run.id), review="all")

    assert [a.company_or_project for a in only_unreviewed["assessments"]] == ["Unreviewed One"]
    assert [a.company_or_project for a in only_reviewed["assessments"]] == ["Reviewed One"]
    assert len(everything["assessments"]) == 2
    # total_count follows the tab; the counts sum.
    assert only_unreviewed["total_count"] == 1
    assert everything["review_counts"] == {"unreviewed": 1, "reviewed": 1, "all": 2}


async def test_an_unknown_review_value_falls_back_to_the_default(db_session):
    run = await factories.make_simulation_run(db_session)
    view = await list_assessments(db_session, str(run.id), review="no-such-tab")
    assert view["review"] == "unreviewed"


async def test_the_warnings_and_dropdowns_ignore_the_review_filter(db_session):
    """Warnings under-warn if narrowed, and a dropdown computed post-filter
    offers no way back — the same rule `lab` already follows."""
    run = await factories.make_simulation_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="Unvetted One",
    )
    db_session.add(row)
    await db_session.flush()
    reviewer = await factories.make_user(db_session)
    db_session.add(AssessmentReview(
        assessment_id=row.id, reviewer_user_id=reviewer.id, reviewer_name="R",
        score=4, comment="ok", feedback_mode="log_only",
    ))
    await db_session.flush()

    view = await list_assessments(db_session, str(run.id), review="unreviewed")
    assert view["assessments"] == []
    assert view["incomplete_panel_count"] == 1
    assert view["lab_options"] == ["wang"]
    assert view["assessment_counts_by_run"][run.id] == 1
```

- [ ] **Step 2: Run them and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_directory_assessment_sorting.py -k review -q
```
Expected: FAIL — `list_assessments() got an unexpected keyword argument 'review'`.

- [ ] **Step 3: Add the predicate and the service plumbing**

In `src/services/assessment_detail.py`, directly below `unvetted_panel_filter`:

```python
def has_review_filter():
    """The SQL for "a human has written feedback on this assessment".

    Lives beside ``unvetted_panel_filter`` for the same reason that one does:
    one rule, one place. An EXISTS rather than a JOIN because a row with three
    reviews must appear ONCE — a join would multiply it by its feedback count
    and silently inflate both the page and ``total_count``.

    Deliberately narrower than ``review_columns_for``'s "Reviewed by", which
    unions feedback authors with status-event actors. The two can disagree for
    a row that carries a status event and no feedback: it renders a "Reviewed
    by" name and a chip while sitting in the Unreviewed tab. Zero such rows
    exist in production and nothing in the app can create one since the
    approve/disapprove controls were removed, but a restore could — which is
    why the tab strip says in words that a tab counts written feedback.
    """
    from src.models import AssessmentReview

    return select(AssessmentReview.id).where(
        AssessmentReview.assessment_id == OpportunityAssessment.id
    ).exists()
```

In `src/services/directory.py`, beside the sort constants:

```python
#: The review sub-tabs (spec 2026-09-21 §4). `unreviewed` is the default: this
#: page is a work queue, and the tab a reader lands on should be the work.
ASSESSMENT_REVIEW_FILTERS: tuple[str, ...] = ("unreviewed", "reviewed", "all")
ASSESSMENT_REVIEW_DEFAULT = "unreviewed"
```

Add `review: str | None = None` to the signature, and after `sort_key` is
resolved:

```python
    review_key = review if review in ASSESSMENT_REVIEW_FILTERS else ASSESSMENT_REVIEW_DEFAULT
```

Capture the run+lab scope **before** the review predicate narrows it — this is
`scope_query`, and the two counts below are computed from it:

```python
    # The run+lab scope, kept so the tab counts can be taken from it. `query`
    # is about to be narrowed by the review filter; these counts must not be.
    scope_query = query
```

Then apply the filter to `query`, immediately after the lab filter and
**before** `total_count` is computed:

```python
    # Applied BEFORE total_count and before the LIMIT, so the "top N of TOTAL"
    # note, the five recommendation cards and dimension_stats all describe the
    # tab the reader is on rather than the whole run.
    if review_key == "reviewed":
        query = query.where(has_review_filter())
    elif review_key == "unreviewed":
        query = query.where(~has_review_filter())
```

Add the counts, scoped by run+lab but NOT by the tab, next to `total_count`:

```python
    # Both tabs' sizes in one place, so the strip never has to derive "All" by
    # addition and cannot disagree with the rows below it.
    reviewed_count = (await db.execute(
        select(func.count()).select_from(scope_query.where(has_review_filter()).subquery())
    )).scalar() or 0
    all_count = (await db.execute(
        select(func.count()).select_from(scope_query.subquery())
    )).scalar() or 0
    review_counts = {
        "reviewed": reviewed_count,
        "unreviewed": all_count - reviewed_count,
        "all": all_count,
    }
```

where `scope_query` is the run+lab-filtered `select(OpportunityAssessment)`
captured **before** the review predicate is applied. Return `"review":
review_key` and `"review_counts": review_counts` from the view dict.

Leave `incomplete_query`, the drops query, `lab_options_query` and
`assessment_counts_by_run` untouched, and say so in the docstring:

```
    ``review`` splits the queue into the reviewed and unreviewed sub-tabs and
    narrows ``total_count``, the rendered rows and everything derived from them.
    It deliberately does NOT narrow ``incomplete_panel_count``, the drop counts,
    ``lab_options`` or ``assessment_counts_by_run`` — the first two are warnings
    and the failure mode of a warning is under-warning, and the last two are the
    controls' own option sets, which are computed pre-filter so a reader always
    has a way back.
```

- [ ] **Step 4: Run the service tests and verify they pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_directory_assessment_sorting.py -q
```
Expected: PASS.

- [ ] **Step 5: Write the failing wiring tests**

```python
@pytest.mark.parametrize(("base", "role"), [("/admin", USER_ROLE_ADMIN), ("/manager", USER_ROLE_MANAGER)])
async def test_both_surfaces_offer_the_review_tabs(client, db_session, base, role):
    staff = await factories.make_user(db_session, user_role=role, email=f"tabs-{role}@example.org")
    run, _ = await _seed_narrative_row(db_session, project="Tab Co")

    html = (await client.get(
        f"{base}/assessments?run_id={run.id}", headers=auth_headers(staff.id)
    )).text

    assert f'href="{base}/assessments?' in html
    assert "review=reviewed" in html and "review=unreviewed" in html and "review=all" in html
    assert 'aria-current="page"' in html


async def test_the_review_tab_survives_a_sort_change(client, db_session, admin):
    """The run/sort/lab controls are ONE GET form with no hidden inputs, so a
    param that is not a form field is dropped the moment a select changes."""
    run, _ = await _seed_narrative_row(db_session, project="Sticky Tab Co")
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}&review=all", headers=auth_headers(admin.id)
    )).text
    assert '<input type="hidden" name="review" value="all">' in html


async def test_the_empty_state_names_the_tab_not_the_run(client, db_session, admin):
    """A run whose only row is reviewed, seen from the Unreviewed tab: "no
    assessments stored for this run" is false, and the run menu on the same
    screen says so."""
    run, assessment = await _seed_narrative_row(db_session, project="All Reviewed Co")
    reviewer = await factories.make_user(db_session, email="empty-state-rev@example.org")
    db_session.add(AssessmentReview(
        assessment_id=assessment.id, reviewer_user_id=reviewer.id,
        reviewer_name="R", score=4, comment="ok", feedback_mode="log_only",
    ))
    await db_session.flush()

    html = (await client.get(
        f"/admin/assessments?run_id={run.id}&review=unreviewed", headers=auth_headers(admin.id)
    )).text
    assert "No assessments stored for" not in html
    assert "unreviewed" in html.lower()


async def test_the_admin_surface_forwards_the_review_keys(client, db_session, admin):
    """admin_assessments allowlists every context key; a key added to the
    service and not to that list renders as silently-falsy Undefined here and
    nowhere else."""
    run, _ = await _seed_narrative_row(db_session, project="Allowlist Co")
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    assert "review=all" in html  # the All tab link renders its count
    assert "(1)" in html or "(0)" in html
```

And in `tests/integration/test_reviews_router.py`:

```python
async def test_the_review_tab_round_trips_through_a_feedback_write(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "ok", "feedback_mode": "log_only",
            "surface": "manager-list", "review": "reviewed",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "review=reviewed" in r.headers["location"]


async def test_the_default_review_tab_is_dropped_from_the_redirect(client, db_session):
    """D13: only a non-default value is emitted, which is what keeps the three
    exact-Location assertions in this file byte-identical."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "ok", "feedback_mode": "log_only",
            "surface": "manager-list", "review": "unreviewed",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/assessments#a-{assessment.id}"
```

- [ ] **Step 6: Run them and verify they fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k "review_tab or empty_state or forwards_the_review" tests/integration/test_reviews_router.py -k review_tab -q
```
Expected: FAIL.

- [ ] **Step 7: Wire the routers**

`src/routers/admin.py::admin_assessments` — add `review: str | None = None` to
the signature, pass it to `list_assessments`, and forward **both** new keys
explicitly:

```python
    view = await list_assessments(db, run_id, sort=sort, lab=lab, review=review)
    ...
            review=view["review"],
            review_counts=view["review_counts"],
```

`src/routers/manager.py::manager_assessments` — add the parameter and pass it
through; the `**view` splat carries the keys.

`src/routers/reviews.py::_list_filter_query` — add the parameter and emit it
only when non-default:

```python
def _list_filter_query(
    run_id: str | None, sort: str | None, lab: str | None, review: str | None = None
) -> str:
    ...
    # Emitted only when it is NOT the default: an absent `review` already means
    # `unreviewed`, and emitting it anyway would rewrite three exact-Location
    # assertions in tests/integration/test_reviews_router.py for no gain.
    if review in ASSESSMENT_REVIEW_FILTERS and review != ASSESSMENT_REVIEW_DEFAULT:
        params.append(("review", review))
```

Thread `review: str = Form("")` through `submit_review_feedback` and
`_assessments_redirect` the same way `run_id`/`sort`/`lab` are threaded.

- [ ] **Step 8: Wire the templates**

`templates/admin/_assessments_body.html` — `qs_filters` gains a fifth input:

```jinja
{% macro qs_filters() %}<input type="hidden" name="surface" value="{{ list_surface }}"><input type="hidden" name="run_id" value="{{ 'all' if show_all_runs else selected_run_id }}"><input type="hidden" name="sort" value="{{ sort }}"><input type="hidden" name="lab" value="{{ lab_filter or '' }}"><input type="hidden" name="review" value="{{ review }}">{% endmacro %}
```

Update the header comment (`:246-250`) to name `review` alongside the keys that
macro already reads, so the "NO NEW CONTEXT KEY" prohibition and the code agree.

Replace the empty state (`:573-578`):

```jinja
<div class="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-500">
    {% if review == 'reviewed' %}
    No <strong>reviewed</strong> assessments{{ '' if show_all_runs else ' in this run' }} &mdash;
    {{ review_counts.unreviewed }} still await review on the Unreviewed tab.
    {% elif review == 'unreviewed' %}
    No <strong>unreviewed</strong> assessments{{ '' if show_all_runs else ' in this run' }} &mdash;
    all {{ review_counts.reviewed }} have review feedback. Nothing is owed here.
    {% else %}
    No assessments stored for {{ 'any run' if show_all_runs else 'this run' }} &mdash;
    the run menu shows each run's stored count.
    {% endif %}
</div>
```

Both wrappers (`templates/admin/assessments.html`, and the `/manager` twin with
its own paths) — add the hidden input inside the existing GET form, next to the
three selects:

```jinja
        <input type="hidden" name="review" value="{{ review }}">
```

and the tab strip, above the summary cards, after the truncation note:

```jinja
{# Review sub-tabs (2026-09-21). Links, not a form: they carry the reader's
   current run/sort/lab so changing tab never resets the queue. The run id uses
   the same `'all' if show_all_runs` expression qs_filters uses — passing
   selected_run_id alone renders a UUID for an All-Runs view and silently pins
   the tab to one run. The active tab is marked by aria-current AND by visible
   styling, never by colour alone. #}
<nav class="assessment-review-tabs mb-4 flex flex-wrap items-center gap-2 border-b border-gray-200" aria-label="Review status">
    {% for value, label in [('unreviewed', 'Unreviewed'), ('reviewed', 'Reviewed'), ('all', 'All')] %}
    <a href="/admin/assessments?run_id={{ 'all' if show_all_runs else selected_run_id }}&amp;sort={{ sort }}&amp;lab={{ lab_filter or '' }}&amp;review={{ value }}"
       {% if review == value %}aria-current="page" class="border-b-2 border-indigo-600 px-3 py-2 text-sm font-semibold text-indigo-700"{% else %}class="px-3 py-2 text-sm text-gray-600 hover:text-gray-900"{% endif %}>{{ label }} ({{ review_counts[value] }})</a>
    {% endfor %}
    <span class="ml-2 text-xs text-gray-500">A tab counts written review feedback, not assignment.</span>
</nav>
```

Also add `&amp;review={{ review }}` to the "view all runs" link in both
wrappers, and extend the truncation note in both to name the review filter:

```jinja
        assessments in the current sort order; the rest sit further down that
        order — not excluded by the run, lab or review filter. Re-sort, narrow
        by lab, or switch tab to reach them.
```

- [ ] **Step 9: Fix the two pre-existing tests whose fixture is reviewed**

`tests/integration/test_assessment_queue_controls.py::test_list_pages_show_reviewer_columns`
and
`tests/integration/test_reviewer_role.py::test_reviewer_sees_the_review_columns_on_manager_assessments`
both seed via `_seed_reviewed_row`, which inserts an `AssessmentReview`. Add
`&review=all` to each GET, with a comment:

```python
    # `&review=all`: the default tab is Unreviewed (2026-09-21) and this
    # fixture carries feedback, so it lives on the Reviewed tab now.
```

Then audit the rest: `grep -n "_seed_reviewed_row" tests/ -r` and check every
call site that GETs a list page without a `review` param.

- [ ] **Step 10: Run everything this task touches**

```bash
.venv-test/bin/python -m pytest tests/unit/test_directory_assessment_sorting.py tests/integration/test_assessment_queue_controls.py tests/integration/test_reviewer_role.py tests/integration/test_reviews_router.py tests/integration/test_manager_views.py tests/unit/test_reachability.py -q
```
Expected: PASS, including the three exact-`Location` assertions at
`test_reviews_router.py:822`, `:852`, `:877`, unchanged.

- [ ] **Step 11: Commit**

```bash
git add src/services/assessment_detail.py src/services/directory.py src/routers/admin.py src/routers/manager.py src/routers/reviews.py templates/admin/_assessments_body.html templates/admin/assessments.html templates/manager/assessments.html tests/
git commit -m "feat(assessments): split the queue into reviewed and unreviewed sub-tabs

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: The headline contract

Spec §2. Independent of tasks 1-8.

**Files:**
- Modify: `prompts/roles/scout_hub/phase4-thread-reply.md:230-250`
- Modify: `prompts/roles/scout_hub/role.toml` (`version`)
- Modify: `docs/specs/2026-08-07-hub-bot-prompts.md` (generated)
- Test: `tests/unit/test_headline_contract.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_headline_contract.py`:

```python
"""The headline contract's two mandatory elements (spec 2026-09-21 §2).

A prompt-TEXT assertion, deliberately: the suite drives tests/fakes.py's
FakeAnthropic and never reaches a real model, so nothing here can observe what
the hub actually writes. What it can do is stop the requirement being deleted
or softened without a decision.

Note the "two `Write:` examples" assertion. The unmodified file already had one
`Write:` and one `Not:`, so an "at least two worked examples" check would have
passed against it and detected nothing.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

PROMPT = pathlib.Path("prompts/roles/scout_hub/phase4-thread-reply.md")


def _item_six() -> str:
    text = PROMPT.read_text()
    start = text.index("6. **Headline.**")
    end = text.index("7. **Key points.**", start)
    return text[start:end]


def test_the_headline_item_requires_a_named_disease_area():
    item = _item_six().lower()
    assert "disease area" in item
    assert "patient population" in item


def test_the_headline_item_requires_what_the_intervention_does():
    item = _item_six().lower()
    assert "what it does" in item or "the action" in item
    assert "modality" in item, (
        "the rule must say a modality noun alone does not satisfy it"
    )


def test_the_headline_item_is_modality_neutral():
    """The corpus is mostly diagnostics and platforms, not drugs."""
    item = _item_six()
    assert not re.search(r"\bthe drug\b", item, re.I)


def test_the_headline_item_carries_two_positive_examples():
    assert _item_six().count("Write:") == 2


def test_the_headline_cap_is_unchanged():
    assert "at most 140 characters" in _item_six()
```

- [ ] **Step 2: Run it and verify it fails**

```bash
.venv-test/bin/python -m pytest tests/unit/test_headline_contract.py -q
```
Expected: FAIL on the disease-area, intervention-action and two-examples checks.

- [ ] **Step 3: Rewrite item 6**

Replace `prompts/roles/scout_hub/phase4-thread-reply.md:230-250` with:

```markdown
6. **Headline.** One plain-language sentence of **at most 140 characters**,
   written for a Blackbird reviewer who is not a specialist in this field and
   has never heard of this lab. Three things must be obvious, and the first two
   are mandatory — a headline missing either is not a headline:

   1. **The disease area.** The specific disease, condition or patient
      population this serves, named in words a non-specialist recognises. A
      biomarker panel's disease area is the disease it stratifies, not the
      assay chemistry. If the work is a genuinely disease-agnostic platform,
      name the disease of the FIRST application; "multiple indications" is not
      a disease area.
   2. **What the intervention is, and what it does.** What the thing physically
      is — a pill, an antibody, a blood test, an implant, a screening platform
      — and the action it performs that produces the benefit: blocks an enzyme,
      identifies responders before treatment, kills cells carrying a
      transporter. A modality noun on its own does not satisfy this; name the
      action.
   3. **Why it is fundable.** This may be carried implicitly by naming a
      decision the funder acts on ("predicts which patients will respond"),
      because all three elements rarely fit 140 characters otherwise.

   No colon-stacked noun phrases. No slash-separated alternatives. No
   parenthetical lab or institution suffix — the page already shows the lab
   separately. At most one abbreviation, spelled out on first use; a chain of
   gene symbols is not a headline. This is NOT the project label;
   `company_or_project` already carries that, and both are stored.

       Write: "A blood test taken before treatment that predicts which
       liver-cancer patients will respond to immunotherapy."

       Write: "An oral drug that blocks the enzyme making a brain metabolite
       that builds up to toxic levels in children with Canavan disease."

       Not: "Pre-treatment plasma IL-17F/IL-21/IL-23/IL-8 signature for
       exceptional ICI response in HCC/biliary cancer — real association, but
       no fitted classifier and no demonstrated edge over published IL-8
       alone."

       Not: "Low-coverage-WGS cfDNA fragmentome classifier proposed to identify
       F2-F3 at-risk MASH in the FIB-4 indeterminate zone — the resmetirom
       prescribing gate — on a published, running platform."

   Both `Not:` examples are real headlines this prompt produced: each names a
   method and a disease somewhere inside a noun stack, and neither says in
   plain words what the thing does or who it is for.

   Record it in `headline`.
```

- [ ] **Step 4: Bump the prompt-set version and regenerate the docs**

In `prompts/roles/scout_hub/role.toml`: `version = "1.6.0"`.

```bash
.venv-test/bin/python scripts/sync_prompt_set_docs.py
.venv-test/bin/python scripts/sync_prompt_set_docs.py --check
```
Expected: the second prints no drift.

- [ ] **Step 5: Run the tests**

```bash
.venv-test/bin/python -m pytest tests/unit/test_headline_contract.py tests/unit/test_doc_prompt_sync.py tests/unit/test_rubric_prompt_sync.py -q
```
Expected: PASS. If `test_rubric_prompt_sync` fails, the edit touched the sidecar
skeleton — revert that part; this change adds no key.

- [ ] **Step 6: Commit**

```bash
git add prompts/roles/scout_hub/phase4-thread-reply.md prompts/roles/scout_hub/role.toml docs/specs/2026-08-07-hub-bot-prompts.md tests/unit/test_headline_contract.py
git commit -m "feat(prompts): headline must name the disease area and what the intervention does (scout_hub 1.6.0)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Full gate and deploy note

- [ ] **Step 1: Run the whole gate**

```bash
./scripts/ci.sh
```
Expected: alembic sanity (single head, no duplicate revisions), the
upgrade→downgrade→upgrade round trip, `ruff check` clean on `tests/` and under
the `src/` ceiling, and the full pytest run above the branch-coverage floor.

- [ ] **Step 2: Confirm the shared-body path rule still holds**

```bash
grep -nE '"/(admin|manager)/' templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html
```
Expected: no output.

- [ ] **Step 3: Confirm no snapshot moved**

```bash
git status --porcelain tests/characterization/__snapshots__/
```
Expected: no output. A modified `.ambr` is a finding, not something to accept.

- [ ] **Step 4: Record the deploy shape in the commit message of a final
      docs-only commit, if anything in the spec changed during execution**

No migration. Both images rebuild; the agent rebuild is **mandatory** because
of the prompt-set bump:

```
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC --profile agent build agent
$DC up -d blackbird-app worker
$DC up -d agent          # supervisor returns IDLE
```

**Do not start the simulation** — that is a separate, explicit operator action
from `/admin/simulation`.
