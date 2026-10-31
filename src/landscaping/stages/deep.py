"""S6 deep analysis: one synchronous analysis per idea version and epoch (spec §7.7; D12, D28).

``run_deep`` serves the ideas of the current cut (whose ``pending`` rows the cut queued) and
the blind-batch probe ideas (L64), full text included for both; in a candidate epoch it also
serves every issued probe (D62). It creates the row for an idea version that has none (a
probe's, ``cut_id`` NULL). The caller picks the ideas.

Steps, each committing on its own (spec §12.2):

1. **Full text** (§6.2, L36). Each paper of the dossier is fetched once
   (``fulltext.fetch_full_text``, stored by ``fulltext.store_full_text``). A ``fetch_failed``
   document is retried by later runs, up to `FULL_TEXT_MAX_FETCHES` fetches, before its
   summary stands in (D41): until then the row goes back to ``pending``. Each full text is
   re-extracted with the S3 prompt and schema (stage ``deep_extract``, on the deep model) and
   quote-checked against the full text, into a ``full_text`` extraction row. That row is S6
   evidence only: it never attaches to an idea, so the idea version never changes (D28).
2. **Database records** for the disease profile and target biology (§7.7 steps 3-4): Open
   Targets, Orphadata and ClinicalTrials.gov from the idea's assembled evidence (looked up
   again when missing), plus UniProt, HPA and openFDA, all through the evidence cache.
3. **The analysis**: one conversation with the projects research tools, at most
   `MAX_SEARCH_ROUNDS` rounds of searching, then the ``deep`` JSON. Support references are
   checked in code: a claim keeps only references to a dossier document, a database record or
   a source the session retrieved (``project_plan.resolve_source``); a field left with none is
   ``"estimate"``. Prevalence is ``"estimate"`` unless it cites the Orphadata record (L31);
   approved drugs keep only openFDA and Open Targets records; a target-biology line keeps only
   UniProt, HPA and Open Targets records and is dropped without one; an outside group needs a
   retrieved source.
4. **The panel** (``src.landscaping.panel``), for a row that is ``done``. A ``not_met`` gate
   gives ``excluded:deep_gate`` and no consults.

The system prompts, the panel personas, the rubric that weighs the provisional scores and the
models come from the epoch's own inputs (``epochs.epoch_inputs``), never the files on disk or
the current settings (spec §7.11); each persona was composed as the engine composes it
(``panel.compose_persona``, §7.7 step 8) when the epoch was created.

S6 yields to the simulation (D33): while ``liveness.simulation_live`` is true, ``run_deep``
returns before claiming the row, fetching or calling anything. The check is made once per run;
an analysis already under way finishes.
"""
from __future__ import annotations

import gzip
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.prompt_safety import delimit
from src.agent.specialists import parse_opinion
from src.landscaping import batches, epochs, fulltext, liveness
from src.landscaping.epochs import EpochInputs
from src.landscaping.evidence import cache, hpa, openfda, opentargets, orphadata, uniprot
from src.landscaping.ideas import dossier_documents
from src.landscaping.llm_client import CallResult, build_request, call_sync
from src.landscaping.panel import consult_message, panel_domains
from src.landscaping.quotes import apply_quote_check
from src.landscaping.schemas import DIMENSION_KEYS, DISEASE_PROFILE_KEYS, GATING_KEYS, SCHEMAS
from src.landscaping.scoring import epoch_weighted_score
from src.landscaping.stages.screen import KIND_LABELS, fenced_text, parse_output
from src.models.landscape import (
    LandscapeConsult,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeIdeaLink,
    LandscapeLab,
    LandscapeText,
    LandscapeTriage,
)
from src.services import project_search
from src.services.job_queue import job_error_text
from src.services.project_plan import resolve_source

logger = logging.getLogger(__name__)

STAGE = "deep"
EXTRACT_STAGE = "deep_extract"
#: Rounds of research-tool calls before the analysis must answer (§7.7 step 2).
MAX_SEARCH_ROUNDS = 8
#: Fetches of a document whose full text failed transiently before its summary stands in
#: (§6.2, D41).
FULL_TEXT_MAX_FETCHES = 3
#: openFDA lookups per analysis; keyless openFDA allows 1,000 requests a day (§6.3).
MAX_DRUG_LOOKUPS = 10

_PAPER_KINDS = frozenset({"work", "preprint"})
_GATE_VALUES = frozenset({"met", "not_met", "unconfirmed"})
_FLAG_STATUSES = frozenset({"confirmed", "cleared"})
_TRIAGE_DONE = ("done", "rescued")
#: A database record is shown clipped to this many characters of JSON.
_RECORD_MAX_CHARS = 4_000
_DETAILS_MAX_CHARS = 1_500
_RETRIEVED_PREFIXES = ("pmid:", "nct:", "patent:")
_DRUG_PREFIXES = ("openfda:", "opentargets:")
_BIOLOGY_PREFIXES = ("uniprot:", "hpa:", "opentargets:")
_FINAL_ROUND_TEXT = (
    "Every search round is used. Return the JSON object now, citing only what this session "
    "retrieved and what the dossier holds."
)
_PUBMED_DOWN = "PubMed could not be searched just now; say so rather than treat it as absence."


