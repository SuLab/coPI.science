"""Read-only per-PI corpus classification
(docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md §3; §5, Tasks 2 and 8).

Re-runnable measurement instrument, alongside ``scripts/audit_pub_dois.py``.
For every PI in scope: refetch each stored PMID and classify it against the
CURRENT ``match_pi_author`` (strong match / bare-initial-only /
no-surname-author / ambiguous / incomplete list), count ``EXCLUDED_TYPES`` hits
(split pure vs. secondary, D4), count duplicate-title groups, and report the ``resolve_corpus`` delta —
the over-match alarm for any matcher change (stored, resolved, overlap,
resolved-not-stored, stored-not-resolved, ``dropped["identity"]``).

Never writes anything — no DB write, no file write, no re-export.
Live resolve uses the free-budget admission/request guards; unresolved coverage
is recorded in resolve_error and exits nonzero (not for explicit --skip-resolve).

Output: one JSON object per PI, stable key order, one per line, to stdout —
easy to diff between runs. A human-readable summary goes to stderr so stdout
stays clean JSONL.

Modes (mutually exclusive):

    python scripts/audit_pi_corpus.py                       # legacy: every PI
    python scripts/audit_pi_corpus.py --orcid 0000-...       # scoped (repeatable)
    python scripts/audit_pi_corpus.py --skip-resolve         # classification only,
                                                              # no resolve_corpus delta
                                                              # (no ORCID/OpenAlex/S4 calls)
    python scripts/audit_pi_corpus.py --baseline [--orcid ...] [--skip-resolve]
    python scripts/audit_pi_corpus.py --compare FILE|- [--orcid ...]

``--baseline`` runs on the image being replaced, with this script bind-mounted over its
own, and records each PI's anchored in-use rows with today's gate verdicts, the refetched
records and the inputs of one live identity gate (``audit_pi_corpus.baseline/1`` JSONL).
Its session is read-only and ends before any network call; it writes no file.
``--compare`` runs on the new image with no database and no network, re-judges the same
inputs with the new code and applies the F6 stop rule of the 2026-10-15
publication-handling design: more than 1% of the evaluated rows newly failing, or any PI
whose in-use count would rise by more than 25% and more than 10 papers. Exit 0 PASS,
2 STOP FOR OWNER REVIEW, 1 INCOMPLETE. ``--baseline`` imports only names the pre-Phase-3
image has (tests/unit/test_audit_pi_corpus.py pins them).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from scripts._bulk_enqueue import pi_population  # noqa: E402
from scripts.repair_pi_corpus import (  # noqa: E402
    classify_excluded_type,
    load_stored_publications,
    normalize_title,
    refetch_pmids,
)
from src.database import get_session_factory  # noqa: E402
from src.models import User  # noqa: E402
from src.models.publication import PROVENANCE_OPENALEX, Publication  # noqa: E402
from src.services import corpus, openalex_budget  # noqa: E402
from src.services.corpus import (  # noqa: E402
    CorpusStageError,
    _dedupe_and_rank,
    _gate_records,
    _match_pi_author_detail,
    resolve_corpus,
)
from src.services.jhu_rules import derive_start_from_papers  # noqa: E402
from src.services.job_queue import JobDeferred  # noqa: E402
from src.services.non_pubmed_works import _attributed, _authorships  # noqa: E402
from src.services.orcid import fetch_orcid_profile  # noqa: E402
from src.services.pubmed import (  # noqa: E402
    PubMedParseError,
    _doi_fold,
    fetch_pubmed_records,
    normalize_doi,
)

BASELINE_FORMAT = "audit_pi_corpus.baseline/1"
COMPARE_FORMAT = "audit_pi_corpus.compare/1"
#: The stage-list arm of PROVENANCE_CHECK_SQL (src/models/publication.py): an anchored row.
STAGE_LIST = re.compile(r"^(s[1-4],)*s[13](,s[1-4])*$")
#: F6 (1): stop when more than this share of the evaluated rows newly fails the gate.
DROP_LIMIT = 0.01
#: F6 (2): stop for a PI whose in-use count would rise by more than this share AND RISE_MIN.
RISE_FRACTION = 0.25
RISE_MIN = 10
_RECORD_KEYS = ("pmid", "title", "year", "pub_types", "author_list_complete",
                "update_of", "updated_in")
_AUTHOR_KEYS = ("last", "fore", "initials", "collective", "affiliations", "orcid", "valid")
_RESOLVE_ERRORS = (CorpusStageError, JobDeferred, httpx.HTTPError, PubMedParseError)


async def _classify_pi(db, user: User, *, skip_resolve: bool = False) -> dict:
    stored = await load_stored_publications(db, user.id)
    pmids = sorted({p.pmid for p in stored if p.pmid})
    refetched = await refetch_pmids(pmids)

    identity_counts: Counter[str] = Counter()
    excluded_type_counts: Counter[str] = Counter()
    title_groups: dict[str, int] = {}
    no_pmid = unrefetchable = 0

    for pub in stored:
        if pub.pmid is None:
            no_pmid += 1
            continue
        record = refetched.get(pub.pmid)
        if record is None:
            unrefetchable += 1
            continue

        kind, _, bare_initial = _match_pi_author_detail(record, user.name, user.orcid)
        if kind in ("no_match", "consortium"):
            identity_counts["no_surname_author"] += 1
        elif kind in ("ambiguous", "incomplete_list"):
            identity_counts[kind] += 1
        elif bare_initial:
            identity_counts["bare_initial_only"] += 1
        else:
            identity_counts["strong"] += 1

        type_hit = classify_excluded_type(record.get("pub_types") or [])
        if type_hit:
            excluded_type_counts[type_hit] += 1

        title = record.get("title") or pub.title
        key = normalize_title(title) or f"pmid:{pub.pmid}"
        title_groups[key] = title_groups.get(key, 0) + 1

    duplicate_title_groups = sum(1 for n in title_groups.values() if n >= 2)

    resolve_delta: dict | None = None
    resolve_error: str | None = None
    if skip_resolve:
        resolve_error = "skipped (--skip-resolve)"
    else:
        try:
            with openalex_budget.bulk_requests():
                await openalex_budget.check_inline_corpus_budget()
                resolved = await resolve_corpus(user.orcid, user.name, user.institution)
        except (CorpusStageError, JobDeferred) as exc:
            resolve_error = str(exc)
        else:
            stored_pmids = {p.pmid for p in stored if p.pmid}
            resolved_pmids = {str(r.get("pmid")) for r in resolved.kept if r.get("pmid")}
            resolve_delta = {
                "stored": len(stored_pmids),
                "resolved": len(resolved_pmids),
                "overlap": len(stored_pmids & resolved_pmids),
                "resolved_not_stored": len(resolved_pmids - stored_pmids),
                "stored_not_resolved": len(stored_pmids - resolved_pmids),
                "dropped_identity": resolved.dropped.get("identity", 0),
            }

    return {
        "orcid": user.orcid,
        "name": user.name,
        "stored_count": len(stored),
        "classification": {
            "strong": identity_counts["strong"],
            "bare_initial_only": identity_counts["bare_initial_only"],
            "no_surname_author": identity_counts["no_surname_author"],
            "ambiguous": identity_counts["ambiguous"],
            "incomplete_list": identity_counts["incomplete_list"],
            "no_pmid": no_pmid,
            "unrefetchable": unrefetchable,
        },
        "excluded_type_hits": {
            "excluded_type": excluded_type_counts["excluded_type"],
            "secondary_excluded_type": excluded_type_counts["secondary_excluded_type"],
        },
        "duplicate_title_groups": duplicate_title_groups,
        "resolve_delta": resolve_delta,
        "resolve_error": resolve_error,
    }


async def _run(orcids: list[str], skip_resolve: bool) -> int:
    async with get_session_factory()() as db:
        q = select(User).where(User.user_role == "pi").order_by(User.orcid)
        if orcids:
            q = q.where(User.orcid.in_(orcids))
        users = (await db.execute(q)).scalars().all()

        totals: Counter[str] = Counter()
        incomplete = False
        for user in users:
            report = await _classify_pi(db, user, skip_resolve=skip_resolve)
            print(json.dumps(report, sort_keys=True))
            incomplete = incomplete or bool(report["resolve_error"] and not skip_resolve)

            totals["pis"] += 1
            totals["stored"] += report["stored_count"]
            totals["strong"] += report["classification"]["strong"]
            totals["bare_initial_only"] += report["classification"]["bare_initial_only"]
            totals["no_surname_author"] += report["classification"]["no_surname_author"]
            totals["ambiguous"] += report["classification"]["ambiguous"]
            totals["incomplete_list"] += report["classification"]["incomplete_list"]
            totals["excluded_type"] += report["excluded_type_hits"]["excluded_type"]
            totals["secondary_excluded_type"] += report["excluded_type_hits"][
                "secondary_excluded_type"
            ]
            totals["duplicate_title_groups"] += report["duplicate_title_groups"]

        print(f"\n# {totals['pis']} PI(s), {totals['stored']} stored rows", file=sys.stderr)
        print(
            f"# classification: strong={totals['strong']} "
            f"bare_initial_only={totals['bare_initial_only']} "
            f"no_surname_author={totals['no_surname_author']} "
            f"ambiguous={totals['ambiguous']} "
            f"incomplete_list={totals['incomplete_list']}",
            file=sys.stderr,
        )
        print(
            f"# excluded types: pure={totals['excluded_type']} "
            f"secondary={totals['secondary_excluded_type']}",
            file=sys.stderr,
        )
        print(
            f"# duplicate-title groups: {totals['duplicate_title_groups']}",
            file=sys.stderr,
        )
        return int(incomplete)


# ---------------------------------------------------------------------------
# F6 before/after gate (spec 2026-10-15 R3.10): --baseline on the image being replaced,
# --compare on the new one.
# ---------------------------------------------------------------------------


def _trim(record: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``record`` holding only what the identity gate, the dedupe and tenure dating
    read; ``author_list_complete`` and an author's ``valid`` only when present."""
    out = {k: copy.deepcopy(record[k]) for k in _RECORD_KEYS if k in record}
    if "authors" in record:
        out["authors"] = [
            {k: copy.deepcopy(author[k]) for k in _AUTHOR_KEYS if k in author}
            for author in record.get("authors") or []
        ]
    return out


