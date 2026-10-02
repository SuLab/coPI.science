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