@dataclass
class _Text:
    """What S6 read for one paper of the dossier."""

    basis: str
    text: str = ""
    route: str | None = None
    license: str | None = None
    truncated: bool = False


@dataclass
class _Session:
    """The literature loop's state: the retrieved sources by key and the call transcript."""

    rounds: int = 0
    retrieved: dict[str, dict] = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)


@dataclass
class _Facts:
    """Everything the dossier is built from, read in one session."""

    idea: LandscapeIdea
    documents: list[LandscapeDocument]
    labs: list[LandscapeLab]
    linked: list[LandscapeIdea]
    triage: LandscapeTriage | None


def _json(value: Any, limit: int | None = None) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return text if limit is None or len(text) <= limit else text[:limit] + " …(clipped)"


# --- claiming and finishing the row ----------------------------------------------------------


async def _claim(db_factory, idea_id: uuid.UUID, epoch_id: uuid.UUID) -> uuid.UUID | None:
    """The S6 row of the idea's current version, moved ``pending`` → ``running``; created
    first when missing (a probe's, ``cut_id`` NULL). None while a simulation run is live, or
    when the idea is not in the epoch, has no version, or its row is not ``pending``."""
    async with db_factory() as db:
        if await liveness.simulation_live(db):
            return None
        version = await db.scalar(
            select(LandscapeIdea.version_hash).where(
                LandscapeIdea.id == idea_id, LandscapeIdea.epoch_id == epoch_id
            )
        )
        if version is None:
            return None
        await db.execute(
            pg_insert(LandscapeDeep)
            .values(id=uuid.uuid4(), idea_id=idea_id, idea_version=version, epoch_id=epoch_id)
            .on_conflict_do_nothing(constraint="uq_landscape_deep_idea_version_epoch")
        )
        row_id = await db.scalar(
            update(LandscapeDeep)
            .where(
                LandscapeDeep.idea_id == idea_id, LandscapeDeep.idea_version == version,
                LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.status == "pending",
            )
            .values(status="running", updated_at=func.now())
            .returning(LandscapeDeep.id)
        )
        await db.commit()
        return row_id


async def _store(
    db_factory, row_id: uuid.UUID, values: dict, consults: list[dict] | None = None
) -> None:
    """Write the row's values (and consults, and the idea's outside-group count) only while it
    is still ``running``: a row reset as stale meanwhile keeps its reset."""
    async with db_factory() as db:
        idea_id = await db.scalar(
            update(LandscapeDeep)
            .where(LandscapeDeep.id == row_id, LandscapeDeep.status == "running")
            .values(**values, updated_at=func.now())
            .returning(LandscapeDeep.idea_id)
        )
        if idea_id is not None:
            for consult in consults or []:
                await db.execute(
                    pg_insert(LandscapeConsult)
                    .values(id=uuid.uuid4(), deep_id=row_id, **consult)
                    .on_conflict_do_nothing(constraint="uq_landscape_consults_deep_domain")
                )
            if values.get("outside_group_count") is not None:
                await db.execute(
                    update(LandscapeIdea).where(LandscapeIdea.id == idea_id)
                    .values(outside_group_count=values["outside_group_count"])
                )
        await db.commit()


async def _errored(db_factory, row_id: uuid.UUID, error: str) -> dict:
    """``batches.transition("errored")`` for the row, with the stored error text: one more
    try, then ``failed:errored``."""
    async with db_factory() as db:
        row = await db.get(LandscapeDeep, row_id)
        attempts = row.attempts if row is not None else 1
        truncated_once = row.truncated_once if row is not None else False
    values = batches.transition(
        "errored", attempts=attempts, truncated_once=truncated_once, model=""
    )
    return {**values, "error": error}


def _call_failure(result: CallResult) -> dict | None:
    """Row values for a refused or still-truncated call; None otherwise (API errors are the
    caller's)."""
    if result.stop_reason == "refusal":
        return {"status": "refused", "refusal_category": result.refusal_category,
                "served_by_model": result.served_by}
    if result.stop_reason == "max_tokens":
        return {"status": "failed:truncated", "truncated_once": True,
                "served_by_model": result.served_by}
    return None


# --- reading the idea --------------------------------------------------------------------------


