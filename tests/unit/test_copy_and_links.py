"""B-09 and D-28 (copy that no longer describes the page) and B-18 (encoded lab filter)."""

from pathlib import Path

T = Path(__file__).resolve().parents[2] / "templates"


def test_the_manager_detail_page_does_not_call_itself_read_only():
    assert "Read-only view." not in (T / "workspace/assessment_detail.html").read_text()


def test_the_agent_request_page_has_no_stale_copy():
    text = (T / "agent/request.html").read_text()
    assert "Scripps" not in text
    assert "admin will review and provision" not in text


def test_review_tab_links_encode_the_lab_filter():
    from fastapi import Request

    from src.main import create_app
    from src.web.urls import page_url

    text = (T / 'workspace/assessments.html').read_text()
    assert "page_url(request, 'workspace_assessments', query=" in text
    assert "'lab': lab_filter or ''" in text
    app = create_app()
    request = Request({'type': 'http', 'app': app})
    url = page_url(request, 'workspace_assessments', query={'lab': 'A&B/<script>', 'review': 'all'})
    assert url == '/workspace/assessments?lab=A%26B%2F%3Cscript%3E&review=all'
