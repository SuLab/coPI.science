"""S3 agreement by the targets and diseases an extraction names (spec §10.1, D74).

Every gene, disease and id below is invented."""
from types import SimpleNamespace

import pytest

from src.landscaping.eval.bakeoff import (
    EXTRACT_MIN_RELATIVE,
    expected_identities,
    extract_agreement,
    extract_metrics,
    idea_identities,
    key_identities,
)


def _idea(targets, disease, direction="inhibit"):
    return idea_identities({
        "idea_type": "biology", "targets": targets, "disease": disease, "direction": direction,
    })


def _got(*ideas, verified=1, dropped=0):
    return SimpleNamespace(identities=tuple(ideas), verified=verified, dropped=dropped)


KINX = {"as_written": "invented kinase X (INVX)", "hgnc_id": "HGNC:9001", "symbol": "KINX"}
FIB = {"as_written": "invented fibrosis", "mondo_id": "MONDO:0000002",
       "label": "invented fibrosis"}


def test_an_id_meets_the_same_entity_written_out():
    want = [_idea([KINX], FIB)]
    # provisional on this side: the written name, an abbreviation, a Greek letter
    got = _got(_idea([{"as_written": "INVX"}], {"as_written": "Invented Fibrosis (IF)"}))
    found = extract_agreement([(want, got)])
    assert found["agreement"] == 1.0
    assert found["pair_agreement"] == 1.0
    assert extract_agreement([(
        [_idea([{"as_written": "TNF-α"}], {})], _got(_idea([{"as_written": "TNFa"}], {}))
    )])["agreement"] == 1.0


def test_splitting_ideas_does_not_change_agreement():
    two_targets = [_idea([KINX, {"as_written": "invented ligase"}], FIB)]
    split = _got(_idea([KINX], FIB), _idea([{"as_written": "Invented ligase"}], FIB))
    assert extract_agreement([(two_targets, split)])["agreement"] == 1.0


def test_missed_and_invented_entities_count_against_it():
    want = [_idea([KINX], FIB)]
    invented = _got(_idea([KINX], FIB), _idea([{"as_written": "invented phosphatase"}], FIB))
    found = extract_agreement([(want, invented)])
    # 2 of 2 wanted matched; 3 named by the extraction
    assert found["agreement"] == pytest.approx(2 * 2 / 5)
    assert found["target_recall"] == 1.0 and found["target_precision"] == 0.5
    nothing = extract_agreement([(want, None)])
    assert nothing["agreement"] == 0.0 and nothing["target_recall"] == 0.0
    both_empty = extract_agreement([([], _got())])
    assert both_empty["agreement"] == 1.0


def test_direction_is_reported_not_compared():
    found = extract_agreement([
        ([_idea([KINX], FIB, "inhibit")], _got(_idea([KINX], FIB, "activate"))),
    ])
    assert found["agreement"] == 1.0
    assert found["direction_agreement"] == 0.0


def test_stored_keys_read_back_as_identities():
    key = ["HGNC:9001,prov:target:invented ligase (inl)", "inhibit", "prov:disease:invented ail"]
    identity = key_identities(key)
    assert identity["targets"] == [["HGNC:9001"], ["inl", "inventedligase"]]
    assert identity["disease"] == ["inventedail"]
    assert expected_identities({"keys": [key]}) == [identity]
    stored = [_idea([KINX], FIB)]
    assert expected_identities({"keys": [key], "ideas": stored}) == stored
    got = _got(_idea([KINX, {"as_written": "INL"}], {"as_written": "invented ail"}))
    assert extract_agreement([([identity], got)])["agreement"] == 1.0


def test_the_bar_is_relative_to_the_reference():
    want = [_idea([KINX], FIB)]
    half = _got(_idea([KINX], {"as_written": "invented ail"}))
    found = extract_metrics([(want, half)], reference_agreement=0.5)
    assert found["agreement"] == 0.5
    assert found["agreement_bar"] == pytest.approx(EXTRACT_MIN_RELATIVE * 0.5)
    assert found["checks"]["agreement"] is True
    assert extract_metrics([(want, half)], reference_agreement=1.0)["checks"]["agreement"] is False
    unquoted = _got(_idea([KINX], FIB), verified=1, dropped=1)
    assert extract_metrics([(want, unquoted)], reference_agreement=1.0)["passed"] is False