async def _facts(db: AsyncSession, idea_id: uuid.UUID, epoch_id: uuid.UUID) -> _Facts:
    idea = await db.get(LandscapeIdea, idea_id)
    if idea is None:
        raise LookupError(f"idea {idea_id} is gone")
    documents = list(
        (
            await db.scalars(
                select(LandscapeDocument)
                .join(LandscapeExtraction, LandscapeExtraction.document_id == LandscapeDocument.id)
                .join(
                    LandscapeIdeaEvidence,
                    LandscapeIdeaEvidence.extraction_id == LandscapeExtraction.id,
                )
                .where(
                    LandscapeIdeaEvidence.idea_id == idea_id,
                    LandscapeDocument.merged_into.is_(None),
                )
                .distinct()
            )
        ).all()
    )
    documents = dossier_documents(idea, documents)
    lab_keys = sorted({d.lab_key for d in documents if d.lab_key})
    labs = (
        list((await db.scalars(select(LandscapeLab).where(LandscapeLab.lab_key.in_(lab_keys)))).all())
        if lab_keys else []
    )
    link = LandscapeIdeaLink
    linked_ids = {
        b if a == idea_id else a
        for a, b in (
            await db.execute(
                select(link.idea_a, link.idea_b).where(
                    link.epoch_id == epoch_id, or_(link.idea_a == idea_id, link.idea_b == idea_id)
                )
            )
        ).all()
    }
    linked = (
        list((await db.scalars(select(LandscapeIdea).where(LandscapeIdea.id.in_(linked_ids)))).all())
        if linked_ids else []
    )
    triage = await db.scalar(
        select(LandscapeTriage)
        .where(
            LandscapeTriage.idea_id == idea_id, LandscapeTriage.epoch_id == epoch_id,
            LandscapeTriage.idea_version == idea.version_hash,
            LandscapeTriage.status.in_(_TRIAGE_DONE),
        )
        .order_by(LandscapeTriage.updated_at.desc())
        .limit(1)
    )
    return _Facts(idea, documents, labs, linked, triage)


# --- step 1: full text and its re-extraction ---------------------------------------------------


def _stored_text(stored: LandscapeText) -> _Text:
    text = gzip.decompress(stored.text_gz).decode("utf-8") if stored.text_gz else ""
    return _Text("full_text", text, stored.route, stored.license, stored.truncated)


async def _one_text(db_factory, doc: LandscapeDocument) -> tuple[_Text, bool]:
    """``(what S6 reads, deferred)`` for one paper; ``deferred`` when a transient failure
    still has fetches left (D41)."""
    async with db_factory() as db:
        stored = await db.scalar(select(LandscapeText).where(LandscapeText.document_id == doc.id))
        if stored is not None and stored.text_basis == "full_text" and stored.text_gz:
            return _stored_text(stored), False
        attempts = stored.fetch_attempts if stored is not None else 0
        if stored is not None and (
            stored.text_basis == "summary" or attempts >= FULL_TEXT_MAX_FETCHES
        ):
            return _Text("summary", license=stored.license), False
    result = await fulltext.fetch_full_text(doc)
    async with db_factory() as db:
        await fulltext.store_full_text(db, doc.id, result)
        await db.commit()
    if result.basis == "full_text":
        return _Text("full_text", result.text, result.route, result.license, result.truncated), False
    if result.basis == "fetch_failed":
        return _Text("fetch_failed", license=result.license), attempts + 1 < FULL_TEXT_MAX_FETCHES
    return _Text("summary", license=result.license), False


async def _full_texts(
    db_factory, documents: list[LandscapeDocument]
) -> tuple[dict[uuid.UUID, _Text], bool]:
    """What S6 reads per paper of the dossier, and whether any fetch must be retried first."""
    texts: dict[uuid.UUID, _Text] = {}
    deferred = False
    for doc in documents:
        if doc.kind not in _PAPER_KINDS:
            continue
        texts[doc.id], doc_deferred = await _one_text(db_factory, doc)
        deferred = deferred or doc_deferred
    return texts, deferred


def _extraction_values(result: CallResult, text: str, doc: LandscapeDocument) -> dict:
    """A full-text extraction row's values: the quote-checked extraction, or the failure."""
    if result.error is not None:
        return {"status": "failed:errored", "error": result.error}
    failure = _call_failure(result)
    if failure is not None:
        return failure
    parsed = parse_output(result.text)
    if parsed is None or not isinstance(parsed.get("ideas"), list):
        return {"status": "failed:invalid",
                "error": job_error_text("deep_extract output is not valid")}
    cleaned, verified, dropped = apply_quote_check(parsed, text)
    return {
        "status": "done", "raw": cleaned, "quotes_total": verified + dropped,
        "quotes_verified": verified, "summary_hash": doc.summary_hash,
        "served_by_model": result.served_by, "error": None,
    }


