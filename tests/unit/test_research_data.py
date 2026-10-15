"""R1.21: ResearchData's predicate and wording (2026-10-15 publication-handling design)."""
from src.services.user_deletion import ResearchData


def test_items_text_reads_naturally():
    assert ResearchData(True, 1, "x").items_text == (
        "a researcher profile, 1 stored publication and a lab agent"
    )
    assert ResearchData(False, 0, "x").items_text == "a lab agent"
    assert ResearchData(True, 2, None).items_text == "a researcher profile and 2 stored publications"
    assert ResearchData(True, 2, None).has_any is True
    assert ResearchData(False, 0, None).has_any is False
