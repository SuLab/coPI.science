"""Deterministic normalization of extracted targets, diseases and agents (spec 2026-10-30
§7.4 step 4, analysis F5).

Each name is matched to an HGNC, Mondo or ChEMBL id only when the evidence picks exactly
one; anything else is left unmatched rather than guessed. An unmatched target or disease
gets a ``provisional_key`` instead, which a later resolution can merge through an alias
(D36).

PubTator3 annotations are read in the flat shape ``evidence.pubtator.pubtator_annotations``
returns: ``type``, ``text``, ``identifier`` and ``name`` keys on each mention.
"""
from __future__ import annotations

from src.landscaping.evidence.chembl import chembl_molecule
from src.landscaping.evidence.hgnc import hgnc_lookup
from src.landscaping.evidence.mondo import (
    mondo_from_mesh,
    mondo_label,
    mondo_label_match,
    mondo_mesh,
    mondo_orpha,
)
from src.landscaping.evidence.opentargets import ot_target
from src.landscaping.quotes import fold_text

#: Target kinds that name one gene product; pathway nodes, complexes and cell populations
#: are never matched to a single HGNC id.
_GENE_KINDS = frozenset({"gene", "protein", "rna"})
#: How an Open Targets homologue entry names each extract ``species`` value: NCBI taxon id,
#: Latin name and common name, compared case-folded. ``other`` accepts any species.
_SPECIES_NAMES = {
    "mouse": frozenset({"10090", "mus musculus", "mouse"}),
    "rat": frozenset({"10116", "rattus norvegicus", "rat"}),
}
_HUMAN_SPECIES = frozenset({"human", "unstated", ""})


def provisional_key(kind: str, as_written: str) -> str:
    """The provisional key of an unmatched ``kind`` (``target`` or ``disease``) name."""
    return f"prov:{kind}:{fold_text(as_written)}"


def _field(annotation: dict, key: str) -> str:
    """``key`` of a PubTator3 annotation (``pubtator_annotations``' flat shape); ``""``
    when absent."""
    value = annotation.get(key)
    return "" if value is None else str(value)


def _pubtator_matches(pubtator: list[dict], kind: str, as_written: str) -> list[dict]:
    """The annotations of type ``kind`` (``Gene`` or ``Disease``) whose text is the name."""
    wanted = fold_text(as_written)
    return [
        a for a in pubtator
        if isinstance(a, dict) and _field(a, "type").casefold() == kind.casefold()
        and fold_text(_field(a, "text")) == wanted
    ]


def _single(values) -> str | None:
    distinct = {v for v in values if v}
    return distinct.pop() if len(distinct) == 1 else None


async def _single_gene(symbol: str) -> dict | None:
    """The one HGNC record ``symbol`` maps to by symbol, alias or previous symbol, else None."""
    records = [r for r in await hgnc_lookup(symbol) if isinstance(r, dict) and r.get("hgnc_id")]
    if len({r["hgnc_id"] for r in records}) != 1:
        return None
    return records[0]


def _is_ortholog(entry: dict, symbol: str, species: str) -> bool:
    if not str(entry.get("homologyType") or "").startswith("ortholog"):
        return False
    if str(entry.get("targetGeneSymbol") or "").casefold() != symbol.casefold():
        return False
    names = _SPECIES_NAMES.get(species)
    if names is None:
        return True
    given = {str(entry.get(k) or "").casefold() for k in ("speciesId", "speciesName")}
    return bool(given & names)


async def _human_ortholog(symbol: str, species: str) -> dict | None:
    """The HGNC record of the human gene whose Open Targets ``homologues`` list ``symbol`` as
    an ortholog in ``species``, when ``symbol`` maps to exactly one human gene."""
    record = await _single_gene(symbol)
    if record is None or not record.get("ensembl_gene_id"):
        return None
    target = await ot_target(record["ensembl_gene_id"])
    homologues = target.get("homologues") or []
    if any(isinstance(h, dict) and _is_ortholog(h, symbol, species) for h in homologues):
        return record
    return None


async def _from_pubtator(name: str | None, species: str) -> dict | None:
    """The HGNC record of PubTator3's normalized gene ``name``: by exact approved symbol for
    a human gene, through the ortholog check otherwise."""
    if not name:
        return None
    if species not in _HUMAN_SPECIES:
        return await _human_ortholog(name, species)
    record = await _single_gene(name)
    return record if record and record.get("symbol") == name else None


async def _from_lookup(as_written: str, species: str, criteria) -> dict | None:
    """The HGNC fallback: never for a criteria-listed ambiguous word, else exactly one gene."""
    if as_written.strip().upper() in criteria.ambiguous_gene_words:
        return None
    if species not in _HUMAN_SPECIES:
        return await _human_ortholog(as_written.strip(), species)
    return await _single_gene(as_written.strip())


