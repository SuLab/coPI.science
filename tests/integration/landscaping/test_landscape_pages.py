"""The Landscape workspace pages (spec §13.1, §13.2, §13.4, §12.5; D46, D63) and the remediated
markup of docs/plans/2026-11-19-landscape-ui-remediation-plan.md (Task A6): the card, the
detail page's Markdown, sources and deal-breakers, and the pipeline page's sentences.

Seed data comes from ``seed_landscape_opportunity`` (the rendered-page gate's seed); every name
is invented. The pipeline seeds below (``seed_cut``, ``seed_source``, ``seed_reject_audit``)
are shared with ``test_landscape_projection.py``.
"""
import re
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeConsult,
    LandscapeCut,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeOpportunityVersion,
    LandscapeRejectAudit,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeScreen,
    LandscapeSource,
)
from src.routers import workspace
from src.routers.workspace import landscape as landscape_pages
from src.services import landscape_view
from src.services.blackbird_rubric import BANDING
from src.web.navigation import WORKSPACE_NAVIGATION, navigation_for
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_rendered_page_gate import (
    GATE_DOC_DOI,
    GATE_DOC_TITLE,
    GATE_QUOTE,
    page_problems,
    seed_landscape_opportunity,
)

pytestmark = pytest.mark.integration

_LIST = "/workspace/landscape"
_PIPELINE = "/workspace/landscape/pipeline"
_OVERRIDE_LINE = "These criteria went into use by an admin override after a failed check."


def _routes(router):
    for route in router.routes:
        context = getattr(route, "include_context", None)
        if context:
            yield from _routes(context.included_router)
        else:
            yield route


def _landscape_routes():
    return [r for r in _routes(workspace.router) if "/landscape" in getattr(r, "path", "")]


async def _get(client, user, path):
    return await client.get(path, headers=auth_headers(user.id), follow_redirects=False)


# --- seed and page helpers ---------------------------------------------------------------------


async def active_epoch(db) -> LandscapeEpoch:
    return await db.scalar(select(LandscapeEpoch).where(LandscapeEpoch.status == "active"))


async def _assessment(db, opportunity_id) -> LandscapeAssessment:
    return await db.scalar(
        select(LandscapeAssessment).where(LandscapeAssessment.opportunity_id == opportunity_id)
    )


async def seed_cut(db, opportunity_id) -> LandscapeCut:
    """A complete cut of the active epoch (40 ideas ranked, 12 kept) that grouped its ideas
    into the seeded opportunity alone."""
    epoch = await active_epoch(db)
    cut = LandscapeCut(id=uuid.uuid4(), epoch_id=epoch.id, status="complete", n=40, k=12,
                       completed_at=datetime.now(UTC))
    db.add(cut)
    await db.flush()
    version = await db.scalar(select(LandscapeOpportunityVersion).where(
        LandscapeOpportunityVersion.opportunity_id == opportunity_id
    ))
    version.cut_id = cut.id
    await db.flush()
    return cut


async def seed_source(db) -> LandscapeSource:
    """A discovery source whose last slice came back half complete."""
    source = LandscapeSource(source=f"gate-{uuid.uuid4().hex[:8]}", enabled=True,
                             last_completeness_ratio=0.5)
    db.add(source)
    await db.flush()
    return source


async def seed_reject_audit(db) -> LandscapeRejectAudit:
    """A screened-out document whose reject audit disagreed, in the active epoch."""
    epoch = await active_epoch(db)
    doc = LandscapeDocument(
        id=uuid.uuid4(), kind="work", canonical_external_id=f"gate-audit:{uuid.uuid4().hex}",
        title="Gate audited paper", s1_status="eligible",
    )
    db.add(doc)
    await db.flush()
    screen = LandscapeScreen(id=uuid.uuid4(), document_id=doc.id, epoch_id=epoch.id,
                             status="excluded:screen", reason="Gate screen reason.")
    db.add(screen)
    await db.flush()
    audit = LandscapeRejectAudit(
        id=uuid.uuid4(), item_kind="screen", item_id=screen.id, epoch_id=epoch.id,
        status="done", agree=False, original_decision="exclude", audit_decision="include",
        reason="Gate audit reason.",
    )
    db.add(audit)
    await db.flush()
    return audit


