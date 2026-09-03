"""Unit tests for the in-run publication dedup fix (issue #22 COR-16)."""

from src.services.profile_pipeline import _dedup_pmids


def test_pmids_are_deduplicated_across_orcid_works_preserving_order():
    pmids, seen = _dedup_pmids(
        [{"pmid": "111"}, {"pmid": "111"}, {"pmid": "222"}, {"pmid": None}, {}]
    )
    assert pmids == ["111", "222"]
    assert seen == {"111", "222"}
