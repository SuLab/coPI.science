"""Data deal-breaker flags (plan Task 4.3; spec §7.4 step 5, L24).

The Open Targets payload is an invented fixture in the shape of the ``target`` query's fields;
the Mondo hierarchy is faked.
"""
from src.landscaping import flags
from src.landscaping.flags import FLAG_NAMES, combine_flags, data_flags

DISEASE = "MONDO:0000100"
CHILD = "MONDO:0000101"

OT_TARGET = {
    "id": "ENSG00000000001",
    "isEssential": True,
    "depMapEssentiality": [{"tissueName": "invented tissue", "isEssential": True}],
    "safetyLiabilities": [{"event": "invented cardiac event", "datasource": "Invented DB"}],
    "drugAndClinicalCandidates": {
        "count": 2,
        "rows": [
            {"drug": {"id": "CHEMBL0000001", "name": "INVENTOMAB"},
             "diseases": [{"disease": {"id": "MONDO_0000101", "name": "invented subtype"}}]},
            {"drug": {"id": "CHEMBL0000002", "name": "OTHERNIB"},
             "diseases": [{"disease": {"id": "EFO_0000001", "name": "unrelated"}}]},
        ],
    },
}


def _ancestor(monkeypatch):
    monkeypatch.setattr(flags, "is_ancestor", lambda a, b: (a, b) == (DISEASE, CHILD))


def test_flags_from_fixture_ot_payload(monkeypatch):
    _ancestor(monkeypatch)
    out = data_flags(
        OT_TARGET, OT_TARGET["drugAndClinicalCandidates"], {"by_phase": {"phase1": 0}},
        disease=DISEASE,
    )
    assert set(out) == set(FLAG_NAMES)
    assert out["target_essential"]["raised"] is True
    assert out["target_essential"]["evidence"]["isEssential"] is True
    assert out["safety_liabilities"]["raised"] is True
    assert out["safety_liabilities"]["evidence"][0]["event"] == "invented cardiac event"
    # A candidate for a Mondo descendant of the idea's disease counts; an unrelated one not.
    clinic = out["same_target_in_clinic"]
    assert clinic["raised"] is True
    assert [c["candidate"] for c in clinic["evidence"]["candidates"]] == ["INVENTOMAB"]

    quiet = data_flags(
        {"isEssential": False, "safetyLiabilities": []}, {"rows": []}, {}, disease=DISEASE
    )
    assert not any(quiet[name]["raised"] for name in FLAG_NAMES)

    # Trials alone raise same_target_in_clinic; with no disease nothing can.
    assert data_flags({}, {}, {"total": 3}, disease=DISEASE)["same_target_in_clinic"]["raised"]
    assert not data_flags(OT_TARGET, OT_TARGET["drugAndClinicalCandidates"], {"total": 3},
                          disease="")["same_target_in_clinic"]["raised"]


def test_combine_flags_any_target_raises(monkeypatch):
    _ancestor(monkeypatch)
    raised = data_flags(OT_TARGET, {}, {}, disease=DISEASE)
    quiet = data_flags({}, {}, {}, disease=DISEASE)
    out = combine_flags({"HGNC:1": quiet, "HGNC:2": raised})
    assert out["target_essential"]["raised"] is True
    assert list(out["target_essential"]["evidence"]) == ["HGNC:2"]
    assert out["same_target_in_clinic"]["raised"] is False
    assert set(out["same_target_in_clinic"]["evidence"]) == {"HGNC:1", "HGNC:2"}
    assert combine_flags({})["safety_liabilities"] == {"raised": False, "evidence": {}}
