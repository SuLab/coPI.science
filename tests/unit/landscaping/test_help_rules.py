"""The landscape help keeps the help content rules (docs/operations/help-content.md, "Content
rules"; spec §13.6): no number other than the 1-5 rating and the 10,000-character comment limit,
staff-only content inside ``{% if permissions.staff %}`` on pages reviewers can open, and no
repository paths.

Text is the template source with its Jinja tags, expressions and comments removed, so a route
name or a ``{% if %}`` condition is not read as prose.
"""
import re
from pathlib import Path

import pytest

from src.web import help_pages as hp

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
LANDSCAPE_TEMPLATES = (
    "help/pages/landscape.html",
    "help/tutorials/reviewer/09-landscape.html",
    "help/tutorials/manager/11-landscape.html",
)
ALLOWED_NUMBERS = frozenset({"1", "2", "3", "4", "5", "10,000"})
#: Labels and route names of staff-only Landscape features. None may reach a reviewer.
STAFF_ONLY = (
    "workspace_landscape_pipeline", "Pause", "Resume", "Rescue", "Positive controls",
    "All assessed", "Current", "backfill", "probe",
)

_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.DOTALL)
_TAG = re.compile(r"<[^>]*>")
_NUMBER = re.compile(r"\d(?:[\d,]*\d)?")
_PATH = re.compile(r"[\w.-]+/[\w./-]+|\b[\w-]+\.(?:py|html|toml|md|json|ya?ml|txt|png|css|js)\b")
_IF_TAG = re.compile(r"\{%-?\s*(if|elif|else|endif)\b(.*?)-?%\}", re.DOTALL)
#: Conditions true only for staff: admins are staff.
_STAFF_CONDITION = re.compile(r"^\s*permissions\.(staff|admin)\s*$")


def _source(template: str) -> str:
    return (TEMPLATES / template).read_text(encoding="utf-8")


def _prose(source: str) -> str:
    return _TAG.sub(" ", _JINJA.sub(" ", source))


def _reviewer_visible(source: str) -> str:
    """The source a non-staff viewer can render: the text outside every ``{% if
    permissions.staff %}`` (or ``permissions.admin``) branch, with that ``if``'s ``else``
    branch kept. Branches of any other condition are all kept, so this over-approximates."""
    out: list[str] = []
    stack: list[list[bool]] = []  # [staff_gated, in_else]
    pos = 0

    def visible() -> bool:
        return all(not gated or in_else for gated, in_else in stack)

    for m in _IF_TAG.finditer(source):
        if visible():
            out.append(source[pos:m.start()])
        pos = m.end()
        kind, condition = m.group(1), m.group(2)
        if kind == "if":
            stack.append([bool(_STAFF_CONDITION.match(condition)), False])
        elif kind in ("elif", "else"):
            stack[-1][1] = True
        else:
            stack.pop()
    assert not stack, "unbalanced {% if %} tags"
    out.append(source[pos:])
    return "".join(out)


@pytest.mark.parametrize("template", LANDSCAPE_TEMPLATES)
def test_no_numbers_but_the_rating_and_the_comment_limit(template):
    numbers = set(_NUMBER.findall(_prose(_source(template))))
    assert numbers <= ALLOWED_NUMBERS, f"{template} states {sorted(numbers - ALLOWED_NUMBERS)}"


@pytest.mark.parametrize("template", LANDSCAPE_TEMPLATES)
def test_no_file_paths(template):
    assert _PATH.findall(_prose(_source(template))) == [], template


def _all_audience_templates() -> list[str]:
    pages = [p.template for p in hp.HELP_PAGES if p.audience == hp.AUDIENCE_ALL]
    steps = [s.template for t in hp.TUTORIALS if t.audience == hp.AUDIENCE_ALL for s in t.steps]
    return [t for t in (*pages, *steps) if t in LANDSCAPE_TEMPLATES]


def test_the_landscape_page_and_reviewer_step_are_for_everyone_and_the_manager_step_for_staff():
    assert _all_audience_templates() == [
        "help/pages/landscape.html", "help/tutorials/reviewer/09-landscape.html"]
    manager = next(t for t in hp.TUTORIALS if t.key == "manager")
    assert manager.audience == hp.AUDIENCE_STAFF
    assert manager.steps[-1].template == "help/tutorials/manager/11-landscape.html"


@pytest.mark.parametrize("template", ["help/pages/landscape.html",
                                      "help/tutorials/reviewer/09-landscape.html"])
def test_staff_only_content_sits_inside_a_staff_block(template):
    source = _source(template)
    visible = _reviewer_visible(source)
    leaked = [label for label in STAFF_ONLY if label in visible]
    assert leaked == [], f"{template} shows reviewers staff-only content: {leaked}"


def test_the_pipeline_section_is_written_for_staff():
    """Control: the staff text exists, and only the staff block hides it."""
    source = _source("help/pages/landscape.html")
    assert "workspace_landscape_pipeline" in source and "Rescue" in source
    assert "workspace_landscape_pipeline" not in _reviewer_visible(source)


def test_the_extractor_drops_staff_branches_and_keeps_their_else():
    sample = ("a{% if permissions.staff %}b{% if x %}c{% endif %}{% else %}d{% endif %}"
              "{% if permissions.admin %}e{% endif %}{% if microsoft_sign_in %}f{% else %}g"
              "{% endif %}")
    assert _reviewer_visible(sample) == "adfg"