def gate_verdict(
    record: dict[str, Any], stages: Iterable[str], name: str, institution: str | None,
    orcid: str | None,
) -> str:
    """The identity and type gate's verdict on one record with these stages: ``"kept"``,
    ``"flagged:<reason>"``, ``"dropped:excluded_type"`` or ``"dropped:consortium"``."""
    kept, flagged, dropped = _gate_records(
        [copy.deepcopy(record)], {str(record["pmid"]): set(stages)}, name, institution, orcid
    )
    if kept:
        return "kept"
    if flagged:
        return f"flagged:{flagged[0]['reason']}"
    if dropped["excluded_type"]:
        return "dropped:excluded_type"
    if dropped["consortium"]:
        return "dropped:consortium"
    raise RuntimeError(f"_gate_records gave no verdict for PMID {record.get('pmid')}")


def admitted_and_tenure(
    gate_inputs: dict[str, Any], name: str, institution: str | None, orcid: str | None,
) -> tuple[list[str], int | None]:
    """The anchored PMIDs one resolve's gate inputs store (s1 or s3 among a ranked record's
    stages), and the paper-derived tenure start they date (ranked plus superseded records)."""
    records = copy.deepcopy(gate_inputs["records"])
    stages = {str(pmid): set(found) for pmid, found in gate_inputs["stages"].items()}
    kept, _flagged, dropped = _gate_records(records, stages, name, institution, orcid)
    pairs: list[tuple[dict[str, Any], str]] = []
    ranked = _dedupe_and_rank(kept, dropped, pairs)
    admitted = sorted(
        str(r["pmid"]) for r in ranked if set(r.get("stages") or []) & {"s1", "s3"}
    )
    return admitted, derive_start_from_papers([*ranked, *[r for r, _succ in pairs]])


