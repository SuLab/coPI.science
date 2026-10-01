"""Verbatim copies of ``convert_dois_to_pmids`` (src/services/pubmed.py) and
``resolve_corpus`` (src/services/corpus.py) at the Phase 3 base, 9b66cc85, before
Task 14 batched DOI verification and Task 15 split the resolver.

Differential oracle for tests/unit/test_doi_resolution_batching.py: one EFetch per
ESearch hit, a fresh client per request (no ``ncbi_session``), and one EFetch stage
over every PMID in ``stages`` order. ``resolve_corpus`` here calls the
``convert_dois_to_pmids`` below; every other helper is imported from src, where it
is unchanged since the base. Tests patch ``fetch_orcid_works`` and
``fetch_works_by_orcid`` on this module as well as on ``src.services.corpus``. Do
not edit.
"""

import logging
from typing import Any

from src.services.corpus import (
    DEFAULT_CAP,
    EXCLUDED_TYPES,
    SEARCH_RETMAX,
    CorpusResult,
    CorpusStageError,
    _aff_match,
    _match_pi_author_detail,
    _normalize_title,
    _pmid_sort_key,
    build_pubmed_query,
)
from src.services.openalex import fetch_works_by_orcid
from src.services.orcid import fetch_orcid_works
from src.services.pubmed import (
    _SYSTEMIC_RUN,
    EUTILS_BASE,
    IDCONV_BASE,
    _describe,
    _doi_fold,
    _failure_signature,
    _fetch_pubmed_batch,
    _is_per_item_failure,
    _ncbi_get,
    fetch_pubmed_records,
    normalize_doi,
    search_pmids,
)

logger = logging.getLogger(__name__)


