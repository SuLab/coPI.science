"""The page-parity rule (spec §4.1, D2, §11.2): each tier's chat record carries only
what that tier's detail page renders.

A fixture rubric with sentinels stands in for the live document at every binding the
page and the record read, and every stored value carries a sentinel, so the test can
say exactly what a record may and may not contain:

(1) containment — every token of every quoted record line occurs in the page's
    rendered ``<main>`` text (text nodes plus data-markdown, title and href values);
(2) the staff-only verdict fields are in the staff record and not the reviewer's;
(3) the rubric anchors and gate definitions the page renders are in the record;
(4) what no page renders is in no record;
(5) every anchor a block cites resolves to a rendered element id on that tier's page —
    otherwise a renamed template id would silently turn "Show in page" into a no-op.

Tokens rather than lines, because the page reflows prose and rewrites URLs into
"cited paper" links; containment of tokens is still what fails the moment the
builder gains a field the page does not render. The corpus is scoped to ``<main>``
(nav/head chrome is not something the record may cite anyway, and including it would
let an unrelated match hide a real leak).
"""

import dataclasses
import re
import time
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    LlmCallLog,
    OpportunityAssessment,
    SpecialistConsult,
)
from src.services import assessment_detail, rubric_revisions
from src.services.assessment_chat_record import load_chat_record, quoted_lines
from src.services.blackbird_rubric import load_rubric
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

CHANNEL = "chat-parity-channel"
HUB = "blackbird"
SUBJECT = "vogelstein"
RECORD_URL = "https://doi.org/10.1000/parity-url"

STAFF_ONLY = (
    "PARITY-HUB-STRENGTH",
    "PARITY-HUB-RISK",
    "PARITY-HUB-LANDSCAPE",
    "PARITY-HUB-MATURITY",
)
EXCLUDED = (
    "PARITY-RAW-VERDICT",
    "PARITY-RAW-OPINION",
    "PARITY-CONTEXT-EXCERPT",
    "PARITY-EST-NONLATEST",
    "PARITY-EST-FOURTH",
    "PARITY-AGENT-SENDER",
    "PARITY-TOOL-LOG",
    "PARITY-OTHER-INTERVIEW",
    "PARITY-OTHER-CONSULT",
    "PARITY-EXCLUDED-INTRO",
    "PARITY-EXCLUDED-PREAMBLE",
    "PARITY-EXCLUDED-RFINTRO",
    "PARITY-EXCLUDED-RFGUIDE",
    "PARITY-EXCLUDED-RECOMMENDATION",
    "PARITY-EXCLUDED-HEURISTIC",
    "PARITY-EXCLUDED-BANDING",
    "PARITY-EXCLUDED-EVIDENCE",
)
PAGES = {
    "admin-as-admin": (USER_ROLE_ADMIN, "admin", "staff"),
    "manager-as-manager": (USER_ROLE_MANAGER, "manager", "staff"),
    "manager-as-reviewer": (USER_ROLE_REVIEWER, "manager", "reviewer"),
}
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


@pytest.fixture
def fixture_rubric(monkeypatch):
    """The live rubric with sentinels in every text field, patched in at each
    binding the detail service reads (assessment_detail.py:60 binds three names at
    import; rubric_revisions.live_revision_view calls its own load_rubric)."""
    live = load_rubric()
    rubric = dataclasses.replace(
        live,
        version="9.9.9",
        content_hash="feedfacecafe",
        intro="PARITY-EXCLUDED-INTRO",
        scoring_preamble="PARITY-EXCLUDED-PREAMBLE",
        red_flags_intro="PARITY-EXCLUDED-RFINTRO",
        red_flags=("PARITY-EXCLUDED-RFGUIDE",),
        recommendation="PARITY-EXCLUDED-RECOMMENDATION",
        heuristic="PARITY-EXCLUDED-HEURISTIC",
        banding_semantics="PARITY-EXCLUDED-BANDING",
        gating={
            key: {"title": f"Parity gate {key}", "description": f"PARITY-GATEDESC-{key.upper()}"}
            for key in live.gating
        },
        dimensions=tuple(
            dataclasses.replace(
                d,
                anchors=f"PARITY-ANCHOR-{d.key.upper()} one is weak and five is strong",
                evidence=("PARITY-EXCLUDED-EVIDENCE",),
            )
            for d in live.dimensions
        ),
    )
    monkeypatch.setattr(assessment_detail, "load_rubric", lambda: rubric)
    monkeypatch.setattr(assessment_detail, "RUBRIC_VERSION", rubric.version)
    monkeypatch.setattr(
        assessment_detail,
        "BANDING",
        {
            "advance_min": rubric.advance_min,
            "conditional_min": rubric.conditional_min,
            "pass_label": rubric.pass_label,
        },
    )
    monkeypatch.setattr(rubric_revisions, "load_rubric", lambda: rubric)
    return rubric


