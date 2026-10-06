"""Phase 1 grants repair (spec 2026-10-05 §6.1 "Data repair", §9 Phase 1).

  $DC run --rm --no-deps blackbird-app python scripts/grants_remediation.py            # dry run
  $DC run --rm --no-deps blackbird-app python scripts/grants_remediation.py --apply
  $DC run --rm --no-deps blackbird-app python scripts/grants_remediation.py --verify --deploy-ts 2026-10-06T14:00:00Z

Dry run: per PI, old vs new identity and status and the Active/Past sections the new code
renders; then the held, unconfirmed, no-match and firehose lists (the implementer reviews
them, D64). Apply: ORCID fundings fetched inline (strict, paced), enrich_grants for every PI
through scripts/_bulk_enqueue.py (BULK; 0 OpenAlex credits, so unstaggered), dead jobs
re-enqueued once, every persona re-exported (mechanism `reexport`), and the app setting
`persona_sweep_enabled` set LAST, only when the completeness predicate holds. Verify: the
read-only checks of spec §9. Never starts a simulation run (D52).

`--orcid <iD>` scopes the dry run or apply to one PI (a canary); a canary apply never sets
the flag. Exit codes: apply 0 when the flag was set, 2 otherwise; verify 1 on any FAIL."""
import argparse
import asyncio
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import DateTime, bindparam, select, text  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from scripts import _bulk_enqueue as bulk  # noqa: E402
from src.database import get_session_factory  # noqa: E402
from src.models import (  # noqa: E402
    AgentRegistry,
    PiGrant,
    PiGrantIdentity,
    PiOrcidFunding,
    ResearcherProfile,
    User,
)
from src.services import grant_sections  # noqa: E402
from src.services.grant_enrichment import GrantOutcome, resolve_grants  # noqa: E402
from src.services.jhu_rules import export_tenure_start  # noqa: E402
from src.services.orcid_fundings import (  # noqa: E402
    OrcidFunding,
    fetch_orcid_fundings,
    store_orcid_fundings,
)
from src.services.person_names import parse_person_name, surname_keys  # noqa: E402
from src.services.persona_sweep import enable_persona_sweep, sweep_enabled  # noqa: E402
from src.services.profile_publish import (  # noqa: E402
    lock_persona_writer,
    persona_file_text,
    reexport_persona,
    render_persona_from_db,
    write_persona_files,
)

LINE_RE = re.compile(
    r"^- (?P<title>.+) \((?P<label>[^()]+?)(?:, (?P<start>\d{4})?–(?P<end>\d{4})?)?\)$"
)
SECTION_RE = re.compile(r"^## (Active Grants|Past Grants \(since \d{4}\))\s*$")
NAMED_PIS = ("Zavala", "Coppens", "Norris")
JOB_TYPE = "enrich_grants"
REVIEW_STATUSES = ("held", "unconfirmed", "no_match", "firehose")

COMPLETENESS_SQL = """\
WITH pop AS (
  SELECT id AS user_id FROM users WHERE user_role = 'pi'
  UNION
  SELECT user_id FROM agents WHERE role = 'pi_lab' AND user_id IS NOT NULL
)
SELECT u.name, u.orcid, gi.evaluated_at, gi.orcid_fetched_at
FROM pop JOIN users u ON u.id = pop.user_id
LEFT JOIN pi_grant_identity gi ON gi.user_id = pop.user_id
WHERE pop.user_id = ANY(:ids)
  AND (gi.user_id IS NULL OR gi.evaluated_at IS NULL OR gi.evaluated_at < :deploy_ts
       OR gi.orcid_fetched_at IS NULL OR gi.orcid_fetched_at < :deploy_ts)
ORDER BY u.name"""


@dataclass(frozen=True)
class ParsedLine:
    section: str      # "active" | "past"
    title: str
    label: str
    start: int | None
    end: int | None


