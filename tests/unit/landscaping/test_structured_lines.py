"""Structured lines composed in code from an idea's normalized fields (spec §7.10, D29).

Every gene, disease and agent below is invented.
"""
from types import SimpleNamespace

from src.landscaping.structured_lines import structured_line


def test_structured_line_biology_and_chemistry():
    biology = SimpleNamespace(
        idea_type="biology", direction="inhibit",
        targets=[{"as_written": "inv1", "symbol": "INV1", "hgnc_id": "HGNC:99001"}],
        disease={"as_written": "invented fibrosis", "mondo_id": "MONDO:0900001"},
        agent=None, modality_variants=["antisense oligonucleotide"],
    )
    assert structured_line(biology) == (
        "Inhibit INV1 · invented fibrosis · antisense oligonucleotide"
    )

    open_modality = {**vars(biology), "modality_variants": None, "direction": "activate"}
    assert structured_line(open_modality) == "Activate INV1 · invented fibrosis · modality open"

    two_targets = {
        **vars(biology),
        "targets": [{"symbol": "INV1"}, {"as_written": "inv2", "provisional_key": "prov:target:inv2"}],
    }
    assert structured_line(two_targets).startswith("Inhibit INV1/inv2 · ")

    chemistry = SimpleNamespace(
        idea_type="chemistry", direction=None,
        agent={"as_written": "QF-101", "kind": "small_molecule", "lab_key": "lab:invented"},
        targets=[{"as_written": "INV1", "symbol": "INV1", "hgnc_id": "HGNC:99001"}],
        disease={"as_written": "invented fibrosis"}, modality_variants=None,
    )
    assert structured_line(chemistry) == "QF-101 (small molecule) · INV1 · invented fibrosis"

    platform = SimpleNamespace(
        idea_type="chemistry", direction=None,
        agent={"as_written": "invented lipid carrier", "kind": "delivery_technology"},
        targets=None, disease=None, modality_variants=None,
    )
    assert structured_line(platform) == (
        "invented lipid carrier (delivery technology) · no single target · no single disease"
    )
