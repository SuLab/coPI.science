"""Normalization of targets, diseases and agents (plan Task 4.2; spec §7.4 step 4).

The evidence clients are replaced by in-memory fakes; every gene symbol, id and name below
is invented except where a real HGNC/MeSH shape is needed.
"""
import pytest

from src.landscaping import normalize
from src.landscaping.criteria import load_criteria

HGNC = {
    "KINX": [{"hgnc_id": "HGNC:9001", "symbol": "KINX", "ensembl_gene_id": "ENSG0000009001"}],
    "Kinx": [{"hgnc_id": "HGNC:9001", "symbol": "KINX", "ensembl_gene_id": "ENSG0000009001"}],
    "P-KIN": [{"hgnc_id": "HGNC:9001", "symbol": "KINX", "ensembl_gene_id": "ENSG0000009001"}],
    "DUAL1": [
        {"hgnc_id": "HGNC:9002", "symbol": "GENEA", "ensembl_gene_id": "ENSG0000009002"},
        {"hgnc_id": "HGNC:9003", "symbol": "GENEB", "ensembl_gene_id": "ENSG0000009003"},
    ],
    "SET": [{"hgnc_id": "HGNC:9004", "symbol": "SET", "ensembl_gene_id": "ENSG0000009004"}],
}
OT = {
    "ENSG0000009001": {"homologues": [
        {"speciesId": "10090", "speciesName": "Mouse", "homologyType": "ortholog_one2one",
         "targetGeneId": "ENSMUSG0000009001", "targetGeneSymbol": "Kinx"},
    ]},
}

MONDO_NAMES = {"MONDO:0000001": "invented syndrome", "MONDO:0000002": "invented fibrosis"}


@pytest.fixture
def lookups(monkeypatch):
    calls = {"hgnc": [], "mesh": [], "label": []}

    async def hgnc_lookup(symbol):
        calls["hgnc"].append(symbol)
        return HGNC.get(symbol, [])

    async def ot_target(ensembl_id):
        return OT.get(ensembl_id, {"homologues": []})

    async def mondo_from_mesh(mesh_id):
        calls["mesh"].append(mesh_id)
        return {"MESH:D000001": "MONDO:0000001"}.get(mesh_id)

    def mondo_label_match(text):
        calls["label"].append(text)
        return {"invented fibrosis": "MONDO:0000002"}.get(text)

    monkeypatch.setattr(normalize, "hgnc_lookup", hgnc_lookup)
    monkeypatch.setattr(normalize, "ot_target", ot_target)
    monkeypatch.setattr(normalize, "mondo_from_mesh", mondo_from_mesh)
    monkeypatch.setattr(normalize, "mondo_label_match", mondo_label_match)
    monkeypatch.setattr(normalize, "mondo_label", MONDO_NAMES.get)
    monkeypatch.setattr(normalize, "mondo_orpha", lambda mondo_id: None)
    monkeypatch.setattr(normalize, "mondo_mesh", lambda mondo_id: None)
    return calls


def _gene(text, name, identifier="9001"):
    """A gene mention in ``pubtator.pubtator_annotations``' flat shape."""
    return {"type": "Gene", "text": text, "identifier": identifier, "database": "ncbi_gene",
            "name": name}


async def test_pubtator_first(lookups):
    pubtator = [_gene("p-kin", "KINX")]
    [target] = await normalize.normalize_targets(
        [{"as_written": "p-kin", "kind": "protein", "species": "human"}],
        pubtator=pubtator, criteria=load_criteria(),
    )
    assert target["hgnc_id"] == "HGNC:9001"
    assert "provisional_key" not in target
    # evidence assembly reads the approved symbol and the Ensembl gene id
    assert target["symbol"] == "KINX"
    assert target["ensembl_id"] == "ENSG0000009001"
    # resolved through PubTator3's normalized symbol, not the as-written name
    assert lookups["hgnc"] == ["KINX"]


async def test_matched_target_keeps_gene_group(lookups, monkeypatch):
    groups = ["Invented kinase family", "Invented receptor group"]
    records = {"KINX": [{**HGNC["KINX"][0], "gene_group": groups}]}

    async def hgnc_lookup(symbol):
        return records.get(symbol, HGNC.get(symbol, []))

    monkeypatch.setattr(normalize, "hgnc_lookup", hgnc_lookup)
    matched, ungrouped, unmatched = await normalize.normalize_targets(
        [
            {"as_written": "KINX", "kind": "gene", "species": "human"},
            {"as_written": "P-KIN", "kind": "protein", "species": "human"},
            {"as_written": "DUAL1", "kind": "gene", "species": "human"},
        ],
        pubtator=[], criteria=load_criteria(),
    )
    # S8's gene_group related link reads the HGNC record's list as given
    assert matched["gene_group"] == groups
    assert ungrouped["hgnc_id"] == "HGNC:9001" and ungrouped["gene_group"] == []
    assert "gene_group" not in unmatched


async def test_ambiguous_alias_left_unmatched(lookups):
    criteria = load_criteria()
    assert "SET" in criteria.ambiguous_gene_words
    targets = await normalize.normalize_targets(
        [
            {"as_written": "DUAL1", "kind": "gene", "species": "human"},
            {"as_written": "SET", "kind": "protein", "species": "human"},
        ],
        pubtator=[], criteria=criteria,
    )
    assert all("hgnc_id" not in t for t in targets)
    assert [t["provisional_key"] for t in targets] == ["prov:target:dual1", "prov:target:set"]
    # the ambiguous word is never looked up
    assert lookups["hgnc"] == ["DUAL1"]


