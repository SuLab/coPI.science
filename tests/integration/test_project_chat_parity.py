"""The page-parity rule for the project chat (spec §8.4): the record carries only what
the project details page renders, for every role that can open it.

Modelled on tests/integration/test_assessment_chat_parity.py. Every stored value
carries a sentinel that is ONE token of the containment check's ``[a-z0-9]{3,}`` split,
so the test can say exactly what a record may and may not contain:

(1) containment — every token of every quoted record line occurs in that role's
    rendered ``<main>`` (text nodes plus data-markdown, title and href values: the
    lab's reply and the thread's posts are rendered client-side from ``data-markdown``,
    so the attribute value is what the page carries for them);
(2) every anchor a block cites is an element id inside that ``<main>``;
(3) what no page shows, or no non-admin detail holds — a consult's signal, confidence
    and raw opinion, the costs, the calls link, the ``research`` JSON, another
    project — is in no record;
(4) link tokens are the reply's URL and the sources' links, never the posting's.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    SpecialistConsult,
)
from src.models.hub_project import HubProject, HubProjectCall, HubProjectReview
from src.services.assessment_chat_record import quoted_lines
from src.services.project_chat_record import load_project_chat_record
from src.services.project_research import ledger_row_cost
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LAB = "lab1"
HUB = "blackbird"
POSTING_TS = "100.000100"
REPLY_TS = "200.000200"
OTHER_REPLY_TS = "900.000900"
POSTING_URL = "https://example.org/parityposting"
REPLY_URL = "https://doi.org/10.1000/parityreply"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/31500698/"
TRIAL_URL = "https://clinicaltrials.gov/study/NCT01903460"
POSTING_TEXT = f"PARITYPOSTING the project in full, see {POSTING_URL}"

OPENING_PLAN = {
    "indication": "PARITYOPENINDICATION",
    "target": "PARITYTARGET",
    "model": "PARITYMODEL",
    "positive_control": "PARITYPOSCONTROL",
    "negative_control": "PARITYNEGCONTROL",
    "standard_of_care": "PARITYSOC",
    "readout": "PARITYREADOUT",
    "design": "PARITYDESIGN",
    "time_and_budget": "PARITYBUDGET",
    "lab_tools": ["PARITYTOOL"],
}
CHOICE = {
    "chosen": {"use": "PARITYUSE", "why": "w", "domain": "scientific"},
    "why_chosen": "PARITYWHYCHOSEN",
    "others": [{"use": "PARITYOTHERUSE", "domain": "clinical", "note": "PARITYOTHERNOTE"}],
    "title": "Choice title",
    "summary": "PARITYSUMMARY",
    "plan": OPENING_PLAN,
    "anchors": [{"section": "Model Systems", "item": "PARITYANCHORITEM"}],
    "sources": [
        {"key": "pmid:31500698", "kind": "pubmed", "title": "PARITYPAPER",
         "domain": "scientific", "shows": "s"},
        {"key": "nct:NCT01903460", "kind": "trial", "title": "PARITYTRIAL",
         "domain": "clinical", "shows": "s"},
        {"key": "patent:US11234567B2", "kind": "patent", "title": "PARITYPATENT",
         "domain": "legal", "shows": "s"},
    ],
}
FINAL = {
    "plan": {**OPENING_PLAN, "indication": "PARITYFINALINDICATION",
             "lab_tools": ["PARITYTOOL", "PARITYSECONDTOOL"]},
    "hub_position": "agree",
    "lab_position": "disagree",
    "gaps": ["PARITYGAP"],
    "disagreements": ["PARITYDISAGREEMENT"],
}
EXCLUDED = (
    "blocking",
    "moderate",
    "PARITYRAWOPINION",
    "PARITYRESEARCHJSON",
    "PARITYOTHERPROJECT",
    "PARITYOTHERSUMMARY",
    "PARITYOTHERCONSULT",
    "llm-calls",
    "Cost:",
)
PAGES = {
    "admin": (USER_ROLE_ADMIN, "staff"),
    "manager": (USER_ROLE_MANAGER, "staff"),
    "reviewer": (USER_ROLE_REVIEWER, "reviewer"),
}
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


async def _seed(db):
    run = await factories.make_simulation_run(db, started_at=T0)
    project = HubProject(
        simulation_run_id=run.id, agent_id=LAB, slot=1, status="not_agreed",
        requested_run_seconds=0.0, requested_at=T0, title="PARITYTITLE",
        choice=CHOICE, final=FINAL, posting_text=POSTING_TEXT, posting_ts=POSTING_TS,
        reply_ts=REPLY_TS, posted_at=T0 + timedelta(seconds=100),
        finished_at=T0 + timedelta(hours=4), research={"sentinel": "PARITYRESEARCHJSON"},
    )
    other = HubProject(
        simulation_run_id=run.id, agent_id="lab2", slot=2, status="ready",
        requested_run_seconds=0.0, requested_at=T0, title="PARITYOTHERPROJECT",
        choice={**CHOICE, "summary": "PARITYOTHERSUMMARY"}, reply_ts=OTHER_REPLY_TS,
        finished_at=T0 + timedelta(hours=5),
    )
    db.add_all([project, other])
    await db.flush()
    base = T0.timestamp()
    for fields in (
        dict(agent_id=HUB, phase="hub_project", message_ts=POSTING_TS, content=POSTING_TEXT,
             sender_name="BlackbirdBot", posted_at=base + 100),
        dict(agent_id=LAB, phase="project_reply", message_ts=REPLY_TS,
             content=f"PARITYLABREPLY with **markdown**, see {REPLY_URL}",
             sender_name="Lab1Bot", posted_at=base + 200),
        dict(agent_id=HUB, phase="thread_reply", message_ts="400.000400", thread_ts=REPLY_TS,
             content="PARITYHUBCLOSE closing the thread", sender_name="BlackbirdBot",
             posted_at=base + 400),
        dict(agent_id="lab2", phase="project_reply", message_ts=OTHER_REPLY_TS,
             content="PARITYOTHERPROJECT reply", sender_name="Lab2Bot", posted_at=base + 900),
    ):
        await factories.make_agent_message(db, run=run, channel_name="general", **fields)

    def consult(offset, thread, **fields):
        data = dict(
            simulation_run_id=run.id, agent_id=HUB, thread_id=thread,
            verdict_signal="blocking", confidence="moderate", raw_opinion="PARITYRAWOPINION",
            created_at=datetime.fromtimestamp(base + offset, UTC),
        )
        data.update(fields)
        return SpecialistConsult(**data)

    db.add_all([
        consult(250, REPLY_TS, domain="scientific", question="PARITYASKEDEARLY",
                established=["PARITYESTEARLY"], concerns=["PARITYCONCERNEARLY"],
                questions_to_ask=["PARITYQTAEARLY"], truncated=False),
        consult(300, REPLY_TS, domain="scientific", question="PARITYASKEDLATEST",
                established=["PARITYESTLATEST"], concerns=["PARITYCONCERNLATEST"],
                questions_to_ask=["PARITYQTALATEST"], truncated=True),
        consult(950, OTHER_REPLY_TS, domain="clinical", question="PARITYOTHERCONSULT",
                established=[], concerns=["PARITYOTHERCONSULT"], questions_to_ask=[],
                truncated=False),
    ])
    db.add(HubProjectCall(project_id=project.id, step="choose", model="claude-opus-5",
                          status="done", usage_by_model=None))
    author = await factories.make_user(db, user_role=USER_ROLE_MANAGER)
    db.add(HubProjectReview(project_id=project.id, reviewer_user_id=author.id,
                            reviewer_name="PARITYREVIEWER", rating=4,
                            comment="PARITYREVIEWCOMMENT", fund=True))
    await db.flush()
    return project.id


class _Corpus(HTMLParser):
    """A page's rendered ``<main>`` text — text nodes plus data-markdown, title and href
    values, HTML-unescaped — and every element id inside ``<main>``. Script and style
    bodies and the chat drawer (``<aside id="assessment-chat">``, nested in ``<main>``)
    are left out."""

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

    def handle_startendtag(self, tag, attrs):
        # A self-closing tag opens nothing: record its attributes without letting a
        # self-closed <aside>, <script> or <main> unbalance the depth counters.
        if tag in ("main", "aside", "script", "style"):
            return
        self.handle_starttag(tag, attrs)

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


def _uncontained(blocks: list[str], page: _Corpus) -> dict[str, list[str]]:
    page_tokens = set(_TOKEN_RE.findall(" ".join(page.parts).lower()))
    missing = {}
    for block in blocks:
        extra = set(_TOKEN_RE.findall(" ".join(quoted_lines(block)).lower())) - page_tokens
        if extra:
            missing[block.split("\n", 1)[0]] = sorted(extra)
    return missing


@pytest.mark.parametrize("role_key", list(PAGES))
async def test_record_carries_only_what_the_page_renders(client, db_session, role_key):
    role, tier = PAGES[role_key]
    project_id = await _seed(db_session)
    viewer = await factories.make_user(db_session, user_role=role)
    resp = await client.get(f"/workspace/projects/{project_id}", headers=auth_headers(viewer.id))
    assert resp.status_code == 200
    page = _parse_page(resp.text)

    loaded = await load_project_chat_record(db_session, project_id, tier=tier)
    assert loaded is not None
    record, row = loaded
    assert row.id == project_id
    blocks = [block["text"] for doc in record.documents for block in doc["source"]["content"]]
    record_text = "\n".join(blocks)

    # (1) containment
    missing = _uncontained(blocks, page)
    assert not missing, missing

    # (2) every cited anchor is an element id on this page
    anchors = {t.anchor for doc in record.targets for t in doc if t.anchor is not None}
    assert anchors - page.ids == set(), anchors - page.ids
    assert {"brief", "plan", "positions", "others", "research", "reviews"} <= anchors
    assert {"m-1", "m-2", "m-3", "consult-1", "consult-2"} <= anchors

    # (3) what no record may hold
    research = ledger_row_cost(None, "claude-opus-5", "choose")
    for sentinel in (*EXCLUDED, f"${research:.2f}"):
        assert sentinel not in record_text, sentinel
    for doc in record.documents:
        for word in ("blocking", "moderate", "confidence", "signal"):
            assert word not in doc["context"].lower(), word

    # (4) link tokens
    assert REPLY_URL in record.url_tokens
    assert PUBMED_URL in record.url_tokens and TRIAL_URL in record.url_tokens
    assert POSTING_URL not in record.url_tokens

    # Positive controls: the seed exercised every document.
    for sentinel in ("PARITYTITLE", "PARITYSUMMARY", "PARITYWHYCHOSEN", "PARITYOTHERUSE",
                     "PARITYANCHORITEM", "PARITYFINALINDICATION", "PARITYOPENINDICATION",
                     "PARITYSECONDTOOL", "PARITYGAP", "PARITYDISAGREEMENT", "PARITYPOSTING",
                     "PARITYLABREPLY", "PARITYHUBCLOSE", "PARITYASKEDEARLY",
                     "PARITYCONCERNLATEST", "PARITYQTALATEST", "PARITYPAPER", "PARITYTRIAL",
                     "PARITYPATENT", "PARITYREVIEWCOMMENT"):
        assert sentinel in record_text, sentinel


async def test_missing_project_has_no_record(db_session):
    assert await load_project_chat_record(db_session, uuid.uuid4(), tier="staff") is None