async def _extraction_row(
    db_factory, doc_id: uuid.UUID, epoch_id: uuid.UUID
) -> tuple[uuid.UUID, dict | None]:
    """``(row id, raw)`` of the document's full-text extraction in the epoch; ``raw`` is set
    when it is already done, else the row (created if missing) is marked ``running``."""
    E = LandscapeExtraction
    async with db_factory() as db:
        await db.execute(
            pg_insert(E)
            .values(id=uuid.uuid4(), document_id=doc_id, epoch_id=epoch_id, basis="full_text")
            .on_conflict_do_nothing(index_elements=["document_id", "epoch_id", "basis"])
        )
        row = await db.scalar(
            select(E).where(E.document_id == doc_id, E.epoch_id == epoch_id, E.basis == "full_text")
        )
        row_id = row.id
        if row.status == "done":
            return row_id, row.raw
        row.status = "running"
        await db.commit()
        return row_id, None


def _section_text(text: _Text, doc: LandscapeDocument) -> str:
    label = KIND_LABELS.get(doc.kind, doc.kind)
    return f"Document kind: {label}\n\n{delimit(text.text, 'methods_section')}"


async def _full_text_evidence(
    db_factory, inputs: EpochInputs, epoch_id: uuid.UUID, row_id: uuid.UUID,
    documents: list[LandscapeDocument], texts: dict[uuid.UUID, _Text],
) -> tuple[list[dict], dict[uuid.UUID, dict]]:
    """``(descriptors, extraction by document)``: one descriptor per paper (basis, route,
    license, truncation, extraction id and status), and each done full-text extraction."""
    descriptors: list[dict] = []
    extracted: dict[uuid.UUID, dict] = {}
    for doc in documents:
        text = texts.get(doc.id)
        if text is None:
            continue
        entry = {"document_id": str(doc.id), "basis": text.basis, "route": text.route,
                 "license": text.license, "truncated": text.truncated}
        descriptors.append(entry)
        if text.basis != "full_text" or not text.text:
            continue
        ext_id, raw = await _extraction_row(db_factory, doc.id, epoch_id)
        if raw is None:
            request = build_request(
                EXTRACT_STAGE, model=inputs.model(EXTRACT_STAGE),
                system=inputs.system(EXTRACT_STAGE), user=_section_text(text, doc),
                schema=SCHEMAS["extract"],
            )
            result = await call_sync(
                EXTRACT_STAGE, request, db_factory=db_factory, item_ref=f"deep:{row_id}:{doc.id}"
            )
            values = _extraction_values(result, text.text, doc)
            async with db_factory() as db:
                await db.execute(
                    update(LandscapeExtraction).where(LandscapeExtraction.id == ext_id)
                    .values(**values, updated_at=func.now())
                )
                await db.commit()
            raw = values.get("raw")
        entry.update(extraction_id=str(ext_id), extraction_status="done" if raw else "failed")
        if raw:
            extracted[doc.id] = raw
    return descriptors, extracted


# --- step 2: database records ------------------------------------------------------------------


async def _lookup(
    db: AsyncSession, source: str, key: str, fetch: Callable[[], Awaitable[dict]],
    *, release_source: str | None = None,
) -> dict | None:
    """``fetch`` through the evidence cache; None when it fails (shown as no record)."""
    try:
        async with db.begin_nested():
            release = await cache.current_release(release_source or source)
            return await cache.cached(db, source, release, key, fetch)
    except Exception as exc:  # noqa: BLE001 - a missing record never stops S6
        logger.warning("S6 %s lookup %r failed: %s", source, key, exc)
        return None


def _wrapped(fn: Callable[..., Awaitable[Any]], *args: Any, wrap: str) -> Callable[[], Awaitable[dict]]:
    async def fetch() -> dict:
        return {wrap: await fn(*args)}
    return fetch


def _plain(fn: Callable[..., Awaitable[dict]], *args: Any) -> Callable[[], Awaitable[dict]]:
    async def fetch() -> dict:
        return await fn(*args)
    return fetch


def _usable(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload) and not payload.get("unavailable")


async def _target_records(db: AsyncSession, idea: LandscapeIdea) -> dict[str, Any]:
    """Open Targets (from the assembled evidence, else looked up), HPA and UniProt, per
    matched target."""
    assembled = (idea.evidence_summary or {}).get("opentargets_target")
    assembled = assembled if isinstance(assembled, dict) else {}
    records: dict[str, Any] = {}
    for target in idea.targets or []:
        if not isinstance(target, dict):
            continue
        ensembl = target.get("ensembl_id") or target.get("ensembl_gene_id")
        if ensembl:
            ot = assembled.get(target.get("hgnc_id")) if target.get("hgnc_id") else None
            if not _usable(ot):
                ot = await _lookup(db, "opentargets_target", ensembl,
                                   _plain(opentargets.ot_target, ensembl),
                                   release_source="opentargets")
            if _usable(ot):
                records[f"opentargets:{ensembl}"] = ot
            expression = await _lookup(db, "hpa", ensembl, _plain(hpa.hpa_expression, ensembl))
            if _usable(expression):
                records[f"hpa:{ensembl}"] = expression
        accession = target.get("uniprot_id")
        if accession:
            function = await _lookup(db, "uniprot", accession,
                                     _plain(uniprot.uniprot_function, accession))
            if _usable(function):
                records[f"uniprot:{accession}"] = function
    return records