async def seed_held_then_overridden(db) -> None:
    """The active epoch held by its check and then overridden, beside a candidate set still
    held."""
    epoch = await active_epoch(db)
    epoch.hold_reason = "Gate regression held."
    epoch.override_reason = "Gate override reason."
    epoch.override_at = datetime.now(UTC)
    db.add(LandscapeEpoch(id=uuid.uuid4(), status="held", criteria_version="gate-2",
                          prompt_set_version="gate-2", hold_reason="Gate still held."))
    await db.flush()


def _main(html: str) -> str:
    """The page's ``<main>``, where the page's own content is."""
    return html[html.index('<main id="main-content" class='):html.index("</main>")]


def _section(html: str, marker: str, end: str = "</section>") -> str:
    """From `marker` to the first `end` after it."""
    start = html.index(marker)
    return html[start:html.index(end, start)]


def _week_card(html: str, opportunity_id) -> str:
    """This week's card for the opportunity, without its decision form (whose reason tags
    come from the criteria file)."""
    return _section(html, f'id="o-week-{opportunity_id}"', 'class="landscape-decision')


class _Text(HTMLParser):
    """A document's text outside tags (scripts and styles skipped) and its ``data-markdown``
    attribute values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.markdown: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        self.markdown += [value or "" for name, value in attrs if name == "data-markdown"]

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


def _parse(html: str) -> _Text:
    parser = _Text()
    parser.feed(html)
    return parser


def _plain(html: str) -> str:
    return " ".join(" ".join(_parse(html).text).split())


# --- routes and access -------------------------------------------------------------------------


def test_pipeline_route_not_shadowed_by_detail():
    names = [r.name for r in _landscape_routes()]
    assert names.index("workspace_landscape_pipeline") < names.index("workspace_landscape_detail")
    detail = next(r for r in _landscape_routes() if r.name == "workspace_landscape_detail")
    # The UUID converter alone would refuse "pipeline"; the order pins it twice (D46).
    assert detail.path.endswith("{opportunity_id:uuid}")


async def test_pipeline_path_reaches_the_pipeline_handler(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    response = await _get(client, manager, _PIPELINE)
    assert response.status_code == 200
    assert "Landscape pipeline" in response.text


async def test_pi_gets_403(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    for path in (_LIST, _PIPELINE, f"{_LIST}/{opportunity_id}"):
        assert (await _get(client, pi, path)).status_code == 403, path


async def test_reviewer_sees_batch_not_lists(client, db_session):
    await seed_landscape_opportunity(db_session)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    response = await _get(client, reviewer, f"{_LIST}?view=current")
    assert response.status_code == 200
    assert "This week&#39;s batch" in response.text or "This week's batch" in response.text
    assert "A gate headline" in response.text
    # No link to the staff lists (the page's own URL still appears in base.html's help
    # dismiss form, as its `next` value).
    assert f'href="{_LIST}?view=current"' not in response.text
    assert "All assessed" not in response.text
    assert (await _get(client, reviewer, _PIPELINE)).status_code == 403

    staff = await _get(client, manager, f"{_LIST}?view=current")
    assert staff.status_code == 200
    assert "Unreviewed (" in staff.text and "All assessed" in staff.text


async def test_nav_item_for_reviewers_and_staff(client, db_session):
    item = next(i for i in WORKSPACE_NAVIGATION if i.section == "landscape")
    assert (item.label, item.route_name) == ("Landscape", "workspace_landscape")
    assert not item.staff_only and not item.admin_only
    for role in (USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN):
        user = await factories.make_user(db_session, user_role=role)
        response = await _get(client, user, "/workspace/assessments")
        assert f'href="{_LIST}"' in response.text, role
    no_research = SimpleNamespace(research=False, staff=False, admin=False)
    assert navigation_for(None, no_research) == ()


# --- the detail page ---------------------------------------------------------------------------


async def test_detail_renders_member_sections_and_badges(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    response = await _get(client, admin, f"{_LIST}/{opportunity_id}")
    assert response.status_code == 200
    html = response.text
    for anchor in ("brief", "verdict", "gating", "scores", "evidence", "disease-profile",
                   "panel", "reviews", "review"):
        assert f'id="{anchor}"' in html, anchor
    for text in ("Gate programme", "A gate headline", "Target biology", "GATEK drives fibrosis.",
                 "Disease profile", "Rare", "Gate concern", "target essential", "cleared",
                 "A gate experiment plan.", "Gate red flag", "Raw verdict JSON", GATE_QUOTE):
        assert text in html, text
    # The support badges (templates/landscape/_signals.html): a document reference is green
    # "sourced" and names the document with its DOI link; "estimate" stays grey.
    assert 'class="landscape-support landscape-support-sourced' in html
    assert 'class="landscape-support landscape-support-estimate' in html
    assert ">sourced</span>" in html and ">estimate</span>" in html
    assert f'href="https://doi.org/{GATE_DOC_DOI}"' in html
    assert f">{GATE_DOC_TITLE}</a>" in html
    assert f'action="/reviews/landscape/{opportunity_id}/decision"' in html


async def test_expert_form_only_on_own_sample_item(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    expert = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    other = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    db_session.add(LandscapeExpertJudgment(
        id=uuid.uuid4(), opportunity_id=opportunity_id, expert_id=expert.id,
        expert_name=expert.name, quarter=landscape_view._quarter(datetime.now(UTC)),
    ))
    await db_session.flush()
    action = f'action="/reviews/landscape/{opportunity_id}/expert-judgment"'

    own = await _get(client, expert, f"{_LIST}/{opportunity_id}")
    assert own.status_code == 200 and action in own.text
    # S9's calls are hidden on the expert's own open item (D63).
    assert "Gate score rationale." not in own.text

    theirs = await _get(client, other, f"{_LIST}/{opportunity_id}")
    assert theirs.status_code == 200 and action not in theirs.text


async def test_expert_red_flags_section_says_withheld_not_none(client, db_session):
    """On an expert's open item the red flags are S9's calls, withheld: the section must not
    claim S9 recorded none (D63)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    expert = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    db_session.add(LandscapeExpertJudgment(
        id=uuid.uuid4(), opportunity_id=opportunity_id, expert_id=expert.id,
        expert_name=expert.name, quarter=landscape_view._quarter(datetime.now(UTC)),
    ))
    await db_session.flush()

    html = (await _get(client, expert, f"{_LIST}/{opportunity_id}")).text
    red_flags = html.split('class="landscape-red-flags', 1)[1].split("</div>", 1)[0]
    assert "None recorded." not in red_flags and "Gate red flag" not in red_flags
    assert "hidden until you submit your judgment" in red_flags


