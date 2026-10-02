"""The chat record's gate blocks (spec §5.1): a stored gate reason is appended as the
block's last quoted line; a row without a usable one renders exactly as the frozen
record (the chat model's input for existing rows does not change)."""
import pytest

from src.services import assessment_chat_record as rec
from tests.assessment_chat_support import synthetic_detail
from tests.unit import _frozen_verdict_doc as frozen

REASON_LABEL = "; the last quoted line is the hub's own reason for this state"


def _gate_blocks(record, name):
    return [
        block["text"]
        for block in record.documents[0]["source"]["content"]
        if block["text"].startswith(f"[Gate — {name}")
    ]


@pytest.mark.parametrize("tier", ["staff", "reviewer"])
def test_a_stored_reason_is_the_gate_blocks_last_quoted_line(tier):
    detail = synthetic_detail()
    detail["assessment"].gating_rationales = {
        " Credible_Science ": "GATE-REASON-SCIENCE",
        "translational_potential": "GATE-REASON-TRANSLATIONAL",
        "life_sciences_domain": "   ",
    }
    record = rec.build_chat_record(detail, tier=tier)
    assert _gate_blocks(record, "credible science") == [
        f"[Gate — credible science{REASON_LABEL}]\n> not met\n> GATE-DESC-SCIENCE"
        "\n> GATE-REASON-SCIENCE"
    ]
    assert _gate_blocks(record, "translational potential") == [
        f"[Gate — translational potential{REASON_LABEL}]\n> unconfirmed — never asked"
        "\n> GATE-DESC-TRANSLATIONAL\n> GATE-REASON-TRANSLATIONAL"
    ]
    # A blank reason is no reason: the block is exactly today's.
    assert _gate_blocks(record, "life sciences domain") == [
        "[Gate — life sciences domain]\n> met\n> GATE-DESC-LIFE"
    ]


def test_an_archived_row_quotes_its_reason_without_a_definition():
    detail = synthetic_detail(gating_descriptions={})
    detail["assessment"].gating_rationales = {"credible_science": "GATE-REASON-SCIENCE"}
    record = rec.build_chat_record(detail, tier="staff")
    assert _gate_blocks(record, "credible science") == [
        f"[Gate — credible science{REASON_LABEL}]\n> not met\n> GATE-REASON-SCIENCE"
    ]


@pytest.mark.parametrize("stored", [None, {}, ["a reason"], "a reason", {"credible_science": 5}],
                         ids=["absent", "empty", "list", "string", "non-string-value"])
@pytest.mark.parametrize("tier", ["staff", "reviewer"])
def test_rows_without_a_usable_reason_render_exactly_as_the_frozen_record(stored, tier):
    detail = synthetic_detail()
    if stored is not None:
        detail["assessment"].gating_rationales = stored
    new, old = rec._verdict_doc(detail, tier), frozen._verdict_doc(detail, tier)
    assert new.blocks == old.blocks
    assert new.targets == old.targets
