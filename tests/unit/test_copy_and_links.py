"""B-09 and D-28 (copy that no longer describes the page) and B-18 (encoded lab filter)."""

from pathlib import Path

T = Path(__file__).resolve().parents[2] / "templates"


def test_the_manager_detail_page_does_not_call_itself_read_only():
    assert "Read-only view." not in (T / "manager/assessment_detail.html").read_text()


def test_the_agent_request_page_has_no_stale_copy():
    text = (T / "agent/request.html").read_text()
    assert "Scripps" not in text
    assert "admin will review and provision" not in text


def test_review_tab_links_encode_the_lab_filter():
    for name in ("admin/assessments.html", "manager/assessments.html"):
        text = (T / name).read_text()
        assert "lab={{ (lab_filter or '') | urlencode }}" in text, name
        assert "lab={{ lab_filter or '' }}" not in text, name