async def test_multi_member_page_links_ideas(client, db_session):
    """Both member ideas render under their "Idea N" ids, and the Evidence summary's row for
    member 2's panel opinion links to it (Review Focus 4)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, f"{_LIST}/{opportunity_id}")).text
    assert 'id="idea-1"' in html and 'id="idea-2"' in html
    evidence = _section(html, 'id="evidence"', 'id="verdict"')
    assert 'href="#idea-2"' in evidence
    assert "Gate budget concern" in evidence
    # The first member keeps the chat record's anchors; the second's are numbered.
    assert 'id="panel-2"' in html and 'id="disease-profile-2"' in html
    assert "Gate Lab One" in html and "Gate Lab Two" in html


async def test_no_literal_markdown(client, db_session):
    """The Markdown rationale reaches the page only as ``data-markdown`` for the client-side
    renderer; no ``**`` shows as text."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, f"{_LIST}/{opportunity_id}")).text
    parsed = _parse(_main(html))
    assert any("**Gate rationale.**" in value for value in parsed.markdown)
    assert any("A gate experiment plan." in value for value in parsed.markdown)
    assert "**" not in "".join(parsed.text)


async def test_no_raw_json_for_manager(client, db_session):
    """The raw flag and database evidence, the raw verdict and the panel's raw opinions are an
    admin's only (contract C6)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    managed = _main((await _get(client, manager, f"{_LIST}/{opportunity_id}")).text)
    assert "<pre" not in managed
    for text in ("Gate essentiality screen", "Gate evidence", "Raw flag evidence",
                 "Database evidence", "Raw verdict JSON", "Gate raw opinion"):
        assert text not in managed, text

    admins = _main((await _get(client, admin, f"{_LIST}/{opportunity_id}")).text)
    assert "<pre" in admins
    for text in ("Raw flag evidence (admin)", "Database evidence (admin)", "Raw verdict JSON",
                 "Gate essentiality screen", "Gate evidence"):
        assert text in admins, text


async def test_no_doc_tokens_in_text(client, db_session):
    """A ``doc:<8 hex>`` reference in the S6 summary reads as the document's title (C4)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, f"{_LIST}/{opportunity_id}")).text
    assert f"Gate S6 summary, after {GATE_DOC_TITLE}" in html
    assert not re.search(r"doc:[0-9a-f]{8}", html, re.IGNORECASE)


