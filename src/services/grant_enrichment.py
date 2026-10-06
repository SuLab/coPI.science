"""Worker handler for the ``enrich_grants`` job: the PI's NIH RePORTER identity and awards
(pi_grant_identity, pi_grants) and ORCID fundings (pi_orcid_fundings), then a persona
re-export after commit when the rendered persona changed. The identity rule is
grant_resolution's docstring (spec 2026-10-05 §6.1). Every network call happens before the
first write, so the transaction's row locks last milliseconds (the ORCID veto waits at most
5 s for them)."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, PiGrant, PiGrantIdentity, Publication, User
from src.models.enrichment import GRANT_RENDERING_STATUSES
from src.services import job_progress
from src.services.grant_resolution import (
    Candidate,
    GrantRecord,
    filter_and_collapse,
    find_candidates,
    grant_evidence,
    resolve_identity,
)
from src.services.jhu_rules import export_tenure_start
from src.services.job_queue import insert_job_if_absent
from src.services.nih_reporter import (
    JHU_ORG_EXACT,
    PROJECT_FIELDS,
    ReporterFirehoseError,
    publications_for_cores,
    search_projects,
)
from src.services.orcid_fundings import fetch_orcid_fundings, store_orcid_fundings
from src.services.person_names import parse_person_name, surname_candidates
from src.services.profile_publish import (
    lock_persona_writer,
    reexport_persona,
    schedule_persona_write,
)
from src.services.tenure_scope import publication_in_use

if TYPE_CHECKING:
    from src.worker.main import JobContext

logger = logging.getLogger(__name__)

#: meta.total caps (spec 2026-10-05 §6.1): a total over either is `firehose`.
STAGE1_MAX_TOTAL = 2000
STAGE2_MAX_TOTAL = 1500


@dataclass(frozen=True)
class GrantOutcome:
    status: str                              # one of GRANT_IDENTITY_STATUSES
    accepted_profile_ids: tuple[int, ...]
    candidates: tuple[Candidate, ...]        # grant_resolution.Candidate
    profile_ids: tuple[int, ...]             # the ids stage 2 searched; () when it did not run
    records: tuple[GrantRecord, ...]         # stage-2 records; () unless resolved/pinned
    tenure_mode: str                         # GRANT_TENURE_MODES
    evidence_by_core: dict[str, dict]        # core_project_num -> identity_evidence
    evaluated: bool                          # False when the staff state skipped steps 1-4
    note: str                                # the progress detail
    #: True when a pinned run's stage 2 hit the firehose cap: the stored awards are kept.
    stage2_firehose: bool = False


async def enqueue_enrichment_jobs(
    db: AsyncSession,
    user_id: uuid.UUID,
    orcid: str,
    *,
    types: tuple[str, ...] = ("enrich_grants", "industry_evidence"),
    priority: int | None = None,
    not_before: datetime | None = None,
) -> None:
    """Enqueue each of `types` for the user, eligible from `not_before`, unless one is
    already pending or processing (the insert is idempotent under concurrency:
    job_queue)."""
    for jtype in types:
        await insert_job_if_absent(
            db, type=jtype, user_id=user_id,
            payload={"user_id": str(user_id), "orcid": orcid}, priority=priority,
            not_before=not_before,
        )


def _mode(tenure_start: int | None) -> str:
    return "org_and_year" if tenure_start is not None else "org_only"


def _stored_candidates(identity: PiGrantIdentity | None) -> tuple[Candidate, ...]:
    """The candidates the last evaluating run stored, for a run that skips steps 1-4."""
    out = []
    for c in (identity.candidates if identity else None) or []:
        try:
            out.append(Candidate(int(c["id"]), str(c.get("name_on_award") or ""), frozenset(),
                                 frozenset(str(p) for p in c.get("linking_pmids") or [])))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return tuple(out)


async def _stage1(user: User) -> list[dict]:
    """One projects/search per surname candidate, merged by project_num (A5)."""
    rows: dict[str, dict] = {}
    for surname in surname_candidates(user.name):
        criteria = {"pi_names": [{"last_name": surname}], "org_names_exact_match": [JHU_ORG_EXACT]}
        for row in await search_projects(criteria, PROJECT_FIELDS, max_total=STAGE1_MAX_TOTAL):
            # A row without a project_num only needs a key no other row has.
            rows.setdefault(row.get("project_num") or f"{row['core_project_num']}:{len(rows)}", row)
    return list(rows.values())


async def _stage2(*, status: str, ids: tuple[int, ...], accepted: tuple[int, ...],
                  candidates: tuple[Candidate, ...], tenure_start: int | None,
                  links: dict[str, set[str]], corpus: set[str], rule: str,
                  evaluated: bool) -> GrantOutcome:
    """The accepted or pinned profile ids' JHU awards, in tenure when a year is known."""
    criteria: dict = {"pi_profile_ids": list(ids), "org_names_exact_match": [JHU_ORG_EXACT]}
    if tenure_start is not None:
        criteria["fiscal_years"] = list(range(tenure_start, datetime.now(UTC).year + 2))
    try:
        rows = await search_projects(criteria, PROJECT_FIELDS, max_total=STAGE2_MAX_TOTAL)
    except ReporterFirehoseError:
        note = f"firehose: stage-2 total over {STAGE2_MAX_TOTAL} for profile ids {list(ids)}"
        if status == "pinned":
            # A staff pin stands; an over-cap fetch must not wipe the awards it rendered.
            return GrantOutcome("pinned", accepted, candidates, ids, (), _mode(tenure_start), {},
                                evaluated, f"pinned, {note}; stored awards kept",
                                stage2_firehose=True)
        return GrantOutcome("firehose", accepted, candidates, (), (), _mode(tenure_start), {},
                            evaluated, note)
    records, mode = filter_and_collapse(rows, set(ids), tenure_start)
    evidence = {r.core_project_num: grant_evidence(r, links, corpus, rule) for r in records}
    return GrantOutcome(status, accepted, candidates, ids, tuple(records), mode, evidence,
                        evaluated,
                        f"{status}: profile ids {list(ids)}, {len(records)} awards, mode={mode}")