async def _seed(db_session, rubric, *, prose_format):
    run = await factories.make_simulation_run(db_session)
    base = time.time() - 7200
    root = f"{base:.6f}"
    verdict_ts = f"{base + 240:.6f}"
    messages = [
        dict(agent_id=SUBJECT, message_ts=root, thread_ts=None, phase="new_post", posted_at=base,
             content=f"PARITY-PITCH-MESSAGE our isogenic panel, see {RECORD_URL}.",
             sender_name="PARITY-AGENT-SENDER"),
        dict(agent_id=HUB, message_ts=f"{base + 60:.6f}", thread_ts=root, phase="thread_reply",
             posted_at=base + 60, content="PARITY-HUB-QUESTION about the controls?"),
        dict(agent_id=None, message_ts=f"{base + 120:.6f}", thread_ts=root, phase="thread_reply",
             posted_at=base + 120, content="PARITY-HUMAN-POST a comment from a person",
             sender_name="PARITY-HUMAN-POSTER", is_bot=False),
        dict(agent_id="otherlab", message_ts=f"{base + 180:.6f}", thread_ts=root,
             phase="thread_reply", posted_at=base + 180, content="PARITY-OTHER-AGENT-REPLY"),
        dict(agent_id=HUB, message_ts=verdict_ts, thread_ts=root, phase="thread_reply",
             posted_at=base + 240, content="PARITY-VERDICT-REPLY conditional."),
    ]
    for fields in messages:
        await factories.make_agent_message(db_session, run=run, channel_name=CHANNEL, **fields)

    # A second interview in the same channel: none of it may reach this record.
    other_root = f"{base + 1000:.6f}"
    await factories.make_agent_message(
        db_session, run=run, agent_id="otherlab", channel_name=CHANNEL, message_ts=other_root,
        phase="new_post", posted_at=base + 1000, content="PARITY-OTHER-INTERVIEW pitch",
    )

    def consult(offset, thread=root, **fields):
        data = dict(
            simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT, thread_id=thread,
            channel_name=CHANNEL, raw_opinion="PARITY-RAW-OPINION",
            context_excerpt="PARITY-CONTEXT-EXCERPT", read_state="parsed",
            created_at=datetime.fromtimestamp(base + offset, UTC),
        )
        data.update(fields)
        return SpecialistConsult(**data)

    db_session.add_all([
        consult(70, domain="clinical", question="PARITY-ASKED-EARLY", verdict_signal="adequate",
                confidence="high", concerns=["PARITY-CONCERN-EARLY"],
                questions_to_ask=["PARITY-QTA-EARLY"], established=["PARITY-EST-NONLATEST"]),
        consult(90, domain="clinical", question="PARITY-ASKED-LATEST", verdict_signal="adequate",
                confidence="moderate", concerns=["PARITY-CONCERN-LATEST"],
                questions_to_ask=["PARITY-QTA-LATEST"],
                established=["PARITY-EST-ONE", "PARITY-EST-TWO", "PARITY-EST-THREE",
                             "PARITY-EST-FOURTH"]),
        consult(100, domain="legal", question="PARITY-ASKED-CUT", verdict_signal="gap",
                confidence="moderate", concerns=["PARITY-CONCERN-CUT"], questions_to_ask=[],
                truncated=True, read_state="truncated"),
        consult(1010, thread=other_root, domain="clinical", question="PARITY-OTHER-CONSULT",
                verdict_signal="gap", confidence="low", concerns=["PARITY-OTHER-CONSULT concern"],
                questions_to_ask=[]),
    ])
    # The hub's logged tool turn for this thread: admin-page drill-down only.
    db_session.add(LlmCallLog(
        simulation_run_id=run.id, agent_id=HUB, phase="thread_reply", channel=CHANNEL,
        model="claude-test", system_prompt="PARITY-TOOL-LOG system",
        messages_json=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
             "name": "search_prior_art", "input": {"query": "PARITY-TOOL-LOG query"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
             "content": "PARITY-TOOL-LOG result"}]},
        ],
        response_text="<slack_message>\nPARITY-HUB-QUESTION about the controls?\n</slack_message>",
        created_at=datetime.fromtimestamp(base + 60, UTC),
    ))
    dims = [d.key for d in rubric.dimensions]
    assessment = OpportunityAssessment(
        id=uuid.uuid4(),
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=SUBJECT, channel_name=CHANNEL,
        slack_ts=verdict_ts, thread_id=root,
        company_or_project="PARITY-PROJECT-LABEL",
        headline="PARITY-HEADLINE an isogenic panel",
        elevator_pitch=f"PARITY-PITCH-FIELD first sentence; it cites {RECORD_URL}.",
        key_points={"significance": ["PARITY-KP-SIGNIFICANCE"], "key_questions": ["PARITY-KP-QUESTIONS"]},
        score_rationale="PARITY-SCORE-RATIONALE",
        strengths=["PARITY-HUB-STRENGTH"],
        risks=["PARITY-HUB-RISK"],
        competitive_landscape=["PARITY-HUB-LANDSCAPE"],
        evidence_maturity=["PARITY-HUB-MATURITY"],
        recommended_next_experiment="PARITY-ASK-ONE\n\nPARITY-ASK-TWO",
        recommendation="conditional", confidence="Moderate", weighted_score=3.2, band="conditional",
        gating={"life_sciences_domain": "met", "credible_science": "not_met",
                "translational_potential": "unconfirmed"},
        scores={dims[0]: 4, dims[1]: 2},
        red_flags=["PARITY-RED-FLAG"],
        rationale="PARITY-RATIONALE-ONE\n\nPARITY-RATIONALE-TWO",
        raw_verdict={"sentinel": "PARITY-RAW-VERDICT"},
        panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True,
        rubric_version=rubric.version, rubric_content_hash=rubric.content_hash,
        prose_format=prose_format,
    )
    db_session.add(assessment)
    await db_session.flush()
    db_session.add(AssessmentReview(
        assessment_id=assessment.id, reviewer_user_id=None, reviewer_name="PARITY-REVIEWER",
        score=4, dimension_scores={dims[0]: 5}, rubric_version=rubric.version,
        rubric_content_hash=rubric.content_hash, comment="PARITY-REVIEW-COMMENT",
        feedback_mode="learn",
    ))
    await db_session.flush()
    return assessment.id