async def test_mouse_gene_maps_to_human_ortholog_via_homologues(lookups):
    criteria = load_criteria()
    [mapped, rat] = await normalize.normalize_targets(
        [
            {"as_written": "Kinx", "kind": "gene", "species": "mouse"},
            {"as_written": "Kinx", "kind": "gene", "species": "rat"},
        ],
        pubtator=[], criteria=criteria,
    )
    assert mapped["hgnc_id"] == "HGNC:9001"
    # the human gene's symbol, not the mouse one as written
    assert mapped["symbol"] == "KINX"
    # no rat homologue is listed, so the rat gene stays unmatched
    assert "hgnc_id" not in rat and rat["provisional_key"] == "prov:target:kinx"

    [via_pubtator] = await normalize.normalize_targets(
        [{"as_written": "mKinx", "kind": "gene", "species": "mouse"}],
        pubtator=[_gene("mKinx", "Kinx", identifier="19001")], criteria=criteria,
    )
    assert via_pubtator["hgnc_id"] == "HGNC:9001"


async def test_disease_mesh_to_mondo_exact_only(lookups):
    def disease(text, mesh):
        return {"type": "Disease", "text": text, "identifier": mesh, "database": "ncbi_mesh",
                "name": None}

    pubtator = [disease("invented syndrome", "MESH:D000001"), disease("other ail", "D000099")]

    exact = await normalize.normalize_disease(
        {"as_written": "invented syndrome"}, pubtator=pubtator
    )
    assert exact["mondo_id"] == "MONDO:0000001"
    assert exact["mesh_id"] == "MESH:D000001"

    # a MeSH id with no exact Mondo mapping, and no exact label, stays unmatched
    unmapped = await normalize.normalize_disease({"as_written": "Other ail"}, pubtator=pubtator)
    assert "mondo_id" not in unmapped
    assert unmapped["provisional_key"] == "prov:disease:other ail"
    # the bare MeSH id is kept as a CURIE even though no Mondo id matched
    assert unmapped["mesh_id"] == "MESH:D000099"
    assert lookups["mesh"] == ["MESH:D000001", "MESH:D000099"]

    by_label = await normalize.normalize_disease({"as_written": "invented fibrosis"}, pubtator=[])
    assert by_label["mondo_id"] == "MONDO:0000002"
    assert "mesh_id" not in by_label

    empty = await normalize.normalize_disease({"as_written": ""}, pubtator=pubtator)
    assert empty == {"as_written": ""}


async def test_normalized_disease_carries_label_orpha_and_mesh(lookups, monkeypatch):
    monkeypatch.setattr(normalize, "mondo_orpha",
                        {"MONDO:0000001": "ORPHA:900001", "MONDO:0000002": "ORPHA:900002"}.get)
    monkeypatch.setattr(normalize, "mondo_mesh",
                        {"MONDO:0000001": "MESH:D000777", "MONDO:0000002": "MESH:D000002"}.get)
    pubtator = [{"type": "Disease", "text": "invented syndrome", "identifier": "MESH:D000001",
                 "database": "ncbi_mesh", "name": None}]

    via_mesh = await normalize.normalize_disease(
        {"as_written": "invented syndrome"}, pubtator=pubtator
    )
    assert via_mesh["label"] == "invented syndrome"
    assert via_mesh["orpha_code"] == "ORPHA:900001"
    # PubTator3's MeSH id is kept; Mondo's is used only when PubTator3 gave none
    assert via_mesh["mesh_id"] == "MESH:D000001"

    by_label = await normalize.normalize_disease({"as_written": "invented fibrosis"}, pubtator=[])
    assert by_label["mondo_id"] == "MONDO:0000002"
    assert by_label["label"] == "invented fibrosis"
    assert by_label["orpha_code"] == "ORPHA:900002"
    assert by_label["mesh_id"] == "MESH:D000002"

    unmatched = await normalize.normalize_disease({"as_written": "unknown ail"}, pubtator=[])
    assert not {"label", "orpha_code", "mesh_id"} & unmatched.keys()


async def test_agent_needs_exactly_one_chembl_molecule(monkeypatch):
    hits = {
        "inventinib": [{"molecule_chembl_id": "CHEMBL900001"}],
        "invented mix": [{"molecule_chembl_id": "CHEMBL900002"},
                         {"molecule_chembl_id": "CHEMBL900003"}],
    }

    async def chembl_molecule(name):
        return hits.get(name, [])

    monkeypatch.setattr(normalize, "chembl_molecule", chembl_molecule)
    one = await normalize.normalize_agent({"as_written": "inventinib", "kind": "small_molecule"})
    assert one["chembl_id"] == "CHEMBL900001"
    for name in ("invented mix", "unknownmab", ""):
        result = await normalize.normalize_agent({"as_written": name, "kind": "other"})
        assert "chembl_id" not in result


async def test_pubtator_client_output_is_the_shape_normalize_reads(lookups):
    """``_annotation`` turns a BioC mention into the flat mention normalization matches on."""
    from src.landscaping.evidence import pubtator

    mention = pubtator._annotation({
        "text": "p-kin",
        "infons": {"type": "Gene", "identifier": "9001", "database": "ncbi_gene", "name": "KINX"},
    })
    [target] = await normalize.normalize_targets(
        [{"as_written": "p-kin", "kind": "protein", "species": "human"}],
        pubtator=[mention], criteria=load_criteria(),
    )
    assert target["hgnc_id"] == "HGNC:9001"
    assert lookups["hgnc"] == ["KINX"]