async def _disease_records(db: AsyncSession, idea: LandscapeIdea) -> dict[str, Any]:
    """The indication's Mondo record, its Orphadata prevalence (assembled, else looked up)
    and the assembled ClinicalTrials.gov counts."""
    disease = idea.disease if isinstance(idea.disease, dict) else {}
    summary = idea.evidence_summary or {}
    records: dict[str, Any] = {}
    if disease.get("mondo_id"):
        records[f"mondo:{disease['mondo_id']}"] = disease
    code = disease.get("orpha_code")
    if code:
        found = summary.get("orphadata")
        if not _usable(found):
            found = await _lookup(db, "orphadata", str(code),
                                  _wrapped(orphadata.orphadata_prevalence, code, wrap="prevalence"))
        prevalence = (found or {}).get("prevalence")
        if prevalence:
            records[f"orphadata:{str(code).rsplit(':', 1)[-1].strip()}"] = prevalence
    trials = summary.get("trials")
    if _usable(trials):
        for name, counts in sorted(trials.items()):
            records[f"trials:{name}"] = counts
    return records


def _drug_names(idea: LandscapeIdea, target_records: dict[str, Any]) -> list[str]:
    """Drugs to look up in openFDA: Open Targets drugs and candidates recorded against the
    idea's disease, then a chemistry idea's own agent; at most `MAX_DRUG_LOOKUPS`."""
    disease = idea.disease if isinstance(idea.disease, dict) else {}
    efo = opentargets.to_efo_id(disease["mondo_id"]) if disease.get("mondo_id") else None
    names: list[str] = []
    for ref, payload in target_records.items():
        if not ref.startswith("opentargets:") or efo is None:
            continue
        for row in (payload.get("drugAndClinicalCandidates") or {}).get("rows") or []:
            diseases = {
                ((entry or {}).get("disease") or {}).get("id")
                for entry in row.get("diseases") or [] if isinstance(entry, dict)
            }
            name = (row.get("drug") or {}).get("name")
            if efo in diseases and isinstance(name, str) and name.strip():
                names.append(name.strip())
    agent = idea.agent if isinstance(idea.agent, dict) else {}
    agent_name = agent.get("name") or agent.get("as_written")
    if idea.idea_type == "chemistry" and isinstance(agent_name, str) and agent_name.strip():
        names.append(agent_name.strip())
    unique = list({name.casefold(): name for name in names}.values())
    return unique[:MAX_DRUG_LOOKUPS]


async def _drug_records(db: AsyncSession, names: list[str]) -> dict[str, Any]:
    """openFDA Drugs@FDA applications per name, keyed ``openfda:<application number>``."""
    records: dict[str, Any] = {}
    for name in names:
        found = await _lookup(db, "openfda", name.casefold(),
                              _wrapped(openfda.fda_approvals, name, wrap="rows"))
        for application in (found or {}).get("rows") or []:
            number = application.get("application_number")
            if number:
                records[f"openfda:{number}"] = {**application, "looked_up_as": name}
    return records


async def _records(db_factory, idea: LandscapeIdea) -> dict[str, Any]:
    """Every database record the analysis may cite, by reference key (§7.7 steps 3-4)."""
    async with db_factory() as db:
        targets = await _target_records(db, idea)
        records = {**targets, **await _disease_records(db, idea)}
        records.update(await _drug_records(db, _drug_names(idea, targets)))
        await db.commit()
    return records


# --- the dossier --------------------------------------------------------------------------------


def _idea_lines(facts: _Facts) -> list[str]:
    idea = facts.idea
    lines = ["# Idea", f"- Type: {idea.idea_type}"]
    for label, value in (
        ("Targets", idea.targets), ("Direction", idea.direction), ("Agent", idea.agent),
        ("Disease", idea.disease), ("Modality variants", idea.modality_variants),
        ("Stage", idea.stage), ("Evidence grade", idea.evidence_grade),
    ):
        lines.append(f"- {label}: {_json(value) if value else 'none given'}")
    lines.append(f"- Independent JHU groups: {idea.jhu_group_count}")
    for other in facts.linked:
        lines.append(
            f"- Linked {other.idea_type} idea: targets {_json(other.targets)}, agent "
            f"{_json(other.agent)}, disease {_json(other.disease)}, stage {other.stage}"
        )
    return lines