@contextlib.contextmanager
def capture_gate_inputs() -> Iterator[dict[str, Any]]:
    """Record what ``resolve_corpus`` hands its identity gate: ``corpus._gate_records`` is
    replaced, for the block, by a wrapper that stores ``_trim``med copies of the records and
    each PMID's sorted stages, then delegates. ``resolve_corpus`` looks the name up at call
    time, so this works on old and new code alike. The original is restored in every case."""
    captured: dict[str, Any] = {}
    original = corpus._gate_records

    def recording(records, stages, *args, **kwargs):
        captured["records"] = [_trim(r) for r in records]
        captured["stages"] = {str(pmid): sorted(found) for pmid, found in stages.items()}
        return original(records, stages, *args, **kwargs)

    corpus._gate_records = recording
    try:
        yield captured
    finally:
        corpus._gate_records = original


@dataclass(frozen=True)
class BaselinePi:
    """One PI's database facts for the baseline, read before any network call."""

    user_id: str
    orcid: str
    name: str
    institution: str | None
    in_use: int
    stored_pmids: list[str]
    anchored_rows: list[tuple[str, str]]  # (pmid, provenance) of in-use stage-list rows
    openalex_dois: list[str]  # DOIs of in-use provenance = 'openalex' rows


async def read_baseline_rows(db, orcids: Sequence[str]) -> list[BaselinePi]:
    """Every database read the baseline needs, in one read-only transaction that is rolled
    back before returning. The population is ``scripts._bulk_enqueue.pi_population`` (user_role
    'pi' plus owners of a pi_lab agent), scoped to ``orcids`` when given."""
    await db.execute(text("SET TRANSACTION READ ONLY"))
    wanted = set(orcids)
    population = [p for p in await pi_population(db) if not wanted or p[1] in wanted]
    ids = [user_id for user_id, _orcid, _name in population]
    institutions: dict[Any, str | None] = {}
    rows: list[Any] = []
    if ids:
        institutions = dict(
            (await db.execute(select(User.id, User.institution).where(User.id.in_(ids)))).all()
        )
        rows = list((await db.execute(
            select(Publication.user_id, Publication.pmid, Publication.doi,
                   Publication.provenance, Publication.excluded_at)
            .where(Publication.user_id.in_(ids))
        )).all())
    await db.rollback()
    by_user: dict[Any, list[Any]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)
    return [
        _baseline_pi(user_id, orcid, name, institutions.get(user_id), by_user.get(user_id, []))
        for user_id, orcid, name in population
    ]