def _collapse(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_persona_grants(text: str) -> list[ParsedLine]:
    """Lines under the persona's grant headings, in file order; other sections ignored. A
    bare `- Title` bullet (the pre-Phase-1 Active Grants format) parses with label ""."""
    out, section = [], None
    for raw in text.splitlines():
        heading = SECTION_RE.match(raw)
        if heading:
            section = "active" if heading.group(1) == "Active Grants" else "past"
            continue
        if raw.startswith("## "):
            section = None
            continue
        if section is None or not raw.startswith("- "):
            continue
        match = LINE_RE.match(raw)
        if match:
            out.append(ParsedLine(section, _collapse(match["title"]), match["label"],
                                  int(match["start"]) if match["start"] else None,
                                  int(match["end"]) if match["end"] else None))
        else:
            out.append(ParsedLine(section, _collapse(raw[2:]), "", None, None))
    return out


def _format_parsed(line: ParsedLine) -> str:
    if not line.label:
        return f"[{line.section}] {line.title} (no label: pre-Phase-1 bullet)"
    years = "" if line.start is None and line.end is None else \
        f", {line.start or ''}–{line.end or ''}"
    return f"[{line.section}] {line.title} ({line.label}{years})"


def transient_rows(user_id, outcome: GrantOutcome, fundings: Sequence[OrcidFunding]):
    """Unattached PiGrantIdentity / PiGrant / PiOrcidFunding objects for build_grant_sections
    (never added to a session: the dry run writes nothing)."""
    identity = PiGrantIdentity(
        user_id=user_id, status=outcome.status,
        accepted_profile_ids=list(outcome.accepted_profile_ids),
        pinned_profile_ids=list(outcome.profile_ids) if outcome.status == "pinned" else None,
        none_confirmed=outcome.status == "none_confirmed",
    )
    grants = [PiGrant(user_id=user_id, core_project_num=r.core_project_num,
                      reporter_profile_id=r.reporter_profile_id, title=r.title,
                      activity_code=r.activity_code, first_fy=r.first_fy, last_fy=r.last_fy,
                      project_end=r.project_end, vetoed_at=None) for r in outcome.records]
    rows = [PiOrcidFunding(user_id=user_id, group_key=f.group_key, title=f.title,
                           funder_name=f.funder_name, start_year=f.start_year,
                           start_month=f.start_month, end_year=f.end_year,
                           end_month=f.end_month, external_ids=list(f.external_ids),
                           vetoed_at=None) for f in fundings]
    return identity, grants, rows


def check_shared_awards(rows_by_user: dict, ids_by_user: dict, keys_by_user: dict) -> list[str]:
    """(d): problems with awards rendered for two of NAMED_PIS. rows_by_user[uid] maps a
    core to its PiGrant; ids_by_user[uid] is rendered_profile_ids; keys_by_user[uid] the
    owner's surname_keys."""
    problems = []
    users = sorted(rows_by_user)
    for i, a in enumerate(users):
        for b in users[i + 1:]:
            for core in sorted(set(rows_by_user[a]) & set(rows_by_user[b])):
                ga, gb = rows_by_user[a][core], rows_by_user[b][core]
                if ga.reporter_profile_id == gb.reporter_profile_id:
                    problems.append(f"{core}: both PIs render profile {ga.reporter_profile_id}")
                for uid, g in ((a, ga), (b, gb)):
                    name = (g.identity_evidence or {}).get("name_on_award") or ""
                    if g.reporter_profile_id not in ids_by_user[uid] or not (
                            parse_person_name(name).surname_keys & keys_by_user[uid]):
                        problems.append(
                            f"{core}: profile {g.reporter_profile_id} ({name!r}) is not this PI's")
    return problems


async def _agent_for(db, user_id: uuid.UUID) -> AgentRegistry | None:
    return (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user_id)
    )).scalar_one_or_none()


def _print_outcome(outcome: GrantOutcome, sections) -> None:
    print(f"  new: {outcome.status} accepted={list(outcome.accepted_profile_ids)}"
          f" ({outcome.note})")
    for c in outcome.candidates:
        print(f"    candidate {c.profile_id} {c.name_on_award!r} linked={bool(c.linking_pmids)}"
              f" pmids={sorted(c.linking_pmids)}")
    for line in sections.active:
        print(f"    new active: {line.render()}")
    if sections.tenure_start is None:
        print("    new past: (no tenure start, no Past section)")
    for line in sections.past:
        print(f"    new past: {line.render()}")


