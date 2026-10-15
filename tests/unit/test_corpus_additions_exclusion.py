"""corpus_additions.exclude_by_system (contract C3, R4.2) and incomplete_lookups_reason
(contract C4, R4.4)."""
from datetime import UTC, datetime

import pytest

from src.models import Publication
from src.services.corpus import CorpusResult
from src.services.corpus_additions import exclude_by_system, incomplete_lookups_reason


def test_exclude_by_system():
    manual = Publication(pmid="1", title="t", provenance="manual")
    assert exclude_by_system(manual, "publication_type") is False
    assert (manual.excluded_at, manual.excluded_reason) == (None, None)

    when = datetime(2026, 1, 1, tzinfo=UTC)
    excluded = Publication(pmid="2", title="t", provenance="s1", excluded_at=when,
                           excluded_reason="staff")
    assert exclude_by_system(excluded, "publication_type") is False
    assert (excluded.excluded_at, excluded.excluded_reason) == (when, "staff")

    row = Publication(pmid="3", title="t", provenance="s1")
    for bad in ("staff", "other"):
        with pytest.raises(ValueError):
            exclude_by_system(row, bad)
    assert row.excluded_at is None
    assert exclude_by_system(row, "publication_type") is True
    assert row.excluded_at is not None and row.excluded_reason == "publication_type"
    assert row.excluded_by_user_id is None


def _complete(**kw):
    kw.setdefault("stage_map", {"1": ("s1",)})
    kw.setdefault("stage_counts", {"s1": 1})
    return CorpusResult(kept=[], flagged=[], **kw)


@pytest.mark.parametrize(("result", "reason"), [
    (CorpusResult(kept=[], flagged=[], stage_counts={"s1": 1}),
     "the resolve recorded no stage map"),
    (_complete(truncated_stages=["s3"]), "a search stopped at its cap (s3)"),
    (_complete(permanently_dropped=["9"]), "1 records could not be fetched"),
    (_complete(stage_counts={"s1": 0}), "the ORCID record listed no works"),
    (_complete(), None),
], ids=["no-stage-map", "truncated", "dropped", "empty-orcid", "complete"])
def test_incomplete_lookups_reason(result, reason):
    assert incomplete_lookups_reason(result) == reason