def _baseline_pi(user_id, orcid, name, institution, rows) -> BaselinePi:
    in_use = [r for r in rows if r.excluded_at is None]
    return BaselinePi(
        user_id=str(user_id),
        orcid=orcid,
        name=name,
        institution=institution,
        in_use=len(in_use),
        stored_pmids=sorted({r.pmid for r in rows if r.pmid}),
        anchored_rows=sorted(
            (r.pmid, r.provenance) for r in in_use
            if r.pmid and r.provenance and STAGE_LIST.match(r.provenance)
        ),
        openalex_dois=sorted(
            r.doi for r in in_use if r.provenance == PROVENANCE_OPENALEX and r.doi
        ),
    )


def _authorship_for(authorships: dict[str, Any], doi: str) -> dict[str, Any] | None:
    """A stored DOI's authorship, keyed as ``non_pubmed_works._authorships`` keys them; None
    for a DOI ``normalize_doi`` rejects (``_doi_fold`` would fail on None)."""
    key = normalize_doi(doi)
    return authorships.get(_doi_fold(key)) if key else None


async def _openalex_inputs(pi: BaselinePi) -> dict[str, Any]:
    organizations = list((await fetch_orcid_profile(pi.orcid)).get("organizations") or [])
    authorships = await _authorships(pi.orcid, None)
    return {
        "organizations": organizations,
        "authorships": authorships,
        "rows": [
            {"doi": doi, "attributed": _attributed(_authorship_for(authorships, doi), organizations)}
            for doi in pi.openalex_dois
        ],
    }


async def _resolve_into(line: dict[str, Any], pi: BaselinePi) -> None:
    """One live resolve with its gate inputs captured, and, for a PI with in-use 'openalex'
    rows, today's attribution of each. Charged OpenAlex calls run under the inline budget
    guards, as the legacy mode's do. ``gate_inputs`` is set only when everything succeeded."""
    with openalex_budget.bulk_requests():
        await openalex_budget.check_inline_corpus_budget()
        with capture_gate_inputs() as captured:
            await resolve_corpus(pi.orcid, pi.name, pi.institution)
        if pi.openalex_dois:
            line["openalex"] = await _openalex_inputs(pi)
    line["gate_inputs"] = captured
    line["admitted"], line["paper_tenure_start"] = admitted_and_tenure(
        captured, pi.name, pi.institution, pi.orcid
    )


SKIPPED_RESOLVE = "skipped (--skip-resolve)"