async def dry_run(db, population, *, pace: float) -> dict:
    """Print old vs new per PI and return the review lists (PI names per status in
    REVIEW_STATUSES, plus `external_id_types`). Writes nothing: ends with a rollback."""
    report: dict = {status: [] for status in REVIEW_STATUSES}
    id_types: set[str] = set()
    for uid, orcid, name in population:
        user = await db.get(User, uid)
        identity = await db.get(PiGrantIdentity, uid)
        agent = await _agent_for(db, uid)
        print(f"\n== {name} ({orcid})")
        if identity is None:
            print("  old: none")
        else:
            print(f"  old: {identity.status or 'NULL'}"
                  f" accepted={identity.accepted_profile_ids or []}"
                  f" pinned={identity.pinned_profile_ids or []}")
        old_text = persona_file_text(agent.agent_id) if agent else None
        for line in parse_persona_grants(old_text or ""):
            print(f"    old line: {_format_parsed(line)}")
        outcome = await resolve_grants(db, user, identity)
        try:
            fundings = await fetch_orcid_fundings(orcid, strict=True) or []
        except Exception as exc:  # the preview goes on without ORCID rows
            print(f"  ORCID fetch failed: {exc!r}")
            fundings = []
        for f in fundings:
            id_types.update(str(e.get("type")) for e in f.external_ids if isinstance(e, dict))
        tenure = await export_tenure_start(db, uid, agent.agent_id if agent else None)
        t_identity, t_grants, t_fundings = transient_rows(uid, outcome, fundings)
        sections = grant_sections.build_grant_sections(
            identity=t_identity, grants=t_grants, fundings=t_fundings, tenure_start=tenure,
            today=grant_sections.sections_today())
        _print_outcome(outcome, sections)
        if outcome.status in report:
            report[outcome.status].append(name)
        await asyncio.sleep(pace)
    await db.rollback()
    report["external_id_types"] = sorted(id_types)
    for status in REVIEW_STATUSES:
        print(f"\n{status} ({len(report[status])}): {', '.join(report[status]) or '-'}")
    print(f"\nORCID external-id types seen: {', '.join(report['external_id_types']) or '-'}")
    return report


async def apply_orcid(factory, population, *, pace: float) -> list[str]:
    """Strict ORCID fetch + store per PI, committed per PI; returns the failures (not fatal:
    the enrich_grants job refetches)."""
    failures = []
    for uid, orcid, name in population:
        try:
            fundings = await fetch_orcid_fundings(orcid, strict=True) or []
            async with factory() as db:
                # Persona writer locks first (lock order: profile_publish module docstring).
                await lock_persona_writer(db, uid)
                await store_orcid_fundings(db, uid, fundings)
                await db.commit()
            print(f"  ORCID {name}: {len(fundings)} funding(s)")
        except Exception as exc:
            failures.append(f"{name} ({orcid}): {exc!r}")
            print(f"  ORCID {name}: FAILED {exc!r}")
        await asyncio.sleep(pace)
    return failures


async def reexport_all(factory, population) -> int:
    """Re-export every PI with an agent and a profile (revision `reexport` when the text
    changed), commit, then write the files after the commit. Returns the changed count."""
    ids = [uid for uid, _, _ in population]
    async with factory() as db:
        owners = (await db.execute(
            select(AgentRegistry.user_id, AgentRegistry.agent_id)
            .join(ResearcherProfile, ResearcherProfile.user_id == AgentRegistry.user_id)
            .where(AgentRegistry.user_id.in_(ids)).order_by(AgentRegistry.agent_id)
        )).all()
    changed = 0
    for uid, agent_id in owners:
        async with factory() as db:
            rendered = await reexport_persona(db, uid, mechanism="reexport",
                                              skip_if_file_matches=True)
            await db.commit()
            path = await write_persona_files(db, uid)
        changed += rendered is not None
        state = "changed" if rendered is not None else "unchanged"
        print(f"  {agent_id}: {state}, {path.name if path else 'FAILED (see the ERROR log)'}")
    return changed