def _lab_lines(facts: _Facts) -> list[str]:
    lines = ["", "# Qualifying labs"]
    institutions: dict[str, set[str]] = {}
    for doc in facts.documents:
        if doc.lab_key and doc.qualifying_institution:
            institutions.setdefault(doc.lab_key, set()).add(doc.qualifying_institution)
    for lab in facts.labs:
        lines.append(
            f"- {lab.display_name or lab.lab_key}: affiliations "
            f"{', '.join(sorted(institutions.get(lab.lab_key, ()))) or 'unknown'}; "
            f"ORCID {lab.orcid or 'none'}; OpenAlex {lab.openalex_author_id or 'none'}"
        )
    return lines if facts.labs else [*lines, "- none recorded"]


def _document_lines(facts: _Facts, texts: dict[uuid.UUID, _Text]) -> list[str]:
    lines = ["", "# Documents (cite as doc:<id>)"]
    for doc in facts.documents:
        basis = texts[doc.id].basis if doc.id in texts else "summary"
        ids = {"doi": doc.doi, "pmid": doc.pmid, "pmcid": doc.pmcid}
        lines += [
            "", f"## doc:{doc.id}",
            f"- Kind: {doc.kind} ({doc.document_type or 'untyped'}), "
            f"{doc.published_on.isoformat() if doc.published_on else 'undated'}",
            f"- Identifiers: {_json({k: v for k, v in ids.items() if v})}",
            f"- Qualifying authors: {_json(doc.qualifying_authorships or [])}, "
            f"institution {doc.qualifying_institution or 'unknown'}",
            f"- Details: {_json(doc.details or {}, _DETAILS_MAX_CHARS)}",
            f"- Text read: {basis}",
            fenced_text(doc),
        ]
    return lines


def _full_text_lines(extracted: dict[uuid.UUID, dict]) -> list[str]:
    lines = ["", "# Full-text evidence (quote-checked against the full text)"]
    if not extracted:
        return [*lines, "No full text was available."]
    for doc_id, raw in extracted.items():
        lines += ["", f"## doc:{doc_id}",
                  delimit(_json(raw.get("ideas") or []), "methods_section")]
    return lines


def _record_lines(records: dict[str, Any]) -> list[str]:
    lines = ["", "# Database records (cite by the key shown)"]
    if not records:
        return [*lines, "No database record was found."]
    return [*lines, *(f"- `{ref}`: {_json(payload, _RECORD_MAX_CHARS)}"
                      for ref, payload in sorted(records.items()))]


def _flag_lines(facts: _Facts) -> list[str]:
    lines = ["", "# Flags to review (shown, never excluding)"]
    flags = facts.idea.data_flags or {}
    lines += [f"- data flag {key}: {_json(flags[key])}" for key in sorted(flags)]
    triage = facts.triage
    if triage is not None:
        llm_flags = triage.llm_flags or {}
        lines += [f"- triage flag {key}: {_json(llm_flags[key])}" for key in sorted(llm_flags)]
        lines.append(f"- triage gates: {_json(triage.gating or {})}")
    return lines if len(lines) > 2 else [*lines, "- none raised"]


def _dossier(
    facts: _Facts, texts: dict[uuid.UUID, _Text], extracted: dict[uuid.UUID, dict],
    records: dict[str, Any],
) -> str:
    """The S6 user message: the idea, its labs, every listed document (summary fenced),
    the full-text evidence (fenced ``methods_section``, D44), the database records and the
    flags to review. Not blind: S6 judges the team from the record (§7.7 step 7)."""
    lines = _idea_lines(facts)
    lines += _lab_lines(facts)
    lines += _document_lines(facts, texts)
    lines += _full_text_lines(extracted)
    lines += _record_lines(records)
    lines += _flag_lines(facts)
    return "\n".join(lines)


# --- step 3: the analysis and its literature loop ---------------------------------------------


async def _run_tools(blocks: list[Any], session: _Session) -> list[dict]:
    """One ``tool_result`` per ``tool_use`` block; every source returned joins
    ``session.retrieved`` and the transcript. PubMed being down is told to the model."""
    results = []
    for block in blocks:
        tool_input = dict(block.input) if isinstance(block.input, Mapping) else {}
        try:
            found = await project_search.execute_research_tool(block.name, tool_input)
        except project_search.PubMedUnavailable:
            found = project_search.ToolResult(_PUBMED_DOWN)
        keys = []
        for source in found.sources:
            if source.get("key"):
                session.retrieved[source["key"]] = {
                    "kind": source.get("kind"), "title": source.get("title") or "",
                }
                keys.append(source["key"])
        session.transcript.append(
            {"round": session.rounds, "tool": block.name, "input": tool_input, "sources": keys}
        )
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": found.text})
    return results


def _with_final_text(messages: list[dict]) -> list[dict]:
    final = {"type": "text", "text": _FINAL_ROUND_TEXT}
    last = messages[-1]
    content = last["content"] if isinstance(last["content"], list) else [
        {"type": "text", "text": last["content"]}
    ]
    return [*messages[:-1], {**last, "content": [*content, final]}]


