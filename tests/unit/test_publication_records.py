"""C12 ground truth, the pure half (spec 2026-10-15 §10 R5.4; F9, F34)."""
from datetime import UTC, datetime

import pytest

from src.agent.dois import Citation
from src.agent.publication_records import (
    NOT_THEIRS_REASONS,
    LabPublicationRecord,
    PublicationIds,
    build_record,
    records_from_rows,
)

TS = datetime(2026, 10, 1, tzinfo=UTC)
PERSONA = "## Recent Publications\n- T. https://doi.org/10.1000/x (PMID 9)\n"
OTHER_REASONS = ("superseded", "pubmed_twin", "duplicate_title", "publication_type")


def test_the_not_theirs_reasons_are_f9s():
    assert NOT_THEIRS_REASONS == frozenset({"staff", "openalex_affiliation", "repair_script"})


def test_records_from_rows_canonicalises_and_counts():
    out = records_from_rows(["a", "b"], [
        ("a", "10.1016/S0092-8674(02)00722-5", "123", None, None),
        ("a", None, "0045", None, None),
        ("a", "doi:10.1000/X", None, None, None),
    ])
    a = out["a"]
    assert a.in_use_dois == {"10.1016/s0092-8674(02)00722-5", "10.1000/x"}
    assert a.in_use_pmids == {"123", "45"}
    assert a.row_count == 3
    assert a.not_theirs_dois == frozenset() and a.not_theirs_pmids == frozenset()
    assert out["b"] == PublicationIds()


@pytest.mark.parametrize("reason", sorted(NOT_THEIRS_REASONS))
def test_records_from_rows_collects_not_theirs(reason):
    a = records_from_rows(["a"], [("a", "10.1000/x", "9", TS, reason)])["a"]
    assert a.not_theirs_dois == {"10.1000/x"} and a.not_theirs_pmids == {"9"}
    assert a.row_count == 0


@pytest.mark.parametrize("reason", [*OTHER_REASONS, None])
def test_records_from_rows_ignores_other_exclusions(reason):
    assert records_from_rows(["a"], [("a", "10.1000/x", "9", TS, reason)])["a"] == PublicationIds()


def test_in_use_row_wins_over_not_theirs():
    a = records_from_rows(["a"], [
        ("a", "10.1000/x", None, None, None),
        ("a", "10.1000/X", None, TS, "staff"),
    ])["a"]
    assert a.in_use_dois == {"10.1000/x"}
    assert a.not_theirs_dois == frozenset()


def test_a_row_for_another_agent_is_ignored():
    assert records_from_rows(["a"], [("z", "10.1000/x", "9", None, None)]) == {"a": PublicationIds()}


@pytest.mark.parametrize("reason", sorted(NOT_THEIRS_REASONS))
def test_build_record_subtracts_not_theirs_profile_ids(reason):
    ids = records_from_rows(["a"], [("a", "10.1000/x", "9", TS, reason)])["a"]
    record = build_record(PERSONA, ids)
    assert record.ids == frozenset()
    assert record.has_records is False


@pytest.mark.parametrize("reason", OTHER_REASONS)
def test_build_record_keeps_profile_ids_of_other_exclusions(reason):
    ids = records_from_rows(["a"], [("a", "10.1000/x", "9", TS, reason)])["a"]
    record = build_record(PERSONA, ids)
    assert record.ids == {"10.1000/x", "9"}
    assert record.has_records is True


def test_build_record_without_ids_uses_the_persona():
    record = build_record(PERSONA, None)
    assert record.ids == {"10.1000/x", "9"} and record.has_records is True
    assert build_record("# Su Lab\n\nProfile not yet available.", None).has_records is False


def test_record_holds_by_kind():
    record = LabPublicationRecord(dois=frozenset({"10.1/x"}), pmids=frozenset({"5"}))
    assert record.holds(Citation(0, 6, "doi", "10.1/x")) is True
    assert record.holds(Citation(0, 6, "pmid", "10.1/x")) is False
    assert record.holds(Citation(0, 1, "pmid", "5")) is True