async def completeness_gaps(db, population_ids: Sequence[uuid.UUID],
                            deploy_ts: datetime) -> list[str]:
    """Names of the PIs among ``population_ids`` failing the completeness predicate (no
    identity row, or `evaluated_at` / `orcid_fetched_at` missing or before ``deploy_ts``)."""
    stmt = text(COMPLETENESS_SQL).bindparams(
        bindparam("ids", type_=postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
        bindparam("deploy_ts", type_=DateTime(timezone=True)),
    )
    rows = (await db.execute(stmt, {"ids": list(population_ids), "deploy_ts": deploy_ts})).all()
    return [r.name for r in rows]


async def flag_gate(db, population_ids: Sequence[uuid.UUID], *, tag: str, since: datetime,
                    deploy_ts: datetime) -> list[str]:
    """The reasons not to set `persona_sweep_enabled`; empty means set it. Only this run's
    jobs count (bulk_tag = ``tag`` or enqueued at/after ``since``)."""
    reasons = [f"incomplete: {name}"
               for name in await completeness_gaps(db, population_ids, deploy_ts)]
    status = await bulk.run_status(db, job_type=JOB_TYPE, user_ids=list(population_ids),
                                   tag=tag, since=since)
    for state in ("dead", "pending", "processing"):
        if status.get(state):
            reasons.append(f"{status[state]} {JOB_TYPE} job(s) of this run {state}")
    return reasons


async def render_diffs(db) -> list[str]:
    """Agent ids (with an identity row and a profile) whose persona file differs from
    `render_persona_from_db`."""
    user_ids = (await db.execute(
        select(AgentRegistry.user_id)
        .join(PiGrantIdentity, PiGrantIdentity.user_id == AgentRegistry.user_id)
        .join(ResearcherProfile, ResearcherProfile.user_id == AgentRegistry.user_id)
        .where(AgentRegistry.user_id.isnot(None))
        .order_by(AgentRegistry.agent_id)
    )).scalars().all()
    diffs = []
    for uid in user_ids:
        rendered = await render_persona_from_db(db, uid)
        if rendered is None:
            continue
        agent, body = rendered
        if persona_file_text(agent.agent_id) != body:
            diffs.append(agent.agent_id)
    return diffs


def _orcid_match(line: ParsedLine, fundings: Sequence[PiOrcidFunding]) -> PiOrcidFunding | None:
    for f in fundings:
        label = _collapse(f.funder_name) or "ORCID"
        title = _collapse(f.title)
        if line.label and line.title == title and line.label == label:
            return f
        # A funder name holding parentheses defeats LINE_RE; match the raw bullet instead.
        if not line.label and line.title.startswith(f"{title} ({label}"):
            return f
    return None


def _reporter_match(line: ParsedLine, grants: Sequence[PiGrant],
                    ids: frozenset[int]) -> PiGrant | None:
    code = line.label[len("NIH "):]
    for g in grants:
        if (_collapse(g.title) == line.title and (g.activity_code or "") == code
                and g.reporter_profile_id in ids):
            return g
    return None


def line_problems(name: str, lines: Sequence[ParsedLine], identity: PiGrantIdentity | None,
                  grants: Sequence[PiGrant], fundings: Sequence[PiOrcidFunding],
                  today) -> tuple[list[str], list[str]]:
    """Checks (b) and (c) for one PI: (unmapped lines, ended Active lines). ``grants`` and
    ``fundings`` are the PI's non-vetoed rows."""
    ids = grant_sections.rendered_profile_ids(identity)
    unmapped, ended = [], []
    for line in lines:
        funding = _orcid_match(line, fundings)
        if funding is not None:
            if line.section == "active" and not grant_sections.orcid_is_active(funding, today):
                ended.append(f"{name}: {_format_parsed(line)}")
            continue
        if not line.label:
            unmapped.append(f"{name}: stale bare bullet {line.title!r}")
            continue
        grant = _reporter_match(line, grants, ids) if line.label.startswith("NIH ") else None
        if grant is None:
            unmapped.append(f"{name}: {_format_parsed(line)}")
        elif line.section == "active" and not grant_sections.reporter_is_active(grant, today):
            ended.append(f"{name}: {_format_parsed(line)}")
    return unmapped, ended


def _report(label: str, problems: Sequence[str]) -> bool:
    print(f"{'PASS' if not problems else 'FAIL'} {label}")
    for problem in problems:
        print(f"    {problem}")
    return not problems


async def verify(factory, deploy_ts: datetime) -> bool:
    """Spec §9 checks (a)-(e), read-only; True when all pass."""
    today = grant_sections.sections_today()
    named_keys = frozenset().union(*(surname_keys(n) for n in NAMED_PIS))
    async with factory() as db:
        population = await bulk.pi_population(db)
        ids = [uid for uid, _, _ in population]
        gaps = [f"incomplete: {n}" for n in await completeness_gaps(db, ids, deploy_ts)]
        if not await sweep_enabled(db):
            gaps.append("persona_sweep_enabled is not 'true'")
        unmapped, ended = [], []
        rows_by_user, ids_by_user, keys_by_user = {}, {}, {}
        for uid, _, name in population:
            identity = await db.get(PiGrantIdentity, uid)
            grants = (await db.execute(select(PiGrant).where(
                PiGrant.user_id == uid, PiGrant.vetoed_at.is_(None)))).scalars().all()
            fundings = (await db.execute(select(PiOrcidFunding).where(
                PiOrcidFunding.user_id == uid, PiOrcidFunding.vetoed_at.is_(None)))).scalars().all()
            agent = await _agent_for(db, uid)
            text_ = persona_file_text(agent.agent_id) if agent else None
            u, e = line_problems(name, parse_persona_grants(text_ or ""), identity, grants,
                                 fundings, today)
            unmapped += u
            ended += e
            keys = parse_person_name(name).surname_keys
            if keys & named_keys:
                rendered = grant_sections.rendered_profile_ids(identity)
                rows_by_user[uid] = {g.core_project_num: g for g in grants
                                     if g.reporter_profile_id in rendered}
                ids_by_user[uid], keys_by_user[uid] = rendered, keys
        names = [n for uid, _, n in population if uid in rows_by_user]
        print(f"Named PIs checked for (d): {', '.join(names) or 'none found'}")
        shared = check_shared_awards(rows_by_user, ids_by_user, keys_by_user)
        diffs = await render_diffs(db)
        await db.rollback()
    results = [
        _report("(a) completeness and persona_sweep_enabled", gaps),
        _report("(b) every NIH line maps to a rendered, non-vetoed pi_grants row", unmapped),
        _report("(c) no Active line has ended", ended),
        _report("(d) shared awards name distinct, owned profiles", shared),
        _report("(e) render diff 0", [f"{a}: file differs from the database" for a in diffs]),
    ]
    return all(results)


async def _population(factory, orcid: str | None) -> list[tuple[uuid.UUID, str, str]]:
    async with factory() as db:
        population = await bulk.pi_population(db)
    return [p for p in population if p[1] == orcid] if orcid else population


async def run_apply(factory, population, *, deploy_ts: datetime | None, canary: bool,
                    pace: float, poll: float) -> int:
    apply_start = datetime.now(UTC)
    deploy_ts = deploy_ts or apply_start
    tag = f"grants-remediation-{apply_start:%Y-%m-%d}"
    ids = [uid for uid, _, _ in population]
    daily = bulk.OPENALEX_DAILY_CREDITS * bulk.DEFAULT_BUDGET_SHARE
    print(f"apply_start={apply_start.isoformat()} deploy_ts={deploy_ts.isoformat()} tag={tag}")
    print("1. ORCID fundings")
    failures = await apply_orcid(factory, population, pace=pace)
    print(f"   {len(failures)} failure(s) (the enrich_grants job refetches)")
    for failure in failures:
        print(f"    {failure}")
    print(f"2. {JOB_TYPE} for {len(ids)} PI(s)")
    async with factory() as db:
        await bulk.enqueue_paced(db, job_type=JOB_TYPE, users=[(u, o) for u, o, _ in population],
                                 tag=tag, start=apply_start, credits_per_job=0, daily_credits=daily)
    counts = await bulk.wait_until_drained(factory, job_type=JOB_TYPE, user_ids=ids, tag=tag,
                                           since=apply_start, poll_seconds=poll)
    print(f"   this run's newest jobs: {counts}")
    print("3. dead jobs re-enqueued once")
    async with factory() as db:
        resumed = await bulk.resume_dead(db, job_type=JOB_TYPE, tag=tag, credits_per_job=0,
                                         daily_credits=daily)
    print(f"   {len(resumed)} re-enqueued")
    counts = await bulk.wait_until_drained(factory, job_type=JOB_TYPE, user_ids=ids, tag=tag,
                                           since=apply_start, poll_seconds=poll)
    print(f"   this run's newest jobs: {counts}")
    print("4. persona re-export")
    print(f"   {await reexport_all(factory, population)} persona(s) changed")
    print("5. persona_sweep_enabled")
    async with factory() as db:
        reasons = await flag_gate(db, ids, tag=tag, since=apply_start, deploy_ts=deploy_ts)
        if canary:
            reasons.append("--orcid canary: the flag is set only by a full-population apply")
        if not reasons:
            await enable_persona_sweep(db)
            await db.commit()
    if reasons:
        print("   NOT set:")
        for reason in reasons:
            print(f"    {reason}")
        return 2
    print("   set")
    return 0


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


async def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview, writes nothing (default)")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true", help="read-only checks; needs --deploy-ts")
    p.add_argument("--deploy-ts", type=_parse_ts,
                   help="ISO timestamp; required with --verify, defaults to the start with --apply")
    p.add_argument("--orcid", help="scope --dry-run / --apply to one PI (canary)")
    p.add_argument("--orcid-pace-seconds", type=float, default=1.0)
    p.add_argument("--poll-seconds", type=float, default=30.0)
    a = p.parse_args()
    factory = get_session_factory()
    if a.verify:
        if a.deploy_ts is None:
            p.error("--verify needs --deploy-ts")
        return 0 if await verify(factory, a.deploy_ts) else 1
    population = await _population(factory, a.orcid)
    if not population:
        print("No PI matched.")
        return 1
    if a.apply:
        return await run_apply(factory, population, deploy_ts=a.deploy_ts, canary=bool(a.orcid),
                               pace=a.orcid_pace_seconds, poll=a.poll_seconds)
    async with factory() as db:
        await dry_run(db, population, pace=a.orcid_pace_seconds)
    print(f"\nCompleteness predicate (runbook):\n{COMPLETENESS_SQL};")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
