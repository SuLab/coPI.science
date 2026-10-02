"""Compiled Tailwind (spec §6.1; A-03, D4): pinned build, committed output, and a
safelist derived from every place a utility class is assembled at runtime."""

import re
from pathlib import Path

from src.services.bands import BANDS, band_class

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
CONFIG = (ROOT / "tailwind.config.js").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_css.sh").read_text(encoding="utf-8")
CSS_PATH = ROOT / "static" / "css" / "app.css"

#: The (strong, muted) shapes band_class is called with in templates (R5).
BAND_CALLS = {(700, 600)}

#: Template sites that splice a value into a utility name (R1-R4). A new site must
#: add its classes to tailwind.config.js's safelist and its entry here.
CONSTRUCTED_SITES = {
    ("admin/discussions.html", "meta.color"),
    ("manager/discussions.html", "meta.color"),
    ("admin/_discussions_threads.html", "meta.color"),
    ("admin/jobs.html", "color"),
}
#: The same for src/ and static/js (R5).
CONSTRUCTED_CODE_LINES = {"src/services/bands.py:43", "src/services/bands.py:44"}

_TEMPLATE_SPLICE = re.compile(
    r"(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border|ring|from|via|to|fill|stroke|divide|outline"
    r"|decoration|shadow|accent|caret|placeholder)-\{\{\s*([\w.]+)\s*\}\}"
)
_CODE_SPLICE = re.compile(r"""(?<![\w-])(?:bg|text|border|ring)-(?:[a-z]+-)?(?:\{|["'`]\s*\+|\$\{)""")


def _safelist() -> set[str]:
    block = CONFIG[CONFIG.index("safelist: [") :]
    block = block[: block.index("],")]
    return set(re.findall(r'"([^"]+)"', block))


def _text(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _status_meta_colours(name: str) -> set[str]:
    text = _text(name)
    block = text[text.index("{% set status_meta = {") :]
    block = block[: block.index("} %}")]
    return set(re.findall(r"'color':\s*'(\w+)'", block))


def _job_colours() -> set[str]:
    text = _text("admin/jobs.html")
    block = text[text.index("{% for status, label, color in [") :]
    block = block[: block.index("] %}")]
    return set(re.findall(r"\('\w+', '[^']+', '(\w+)'\)", block))


def test_the_build_is_pinned_and_verified():
    assert 'TAILWIND_VERSION="v3.4.19"' in BUILD
    assert 'TAILWIND_ASSET="tailwindcss-linux-x64"' in BUILD
    assert 'TAILWIND_SHA256="4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e"' in BUILD
    assert "sha256sum --check --status" in BUILD
    assert '.tools' in BUILD


def test_the_config_scans_the_spec_globs():
    for glob in ('"./templates/**/*.html"', '"./static/js/**/*.js"', '"./src/**/*.py"'):
        assert glob in CONFIG, glob


def test_the_tools_directory_is_ignored_and_kept_out_of_the_build_context():
    assert ".tools/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".tools" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_band_classes_are_safelisted():
    produced = {band_class(b.name, s, m) for b in BANDS for s, m in BAND_CALLS}
    produced |= {band_class(None, s, m) for s, m in BAND_CALLS}
    assert produced <= _safelist(), sorted(produced - _safelist())


def test_band_class_is_called_only_with_the_inventoried_shapes():
    calls = set()
    for path in TEMPLATES.rglob("*.html"):
        for a, b in re.findall(r"band_class\([^,()]+,\s*(\d+),\s*(\d+)\)", path.read_text(encoding="utf-8")):
            calls.add((int(a), int(b)))
    assert calls == BAND_CALLS


def test_discussion_status_colours_are_safelisted():
    colours = (
        _status_meta_colours("admin/discussions.html")
        | _status_meta_colours("manager/discussions.html")
        | {"gray"}  # _discussions_threads.html's fallback
    )
    wanted = set()
    for c in colours:
        wanted |= {f"hover:border-{c}-300", f"ring-{c}-400", f"bg-{c}-100", f"text-{c}-700"}
    assert wanted <= _safelist(), sorted(wanted - _safelist())


def test_job_status_colours_are_safelisted():
    wanted = {f"text-{c}-700" for c in _job_colours()}
    assert wanted
    assert wanted <= _safelist(), sorted(wanted - _safelist())


def test_no_new_runtime_built_classes_in_templates():
    found = set()
    for path in TEMPLATES.rglob("*.html"):
        rel = path.relative_to(TEMPLATES).as_posix()
        for m in _TEMPLATE_SPLICE.finditer(path.read_text(encoding="utf-8")):
            found.add((rel, m.group(1)))
    assert found == CONSTRUCTED_SITES


def test_no_new_runtime_built_classes_in_src_or_js():
    hits = set()
    for path in [*(ROOT / "src").rglob("*.py"), *(ROOT / "static" / "js").rglob("*.js")]:
        if "__pycache__" in path.parts:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _CODE_SPLICE.search(line):
                hits.add(f"{path.relative_to(ROOT).as_posix()}:{i}")
    assert hits == CONSTRUCTED_CODE_LINES


def test_the_committed_stylesheet_is_the_pinned_build_and_holds_the_safelist():
    css = CSS_PATH.read_text(encoding="utf-8")
    # v3.4.19 emits the --tw-* variable reset before its preflight banner, even minified.
    assert "/*! tailwindcss v3.4.19 | MIT License" in css[:4096]
    missing = [c for c in sorted(_safelist()) if "." + c.replace(":", "\\:") not in css]
    assert missing == []


def test_base_links_the_compiled_stylesheet_and_no_template_loads_the_cdn():
    base = _text("base.html")
    assert '<link rel="stylesheet" href="/static/css/app.css">' in base
    for path in TEMPLATES.rglob("*.html"):
        assert "cdn.tailwindcss.com" not in path.read_text(encoding="utf-8"), path
