"""scripts/unanchored_publications_report.py never lists a PMID-less row
(spec 2026-10-09 §5.7)."""
import sys
from pathlib import Path
from types import SimpleNamespace

from src.services.corpus import CorpusResult

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _row(pid, pmid, provenance):
    return SimpleNamespace(id=pid, pmid=pmid, title=f"Row {pid}", provenance=provenance,
                           excluded_at=None, year=2024, abstract=None, journal=None,
                           pmcid=None)


def test_the_unanchored_report_never_lists_a_pmid_less_row():
    import scripts.unanchored_publications_report as rep

    rows = [_row("a", None, "openalex"), _row("b", "1", None)]
    out = rep.compare(rows, CorpusResult(kept=[], flagged=[], ranked=[]))
    assert [pid for pid, _pmid, _title in out.unanchored] == ["b"]
