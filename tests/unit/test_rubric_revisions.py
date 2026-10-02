"""The revision registry: stored assessments render against the revision that
scored them, never silently against the live document (audit A3)."""
import dataclasses
import re

import pytest

from src.services import rubric_revisions as rr
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, load_rubric
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


# ---------------------------------------------------------------------------
# Gate definitions (hub 1.10.0, docs/specs/2026-10-02-hub-1-10-summary-risks-gates-design.md
# §5.3): the 3.2.0 and 3.4.0 entries carry [revision.gating.<key>] tables.
# ---------------------------------------------------------------------------

_GATED = (("3.2.0", "42aec0479ac6"), ("3.4.0", "b7b0a1d6a4a5"))


@pytest.mark.parametrize("version, content_hash", _GATED, ids=["3.2.0", "3.4.0"])
def test_the_gated_entries_carry_todays_gate_text_verbatim(version, content_hash):
    """F3: the [gating.*] section is byte-identical in both outgoing documents and the
    live one, so the registry copy must parse to exactly the live values."""
    view, provenance = resolve_revision(version, content_hash)
    assert provenance == PROVENANCE_ARCHIVED
    assert view.gating == load_rubric().gating


def test_entries_without_gate_tables_carry_an_empty_map():
    ungated = {
        v.version: v.gating for v in rr._ARCHIVED if (v.version, v.content_hash) not in _GATED
    }
    assert set(ungated) == {"1.0.0", "2.0.0", "2.1.0", "2.2.0", "3.0.0", "3.3.0"}
    assert all(gating == {} for gating in ungated.values())


def test_the_live_view_carries_a_copy_of_the_live_gates():
    view = live_revision_view()
    assert view.gating == load_rubric().gating
    view.gating["credible_science"]["title"] = "mutated by a caller"
    assert load_rubric().gating["credible_science"]["title"] != "mutated by a caller"


def test_gate_tables_stay_out_of_the_pinned_repr_equality_and_hash():
    view, _provenance = resolve_revision("3.4.0", "b7b0a1d6a4a5")
    assert "gating=" not in repr(view)
    assert dataclasses.replace(view, gating={}) == view
    assert hash(view) == hash(dataclasses.replace(view, gating={}))


def test_the_live_rubric_file_is_untouched():
    """Any byte change re-hashes it and turns every 3.5.0 row unknown (spec §5.3)."""
    assert RUBRIC_CONTENT_HASH == "f0b452e95057"


_MINIMAL_REGISTRY = """
[[revision]]
version = "0.0.1"
content_hash = "0123456789ab"

  [[revision.dimension]]
  key = "d"
  title = "D"
  weight = 100
"""


def _registry(tmp_path, gate_toml: str):
    path = tmp_path / "revisions.toml"
    path.write_text(_MINIMAL_REGISTRY + gate_toml, encoding="utf-8")
    return path


def test_a_gate_table_parses_into_the_view(tmp_path):
    [view] = rr._parse_registry(_registry(
        tmp_path, '\n  [revision.gating.g]\n  title = "G"\n  description = "the gate"\n'
    ))
    assert view.gating == {"g": {"title": "G", "description": "the gate"}}


def test_an_entry_without_gate_tables_parses_to_an_empty_map(tmp_path):
    [view] = rr._parse_registry(_registry(tmp_path, ""))
    assert view.gating == {}


@pytest.mark.parametrize("gate_toml, message", [
    ('\n  [revision.gating.g]\n  title = "G"\n', "revision[0].gating.g.description"),
    ('\n  [revision.gating.g]\n  description = "d"\n', "revision[0].gating.g.title"),
    ('\n  [revision.gating.g]\n  title = ""\n  description = "d"\n', "revision[0].gating.g.title"),
    ('\n  [revision.gating]\n  g = "not a table"\n', "revision[0].gating.g must be a table"),
    ("\n  [revision.gating]\n", "revision[0].gating must hold"),
], ids=["no-description", "no-title", "blank-title", "not-a-table", "empty"])
def test_a_malformed_gate_table_fails_at_import(tmp_path, gate_toml, message):
    with pytest.raises(rr.RevisionRegistryError, match=re.escape(message)):
        rr._parse_registry(_registry(tmp_path, gate_toml))
