"""B-11: every review-comment textarea carries the server's cap as maxlength, so
the browser stops typing where the server would refuse."""

import re
from pathlib import Path

from src.services.assessment_reviews import _MAX_COMMENT_CHARS

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = (
    "templates/admin/_assessments_body.html",
    "templates/admin/_assessment_detail_body.html",
)
TEXTAREA = re.compile(r'<textarea\b[^>]*\bname="comment"[^>]*>')


def test_every_review_comment_textarea_carries_the_cap():
    tags = [
        (rel, tag)
        for rel in TEMPLATES
        for tag in TEXTAREA.findall((ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert len(tags) == 3
    for rel, tag in tags:
        assert f'maxlength="{_MAX_COMMENT_CHARS}"' in tag, (rel, tag)