async def test_approve_buttons_removed(client, db_session):
    """No Approve / Disapprove / Clear form on the detail page (D89); the status route stays
    without a caller (tests/unit/test_reachability.py ROUTE_ALLOWLIST)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    for role in (USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN):
        user = await factories.make_user(db_session, user_role=role)
        html = (await _get(client, user, f"{_LIST}/{opportunity_id}")).text
        assert '/status"' not in html, role
        assert ">Approve<" not in html and ">Disapprove<" not in html, role


async def test_override_line_markup(client, db_session):
    """The Verdict says the criteria went into use by an override only when the active epoch
    was overridden (C10)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    path = f"{_LIST}/{opportunity_id}"
    assert _OVERRIDE_LINE not in (await _get(client, manager, path)).text

    await seed_held_then_overridden(db_session)
    assert _OVERRIDE_LINE in (await _get(client, manager, path)).text


# --- the card ----------------------------------------------------------------------------------


async def test_card_b_markup(client, db_session):
    """The card's chip prints the rubric's pass label, the rank never shows, and the score sits
    behind "Why this score" (C1, C2)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    row = await _assessment(db_session, opportunity_id)
    row.recommendation = "pass"
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, _LIST)).text
    card = _week_card(html, opportunity_id)
    chip = re.search(r'class="landscape-recommendation[^"]*">([^<]*)</span>', card)
    assert chip is not None
    assert chip.group(1) == BANDING["pass_label"] == "decline"
    assert not re.search(r"\brank\b", _plain(card), re.IGNORECASE)
    assert re.search(r"<summary[^>]*>Why this score</summary>", card)
    assert "Gate score rationale." in card
    # The cost line is the ask's bold first line without its markers.
    assert re.search(r'class="landscape-cost-line[^"]*">\$1 · 1 month — a gate experiment\.<', card)


async def test_blind_card_markup(client, db_session):
    """An undecided blind card shows no recommendation chip, no flag counts and no score; its
    cost line still shows (C2)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    batch = await db_session.scalar(
        select(LandscapeReviewBatch).join(
            LandscapeReviewBatchItem, LandscapeReviewBatchItem.batch_id == LandscapeReviewBatch.id
        ).where(LandscapeReviewBatchItem.opportunity_id == opportunity_id)
    )
    batch.blind = True
    await db_session.flush()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    html = (await _get(client, reviewer, _LIST)).text
    card = _week_card(html, opportunity_id)
    text = _plain(card)
    assert "landscape-recommendation" not in card
    assert "red flag" not in text and "deal-breaker" not in text
    assert "Why this score" not in text
    assert "landscape-blind-note" in card
    assert "landscape-cost-line" in card and "1 month" in text


async def test_compact_card_markup(client, db_session):
    """The comparison page (D95): the compact card prints the pitch's first two sentences, the
    cost line and, in grey beside the chip, the score and band; no key points, no "Why this
    score" and no rank. Every link and form keeps ``cards=compact``; the default page keeps the
    full card."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    row = await _assessment(db_session, opportunity_id)
    row.recommendation = "pass"
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    listed = await _get(client, manager, f"{_LIST}?cards=compact")
    assert listed.status_code == 200
    html = listed.text
    assert page_problems(html) == []
    assert re.search(r'aria-current="page"[^>]*>Compact</a>', html)
    card = _week_card(html, opportunity_id)
    text = _plain(card)
    assert "landscape-card-compact" in card
    chip = re.search(r'class="landscape-recommendation[^"]*">([^<]*)</span>', card)
    assert chip is not None and chip.group(1) == BANDING["pass_label"]
    assert re.search(r'class="landscape-compact-score[^"]*">score \d\.\d\d', card)
    assert "First gate sentence." in card and "Second gate sentence." in card
    assert "Third." not in card
    assert re.search(r'class="landscape-cost-line[^"]*">\$1 · 1 month — a gate experiment\.<', card)
    assert "Why this score" not in text and "landscape-card-points" not in card
    assert not re.search(r"\brank\b", text, re.IGNORECASE)
    assert f"/workspace/landscape/{opportunity_id}?cards=compact" in card
    form = _section(html, f'id="o-week-{opportunity_id}"', "</form>")
    assert '<input type="hidden" name="cards" value="compact">' in form

    current = (await _get(client, manager, f"{_LIST}?view=current&cards=compact")).text
    assert page_problems(current) == []
    tabs = _section(current, 'class="landscape-tabs', "</nav>")
    assert tabs.count("cards=compact") == 3
    views = _section(current, 'class="landscape-views', "</nav>")
    assert views.count("cards=compact") == 3
    get_form = _section(current, 'action="/workspace/landscape"', "</form>")
    assert '<input type="hidden" name="cards" value="compact">' in get_form

    full = (await _get(client, manager, _LIST)).text
    assert "landscape-card-compact" not in full
    assert "landscape-card-pitch" in _week_card(full, opportunity_id)
    assert re.search(r'aria-current="page"[^>]*>Full</a>', full)
    assert "cards=compact" not in _section(full, 'class="landscape-views', "</nav>")


async def test_blind_compact_card_markup(client, db_session):
    """An undecided blind card in the compact style hides the same values as the full card:
    no chip, no score or band, no flag counts; the pitch and cost line still show (D95)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    batch = await db_session.scalar(
        select(LandscapeReviewBatch).join(
            LandscapeReviewBatchItem, LandscapeReviewBatchItem.batch_id == LandscapeReviewBatch.id
        ).where(LandscapeReviewBatchItem.opportunity_id == opportunity_id)
    )
    batch.blind = True
    await db_session.flush()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    html = (await _get(client, reviewer, f"{_LIST}?cards=compact")).text
    card = _week_card(html, opportunity_id)
    text = _plain(card)
    assert "landscape-card-compact" in card
    assert "landscape-recommendation" not in card and "landscape-compact-score" not in card
    assert "red flag" not in text and "deal-breaker" not in text
    assert "landscape-blind-note" in card
    assert "landscape-cost-line" in card and "First gate sentence." in card


