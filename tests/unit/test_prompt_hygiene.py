"""Doc-accuracy check for specs/email-proposal-review.md's file table (issue
#26 DOC-5). prompts/email-reply-classify.md has zero code readers —
classify_reply (src/services/email_inbound.py) builds its prompt inline —
but the spec's "New Files" table still describes it as a live, code-loaded
prompt. No prompts/ file is touched by this PR; this only corrects the
stale claim about it.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_email_proposal_review_spec_no_longer_lists_it_as_a_live_file():
    spec = (REPO_ROOT / "specs" / "email-proposal-review.md").read_text()
    assert "`prompts/email-reply-classify.md` | LLM prompt for classifying" not in spec
    assert "email_inbound.py" in spec
