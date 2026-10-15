"""The committed Help screenshots against the templates, the registry, shots.py and the seed
(spec docs/specs/2026-10-14-help-and-onboarding-design.md §8, §10.1). No browser, no Docker.

Fails until ``python -m tests.e2e.help_shots.capture`` has written help_assets/shots/.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from src.services.pubmed import _normalize_author_orcid
from src.web.help_pages import AUDIENCE_ALL, AUDIENCE_STAFF, HELP_PAGES, TUTORIALS
from tests.e2e.help_shots import seed
from tests.e2e.help_shots.shots import SHOTS

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "templates"
SHOTS_DIR = REPO_ROOT / "help_assets" / "shots"
MAX_TOTAL_BYTES = 4 * 1024 * 1024
_FIGURE = re.compile(r"""figure\(\s*request\s*,\s*["']([^"']+)["']""")


def _manifest() -> dict:
    return json.loads((SHOTS_DIR / "manifest.json").read_text(encoding="utf-8"))


def _referenced() -> dict[str, set[str]]:
    """Template path under templates/ -> the shot ids its figure() calls name."""
    found = {}
    for path in sorted((TEMPLATES / "help").rglob("*.html")):
        ids = set(_FIGURE.findall(path.read_text(encoding="utf-8")))
        if ids:
            found[path.relative_to(TEMPLATES).as_posix()] = ids
    return found


def _template_audiences() -> dict[str, str]:
    audiences = {page.template: page.audience for page in HELP_PAGES}
    for tutorial in TUTORIALS:
        for step in tutorial.steps:
            audiences[step.template] = tutorial.audience
    return audiences


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", path
    return struct.unpack(">II", head[16:24])


def test_every_referenced_shot_has_a_manifest_entry_and_a_png():
    manifest = _manifest()
    for template, ids in _referenced().items():
        for shot_id in ids:
            assert shot_id in manifest, f"{template}: {shot_id} not in the manifest"
            assert (SHOTS_DIR / f"{shot_id}.png").is_file(), f"{template}: {shot_id}.png missing"


def test_no_png_without_a_manifest_entry():
    manifest = _manifest()
    assert sorted(p.stem for p in SHOTS_DIR.glob("*.png") if p.stem not in manifest) == []


def test_every_entry_has_alt_text():
    assert [i for i, entry in _manifest().items() if not str(entry.get("alt", "")).strip()] == []


def test_png_dimensions_match_the_manifest():
    for shot_id, entry in _manifest().items():
        assert _png_size(SHOTS_DIR / f"{shot_id}.png") == (entry["width"], entry["height"]), shot_id


def test_all_audience_templates_reference_only_all_audience_shots():
    manifest, audiences = _manifest(), _template_audiences()
    for template, ids in _referenced().items():
        if audiences.get(template) == AUDIENCE_ALL:
            staff = sorted(i for i in ids if manifest.get(i, {}).get("audience") != AUDIENCE_ALL)
            assert staff == [], f"{template} shows non-'all' shots to reviewers: {staff}"


def test_pngs_total_at_most_4_mb():
    assert sum(p.stat().st_size for p in SHOTS_DIR.glob("*.png")) <= MAX_TOTAL_BYTES


def test_capture_account_follows_the_audience():
    """An all-audience shot taken as a manager would show reviewers staff-only UI; the
    check reads the seeded account's role, not its key."""
    expected = {AUDIENCE_ALL: "reviewer", AUDIENCE_STAFF: "manager"}
    for shot in SHOTS:
        assert shot.audience in expected, shot.id
        assert seed.ACCOUNT_ROLES.get(shot.account) == expected[shot.audience], shot.id


def test_shot_list_matches_the_templates_and_the_registry():
    shot_ids = {shot.id for shot in SHOTS}
    assert len(shot_ids) == len(SHOTS)
    assert shot_ids == set().union(*_referenced().values())
    step_shots = {i for t in TUTORIALS for step in t.steps for i in step.shots}
    assert step_shots <= shot_ids


def test_manifest_audience_matches_shots_py():
    manifest = _manifest()
    assert {s.id: s.audience for s in SHOTS if s.id in manifest} == {
        i: e["audience"] for i, e in manifest.items() if i in {s.id for s in SHOTS}}


def test_seed_orcids_fail_the_mod_11_2_check():
    """An iD with a wrong check digit can never be issued to anyone (spec §8.1)."""
    orcids = [*seed.SEED_ORCIDS.values(), seed.STAFF_PREVIEW_ORCID]
    assert len(set(orcids)) == len(orcids)
    for orcid in orcids:
        assert re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid), orcid
        assert _normalize_author_orcid(orcid) is None, orcid