def _empty_line(pi: BaselinePi, error: str | None) -> dict[str, Any]:
    """A baseline line with no rows and no resolve results: the start of every line, and
    the whole line for a PI whose anchored rows could not be refetched (``error``)."""
    return {
        "format": BASELINE_FORMAT,
        "captured_at": datetime.now(UTC).isoformat(),
        "user_id": pi.user_id,
        "orcid": pi.orcid,
        "name": pi.name,
        "institution": pi.institution,
        "in_use": pi.in_use,
        "stored_pmids": pi.stored_pmids,
        "rows": [],
        "row_records": [],
        "unrefetchable": [],
        "gate_inputs": None,
        "admitted": [],
        "paper_tenure_start": None,
        "openalex": None,
        "resolve_error": error,
    }


async def baseline_line(pi: BaselinePi, *, skip_resolve: bool) -> dict[str, Any]:
    """One ``audit_pi_corpus.baseline/1`` line. Network only: refetch, gate, resolve.

    The refetch is strict: a PMID PubMed has no record for goes to ``unrefetchable``, but a
    transient failure leaves the PI with no rows and a ``resolve_error``, so the compare
    reports the PI incomplete instead of judging F6 (1) on the rows that happened to load."""
    pmids = [pmid for pmid, _provenance in pi.anchored_rows]
    try:
        fetched = await fetch_pubmed_records(pmids, strict=True) if pmids else []
    except _RESOLVE_ERRORS as exc:
        return _empty_line(pi, f"PubMed refetch failed: {str(exc) or type(exc).__name__}")
    by_pmid = {str(r.get("pmid")): r for r in fetched}
    rows: list[dict[str, Any]] = []
    row_records: list[dict[str, Any]] = []
    unrefetchable: list[str] = []
    for pmid, provenance in pi.anchored_rows:
        record = by_pmid.get(pmid)
        if record is None:
            unrefetchable.append(pmid)
            continue
        verdict = gate_verdict(record, provenance.split(","), pi.name, pi.institution, pi.orcid)
        rows.append({"pmid": pmid, "provenance": provenance, "verdict": verdict})
        row_records.append(_trim(record))
    line = {
        **_empty_line(pi, None),
        "rows": rows,
        "row_records": row_records,
        "unrefetchable": unrefetchable,
    }
    if skip_resolve:
        line["resolve_error"] = SKIPPED_RESOLVE
        return line
    try:
        await _resolve_into(line, pi)
    except _RESOLVE_ERRORS as exc:
        line["gate_inputs"], line["openalex"] = None, None
        line["resolve_error"] = str(exc) or type(exc).__name__
    return line


def _listed(items: Iterable[str], sep: str = ", ") -> str:
    joined = sep.join(items)
    return f" {joined}" if joined else ""


async def _baseline(orcids: Sequence[str], skip_resolve: bool) -> int:
    async with get_session_factory()() as db:
        pis = await read_baseline_rows(db, orcids)
    failed: list[str] = []
    evaluated = unrefetchable = 0
    for pi in pis:
        line = await baseline_line(pi, skip_resolve=skip_resolve)
        print(json.dumps(line, sort_keys=True), flush=True)
        evaluated += len(line["rows"])
        unrefetchable += len(line["unrefetchable"])
        if line["resolve_error"] and line["resolve_error"] != SKIPPED_RESOLVE:
            failed.append(pi.orcid)
    print(
        f"# audit_pi_corpus baseline: {len(pis)} PI(s), {evaluated} anchored in-use rows "
        f"evaluated, {unrefetchable} unrefetchable",
        file=sys.stderr,
    )
    print(f"# resolve_error: {len(failed)} PI(s):" + _listed(failed, sep=" "), file=sys.stderr)
    return int(bool(failed))


