"""R1.17 (Q31; PIPE-18, B-D6, DOC-3): comments describe monthly_refresh as retired and
say excluded rows never count (the 2026-10-15 publication-handling design)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "src/models/profile_revision.py",
    "src/services/profile_versioning.py",
    "src/services/profile_pipeline.py",
)
STALE = ("the monthly_refresh job runs", "A monthly refresh that", "that job records")


def test_no_comment_calls_monthly_refresh_live():
    for rel in FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert [p for p in STALE if p in text] == [], rel
    assert "Excluded rows" in (ROOT / "src/models/profile.py").read_text(encoding="utf-8")
