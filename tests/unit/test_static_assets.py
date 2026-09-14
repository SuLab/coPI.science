"""Static checks for the Tailwind v4 build pipeline and its templates: no
stray CDN script tag, a linked compiled stylesheet with a cache-busting
version, a source CSS file shaped for the v4 build, matching pinned
version/checksum across the Dockerfile and the host build script, no
interpolated Tailwind color-utility fragments, and a gzip size budget on the
compiled output.
"""

import gzip
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates"

CDN_ALLOWED_TEMPLATE = "cabo_graph.html"


def _all_templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_no_template_except_cabo_graph_uses_the_tailwind_cdn():
    offenders = [
        p
        for p in _all_templates()
        if p.name != CDN_ALLOWED_TEMPLATE and "cdn.tailwindcss.com" in p.read_text()
    ]
    assert not offenders, f"CDN script still present in: {offenders}"


def test_base_html_links_the_compiled_stylesheet_with_a_safe_viewport():
    text = (TEMPLATES_DIR / "base.html").read_text()
    assert 'href="/static/css/app.min.css?v=' in text
    _assert_safe_viewport(text)


def test_unsubscribe_html_links_the_compiled_stylesheet_with_a_safe_viewport():
    text = (TEMPLATES_DIR / "unsubscribe.html").read_text()
    assert 'href="/static/css/app.min.css?v=' in text
    _assert_safe_viewport(text)


def test_discussions_export_html_links_the_compiled_stylesheet_with_a_safe_viewport():
    text = (TEMPLATES_DIR / "admin" / "discussions_export.html").read_text()
    # Absolute: the export is downloaded as an attachment, so a root-relative URL
    # would resolve against file:// when the saved document is opened.
    assert 'href="{{ request.base_url }}static/css/app.min.css?v=' in text
    _assert_safe_viewport(text)


def _assert_safe_viewport(text: str) -> None:
    viewport_match = re.search(r'<meta\s+name="viewport"\s+content="([^"]*)"', text)
    assert viewport_match, "expected a viewport meta tag"
    content = viewport_match.group(1)
    assert "maximum-scale" not in content
    assert "user-scalable" not in content


def test_app_css_is_shaped_for_the_v4_build():
    text = (REPO_ROOT / "assets" / "css" / "app.css").read_text()
    assert "source(none)" in text
    assert '@source "../../templates"' in text


def test_dockerfile_and_build_script_pin_the_same_tailwind_version_and_sha256():
    dockerfile_text = (REPO_ROOT / "Dockerfile").read_text()
    script_text = (REPO_ROOT / "scripts" / "build-css.sh").read_text()

    version_re = re.compile(r"TAILWIND_VERSION=(v4\.\S+?)(?:\s|$|\")")
    sha_re = re.compile(r"TAILWIND_SHA256=([0-9a-f]{64})")

    dockerfile_version = version_re.search(dockerfile_text)
    script_version = version_re.search(script_text)
    dockerfile_sha = sha_re.search(dockerfile_text)
    script_sha = sha_re.search(script_text)

    assert dockerfile_version and script_version
    assert dockerfile_sha and script_sha
    assert dockerfile_version.group(1) == script_version.group(1)
    assert dockerfile_sha.group(1) == script_sha.group(1)


def test_no_template_interpolates_a_tailwind_color_utility_fragment():
    # `class="..."` attributes may wrap across lines, so this scans the whole
    # file rather than line-by-line; `[^"]` matches newlines too since no
    # flag narrows it, so a multi-line class value is still captured whole.
    pattern = re.compile(r'class="[^"]*(?:-\{\{|\}\}-)[^"]*"')
    offenders = []
    for path in _all_templates():
        text = path.read_text()
        for match in pattern.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, f"dynamic Tailwind utility fragments found: {offenders}"


def test_compiled_stylesheet_gzip_size_budget():
    css_path = REPO_ROOT / "static" / "css" / "app.min.css"
    if not css_path.exists():
        pytest.skip("run scripts/build-css.sh")
    gzipped = gzip.compress(css_path.read_bytes())
    assert len(gzipped) < 60 * 1024