def read_baseline(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Baseline lines, the last one per ``user_id`` kept; ValueError on any other format."""
    latest: dict[str, dict[str, Any]] = {}
    for raw in lines:
        if not raw.strip():
            continue
        line = json.loads(raw)
        if line.get("format") != BASELINE_FORMAT:
            raise ValueError(f"unknown baseline format: {line.get('format')}")
        latest[line["user_id"]] = line
    return list(latest.values())


def rise_trips(in_use: int, rise: int) -> bool:
    """F6 (2): more than RISE_FRACTION of the in-use count AND more than RISE_MIN papers."""
    return rise > RISE_FRACTION * in_use and rise > RISE_MIN


def _row_changes(base: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    records = {str(r["pmid"]): r for r in base["row_records"]}
    out: dict[str, list[dict[str, str]]] = {"dropped": [], "still_failing": [], "recovered": []}
    for row in base["rows"]:
        after = gate_verdict(records[row["pmid"]], row["provenance"].split(","),
                             base["name"], base["institution"], base["orcid"])
        entry = {"pmid": row["pmid"], "before": row["verdict"], "after": after}
        if row["verdict"] == "kept" and after != "kept":
            out["dropped"].append(entry)
        elif row["verdict"] != "kept":
            out["still_failing" if after != "kept" else "recovered"].append(entry)
    return out


def _openalex_changes(oa: dict[str, Any] | None) -> dict[str, Any] | None:
    if oa is None:
        return None
    fail_after: list[str] = []
    pass_after_only: list[str] = []
    for row in oa["rows"]:
        after = _attributed(_authorship_for(oa["authorships"], row["doi"]), oa["organizations"])
        if row["attributed"] and not after:
            fail_after.append(row["doi"])
        elif after and not row["attributed"]:
            pass_after_only.append(row["doi"])
    return {"rows": len(oa["rows"]), "fail_after": fail_after, "pass_after_only": pass_after_only}


def _author_name(record: dict[str, Any], index: int) -> str:
    author = (record.get("authors") or [])[index]
    return f"{author.get('last') or ''}, {author.get('fore') or ''}"


def _author_label(match: Any, record: dict[str, Any]) -> str:
    """Who the new matcher took for the PI on a newly admitted paper, for the owner's review."""
    if match.kind == "individual" and match.index is not None:
        return _author_name(record, match.index)
    if match.kind == "ambiguous":
        return "ambiguous: " + " | ".join(_author_name(record, c.index) for c in match.candidates)
    if match.kind == "incomplete_list":
        return "incomplete author list"
    return match.kind


def compare_pi(base: dict[str, Any]) -> dict[str, Any]:
    """One ``audit_pi_corpus.compare/1`` line: the baseline's inputs re-judged by this code."""
    # Compare-only: absent from the pre-Phase-3 image, where --baseline must still load.
    from src.services.corpus import _match_pi_author_full

    line: dict[str, Any] = {
        "format": COMPARE_FORMAT,
        "user_id": base["user_id"],
        "orcid": base["orcid"],
        "name": base["name"],
        "in_use": base["in_use"],
        "evaluated": len(base["rows"]),
        "unrefetchable": list(base["unrefetchable"]),
        **_row_changes(base),
        "newly_admitted": [],
        "newly_admitted_authors": {},
        "no_longer_admitted": [],
        "paper_tenure_start": None,
        "openalex": _openalex_changes(base["openalex"]),
        "incomplete": None,
    }
    gate = base["gate_inputs"]
    if gate is None:
        line["incomplete"] = f"no gate inputs: {base['resolve_error']}"
    else:
        admitted, start = admitted_and_tenure(gate, base["name"], base["institution"], base["orcid"])
        stored = set(base["stored_pmids"])
        records = {str(r["pmid"]): r for r in gate["records"]}
        newly = sorted(set(admitted) - set(base["admitted"]) - stored)
        line["newly_admitted"] = newly
        line["newly_admitted_authors"] = {
            pmid: _author_label(
                _match_pi_author_full(records[pmid], base["name"], base["orcid"], anchored=True),
                records[pmid],
            )
            for pmid in newly
        }
        line["no_longer_admitted"] = sorted(set(base["admitted"]) - set(admitted) - stored)
        line["paper_tenure_start"] = {"before": base["paper_tenure_start"], "after": start}
    line["rise"] = len(line["newly_admitted"])
    line["rise_trips"] = rise_trips(line["in_use"], line["rise"])
    return line


def summarize(
    lines: list[dict[str, Any]], captured: str, missing: Sequence[str] = ()
) -> tuple[list[str], int]:
    """The stderr summary and the exit code: 2 when F6 (1) or (2) trips (STOP wins), else 1
    when any PI is incomplete, the baseline judged no PI, or an ``--orcid`` asked for
    (``missing``) is not in it, else 0."""
    evaluated = sum(x["evaluated"] for x in lines)
    dropped = sum(len(x["dropped"]) for x in lines)
    stop_drop = evaluated > 0 and dropped > DROP_LIMIT * evaluated
    pct = 100 * dropped / evaluated if evaluated else 0.0
    risers = [x for x in lines if x["rise_trips"]]
    incomplete = [x for x in lines if x["incomplete"]]
    moved = [
        x for x in lines
        if x["paper_tenure_start"]
        and x["paper_tenure_start"]["before"] != x["paper_tenure_start"]["after"]
    ]
    openalex = [x["openalex"] for x in lines if x["openalex"]]
    out = [
        f"# audit_pi_corpus compare: {len(lines)} PI(s), baseline captured {captured}",
        f"# F6(1) anchored in-use rows newly failing the gate: {dropped} of {evaluated} "
        f"({pct:.2f}%), stop above 1%: {'STOP' if stop_drop else 'PASS'}",
        f"# F6(2) PIs whose in-use count would rise by more than 25% and more than 10 papers: "
        f"{len(risers)}: {'STOP' if risers else 'PASS'}",
        *(f"#   {x['name']} ({x['orcid']}): +{x['rise']} on {x['in_use']} in use" for x in risers),
        f"# recovered: {sum(len(x['recovered']) for x in lines)}; "
        f"still failing (Phase 4 re-check): {sum(len(x['still_failing']) for x in lines)}; "
        f"unrefetchable: {sum(len(x['unrefetchable']) for x in lines)}",
        f"# newly admitted anchored papers: {sum(len(x['newly_admitted']) for x in lines)} "
        f"across {sum(1 for x in lines if x['newly_admitted'])} PI(s); "
        f"no longer admitted: {sum(len(x['no_longer_admitted']) for x in lines)}",
        f"# paper-derived tenure start changed for {len(moved)} PI(s):" + _listed(
            f"{x['name']} {x['paper_tenure_start']['before']} -> {x['paper_tenure_start']['after']}"
            for x in moved
        ),
        f"# OpenAlex rows attributed today and not under Q11: "
        f"{sum(len(o['fail_after']) for o in openalex)} of {sum(o['rows'] for o in openalex)}",
        f"# incomplete PI(s): {len(incomplete)}:"
        + _listed(f"{x['name']} ({x['incomplete']})" for x in incomplete),
    ]
    if not lines:
        out.append("# no PI judged: the baseline is empty or no --orcid matched it")
    if missing:
        out.append("# --orcid not in the baseline:" + _listed(missing, sep=" "))
    if stop_drop or risers:
        verdict, code = "STOP FOR OWNER REVIEW", 2
    elif incomplete or not lines or missing:
        verdict, code = "INCOMPLETE", 1
    else:
        verdict, code = "PASS", 0
    out.append(f"# VERDICT: {verdict}")
    return out, code


def _compare(source: str, orcids: Sequence[str]) -> int:
    opened = contextlib.nullcontext(sys.stdin) if source == "-" else open(source, encoding="utf-8")
    with opened as stream:
        try:
            baselines = read_baseline(stream)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    if orcids:
        baselines = [b for b in baselines if b["orcid"] in set(orcids)]
    missing = sorted(set(orcids) - {b["orcid"] for b in baselines})
    lines = [compare_pi(b) for b in baselines]
    for line in lines:
        print(json.dumps(line, sort_keys=True))
    summary, code = summarize(
        lines, min((b["captured_at"] for b in baselines), default="none"), missing
    )
    for summary_line in summary:
        print(summary_line, file=sys.stderr)
    return code


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--orcid", action="append", default=[], help="Scope to this ORCID (repeatable)")
    parser.add_argument(
        "--skip-resolve",
        action="store_true",
        help="Classification only; skip the resolve_corpus delta (no ORCID/OpenAlex/S4 calls)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--baseline", action="store_true",
        help="F6 baseline from the image being replaced (JSONL on stdout; read-only)",
    )
    modes.add_argument(
        "--compare", metavar="FILE",
        help="F6 compare of a baseline file ('-' for stdin) on the new image; exit 0/1/2",
    )
    args = parser.parse_args()
    if args.compare is not None:
        sys.exit(_compare(args.compare, args.orcid))
    if args.baseline:
        sys.exit(asyncio.run(_baseline(args.orcid, args.skip_resolve)))
    sys.exit(asyncio.run(_run(args.orcid, args.skip_resolve)))


if __name__ == "__main__":
    main()
