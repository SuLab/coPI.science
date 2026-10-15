"""The Help registry (src/web/help_pages.py; spec docs/specs/2026-10-14-help-and-onboarding-design.md
§4.2, §6.1, §10.1): templates exist, slugs are unique and unreserved, tutorials have their
step counts, declared anchors and screenshots appear in their templates, and the role gate
and step parsing answer 403 and 404."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from src.services.web_permissions import WebCapabilities
from src.web import help_pages as hp

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
STAFF = WebCapabilities(
    research=True, staff=True, admin=False, pi_surfaces=False, verify_pi_email=True,
    assign_reviews=True, manage_suggestions=True, chat=True,
)
REVIEWER = WebCapabilities(
    research=True, staff=False, admin=False, pi_surfaces=False, verify_pi_email=False,
    assign_reviews=False, manage_suggestions=False, chat=True,
)


def test_slugs_unique_and_not_reserved():
    slugs = [p.slug for p in hp.HELP_PAGES]
    assert len(slugs) == len(set(slugs))
    assert not set(slugs) & hp.RESERVED_SLUGS


def test_every_template_is_a_literal_existing_file():
    names = [p.template for p in hp.HELP_PAGES] + [
        s.template for t in hp.TUTORIALS for s in t.steps]
    for name in names:
        assert name.startswith("help/") and name.endswith(".html")
        assert (TEMPLATES / name).is_file(), name


def test_tutorial_shapes():
    keys = {t.key: t for t in hp.TUTORIALS}
    assert len(keys["reviewer"].steps) == 8
    assert len(keys["manager"].steps) == 10
    assert [t.key for t in hp.tutorials_for(STAFF)] == ["manager", "reviewer"]
    assert [t.key for t in hp.tutorials_for(REVIEWER)] == ["reviewer"]


def test_pages_for_filters_staff_pages():
    assert hp.pages_for(STAFF) == hp.HELP_PAGES
    reviewer_pages = hp.pages_for(REVIEWER)
    assert reviewer_pages
    assert all(p.audience == hp.AUDIENCE_ALL for p in reviewer_pages)
    assert [p.slug for p in hp.HELP_PAGES if p.audience == hp.AUDIENCE_ALL] == [
        p.slug for p in reviewer_pages]


def test_each_page_template_renders_its_declared_anchors():
    for page in hp.HELP_PAGES:
        source = (TEMPLATES / page.template).read_text(encoding="utf-8")
        for anchor in page.anchors:
            assert f'id="{anchor}"' in source, (page.slug, anchor)


def test_each_step_template_shows_its_declared_shots():
    for tutorial in hp.TUTORIALS:
        for step in tutorial.steps:
            source = (TEMPLATES / step.template).read_text(encoding="utf-8")
            for shot in step.shots:
                assert (f'figure(request, "{shot}"' in source
                        or f"figure(request, '{shot}'" in source), (step.template, shot)


def test_reviewer_cannot_open_staff_pages_or_manager_tutorial():
    with pytest.raises(HTTPException) as exc:
        hp.lookup_page("managing-pis", REVIEWER)
    assert exc.value.status_code == 403 and exc.value.detail == "Manager access required"
    with pytest.raises(HTTPException) as exc:
        hp.lookup_tutorial("manager", REVIEWER)
    assert exc.value.status_code == 403


def test_unknown_page_and_tutorial_are_404():
    for call in (lambda: hp.lookup_page("nope", STAFF), lambda: hp.lookup_tutorial("nope", STAFF)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


@pytest.mark.parametrize("step", ["0", "9", "01", "1.0", "²", "-1", "1000", "", " 1"])
def test_tutorial_step_rejects_non_canonical_numbers(step):
    with pytest.raises(HTTPException) as exc:
        hp.lookup_step("reviewer", step, REVIEWER)
    assert exc.value.status_code == 404


def test_tutorial_step_accepts_every_step_in_range():
    tutorial = next(t for t in hp.TUTORIALS if t.key == "manager")
    for number in range(1, len(tutorial.steps) + 1):
        found, n, step = hp.lookup_step("manager", str(number), STAFF)
        assert found is tutorial and n == number and step is tutorial.steps[number - 1]


def test_tutorial_key_for_roles():
    assert hp.tutorial_key_for("manager") == "manager"
    assert hp.tutorial_key_for("reviewer") == "reviewer"
    assert hp.tutorial_key_for("admin") is None
    assert hp.tutorial_key_for("pi") is None
