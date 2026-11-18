"""Deterministic normalization of extracted targets, diseases and agents (spec 2026-10-30
§7.4 step 4, analysis F5).

Each name is matched to an HGNC, Mondo or ChEMBL id only when the evidence picks exactly
one; anything else is left unmatched rather than guessed. An unmatched target or disease
gets a ``provisional_key`` instead, which a later resolution can merge through an alias
(D36).

S3 also asks the model for a gene target's HGNC-approved symbol (``hgnc_symbol``) and a
disease's standard name (``standard_name``), so that the same entity worded two ways gets one
identity (D73). Each is tried first, and names the provisional key when nothing matches;
``as_written`` is never changed.

A ChEMBL molecule lookup that fails on ChEMBL's side (a 429 or 5xx after one retry, or a
network failure) leaves the agent unmatched (D81), so an extraction is not held behind it;
``ideas.resolve_aliases`` normalizes the provisional idea again on a later day.

PubTator3 annotations are read in the flat shape ``evidence.pubtator.pubtator_annotations``
returns: ``type``, ``text``, ``identifier`` and ``name`` keys on each mention.
"""
from __future__ import annotations

import logging

import httpx

from src.landscaping.evidence.chembl import chembl_molecule
from src.landscaping.evidence.hgnc import hgnc_lookup
from src.landscaping.evidence.mondo import (
    ensure_loaded,
    mondo_from_mesh,
    mondo_label,
    mondo_label_match,
    mondo_mesh,
    mondo_orpha,
)
from src.landscaping.evidence.opentargets import ot_target
from src.landscaping.quotes import fold_text
from src.services.http_pacing import TRANSIENT_STATUSES

logger = logging.getLogger(__name__)

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
    """The one HGNC record ``symbol`` names: the gene whose approved symbol it is (compared
    case-insensitively), else the one gene it is an alias or previous symbol of; None when
    there is none or more than one. An approved symbol wins over another gene's alias of the
    same spelling (``XBP1``, ``SPP1``)."""
    records = [r for r in await hgnc_lookup(symbol) if isinstance(r, dict) and r.get("hgnc_id")]
    wanted = symbol.casefold()
    approved = [r for r in records if str(r.get("symbol") or "").casefold() == wanted]
    if len({r["hgnc_id"] for r in approved}) == 1:
        return approved[0]
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


def _claimed_symbol(target: dict) -> str:
    """The HGNC symbol the model gave a gene, protein or RNA target (D73); ``""`` for any
    other kind, whose symbol is never used."""
    if target.get("kind") not in _GENE_KINDS:
        return ""
    return str(target.get("hgnc_symbol") or "").strip()


async def _target_gene(target: dict, pubtator: list[dict], criteria) -> dict | None:
    """The human gene's HGNC record ``target`` names, or None. The model's ``hgnc_symbol``
    is looked up first, as a human symbol whatever the species, since the model gives the
    human ortholog's; then the written name."""
    as_written = str(target.get("as_written") or "")
    species = str(target.get("species") or "").casefold()
    if target.get("kind") not in _GENE_KINDS:
        return None
    claimed = _claimed_symbol(target)
    if claimed:
        record = await _single_gene(claimed)
        if record:
            return record
    if not as_written.strip():
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
    first choice is the model's ``hgnc_symbol`` (D73), looked up as a human gene. Then the
    document's PubTator3 gene annotation of the same text, resolved to an approved HGNC
    symbol. The fallback is an HGNC symbol, alias or previous-symbol lookup of ``as_written``
    that yields exactly one gene, never for a word on ``criteria.ambiguous_gene_words``. A
    mouse, rat or other-species written name maps to the human gene only when that gene's
    Open Targets ``homologues`` confirm the ortholog. Only gene, protein and RNA targets are
    matched. An unmatched target's provisional key is built from its ``hgnc_symbol`` when it
    has one, else from ``as_written``. A target with neither is returned unchanged.
    """
    out = []
    for target in targets:
        result = dict(target)
        as_written = str(target.get("as_written") or "")
        claimed = _claimed_symbol(target)
        if as_written.strip() or claimed:
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
                result["provisional_key"] = provisional_key("target", claimed or as_written)
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

    The first choice is an exact Mondo label or synonym match on the model's
    ``standard_name`` (D73), which a grant and a paper share whether or not PubTator3 can
    read them. Then the document's PubTator3 disease annotation of the same text gives a MeSH
    id, kept as ``mesh_id`` (``MESH:…``) whichever path matches, and mapped to Mondo only by
    an exact SSSOM match; else an exact Mondo label or synonym match on ``as_written``; else
    unmatched, with the provisional key built from ``standard_name`` when given, else from
    ``as_written``. A matched disease also takes the Mondo term's name as ``label``, its one
    exact Orphanet mapping as ``orpha_code`` (``ORPHA:…``) and, when PubTator3 gave no MeSH
    id, its one exact MeSH mapping as ``mesh_id``; each is absent when Mondo gives none or
    more than one. A disease with neither name is returned unchanged.
    """
    result = dict(disease)
    as_written = str(disease.get("as_written") or "")
    standard = str(disease.get("standard_name") or "").strip()
    if not as_written.strip() and not standard:
        return result
    # The label match and `_add_mondo_fields` read Mondo synchronously; only the MeSH path
    # loads it, so a disease PubTator3 did not tag must not be the first to reach them.
    await ensure_loaded()
    mesh = _single(
        _field(a, "identifier") for a in _pubtator_matches(pubtator, "Disease", as_written)
    ) if as_written.strip() else None
    if mesh:
        result["mesh_id"] = _mesh_curie(mesh)
    mondo_id = mondo_label_match(standard) if standard else None
    if not mondo_id and mesh:
        mondo_id = await mondo_from_mesh(_mesh_curie(mesh))
    if not mondo_id and as_written.strip():
        mondo_id = mondo_label_match(as_written)
    if mondo_id:
        result["mondo_id"] = mondo_id
        _add_mondo_fields(result, mondo_id)
    else:
        result["provisional_key"] = provisional_key("disease", standard or as_written)
    return result


async def normalize_agent(agent: dict) -> dict:
    """A copy of ``agent`` with ``chembl_id`` added when its name resolves to exactly one
    ChEMBL molecule; otherwise unchanged (the chemistry key then uses the name, §7.4 step 7).
    A lookup ChEMBL fails to answer counts as no match (D81, module docstring).
    """
    result = dict(agent)
    name = str(agent.get("as_written") or "").strip()
    if not name:
        return result
    try:
        molecules = await chembl_molecule(name)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and (
            exc.response.status_code not in TRANSIENT_STATUSES
        ):
            raise
        logger.warning("ChEMBL lookup of agent %r failed (%s); left unmatched", name, exc)
        return result
    chembl_id = _single(
        str(m.get("molecule_chembl_id") or "") for m in molecules if isinstance(m, dict)
    )
    if chembl_id:
        result["chembl_id"] = chembl_id
    return result