async def test_card_page_partial_row_renders(client, db_session):
    """A partial S9 row renders its card and page: no pitch, no ask that opens in bold, key
    points that are not a dict or outnumber their support (Review Focus 1)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    row = await _assessment(db_session, opportunity_id)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    variants = (
        {"elevator_pitch": None, "recommended_next_experiment": "A plain ask.",
         # `proposal` is one of the groups key_point_sections renders; `why_it_matters` is not.
         "key_points": {"proposal": ["A gate point.", "A second gate point."]},
         "claim_support": {"proposal": ["pmid:31500698"]}},
        {"elevator_pitch": None, "recommended_next_experiment": None, "key_points": "not a dict",
         "claim_support": None, "score_rationale": None},
    )
    for n, values in enumerate(variants):
        for key, value in values.items():
            setattr(row, key, value)
        await db_session.flush()

        listed = await _get(client, manager, _LIST)
        assert listed.status_code == 200, n
        card = _week_card(listed.text, opportunity_id)
        assert "landscape-card-pitch" not in card and "landscape-cost-line" not in card, n
        assert "landscape-open-link" in card, n
        if n == 0:
            assert "A second gate point." in card
        else:
            assert "landscape-card-points" not in card
        assert page_problems(listed.text) == [], n

        detail = await _get(client, manager, f"{_LIST}/{opportunity_id}")
        assert detail.status_code == 200, n
        assert page_problems(detail.text) == [], n
        brief = _section(detail.text, 'id="brief"', 'id="evidence"')
        assert "In one minute" not in brief, n
        if n == 0:
            # Two bullets, one support entry: the second bullet's badge is an estimate.
            assert brief.count("landscape-support-sourced") == 1
            assert brief.count("landscape-support-estimate") == 1
        else:
            assert "landscape-support" not in brief


# --- the pipeline page -------------------------------------------------------------------------


async def test_pipeline_cut_sentence(client, db_session):
    """The latest cut reads as a sentence on the pipeline page and on the staff lists (C7)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    await seed_cut(db_session, opportunity_id)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pipeline = _plain((await _get(client, manager, _PIPELINE)).text)
    assert re.search(r"Latest cut: 40 ideas ranked, 12 kept, 1 opportunit", pipeline)
    listed = _plain((await _get(client, manager, f"{_LIST}?view=current")).text)
    assert re.search(r"12 ideas kept of 40, grouped into 1 opportunit", listed)


async def test_pipeline_held_then_overridden(client, db_session):
    """An epoch held and then overridden reads "Held, then overridden" with its reasons folded
    away; one still held keeps its amber line (C11)."""
    await seed_landscape_opportunity(db_session)
    await seed_held_then_overridden(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, _PIPELINE)).text
    epochs = _section(html, 'class="landscape-epochs')
    assert "Held, then overridden" in epochs
    folded = _section(epochs, "<details>", "</details>")
    assert "Held: Gate regression held." in folded
    assert "Overridden: Gate override reason." in folded
    assert '<p class="text-amber-800">Held: Gate still held.</p>' in epochs
    assert '<p class="text-amber-800">Held: Gate regression held.' not in epochs