async def _analysis(
    db_factory, inputs: EpochInputs, dossier: str, row_id: uuid.UUID
) -> tuple[CallResult, _Session]:
    """The analysis conversation: tools while rounds remain, then the JSON answer."""
    session = _Session()
    messages: list[dict] = [{"role": "user", "content": dossier}]
    while True:
        final = session.rounds >= MAX_SEARCH_ROUNDS
        request = build_request(
            STAGE, model=inputs.model(STAGE), system=inputs.system(STAGE), user=dossier,
            schema=SCHEMAS[STAGE],
            tools=None if final else project_search.research_tool_definitions(),
        )
        request["messages"] = _with_final_text(messages) if final else list(messages)
        result = await call_sync(STAGE, request, db_factory=db_factory, item_ref=f"deep:{row_id}")
        blocks = list(getattr(result.message, "content", None) or [])
        tool_blocks = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        if final or result.error is not None or not tool_blocks:
            return result, session
        session.rounds += 1
        messages = [
            *messages,
            {"role": "assistant", "content": [b.model_dump() for b in blocks]},
            {"role": "user", "content": await _run_tools(tool_blocks, session)},
        ]


# --- checking the answer -----------------------------------------------------------------------


def _support(
    raw: Any, refs: Mapping[str, Any], retrieved: Mapping[str, Any],
    prefixes: tuple[str, ...] | None = None,
) -> list[str] | str:
    """The cited references that name a dossier document or record (``refs``) or a source the
    session retrieved, limited to ``prefixes`` when given; ``"estimate"`` when none is left."""
    kept: list[str] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        text = entry.strip()
        key = text if text in refs or text in retrieved else resolve_source(text, retrieved)
        if key and (prefixes is None or key.startswith(prefixes)) and key not in kept:
            kept.append(key)
    return kept or "estimate"


def _disease_profile(
    raw: Any, refs: Mapping[str, Any], retrieved: Mapping[str, Any]
) -> dict:
    """Every §7.7 step 3 field, each ``{"value", "support"}`` (L30, L31)."""
    raw = raw if isinstance(raw, dict) else {}
    orpha = {ref: None for ref in refs if ref.startswith("orphadata:")}
    profile = {}
    for key in DISEASE_PROFILE_KEYS:
        entry = raw.get(key) if isinstance(raw.get(key), dict) else {}
        cited = entry.get("support")
        if key == "prevalence":
            support = _support(cited, orpha, {}) if orpha else "estimate"
        elif key == "approved_drugs":
            support = _support(cited, refs, {}, _DRUG_PREFIXES)
        else:
            support = _support(cited, refs, retrieved)
        value = entry.get("value")
        profile[key] = {"value": value if isinstance(value, str) else "", "support": support}
    return profile


def _supported_lines(
    raw: Any, text_key: str, refs: Mapping[str, Any], retrieved: Mapping[str, Any],
    prefixes: tuple[str, ...],
) -> list[dict]:
    """The entries whose support keeps at least one reference under ``prefixes``."""
    kept = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get(text_key), str):
            continue
        support = _support(entry.get("support"), refs, retrieved, prefixes)
        if support != "estimate":
            kept.append({text_key: entry[text_key], "support": support})
    return kept


def _valid(parsed: dict | None) -> bool:
    if parsed is None or not isinstance(parsed.get("summary"), str):
        return False
    gating, scores = parsed.get("gating"), parsed.get("scores")
    return (
        isinstance(gating, dict) and all(gating.get(k) in _GATE_VALUES for k in GATING_KEYS)
        and isinstance(scores, dict)
        and all(isinstance(scores.get(k), int) and not isinstance(scores.get(k), bool)
                for k in DIMENSION_KEYS)
    )


def _strings(raw: Any, keys: tuple[str, ...]) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw[key] if isinstance(raw.get(key), str) else "" for key in keys}


def _judged_from(parsed: dict, refs: Mapping[str, Any], session: _Session, rubric) -> dict:
    """The row's judged columns from a valid answer, its support checked in code; ``refs``
    holds every citable dossier document and database record key, and ``rubric`` (the epoch's)
    weighs the provisional scores."""
    retrieved = session.retrieved
    gating = {key: parsed["gating"][key] for key in GATING_KEYS}
    scores = {key: parsed["scores"][key] for key in DIMENSION_KEYS}
    outside = _supported_lines(parsed.get("outside_groups"), "finding", {}, retrieved,
                               _RETRIEVED_PREFIXES)
    flags = {
        entry["flag"]: {"status": entry["status"], "reason": entry.get("reason") or ""}
        for entry in parsed.get("flag_review") or []
        if isinstance(entry, dict) and isinstance(entry.get("flag"), str)
        and entry.get("status") in _FLAG_STATUSES
    }
    return {
        "status": "excluded:deep_gate" if "not_met" in gating.values() else "done",
        "summary": parsed["summary"],
        "disease_profile": _disease_profile(parsed.get("disease_profile"), refs, retrieved),
        "target_biology": _supported_lines(parsed.get("target_biology"), "line", refs, {},
                                           _BIOLOGY_PREFIXES),
        "gating": gating,
        "gating_rationales": _strings(parsed.get("gating_rationales"), GATING_KEYS),
        "flags_reviewed": flags,
        "provisional_scores": scores,
        "dimension_rationales": _strings(parsed.get("dimension_rationales"), DIMENSION_KEYS),
        "provisional_weighted_score": epoch_weighted_score(rubric, scores),
        "outside_group_count": len(outside),
        "search_transcript": [*session.transcript, {"outside_groups": outside}],
        "refusal_category": None,
        "error": None,
    }