async def _target_gene(target: dict, pubtator: list[dict], criteria) -> dict | None:
    """The human gene's HGNC record ``target`` names, or None."""
    as_written = str(target.get("as_written") or "")
    species = str(target.get("species") or "").casefold()
    if target.get("kind") not in _GENE_KINDS:
        return None
    annotations = _pubtator_matches(pubtator, "Gene", as_written)
    if annotations:
        name = _single(_field(a, "name") for a in annotations)
        record = await _from_pubtator(name, species)
        if record:
            return record
    return await _from_lookup(as_written, species, criteria)


async def normalize_targets(targets: list[dict], *, pubtator: list[dict], criteria) -> list[dict]:
    """Copies of ``targets``, each with ``hgnc_id`` or ``provisional_key`` added. A matched
    target also takes the human gene's approved ``symbol``, its HGNC ``gene_group`` list (as
    the record gives it; empty when it has none), when HGNC records one, ``ensembl_id`` (the
    Ensembl gene id) and, when HGNC records exactly one UniProt accession, ``uniprot_id``;
    evidence assembly reads ``symbol``, ``ensembl_id`` and ``uniprot_id`` and resolves the
    ChEMBL target from ``uniprot_id``, and S8's ``gene_group`` related link reads
    ``gene_group``.

    ``pubtator`` is the document's PubTator3 annotations; ``criteria`` a ``Criteria``. The
    first choice is the document's PubTator3 gene annotation of the same text, resolved to
    an approved HGNC symbol. The fallback is an HGNC symbol, alias or previous-symbol lookup
    that yields exactly one gene, never for a word on ``criteria.ambiguous_gene_words``. A
    mouse, rat or other-species gene maps to the human gene only when that gene's Open
    Targets ``homologues`` confirm the ortholog. Only gene, protein and RNA targets are
    matched. A target with an empty ``as_written`` is returned unchanged.
    """
    out = []
    for target in targets:
        result = dict(target)
        as_written = str(target.get("as_written") or "")
        if as_written.strip():
            record = await _target_gene(target, pubtator, criteria)
            if record:
                result["hgnc_id"] = record["hgnc_id"]
                if record.get("symbol"):
                    result["symbol"] = record["symbol"]
                result["gene_group"] = list(record.get("gene_group") or [])
                if record.get("ensembl_gene_id"):
                    result["ensembl_id"] = record["ensembl_gene_id"]
                uniprot = _single(str(u) for u in record.get("uniprot_ids") or [])
                if uniprot:
                    result["uniprot_id"] = uniprot
            else:
                result["provisional_key"] = provisional_key("target", as_written)
        out.append(result)
    return out


def _mesh_curie(identifier: str) -> str:
    return identifier if identifier.upper().startswith("MESH:") else f"MESH:{identifier}"


def _add_mondo_fields(result: dict, mondo_id: str) -> None:
    """``label``, ``orpha_code`` and (absent one) ``mesh_id`` of the Mondo term, each only
    when Mondo gives one."""
    extra = {
        "label": mondo_label(mondo_id),
        "orpha_code": mondo_orpha(mondo_id),
        "mesh_id": None if result.get("mesh_id") else mondo_mesh(mondo_id),
    }
    result.update({k: v for k, v in extra.items() if v})


async def normalize_disease(disease: dict, *, pubtator: list[dict]) -> dict:
    """A copy of ``disease`` with ``mondo_id`` or ``provisional_key`` added.

    The document's PubTator3 disease annotation of the same text gives a MeSH id, kept as
    ``mesh_id`` (``MESH:…``) and mapped to Mondo only by an exact SSSOM match; else an exact
    Mondo label or synonym match on ``as_written``; else unmatched. A matched disease also
    takes the Mondo term's name as ``label``, its one exact Orphanet mapping as
    ``orpha_code`` (``ORPHA:…``) and, when PubTator3 gave no MeSH id, its one exact MeSH
    mapping as ``mesh_id``; each is absent when Mondo gives none or more than one. A disease
    with an empty ``as_written`` is returned unchanged.
    """
    result = dict(disease)
    as_written = str(disease.get("as_written") or "")
    if not as_written.strip():
        return result
    mesh = _single(
        _field(a, "identifier") for a in _pubtator_matches(pubtator, "Disease", as_written)
    )
    if mesh:
        result["mesh_id"] = _mesh_curie(mesh)
    mondo_id = await mondo_from_mesh(_mesh_curie(mesh)) if mesh else None
    mondo_id = mondo_id or mondo_label_match(as_written)
    if mondo_id:
        result["mondo_id"] = mondo_id
        _add_mondo_fields(result, mondo_id)
    else:
        result["provisional_key"] = provisional_key("disease", as_written)
    return result


async def normalize_agent(agent: dict) -> dict:
    """A copy of ``agent`` with ``chembl_id`` added when its name resolves to exactly one
    ChEMBL molecule; otherwise unchanged (the chemistry key then uses the name, §7.4 step 7).
    """
    result = dict(agent)
    name = str(agent.get("as_written") or "").strip()
    if not name:
        return result
    molecules = await chembl_molecule(name)
    chembl_id = _single(
        str(m.get("molecule_chembl_id") or "") for m in molecules if isinstance(m, dict)
    )
    if chembl_id:
        result["chembl_id"] = chembl_id
    return result
