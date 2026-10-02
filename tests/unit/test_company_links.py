"""The company source links on the detail brief (spec §7.4) are built in Python, the
way prose_citations builds citation links, and only from an http(s) URL with a host."""
from src.services.assessment_detail import _company_link


def test_only_an_http_url_with_a_host_becomes_a_link():
    assert _company_link("javascript:alert(1)", "source") is None
    assert _company_link("ftp://example.org/x", "source") is None
    assert _company_link("https://", "source") is None
    assert _company_link(None, "source") is None
    assert _company_link("", "source") is None


def test_a_link_is_escaped_and_carries_the_citation_link_attributes():
    link = _company_link(" https://example.org/a?b=1&c=2 ", "source")
    assert str(link) == (
        '<a class="citation-link" href="https://example.org/a?b=1&amp;c=2"'
        ' title="https://example.org/a?b=1&amp;c=2" rel="noreferrer">source</a>'
    )