class _Corpus(HTMLParser):
    """A page's rendered ``<main>`` text: text nodes plus data-markdown, title and
    href values, HTML-unescaped, scoped to the ``<main id="main-content">`` element
    (base.html:181) so nav/head chrome outside it never enters the corpus. Script and
    style bodies and the chat drawer (nested inside ``<main>``) are left out. Every
    element ``id`` seen INSIDE ``<main>`` is also collected, for the anchor-resolution
    assertion below."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ids: set[str] = set()
        self.saw_main = False
        self._skip = 0
        self._drawer = 0
        self._main_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "main":
            self._main_depth += 1
            self.saw_main = True
        if self._drawer:
            if tag == "aside":
                self._drawer += 1
            return
        if tag == "aside" and attributes.get("id") == "assessment-chat":
            self._drawer = 1
            return
        if tag in ("script", "style"):
            self._skip += 1
            return
        if not self._main_depth:
            return
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        for name in ("data-markdown", "title", "href"):
            if attributes.get(name):
                self.parts.append(attributes[name])

    def handle_endtag(self, tag):
        if tag == "main" and self._main_depth:
            self._main_depth -= 1
        if self._drawer:
            if tag == "aside":
                self._drawer -= 1
            return
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._main_depth and not self._skip and not self._drawer:
            self.parts.append(data)


def _parse_page(html: str) -> _Corpus:
    corpus = _Corpus()
    corpus.feed(html)
    corpus.close()
    if not corpus.saw_main:
        raise AssertionError("page has no <main> element; cannot scope the parity corpus")
    return corpus


@pytest.mark.parametrize("prose_format", ["markdown", None])
@pytest.mark.parametrize("page", list(PAGES))
async def test_each_tier_record_carries_only_what_its_page_renders(
    client, db_session, fixture_rubric, page, prose_format
):
    role, surface, tier = PAGES[page]
    assessment_id = await _seed(db_session, fixture_rubric, prose_format=prose_format)
    viewer = await factories.make_user(db_session, user_role=role)
    resp = await client.get(f"/{surface}/assessments/{assessment_id}", headers=auth_headers(viewer.id))
    assert resp.status_code == 200
    page = _parse_page(resp.text)
    page_tokens = set(_TOKEN_RE.findall(" ".join(page.parts).lower()))

    loaded = await load_chat_record(db_session, assessment_id, tier=tier)
    assert loaded is not None
    record = loaded[0]
    blocks = [block["text"] for doc in record.documents for block in doc["source"]["content"]]
    record_text = "\n".join(blocks)

    # (1) containment
    missing = {}
    for block in blocks:
        extra = set(_TOKEN_RE.findall(" ".join(quoted_lines(block)).lower())) - page_tokens
        if extra:
            missing[block.split("\n", 1)[0]] = sorted(extra)
    assert not missing, missing

    # (2) staff-only verdict fields
    for sentinel in STAFF_ONLY:
        assert (sentinel in record_text) is (tier == "staff"), sentinel

    # (3) the rubric definitions the page shows
    for dim in fixture_rubric.dimensions:
        assert f"PARITY-ANCHOR-{dim.key.upper()}" in record_text, dim.key
    for key in fixture_rubric.gating:
        assert f"PARITY-GATEDESC-{key.upper()}" in record_text, key

    # (4) what no page renders
    for sentinel in EXCLUDED:
        assert sentinel not in record_text, sentinel

    # (5) every anchor a block cites resolves to a rendered element id on this page
    anchors = {
        target.anchor
        for doc_targets in record.targets
        for target in doc_targets
        if target.anchor is not None
    }
    missing_anchors = anchors - page.ids
    assert not missing_anchors, missing_anchors

    # Positive controls: the seed really exercised the paths the exclusions guard.
    assert "PARITY-EST-THREE" in record_text and "> and 1 more" in record_text
    assert 'display name "PARITY-HUMAN-POSTER" (unverified)' in record_text
    assert "PARITY-OTHER-AGENT-REPLY" in record_text
    assert RECORD_URL in record.url_tokens
