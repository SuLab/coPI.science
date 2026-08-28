"""The revision registry: stored assessments render against the revision that
scored them, never silently against the live document (audit A3)."""
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
    PROVENANCE_UNSTAMPED,
    live_revision_view,
    resolve_revision,
)


def test_the_live_view_mirrors_the_loaded_document():
    view = live_revision_view()
    assert view.version == RUBRIC_VERSION
    assert view.content_hash == RUBRIC_CONTENT_HASH
    assert view.advance_min is not None and view.conditional_min is not None
    assert all(d.weight_note == f"{d.weight}%" for d in view.dimensions)


def test_resolving_the_live_stamp_returns_the_live_view():
    view, provenance = resolve_revision(RUBRIC_VERSION, RUBRIC_CONTENT_HASH)
    assert provenance == PROVENANCE_LIVE
    assert view.content_hash == RUBRIC_CONTENT_HASH


def test_an_archived_hash_resolves_to_its_registry_entry():
    view, provenance = resolve_revision("2.1.0", "2f38fc9bce4d")
    assert provenance == PROVENANCE_ARCHIVED
    assert view.advance_min == 4.0 and view.conditional_min == 3.0
    keys = [d.key for d in view.dimensions]
    assert len(keys) == 13 and "ip_fto" in keys and "chemistry_dc_path" in keys
    ip_fto = next(d for d in view.dimensions if d.key == "ip_fto")
    assert ip_fto.weight_note == "6%/4% (investment/incubation)"
    assert view.banding_note, "dual-scale caveat must be recorded"


def test_a_version_resolves_without_a_hash_only_when_unambiguous():
    view, provenance = resolve_revision("3.0.0", None)
    assert provenance == PROVENANCE_ARCHIVED
    assert [d.key for d in view.dimensions][:2] == [
        "differentiation_unmet_need", "scientific_credibility",
    ]
    assert view.advance_min == 3.4 and view.conditional_min == 2.8


def test_an_unmatched_stamp_is_unknown_never_guessed():
    view, provenance = resolve_revision("9.9.9", "deadbeef0000")
    assert (view, provenance) == (None, PROVENANCE_UNKNOWN)
    # A known version with a WRONG hash is a different document — unknown too.
    view, provenance = resolve_revision("2.1.0", "deadbeef0000")
    assert (view, provenance) == (None, PROVENANCE_UNKNOWN)


def test_no_stamp_at_all_reads_against_the_live_document():
    view, provenance = resolve_revision(None, None)
    assert provenance == PROVENANCE_UNSTAMPED
    assert view.content_hash == RUBRIC_CONTENT_HASH


def test_registry_hashes_are_unique_and_never_shadow_the_live_document():
    from src.services.rubric_revisions import _ARCHIVED
    hashes = [v.content_hash for v in _ARCHIVED]
    assert len(hashes) == len(set(hashes))
    assert RUBRIC_CONTENT_HASH not in hashes, (
        "the live document is derived at read time, never duplicated into the registry"
    )