async def convert_dois_to_pmids(
    dois: list[str],
    *,
    strict: bool = False,
    permanently_dropped: list[str] | None = None,
) -> dict[str, str]:
    """
    Convert DOIs to PMIDs. First tries NCBI ID converter (batch, but PMC-only),
    then falls back to PubMed ESearch for unresolved DOIs.
    Returns dict of {doi: pmid}, keyed by the CALLER's DOI form — every key is
    an element of ``dois``, never the resolver's own spelling of one.

    A DOI absent from the result is an ANSWER ("NCBI does not map it"): an
    idconv ``status == "error"`` record, an ESearch with other than exactly one
    hit, or a round-trip DOI mismatch. A FAILED request (idconv batch, per-DOI
    ESearch, or the round-trip EFetch) is logged and swallowed by default, which
    makes it indistinguishable from an answer. With ``strict=True``
    (``resolve_corpus``) the failure is split by ``_is_per_item_failure``:

    * NOT per-item (transport error, 429, 5xx, or any exception other than a
      4xx/unreadable body — most likely a bug of ours): re-raises, so the job
      retries or fails visibly.
    * PER-ITEM (a 4xx other than 429, an unreadable body): it would repeat on
      every retry, so it is treated as a miss, with a WARNING naming the
      DOI(s). A failed idconv batch leaves its DOIs to the ESearch phase. A
      per-DOI lookup that fails per-item — its ESearch or its round-trip
      EFetch — counts as "no PMID for this DOI" and the DOI is appended to
      ``permanently_dropped`` when that list is given; but ``_SYSTEMIC_RUN``
      consecutive per-DOI lookups failing the same way (the same 4xx status,
      or the same unreadable-body error) re-raise, because that is NCBI
      refusing or failing every request, not that many bad DOIs.

    ``permanently_dropped`` is populated in strict mode only; a DOI answered
    as unmapped (above) is an answer and is never recorded there.
    """
    if not dois:
        return {}

    mapping = {}

    # Phase 1: NCBI ID converter (batch — only finds PMC-indexed papers).
    # The converter echoes each DOI in ITS canonical casing (lowercased), not
    # the caller's: ORCID records often carry the publisher's uppercase form
    # (10.1039/D0RA08249J), and keying the result by the echo handed
    # resolve_corpus a mapping key its doi_pool never held — the bare KeyError
    # that killed the Konig and Slusher generate_profile jobs three attempts
    # each (2026-08-25). Fold the echo back to the requested form; an echo
    # matching no request even case-folded is dropped, and that input then
    # falls through to the ESearch phase, which round-trip-verifies before
    # accepting anything.
    for i in range(0, len(dois), 200):
        batch = dois[i : i + 200]
        requested_by_fold: dict[str, str] = {}
        for d in batch:
            requested_by_fold.setdefault(_doi_fold(d), d)
        try:
            params = {"ids": ",".join(batch), "format": "json"}
            resp = await _ncbi_get(IDCONV_BASE, params)
            data = resp.json()
            for record in data.get("records", []):
                if record.get("status") == "error":
                    continue
                doi = record.get("doi")
                pmid = record.get("pmid")
                if not (doi and pmid):
                    continue
                requested_doi = requested_by_fold.get(_doi_fold(doi))
                if requested_doi is None:
                    logger.warning(
                        "ID converter echoed DOI %r, which matches no "
                        "requested DOI even case-folded; dropping the record",
                        doi,
                    )
                    continue
                mapping[requested_doi] = str(pmid)
        except Exception as exc:
            if not strict:
                logger.warning("Failed batch DOI→PMID via ID converter: %s", exc)
                continue
            if not _is_per_item_failure(exc):
                raise
            logger.warning(
                "ID converter batch failed permanently (%s: %s); its DOIs "
                "fall through to ESearch: %s",
                type(exc).__name__, exc, ", ".join(batch),
            )

    # Phase 2: PubMed ESearch for remaining DOIs
    remaining = [d for d in dois if d not in mapping]
    if remaining:
        logger.info("Resolving %d remaining DOIs via PubMed ESearch", len(remaining))
        # Consecutive per-DOI lookups that failed per-item the same way, and
        # that failure's signature; any other outcome resets the run.
        run, last_sig = 0, None
        for doi in remaining:
            try:
                params = {
                    "db": "pubmed",
                    "term": f"{doi}[doi]",
                    "retmode": "json",
                }
                resp = await _ncbi_get(f"{EUTILS_BASE}/esearch.fcgi", params)
                data = resp.json()
                id_list = data.get("esearchresult", {}).get("idlist", [])
                # D4b (coverage design §7): a multi-hit DOI ESearch is a MISS,
                # not a hit. Taking idlist[0] unchecked resolved one Research
                # Square DOI to four unrelated PMIDs and stored the first —
                # the same wrong paper landed on six PIs' rows.
                if len(id_list) != 1:
                    if len(id_list) > 1:
                        logger.warning(
                            "ESearch for DOI %s returned %d PMIDs; treating "
                            "as a miss (D4b)", doi, len(id_list),
                        )
                else:
                    pmid = id_list[0]
                    # Round-trip verify: the PMID's authoritative DOI must
                    # equal the queried DOI, or the single hit is still the
                    # wrong paper. Strict fetches the one record directly, so
                    # a failure reaches the classification below with its
                    # status intact (through fetch_pubmed_records a one-PMID
                    # batch would be dropped inside it and read here as a
                    # verification miss).
                    if strict:
                        records = await _fetch_pubmed_batch([pmid])
                    else:
                        records = await fetch_pubmed_records([pmid])
                    authoritative = (
                        normalize_doi(records[0].get("doi")) if records else None
                    )
                    queried = normalize_doi(doi)
                    if (
                        authoritative
                        and queried
                        and authoritative.lower() == queried.lower()
                    ):
                        mapping[doi] = pmid
                    else:
                        logger.warning(
                            "ESearch hit for DOI %s (PMID %s) failed round-trip "
                            "verification (authoritative DOI %r); treating as a "
                            "miss (D4b)", doi, pmid, authoritative,
                        )
            except Exception as exc:
                if not strict:
                    logger.debug("ESearch DOI lookup failed for %s: %s", doi, exc)
                    continue
                if not _is_per_item_failure(exc):
                    raise
                sig = _failure_signature(exc)
                run, last_sig = (run + 1, sig) if sig == last_sig else (1, sig)
                if run >= _SYSTEMIC_RUN:
                    logger.error(
                        "DOI lookup: %d consecutive DOIs failed %s; treating "
                        "it as systemic, not per-DOI",
                        run, _describe(sig),
                    )
                    raise
                logger.warning(
                    "ESearch DOI lookup for %s failed permanently (%s: %s); "
                    "treating it as no PMID for this DOI",
                    doi, type(exc).__name__, exc,
                )
                if permanently_dropped is not None:
                    permanently_dropped.append(doi)
                continue
            run, last_sig = 0, None

    return mapping