async def resolve_grants(db: AsyncSession, user: User,
                         identity: PiGrantIdentity | None) -> GrantOutcome:
    """Run the identity rule and stage 2 for `user`. Network and reads only, no writes.

    A staff pin skips steps 1-4 and fetches the pinned ids (`pinned`); "none confirmed"
    skips RePORTER entirely; a name that is an ORCID iD, or has no surname, is `no_match`
    with no RePORTER call (A12)."""
    agent_id = await db.scalar(
        select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user.id)
    )
    tenure_start = await export_tenure_start(db, user.id, agent_id)
    mode = _mode(tenure_start)
    stored = _stored_candidates(identity)
    previous_accepted = tuple(identity.accepted_profile_ids or ()) if identity else ()
    if identity is not None and identity.pinned_profile_ids:
        return await _stage2(status="pinned", ids=tuple(sorted(identity.pinned_profile_ids)),
                             accepted=previous_accepted, candidates=stored,
                             tenure_start=tenure_start, links={}, corpus=set(), rule="pinned",
                             evaluated=False)
    if identity is not None and identity.none_confirmed:
        return GrantOutcome("none_confirmed", previous_accepted, stored, (), (), mode, {}, False,
                            "none_confirmed: staff marked the PI as having no RePORTER profile")
    person = parse_person_name(user.name)
    if person.is_orcid_id or not person.surname_keys:
        return GrantOutcome("no_match", (), (), (), (), mode, {}, True,
                            "no_match: the name carries no usable surname")
    try:
        rows = await _stage1(user)
    except ReporterFirehoseError:
        return GrantOutcome(
            "firehose", (), (), (), (), mode, {}, True,
            f"firehose: a surname search returned more than {STAGE1_MAX_TOTAL}; pin the profile",
        )
    found = find_candidates(rows, person)
    cores = sorted({core for c in found.values() for core in c.cores})
    links = await publications_for_cores(cores)
    corpus = {p for (p,) in (await db.execute(select(Publication.pmid).where(
        Publication.user_id == user.id, Publication.pmid.isnot(None),
        publication_in_use()))).all()}
    result = resolve_identity(found, links, corpus)
    if result.status != "resolved":
        return GrantOutcome(result.status, result.accepted_profile_ids, result.candidates,
                            (), (), mode, {}, True,
                            f"{result.status}: {len(result.candidates)} candidate(s), "
                            f"accepted={list(result.accepted_profile_ids)}")
    return await _stage2(status="resolved", ids=result.accepted_profile_ids,
                         accepted=result.accepted_profile_ids, candidates=result.candidates,
                         tenure_start=tenure_start, links=links, corpus=corpus,
                         rule="pmid_link", evaluated=True)


def _effective(pins: list[int] | None, none_confirmed: bool,
               outcome: GrantOutcome) -> tuple[bool, str | None, str]:
    """(write_status, status, rows) for `outcome` against the staff columns as they stand
    at write time; `rows` is "keep" (touch no pi_grants row), "clear" (delete the
    non-vetoed rows) or "replace" (clear, then insert the stage-2 records)."""
    if pins:
        if outcome.status == "pinned" and sorted(outcome.profile_ids) == sorted(pins):
            return True, "pinned", "keep" if outcome.stage2_firehose else "replace"
        # Pinned (or re-pinned) while this run fetched: the pin route set the status and
        # requested a rerun, which fetches the pinned ids.
        return False, None, "keep"
    if none_confirmed:
        return True, "none_confirmed", "clear"
    if outcome.status in ("pinned", "none_confirmed"):
        # Unpinned while this run fetched: unevaluated until the requested rerun.
        return True, None, "clear"
    rows = "replace" if outcome.status in GRANT_RENDERING_STATUSES else "clear"
    return True, outcome.status, rows


