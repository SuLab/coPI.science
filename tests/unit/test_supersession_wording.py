"""R1.15 (Q12, P4): supersession acts on any UpdateOf link, so no text says it is about
preprints only (the 2026-10-15 publication-handling design)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: The five locations: the progress texts, both corpus_additions docstrings, the runbook
#: paragraph and the help page.
FILES = (
    "src/services/profile_pipeline.py",
    "src/services/corpus_additions.py",
    "docs/operations/pis-and-access.md",
    "templates/help/pages/managing-pis.html",
)
FORBIDDEN = ("(preprints)", "(preprint)", "superseded by a published version")


def test_no_supersession_text_says_preprint_only():
    for rel in FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert [p for p in FORBIDDEN if p in text] == [], rel
        assert "updated or superseded" in text, rel