async def resolve_corpus(
    orcid: str,
    name: str,
    institution: str | None,
    *,
    cap: int = DEFAULT_CAP,
) -> CorpusResult:
    """Resolve, gate, dedupe, rank and cap a PI's publication corpus.

    Every retrieval stage runs inside ``_stage``, so any failure it raises
    becomes ``CorpusStageError`` and no corpus is built. The three lookups
    that swallow failures by default run with ``strict=True``:
    ``fetch_orcid_works`` then raises on anything but a record-state status
    (``_RECORD_STATE_STATUSES``: 301, 404, 409, 410); the two NCBI lookups
    raise on a transient failure or a bug, while a permanent per-item failure
    drops only that PMID/DOI and is reported in
    ``CorpusResult.permanently_dropped`` rather than hidden (a run of
    ``_SYSTEMIC_RUN`` identical ones raises instead).
    """

    stages: dict[str, set[str]] = {}
    doi_pool: dict[str, str] = {}  # doi -> first stage that proposed it

    def _add(pmid: str | None, stage: str) -> None:
        if pmid:
            stages.setdefault(str(pmid), set()).add(stage)

    async def _stage(stage_name: str, coro):
        try:
            return await coro
        except Exception as exc:
            raise CorpusStageError(
                f"corpus stage {stage_name} failed: {exc}"
            ) from exc

    stage_counts: dict[str, int] = {}
    permanently_dropped: list[str] = []

    orcid_dois: dict[str, str] = {}
    orcid_doi_only: dict[str, str] = {}  # doi -> "" until resolved to a pmid

    orcid_works = await _stage(
        "s1_orcid_works", fetch_orcid_works(orcid, strict=True)
    )
    for w in orcid_works:
        if w.get("pmid"):
            _add(w["pmid"], "s1")
            if w.get("doi"):
                orcid_dois[str(w["pmid"])] = w["doi"]
        elif w.get("doi"):
            doi_pool.setdefault(w["doi"], "s1")
            orcid_doi_only[w["doi"]] = ""
    stage_counts["s1"] = len(orcid_works)

    openalex_works = await _stage("s2_openalex", fetch_works_by_orcid(orcid))
    for w in openalex_works:
        if w.get("pmid"):
            _add(w["pmid"], "s2")
        elif w.get("doi"):
            doi_pool.setdefault(w["doi"], "s2")
    stage_counts["s2"] = len(openalex_works)

    s3_pmids = await _stage(
        "s3_pubmed_auid", search_pmids(f"{orcid}[auid]", retmax=SEARCH_RETMAX)
    )
    for pmid in s3_pmids:
        _add(pmid, "s3")
    stage_counts["s3"] = len(s3_pmids)

    if institution and institution.strip():
        term = build_pubmed_query(name, [institution])
        s4_pmids = await _stage(
            "s4_pubmed_name_affiliation",
            search_pmids(term, retmax=SEARCH_RETMAX),
        )
        for pmid in s4_pmids:
            _add(pmid, "s4")
        stage_counts["s4"] = len(s4_pmids)
    else:
        # R4: a missing institution silently disables S4 — say so loudly.
        logger.warning(
            "resolve_corpus(%s): no institution on file, S4 skipped", orcid
        )
        stage_counts["s4"] = 0

    if doi_pool:
        mapping = await _stage(
            "doi_resolution",
            convert_dois_to_pmids(
                list(doi_pool), strict=True,
                permanently_dropped=permanently_dropped,
            ),
        )
        for doi, pmid in mapping.items():
            stage = doi_pool.get(doi)
            if stage is None:
                # convert_dois_to_pmids guarantees its keys are the caller's
                # own forms; when that contract broke (the converter echoed
                # lowercase, 2026-08-25) a bracket lookup here killed the
                # whole profile job. Losing one attribution must never cost
                # the corpus — skip it loudly instead.
                logger.warning(
                    "doi_resolution returned %r, which is not a doi_pool "
                    "key; skipping it",
                    doi,
                )
                continue
            _add(pmid, stage)
            if doi in orcid_doi_only:
                orcid_dois[str(pmid)] = doi

    records = (
        await _stage(
            "efetch",
            fetch_pubmed_records(
                list(stages), strict=True,
                permanently_dropped=permanently_dropped,
            ),
        )
        if stages
        else []
    )
    # A DOI whose lookup failed per-item is not missing if its paper arrived
    # anyway, through another stage's PMID: EFetch returned a record carrying
    # that DOI. Only a DOI with no such record leaves the corpus incomplete.
    fetched_dois = {_doi_fold(r["doi"]) for r in records if r.get("doi")}
    permanently_dropped[:] = [
        item for item in permanently_dropped
        if item not in doi_pool or _doi_fold(item) not in fetched_dois
    ]

    kept: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []
    dropped = {
        "consortium": 0,
        "excluded_type": 0,
        "identity": 0,
        "duplicate_title": 0,
    }

    for rec in records:
        rec_stages = stages.get(str(rec.get("pmid")), set())
        rec["stages"] = sorted(rec_stages)
        kind, pi_affs, bare_initial = _match_pi_author_detail(rec, name)
        rec["authorship"] = kind
        rec["pi_affiliations"] = pi_affs

        pub_types = {t.lower() for t in rec.get("pub_types") or []}
        if pub_types & EXCLUDED_TYPES:
            dropped["excluded_type"] += 1
            continue
        if kind == "consortium":
            dropped["consortium"] += 1
            continue
        if kind == "no_match":
            dropped["identity"] += 1
            flagged.append(
                {
                    "pmid": rec.get("pmid"),
                    "reason": "no_individual_author_match",
                    "title": rec.get("title", ""),
                    "stages": rec["stages"],
                }
            )
            continue
        # Item 5/D3: a single-letter forename is not, on its own, enough
        # identity evidence to trust — corroboration is now required on ANY
        # stage combination for a bare-initial match, not just the old
        # S4-only gate. The S4-only gate itself is KEPT for a full-forename
        # match too (an S4-only candidate has no ORCID anchor at all, so an
        # affiliation match is the rehearsal's own mandatory disambiguation
        # regardless of how specific the name match was) — ``rec_stages ==
        # {"s4"}`` is folded into the same check below since it can never be
        # "anchored" (s1/s3 are absent from the set by construction).
        if bare_initial or rec_stages == {"s4"}:
            anchored = bool(rec_stages & {"s1", "s3"})
            aff_confirmed = bool(institution) and any(
                _aff_match(institution, a) for a in pi_affs
            )
            # Third route, and the one that carries the pre-2000 corpus:
            # the record came back from S4 — whose term is `author AND
            # institution` (`build_pubmed_query`), so PubMed has already
            # confirmed the paper is FROM the PI's institution — AND the
            # matched author entry carries NO affiliation string at all.
            #
            # The `not pi_affs` clause is what keeps this from gutting the
            # rehearsal's S4-only disambiguation. An ABSENT affiliation is
            # absence of evidence: PubMed recorded an affiliation for the
            # first author only until ~2014 and none whatsoever before
            # ~1988, so a last-author 1996 paper can never satisfy the
            # strict `aff_confirmed` form no matter who wrote it. A PRESENT
            # affiliation that does not match is evidence against, and still
            # rejects — which is the case (a co-author supplies the Hopkins
            # string while the named author sits elsewhere) the original
            # gate existed to catch.
            #
            # Measured 2026-09-22: without this route, corroboration
            # re-dropped every one of Rothstein's 12 landmark papers that
            # items 1-4 had just recovered — the widening and the
            # containment exactly cancelling. Residual risk, accepted and
            # recorded in the plan: a DIFFERENT J. Rothstein publishing out
            # of Johns Hopkins in those same years would also pass. Surname
            # + first initial + institution-on-the-paper is the strongest
            # signal these records physically carry.
            inst_searched = (
                bool(institution) and "s4" in rec_stages and not pi_affs
            )
            if not anchored and not aff_confirmed and not inst_searched:
                dropped["identity"] += 1
                flagged.append(
                    {
                        "pmid": rec.get("pmid"),
                        # The two misses are different evidence: an
                        # initial-only name nobody corroborated, versus a
                        # full-name S4-only hit whose author affiliation did
                        # not match. Keep them apart in the progress text.
                        "reason": (
                            "bare_initial_unconfirmed"
                            if bare_initial
                            else "s4_affiliation_mismatch"
                        ),
                        "title": rec.get("title", ""),
                        "stages": rec["stages"],
                    }
                )
                continue
        kept.append(rec)

    # Dedupe by normalized title (PMID dedupe fell out of the stages map).
    # Preprint/journal pairs share a title; keep the later year (the journal
    # version), tiebreak higher PMID. Errata never reach here (EXCLUDED_TYPES).
    by_title: dict[str, dict] = {}
    for rec in kept:
        key = _normalize_title(rec.get("title", "")) or f"pmid:{rec.get('pmid')}"
        rival = by_title.get(key)
        if rival is None:
            by_title[key] = rec
        else:
            dropped["duplicate_title"] += 1
            winner = max(
                (rival, rec),
                key=lambda r: (r.get("year") or 0, _pmid_sort_key(r.get("pmid"))),
            )
            by_title[key] = winner

    ranked = sorted(
        by_title.values(),
        key=lambda r: (r.get("year") or 0, _pmid_sort_key(r.get("pmid"))),
        reverse=True,
    )
    kept = ranked[:cap]  # the cap is applied LAST

    logger.info(
        "resolve_corpus(%s): stages=%s kept=%d flagged=%d dropped=%s "
        "permanently_dropped=%d",
        orcid, stage_counts, len(kept), len(flagged), dropped,
        len(permanently_dropped),
    )
    return CorpusResult(
        kept=kept,
        flagged=flagged,
        stage_counts=stage_counts,
        dropped=dropped,
        orcid_dois=orcid_dois,
        ranked=ranked,
        permanently_dropped=permanently_dropped,
    )