# --- step 4: the panel -------------------------------------------------------------------------


async def _consults(
    db_factory, inputs: EpochInputs, row_id: uuid.UUID, idea_type: str | None,
    dossier: str, summary: str,
) -> list[dict]:
    """One consult per panel domain (D12); a consult that errors, is refused or says nothing
    is logged and left out."""
    consults = []
    for domain in panel_domains(idea_type):
        request = build_request(
            STAGE, model=inputs.model(STAGE), system=inputs.persona(domain),
            user=consult_message(domain, dossier=dossier, summary=summary), schema=None,
        )
        result = await call_sync(
            STAGE, request, db_factory=db_factory, item_ref=f"deep:{row_id}:{domain}"
        )
        if result.error is not None or result.stop_reason == "refusal" or not result.text.strip():
            logger.warning("S6 %s consult for row %s gave no opinion: %s", domain, row_id,
                           result.error or result.stop_reason or "empty")
            continue
        opinion = asdict(parse_opinion(result.text, domain=domain))
        opinion.pop("raw", None)
        consults.append({"domain": domain, "opinion": opinion, "raw_text": result.text,
                         "served_by_model": result.served_by})
    return consults


# --- the runner --------------------------------------------------------------------------------


async def _deep(
    db_factory, row_id: uuid.UUID, idea_id: uuid.UUID, epoch_id: uuid.UUID
) -> tuple[dict, list[dict]]:
    """Steps 1-4 for a claimed row: ``(row values, consults)``."""
    async with db_factory() as db:
        inputs = await epochs.epoch_inputs(db, epoch_id)
        facts = await _facts(db, idea_id, epoch_id)
    texts, deferred = await _full_texts(db_factory, facts.documents)
    if deferred:
        return {"status": "pending"}, []
    descriptors, extracted = await _full_text_evidence(
        db_factory, inputs, epoch_id, row_id, facts.documents, texts
    )
    records = await _records(db_factory, facts.idea)
    dossier = _dossier(facts, texts, extracted, records)
    result, session = await _analysis(db_factory, inputs, dossier, row_id)
    if result.error is not None:
        return await _errored(db_factory, row_id, result.error), []
    failure = _call_failure(result)
    if failure is not None:
        return failure, []
    parsed = parse_output(result.text)
    if not _valid(parsed):
        return {"status": "failed:invalid", "served_by_model": result.served_by,
                "error": job_error_text("deep output is not valid")}, []
    refs = {**records, **{f"doc:{doc.id}": None for doc in facts.documents}}
    values = {**_judged_from(parsed, refs, session, inputs.rubric),
              "served_by_model": result.served_by,
              "full_text_evidence": {"documents": descriptors}}
    if values["status"] != "done":
        return values, []
    consults = await _consults(
        db_factory, inputs, row_id, facts.idea.idea_type, dossier, values["summary"]
    )
    return values, consults


async def run_deep(db_factory, idea_id: uuid.UUID, *, epoch_id: uuid.UUID) -> None:
    """S6 for the idea's current version in ``epoch_id`` (spec §7.7); see the module docstring.

    Does nothing while a simulation run is live, or when the version's row is not ``pending``.
    Ends with the row ``done`` (consults written), ``excluded:deep_gate``, ``refused``,
    ``failed:invalid``, ``failed:truncated``, ``failed:errored`` (after one retry), or back at
    ``pending`` (a full-text fetch to retry, or an error's first retry). Never changes the
    idea's version; sets its ``outside_group_count``. Each step commits on its own.
    """
    row_id = await _claim(db_factory, idea_id, epoch_id)
    if row_id is None:
        return
    try:
        values, consults = await _deep(db_factory, row_id, idea_id, epoch_id)
    except Exception as exc:  # noqa: BLE001 - the row records the failure, the loop goes on
        logger.exception("S6 failed for idea %s in epoch %s", idea_id, epoch_id)
        values, consults = await _errored(db_factory, row_id, job_error_text(exc)), []
    await _store(db_factory, row_id, values, consults)
