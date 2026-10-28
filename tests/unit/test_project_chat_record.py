"""The project chat record (spec §8.4) and its opening questions (§8.6), built from a
synthetic ``build_project_detail(viewer=None, is_admin=False)`` dict.

tests/integration/test_project_chat_parity.py proves the record never exceeds what the
details page renders; these tests pin its shape, anchors, link tokens and questions.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.services.assessment_chat_record import LEGEND_TAIL
from src.services.assessment_chat_suggestions import KIND_TEMPLATE, Suggestion
from src.services.project_chat_record import (
    build_project_chat_record,
    project_page_suggestions,
    project_template_suggestions,
)

T = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PUBMED = "https://pubmed.ncbi.nlm.nih.gov/31500698/"
TRIAL = "https://clinicaltrials.gov/study/NCT01903460"
REPLY_URL = "https://doi.org/10.1000/reply-url"
POSTING_URL = "https://example.org/posting-url"
SUMMARY_URL = "https://example.org/summary-url"


def _detail(**overrides):
    project = {
        "id": "00000000-0000-0000-0000-000000000001",
        "lab": "lab1",
        "pi_user_id": None,
        "title": "PROJECT TITLE",
        "summary": f"SUMMARY TEXT {SUMMARY_URL}",
        "outcome_kind": "not_agreed",
        "outcome_label": "Not agreed",
        "reason_label": None,
        "is_finished": True,
        "run_id": "run",
        "run_started_at": T,
        "requested_at": T,
        "posted_at": T,
        "finished_at": T,
        "has_close": True,
        "why_chosen": "WHY TEXT",
        "others": [{"use": "OTHER USE", "note": "OTHER NOTE"}],
        "anchors": [{"section": "Model Systems", "item": "ANCHOR ITEM"}],
        "plan_title": "Plan",
        "plan_rows": [
            {"label": "Indication", "value": "FINAL INDICATION", "changed": True,
             "opening_value": "OPENING INDICATION"},
            {"label": "Time and budget", "value": "six months", "changed": False,
             "opening_value": None},
        ],
        "tool_rows": [
            {"item": "Rosetta", "in_profile": True, "matched": "Rosetta protein design"},
            {"item": "PyMOL", "in_profile": False, "matched": None},
        ],
        "hub_position": "Agreed",
        "lab_position": "Not agreed",
        "gaps": ["GAP TEXT"],
        "disagreements": ["DISAGREEMENT TEXT"],
        "sources_by_kind": [
            {"kind": "papers", "label": "Papers", "items": [
                {"ident": "PMID 31500698", "title": "PAPER TITLE", "url": PUBMED,
                 "specialist": "scientific"}]},
            {"kind": "trials", "label": "Clinical trials", "items": [
                {"ident": "NCT01903460", "title": "TRIAL TITLE", "url": TRIAL,
                 "specialist": "clinical"}]},
            {"kind": "patents", "label": "Patents", "items": [
                {"ident": "US11234567B2", "title": "PATENT TITLE", "url": None,
                 "specialist": "legal"}]},
        ],
        "evidence": [
            {"domain": "scientific", "anchor": "consult-1", "established": ["EST"],
             "concerns": ["CONCERN"], "questions_to_ask": ["ASK"], "truncated": False,
             "earlier_count": 0},
        ],
        "timeline": [
            {"kind": "posting", "anchor": "m-1", "speaker": "BlackbirdBot", "at": T,
             "text": f"POSTING TEXT {POSTING_URL}", "partial": False},
            {"kind": "post", "anchor": "m-2", "speaker": "Lab1Bot", "at": T,
             "text": f"LAB REPLY see {REPLY_URL}", "closed_thread": False},
            {"kind": "consult", "anchor": "consult-1", "domain": "scientific",
             "question": "QUESTION", "established": ["EST"], "concerns": ["CONCERN"],
             "questions_to_ask": ["ASK"], "concern_count": 1, "truncated": True, "at": T},
            {"kind": "post", "anchor": "m-3", "speaker": "BlackbirdBot", "at": T,
             "text": "HUB CLOSE", "closed_thread": True},
        ],
        "thread_unavailable": False,
        "counts": {"sources": 3, "consults": 1, "posts": 2, "others": 1},
        "reviews": [
            {"id": "r1", "reviewer_name": "AUTHOR NAME", "rating": 4, "comment": "REVIEW TEXT",
             "fund": True, "created_at": T, "edited": True, "can_edit": False},
        ],
        "my_review": None,
        "can_review_projects": False,
    }
    project.update(overrides)
    return {"project": project}


def _blocks(record, doc_index):
    return [b["text"] for b in record.documents[doc_index]["source"]["content"]]


def _all_text(record) -> str:
    return json.dumps(list(record.documents), ensure_ascii=False)


def _anchors(record, doc_index):
    return [t.anchor for t in record.targets[doc_index]]


def test_five_documents_with_citations_enabled():
    record = build_project_chat_record(_detail(), tier="staff")
    assert [d["title"] for d in record.documents] == [
        "Project", "Conversation", "Specialists", "Research", "Reviews",
    ]
    for doc in record.documents:
        assert doc["type"] == "document"
        assert doc["citations"] == {"enabled": True}
        assert doc["context"].endswith(LEGEND_TAIL)
        assert "cache_control" not in doc
        assert doc["source"]["content"]


def test_every_block_is_one_label_line_then_quoted_lines():
    record = build_project_chat_record(_detail(), tier="staff")
    for i in range(5):
        for block in _blocks(record, i):
            first, *rest = block.split("\n")
            assert first.startswith("[") and first.endswith("]"), first
            assert "[" not in first[1:-1] and "]" not in first[1:-1], first
            for line in rest:
                assert line.startswith("> "), (first, line)


def test_anchors_are_the_spec_anchors():
    record = build_project_chat_record(_detail(), tier="staff")
    assert set(_anchors(record, 0)) == {"brief", "plan", "positions", "others"}
    assert _anchors(record, 1) == ["m-1", "m-2", "m-3"]
    assert _anchors(record, 2) == ["consult-1"]
    assert set(_anchors(record, 3)) == {"research"}
    assert set(_anchors(record, 4)) == {"reviews"}


def test_project_document_content():
    text = "\n".join(_blocks(build_project_chat_record(_detail(), tier="staff"), 0))
    for quoted in ("PROJECT TITLE", "WHY TEXT", "OTHER USE", "OTHER NOTE", "ANCHOR ITEM",
                   "FINAL INDICATION", "OPENING INDICATION", "GAP TEXT", "DISAGREEMENT TEXT",
                   "> in the lab's profile", "> matches: Rosetta protein design",
                   "> stated by the lab"):
        assert quoted in text, quoted
    assert "[Gap 1 of 1" in text
    ready = "\n".join(_blocks(build_project_chat_record(
        _detail(outcome_kind="ready", outcome_label="Ready"), tier="staff"), 0))
    assert "[Acknowledged limit 1 of 1" in ready and "[Gap " not in ready
    open_ = "\n".join(_blocks(build_project_chat_record(
        _detail(has_close=False, gaps=[], disagreements=[]), tier="staff"), 0))
    assert "[Positions — no close recorded]" in open_


def test_conversation_and_specialists():
    record = build_project_chat_record(_detail(), tier="staff")
    conversation = _blocks(record, 1)
    assert "POSTING TEXT" in conversation[0] and "the project's posting" in conversation[0]
    assert "closed the thread" in conversation[2].split("\n", 1)[0]
    consult = _blocks(record, 2)[0]
    assert "reply cut off" in consult.split("\n", 1)[0]
    for line in ("> Asked: QUESTION", "> Established:", "> - EST", "> Concerns:", "> - CONCERN",
                 "> Questions to ask:", "> - ASK"):
        assert line in consult


def test_missing_posting_and_unavailable_thread_are_said():
    record = build_project_chat_record(
        _detail(timeline=[], thread_unavailable=True, evidence=[]), tier="staff"
    )
    labels = [b.split("\n", 1)[0] for b in _blocks(record, 1)]
    assert labels[0].startswith("[Posting unavailable")
    assert labels[-1].startswith("[Thread messages unavailable")
    assert _blocks(record, 2)[0].startswith("[No consults")


def test_research_quotes_the_page_group_labels():
    blocks = _blocks(build_project_chat_record(_detail(), tier="staff"), 3)
    assert "> Papers" in blocks[0] and "> Clinical trials" in blocks[1]
    assert "> Patents" in blocks[2]
    assert "> trial" not in "\n".join(blocks).replace("> Clinical trials", "")


def test_url_tokens_are_source_links_and_reply_urls_only():
    record = build_project_chat_record(_detail(), tier="staff")
    assert record.url_tokens == frozenset({PUBMED, TRIAL, REPLY_URL})
    assert POSTING_URL in _all_text(record)  # quoted, but the page does not link it


def test_reviews_document():
    block = _blocks(build_project_chat_record(_detail(), tier="staff"), 4)[0]
    label = block.split("\n", 1)[0]
    assert "AUTHOR NAME" in label and "rating 4 of 5" in label and "Fund" in label
    assert "edited" in label
    assert "> REVIEW TEXT" in block
    empty = _blocks(build_project_chat_record(_detail(reviews=[]), tier="staff"), 4)
    assert empty[0].startswith("[No reviews")


@pytest.mark.parametrize("word", ["blocking", "adequate", "confidence", "signal"])
def test_legends_name_no_label_or_confidence(word):
    record = build_project_chat_record(_detail(), tier="staff")
    for doc in record.documents:
        assert word not in doc["context"].lower(), (doc["title"], word)


def test_costs_in_the_detail_never_reach_the_record():
    detail = _detail(research_cost_usd=12.3456, refinement_cost_usd=7.8912,
                     calls_url="/admin/activity/x/llm-calls?thread=1",
                     raw_choice={"s": "RAWCHOICE"}, raw_final={"s": "RAWFINAL"})
    text = _all_text(build_project_chat_record(detail, tier="staff"))
    for sentinel in ("12.3", "7.89", "llm-calls", "RAWCHOICE", "RAWFINAL", "Cost"):
        assert sentinel not in text, sentinel


def test_tiers_share_the_documents_and_digest():
    staff = build_project_chat_record(_detail(), tier="staff")
    reviewer = build_project_chat_record(_detail(), tier="reviewer")
    assert staff.documents == reviewer.documents
    assert staff.sha256_12 == reviewer.sha256_12
    assert reviewer.tier == "reviewer"
    with pytest.raises(ValueError):
        build_project_chat_record(_detail(), tier="pi")


def test_malformed_values_are_skipped_not_raised():
    record = build_project_chat_record(
        _detail(others="x", anchors=None, plan_rows=[None], tool_rows=5, gaps={"a": 1},
                timeline=[None, {"kind": "post"}], sources_by_kind=[{"items": "x"}],
                reviews=[3]),
        tier="staff",
    )
    assert len(record.documents) == 5


def _texts(suggestions):
    return [s.text for s in suggestions]


def test_not_agreed_asks_about_unresolved_points():
    out = project_template_suggestions(_detail())
    assert out[0] == Suggestion(
        text="What did BlackbirdBot and the lab leave unresolved, and why?",
        anchor="positions", kind=KIND_TEMPLATE,
    )
    assert [s.anchor for s in out] == ["positions", "evidence", "brief", "plan"]


def test_ready_asks_about_acknowledged_limits():
    out = project_template_suggestions(_detail(outcome_kind="ready"))
    assert out[0].text == "Which of the acknowledged limits matter most for this plan?"
    assert out[0].anchor == "positions"


def test_padded_with_the_generic_question_and_never_more_than_four():
    sparse = project_template_suggestions(_detail(
        outcome_kind="not_finished", evidence=[], anchors=[], plan_rows=[]
    ))
    assert _texts(sparse) == ["What is being proposed, in plain terms?"]
    for kind in ("ready", "not_agreed", "not_finished"):
        out = project_template_suggestions(_detail(outcome_kind=kind))
        assert 1 <= len(out) <= 4
        for s in out:
            assert s.kind == KIND_TEMPLATE
            for word in ("fund", "score", "rate"):
                assert word not in s.text.lower(), (s.text, word)


def test_inline_sections_cover_the_project_cards():
    assert Suggestion(text="q", anchor="evidence", kind=KIND_TEMPLATE).section == "evidence"
    assert Suggestion(text="q", anchor="plan", kind=KIND_TEMPLATE).section == "plan"
    assert Suggestion(text="q", anchor="positions", kind=KIND_TEMPLATE).section == "positions"


def test_page_suggestions_off_or_impersonating_is_empty():
    user = SimpleNamespace(_is_impersonated=False)
    assert project_page_suggestions(_detail(), user, chat_enabled=False) == []
    impersonated = SimpleNamespace(_is_impersonated=True)
    assert project_page_suggestions(_detail(), impersonated, chat_enabled=True) == []
    assert project_page_suggestions(_detail(), user, chat_enabled=True) == (
        project_template_suggestions(_detail())
    )
