"""C-02: the downloadable export is opened from disk, where /static/js cannot load,
so proposal markdown is rendered on the server — raw HTML escaped, images as
links, script URLs dropped."""

from src.services.export_markdown import render_export_markdown


def test_markdown_is_rendered():
    assert "<strong>Bold plan</strong>" in render_export_markdown("**Bold plan**")


def test_raw_html_is_escaped():
    html = render_export_markdown("<script>window.__x=1</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;window.__x=1&lt;/script&gt;" in html


def test_an_image_renders_as_a_link_not_an_img():
    html = render_export_markdown("![pic](https://evil.example/p.png)")
    assert "<img" not in html
    assert 'href="https://evil.example/p.png"' in html


def test_a_javascript_link_is_not_a_link():
    assert 'href="javascript:' not in render_export_markdown("[js](javascript:alert(1))")


def test_empty_text_renders_empty():
    assert render_export_markdown("") == ""


def test_only_web_mail_and_fragment_links_survive():
    """The export is opened from disk with no CSP: only the page sanitizer's schemes."""
    kept = render_export_markdown("[a](https://x.example/) [b](mailto:a@x.example) [c](#top)")
    assert 'href="https://x.example/"' in kept and 'href="mailto:a@x.example"' in kept
    assert 'href="#top"' in kept
    for url in ("search-ms:query=x", "ms-msdt:/id", "../../etc/passwd", "file:///etc/passwd",
                "//evil.example/x", "data:text/html,x"):
        assert "href=" not in render_export_markdown(f"[x]({url})"), url
    assert "href=" not in render_export_markdown("<search-ms:query=x>")