async def _replace_grants(db: AsyncSession, user_id: uuid.UUID, outcome: GrantOutcome,
                          records: tuple[GrantRecord, ...]) -> None:
    """Delete the PI's non-vetoed pi_grants rows, then insert `records` except vetoed cores."""
    await db.execute(delete(PiGrant).where(PiGrant.user_id == user_id, PiGrant.vetoed_at.is_(None)))
    # Read AFTER the DELETE: every row left is vetoed, so no insert below can collide with
    # one on (user_id, core_project_num).
    vetoed = {c for (c,) in (await db.execute(select(PiGrant.core_project_num).where(
        PiGrant.user_id == user_id))).all()}
    for r in records:
        if r.core_project_num in vetoed:
            continue
        db.add(PiGrant(
            user_id=user_id, source="nih_reporter", core_project_num=r.core_project_num,
            reporter_profile_id=r.reporter_profile_id, title=r.title, phr_text=r.phr_text,
            terms=r.terms, activity_code=r.activity_code, agency_ic=r.agency_ic,
            funding_mechanism=r.funding_mechanism, org_name=r.org_name, first_fy=r.first_fy,
            last_fy=r.last_fy, project_start=r.project_start, project_end=r.project_end,
            total_award_in_tenure=r.total_award_in_tenure, is_contact_pi=r.is_contact_pi,
            is_subproject=r.is_subproject, tenure_filter_mode=outcome.tenure_mode,
            identity_evidence=outcome.evidence_by_core.get(r.core_project_num),
        ))


async def store_grant_outcome(db: AsyncSession, user_id: uuid.UUID, outcome: GrantOutcome, *,
                              now: datetime) -> None:
    """Write `outcome` under a FOR UPDATE lock on the PI's pi_grant_identity row (inserted,
    status NULL, when absent). The staff columns as they stand under that lock decide
    the effective status and the pi_grants rows (`_effective`), so a pin, unpin or "no
    RePORTER profile" committed while this run fetched wins over what the run computed:

    - pinned now: `pinned` with the run's records only when the run fetched those same
      ids (a pinned stage-2 firehose keeps the stored rows); otherwise nothing is
      written, and the rerun the pin route requested does the work;
    - "no profile" now: `none_confirmed`, non-vetoed rows deleted;
    - neither, but the run was pinned/none_confirmed: status NULL, non-vetoed rows deleted;
    - else the run's status; the non-vetoed rows are replaced by the stage-2 records
      when it renders (`resolved`), deleted otherwise.

    Vetoed rows are never touched. Never writes the staff columns (pinned_*,
    none_confirmed); `accepted_profile_ids` and `candidates` only when the run
    evaluated. Flushes, never commits."""
    await db.execute(pg_insert(PiGrantIdentity).values(user_id=user_id)
                     .on_conflict_do_nothing(index_elements=["user_id"]))
    pins, none_confirmed = (await db.execute(
        select(PiGrantIdentity.pinned_profile_ids, PiGrantIdentity.none_confirmed)
        .where(PiGrantIdentity.user_id == user_id).with_for_update()
    )).one()
    write_status, status, rows = _effective(pins, bool(none_confirmed), outcome)
    if rows != "keep":
        await _replace_grants(db, user_id, outcome, outcome.records if rows == "replace" else ())
    if write_status:
        values: dict = {"status": status}
        if status is not None:
            values["evaluated_at"] = now
        if outcome.evaluated:
            values["accepted_profile_ids"] = list(outcome.accepted_profile_ids)
            values["candidates"] = [c.as_json() for c in outcome.candidates]
        await db.execute(update(PiGrantIdentity).where(PiGrantIdentity.user_id == user_id)
                         .values(**values))
    await db.flush()


async def execute_enrich_grants(ctx: JobContext, db: AsyncSession) -> None:
    """Fetch (strict ORCID fundings, RePORTER identity and awards), then write, then
    record a `pipeline` revision and schedule the post-commit persona write only when
    the rendered persona differs from the file. A strict ORCID failure raises before
    any write, so the worker retries the job."""
    user_id = uuid.UUID(ctx.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    identity = (await db.execute(
        select(PiGrantIdentity).where(PiGrantIdentity.user_id == user_id)
    )).scalar_one_or_none()
    await job_progress.record(ctx.id, "grants1",
                              f"ORCID fundings and RePORTER identity for {user.name}")
    fundings = await fetch_orcid_fundings(user.orcid, strict=True) or []
    outcome = await resolve_grants(db, user, identity)
    # Network work is done; take the persona-writer lock before any child-row lock
    # (pi_orcid_fundings, pi_grant_identity, pi_grants), the order the manager routes
    # use, so a concurrent veto or pin cannot deadlock with this job (spec §4.3).
    await lock_persona_writer(db, user_id)
    await store_orcid_fundings(db, user_id, fundings)
    await store_grant_outcome(db, user_id, outcome, now=datetime.now(UTC))
    text = await reexport_persona(
        db, user_id, mechanism="pipeline",
        change_summary="Grant sections from NIH RePORTER and ORCID",
        skip_if_file_matches=True,
    )
    if text is not None:
        await schedule_persona_write(db, user_id, ctx.after_commit)
    await job_progress.record(ctx.id, "grants_done",
                              f"{outcome.note}; orcid_fundings={len(fundings)}")