async def test_pipeline_last_slice_label(client, db_session):
    """The source table says its completeness is the last slice's (§13.4)."""
    await seed_landscape_opportunity(db_session)
    await seed_source(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, _PIPELINE)).text
    sources = _section(html, 'class="landscape-sources')
    assert "<th" in sources and "Completeness (last slice)" in sources
    assert ">50%<" in sources


async def test_pipeline_audit_label(client, db_session):
    """A reject-audit disagreement names its item as the filtered lists do (C11)."""
    await seed_landscape_opportunity(db_session)
    await seed_reject_audit(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, _PIPELINE)).text
    audit = _section(html, 'class="landscape-audit')
    assert '<span class="font-medium">Gate audited paper</span>' in audit
    assert "Gate audit reason." in audit
    assert page_problems(html) == []


# --- schema, gate and routes -------------------------------------------------------------------


async def test_pages_503_without_0077(client, db_session, monkeypatch):
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    async def not_ready(db):
        return False

    monkeypatch.setattr(landscape_pages, "schema_ready", not_ready)
    monkeypatch.setitem(landscape_pages._SCHEMA, "ready", False)
    for path in (_LIST, _PIPELINE, f"{_LIST}/{opportunity_id}"):
        assert (await _get(client, manager, path)).status_code == 503, path


async def test_rendered_gate_passes(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    problems: list[str] = []
    for role in (USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN):
        user = await factories.make_user(db_session, user_role=role)
        paths = [_LIST, f"{_LIST}/{opportunity_id}"]
        if role != USER_ROLE_REVIEWER:
            paths += [f"{_LIST}?view=current", f"{_LIST}?view=all&tab=all", _PIPELINE]
        for path in paths:
            response = await _get(client, user, path)
            assert response.status_code == 200, (role, path, response.status_code)
            problems += [f"{role} {path}: {p}" for p in page_problems(response.text)]
    assert not problems, "\n".join(problems)


def test_no_post_on_workspace_router():
    routes = _landscape_routes()
    assert {r.name for r in routes} == {
        "workspace_landscape", "workspace_landscape_pipeline", "workspace_landscape_detail",
    }
    for route in routes:
        assert set(route.methods) - {"HEAD"} == {"GET"}, route.path


async def test_budget_line_shows_concern_for_every_signal(client, db_session):
    """The budget specialist's first concern shows beside The ask whatever its signal, so a
    cost the panel disputed is visible even when it signalled "adequate" (audit D1, R13)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    for consult in (await db_session.scalars(select(LandscapeConsult))).all():
        if consult.domain.startswith("budget"):
            opinion = dict(consult.opinion or {})
            opinion["verdict_signal"] = "adequate"
            opinion["concerns"] = ["Closer to twice the asked budget."]
            consult.opinion = opinion
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    html = (await _get(client, manager, f"{_LIST}/{opportunity_id}")).text
    ask = _section(html, 'id="ask"', 'id="verdict"')
    assert "Budget specialist" in ask and "adequate" in ask
    assert "Closer to twice the asked budget." in ask


async def test_ask_bold_line_then_single_newline_has_disclosure(client, db_session):
    """An ask whose bold cost line is followed by a single newline still puts the rest behind
    "Show the full plan" (audit D10)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    row = await _assessment(db_session, opportunity_id)
    row.recommended_next_experiment = "**$1 · 1 month — a gate study.**\nThe gate plan body."
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    ask = _section((await _get(client, manager, f"{_LIST}/{opportunity_id}")).text,
                   'id="ask"', 'id="verdict"')
    assert "Show the full plan" in ask


async def test_ask_wrapped_bold_line_keeps_paragraph_split(client, db_session):
    """A bold cost line that wraps onto a second line is split at the paragraph, so no
    unclosed `**` reaches the rendered first part (re-audit, D10 edge)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    row = await _assessment(db_session, opportunity_id)
    row.recommended_next_experiment = "**$1 ·\n1 month — a gate study.**\n\nThe gate plan body."
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    ask = _section((await _get(client, manager, f"{_LIST}/{opportunity_id}")).text,
                   'id="ask"', 'id="verdict"')
    assert "Show the full plan" in ask
    first = ask[:ask.index("Show the full plan")]
    assert "1 month — a gate study.**" in first.replace("&#34;", '"')
