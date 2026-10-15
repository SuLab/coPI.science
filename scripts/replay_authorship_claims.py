"""Read-only replay of the authorship-claim check over stored bot messages (the
2026-10-15 publication-handling design §11 R6.15; main SPEC GATE-16 adapted).

Replays every stored reply and pitch of a pi_lab or hub agent (agent_messages rows with
is_bot, an agent_id and phase thread_reply or new_post) through check_draft_authorship,
with records built as the live engine builds them (C12: load_publication_ids plus each
lab's persona file, minus F9's not-theirs identifiers; a hub is record-less) and the
thread root as context. Enforce semantics apply to pi_lab speakers, log semantics to the
hub. There is no current gate to compare against, so the report gives absolute numbers
and sets no threshold: the owner decides the switch (Q19).

It writes NOTHING, not even mode='replay' rows: its transaction is SET TRANSACTION READ
ONLY, it must be re-runnable without a cleanup, and run-less rows would never cascade.

  $DC run --rm --no-deps -T blackbird-app python scripts/replay_authorship_claims.py \\
      [--run RUN_ID ...] [--since YYYY-MM-DD] [--agent ID ...] [--samples 5] \\
      [--format md|json] [--profiles-dir profiles] > data/reports/<name>.md

The report holds message text: keep it under data/ (gitignored), never commit it.
Exit 0, or 1 on a database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.agent.authorship_rules import check_draft_authorship  # noqa: E402
from src.agent.claim_check import outcome_of, root_context, roster_entries  # noqa: E402
from src.agent.publication_records import (  # noqa: E402
    LabPublicationRecord,
    build_record,
    load_publication_ids,
)
from src.agent.role_capabilities import hub_role_names  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.models import AgentMessage, AgentRegistry, AuthorshipClaimCheck  # noqa: E402
from src.services.build_info import get_build_info  # noqa: E402

REPLAYED_PHASES = ("thread_reply", "new_post")
LONG_ROW_CHARS = 3900
EXCERPT_CHARS = 400
PI_LAB, HUB = "pi_lab", "scout_hub"
RECORDS_BASIS = (
    "Records: C12 (in-use stored publications plus the persona's identifiers, minus "
    "identifiers held only by rows excluded as staff, openalex_affiliation or "
    "repair_script, F9); hubs are record-less."
)
WRITES = "Nothing was written: the replay ran in a READ ONLY transaction."


@dataclass(frozen=True)
class Scope:
    runs: tuple[uuid.UUID, ...] = ()
    since: date | None = None
    agents: tuple[str, ...] = ()


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "—"


def _persona(profiles_dir: Path, agent_id: str) -> str | None:
    try:
        return (profiles_dir / "public" / f"{agent_id}.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


async def _roster_and_records(db: AsyncSession, profiles_dir: Path):
    """Every agents row (any status: historical speakers include deactivated labs), the
    rules' roster, each agent's C12 record, and its role."""
    rows = (await db.execute(select(AgentRegistry))).scalars().all()
    by_id = {r.agent_id: r for r in rows}
    ids = await load_publication_ids(db, list(by_id))
    hubs = hub_role_names()
    records: dict[str, LabPublicationRecord] = {}
    for aid, r in by_id.items():
        records[aid] = (LabPublicationRecord() if r.role in hubs
                        else build_record(_persona(profiles_dir, aid), ids.get(aid)))
    return by_id, roster_entries(by_id), records


async def _messages(db: AsyncSession, scope: Scope) -> list[AgentMessage]:
    """Every row of the in-scope runs, ordered by (run, posted_at): the replayed rows and
    the thread roots and continuation chunks their context needs."""
    stmt = select(AgentMessage).order_by(AgentMessage.simulation_run_id, AgentMessage.posted_at)
    if scope.runs:
        stmt = stmt.where(AgentMessage.simulation_run_id.in_(scope.runs))
    return list((await db.execute(stmt)).scalars().all())


def _in_scope(row: AgentMessage, scope: Scope) -> bool:
    if not (row.is_bot and row.agent_id):
        return False
    if scope.agents and row.agent_id not in scope.agents:
        return False
    if scope.since and row.created_at.date() < scope.since:
        return False
    return True


def _context(row: AgentMessage, by_ts: dict, threads: dict) -> str | None:
    if row.phase != "thread_reply" or not row.thread_ts:
        return None
    root = by_ts.get((row.simulation_run_id, row.thread_ts))
    if root is None:
        return None
    following = [(r.agent_id, r.content) for r in threads[(row.simulation_run_id, row.thread_ts)]]
    return root_context(root.agent_id, root.content, following)


def _empty_totals() -> dict[str, int]:
    return {"messages": 0, "claims": 0, "grounded": 0, "rejected": 0}


async def collect(db: AsyncSession, scope: Scope, *, profiles_dir: Path, samples: int = 5) -> dict:
    """The report as one JSON-ready dict (keys: generated, scope, totals, enforcement,
    per_lab, reasons, samples, live). Reads only."""
    by_id, roster, records = await _roster_and_records(db, profiles_dir)
    hubs = hub_role_names()

    def record_for(aid: str) -> LabPublicationRecord:
        if aid not in records:
            records[aid] = build_record(_persona(profiles_dir, aid), None)
        return records[aid]

    def label_for(aid: str) -> str:
        return by_id[aid].bot_name if aid in by_id else aid

    rows = await _messages(db, scope)
    by_ts = {(r.simulation_run_id, r.message_ts): r for r in rows if r.message_ts}
    threads: dict[tuple, list[AgentMessage]] = defaultdict(list)
    for r in rows:
        if r.thread_ts and r.phase != "panel_note" and r.message_ts != r.thread_ts:
            threads[(r.simulation_run_id, r.thread_ts)].append(r)

    totals = {PI_LAB: _empty_totals(), HUB: _empty_totals()}
    per_lab: dict[str, dict[str, Any]] = {}
    not_replayed: dict[str, int] = defaultdict(int)
    enforcement = {"pitches": 0, "pitches_blocked": 0, "replies": 0, "replies_blocked": 0}
    blocked_threads: set[tuple] = set()
    blocks_per_thread_lab: dict[tuple, int] = defaultdict(int)
    reasons: dict[str, dict[str, Any]] = {}
    rejected_rows: list[tuple[AgentMessage, str, str]] = []
    counts = {"replies": 0, "pitches": 0, "long_rows": 0, "uid_rows": 0}

    for row in rows:
        if not _in_scope(row, scope):
            continue
        if row.phase not in REPLAYED_PHASES:
            not_replayed[row.phase] += 1
            continue
        counts["replies" if row.phase == "thread_reply" else "pitches"] += 1
        counts["long_rows"] += len(row.content) >= LONG_ROW_CHARS
        counts["uid_rows"] += "<@U" in row.content
        aid = row.agent_id
        verdict = check_draft_authorship(
            row.content, own=record_for(aid), speaker_id=aid, roster=roster,
            record_for=record_for, label_for=label_for,
            context_text=_context(row, by_ts, threads),
        )
        outcome = outcome_of(verdict)
        role = by_id[aid].role if aid in by_id else PI_LAB
        bucket = HUB if role in hubs else PI_LAB
        t = totals[bucket]
        t["messages"] += 1
        t["claims"] += verdict.has_claim
        t["grounded"] += outcome == "grounded"
        t["rejected"] += outcome == "rejected"
        if bucket == PI_LAB:
            rec = record_for(aid)
            lab = per_lab.setdefault(aid, {
                "bot": label_for(aid), "dois": len(rec.dois), "pmids": len(rec.pmids),
                "messages": 0, "claims": 0, "rejected": 0,
            })
            lab["messages"] += 1
            lab["claims"] += verdict.has_claim
            lab["rejected"] += outcome == "rejected"
            kind = "pitches" if row.phase == "new_post" else "replies"
            enforcement[kind] += 1
            if outcome == "rejected":
                enforcement[f"{kind}_blocked"] += 1
                if row.phase == "thread_reply":
                    key = (row.simulation_run_id, row.thread_ts)
                    blocked_threads.add(key)
                    blocks_per_thread_lab[(*key, aid)] += 1
        if outcome == "rejected":
            code = verdict.reasons[0].code
            entry = reasons.setdefault(code, {"count": 0, "labs": set()})
            entry["count"] += 1
            entry["labs"].add(aid)
            rejected_rows.append((row, code, verdict.reasons[0].text))

    for lab in per_lab.values():
        lab["rejected_pct"] = _pct(lab["rejected"], lab["messages"])
    for t in totals.values():
        t["rejected_pct_messages"] = _pct(t["rejected"], t["messages"])
        t["rejected_pct_claims"] = _pct(t["rejected"], t["claims"])
    enforcement["threads"] = len(blocked_threads)
    enforcement["threads_with_two_blocks"] = sum(1 for n in blocks_per_thread_lab.values() if n >= 2)
    enforcement["labs_without_records"] = sorted(
        aid for aid, r in by_id.items() if r.role not in hubs and not record_for(aid).has_records
    )
    sample_rows: dict[str, list[dict]] = defaultdict(list)
    for row, code, reason in sorted(rejected_rows, key=lambda x: x[0].posted_at, reverse=True):
        if len(sample_rows[code]) < samples:
            sample_rows[code].append({
                "run": str(row.simulation_run_id), "message_ts": row.message_ts,
                "agent": row.agent_id, "phase": row.phase, "reason": reason,
                "excerpt": json.dumps(row.content[:EXCERPT_CHARS], ensure_ascii=False),
            })
    build = get_build_info()
    alembic = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": {
            "runs": [str(r) for r in scope.runs] or "all",
            "since": scope.since.isoformat() if scope.since else None,
            "agents": list(scope.agents) or "all",
            **counts,
            "not_replayed": dict(not_replayed),
            "image": {"commit": build.commit, "branch": build.branch},
            "alembic_version": alembic,
            "records_basis": RECORDS_BASIS,
            "writes": WRITES,
        },
        "totals": totals,
        "enforcement": enforcement,
        "per_lab": dict(sorted(per_lab.items(), key=lambda kv: (-kv[1]["rejected"], kv[0]))),
        "reasons": {c: {"count": e["count"], "labs": sorted(e["labs"])} for c, e in sorted(reasons.items())},
        "samples": dict(sample_rows),
        "live": await _live(db),
    }


async def _live(db: AsyncSession) -> list[dict]:
    c = AuthorshipClaimCheck
    rows = (await db.execute(
        select(c.mode, c.phase, c.outcome, func.count(), func.min(c.created_at), func.max(c.created_at))
        .group_by(c.mode, c.phase, c.outcome).order_by(c.mode, c.phase, c.outcome)
    )).all()
    return [{"mode": m, "phase": p, "outcome": o, "count": n,
             "first": first.isoformat(), "last": last.isoformat()} for m, p, o, n, first, last in rows]


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)


def render_markdown(report: dict) -> str:
    s, t, e = report["scope"], report["totals"], report["enforcement"]
    out = [
        "# Authorship-claim check: replay report", "",
        f"- Generated: {report['generated']} (UTC)",
        f"- Image: commit {s['image']['commit']}, branch {s['image']['branch']}; alembic {s['alembic_version']}",
        f"- Scope: runs {s['runs']}; since {s['since'] or 'the start'}; agents {s['agents']}",
        f"- Messages: {s['replies']} replies, {s['pitches']} pitches; not replayed {s['not_replayed'] or 'none'}; "
        f"{s['long_rows']} rows of {LONG_ROW_CHARS}+ characters (possibly one chunk of a split post, "
        f"checked alone); {s['uid_rows']} rows containing <@U (no bot-uid map in the database)",
        f"- {s['records_basis']}", f"- {s['writes']}", "",
        "## Totals", "",
        "| Speaker | Messages | Claims | Grounded | Rejected | Rejected % of messages | Rejected % of claims |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, label in ((PI_LAB, "pi_lab"), (HUB, "scout_hub (log only)")):
        r = t[key]
        out.append(f"| {label} | {r['messages']} | {r['claims']} | {r['grounded']} | {r['rejected']} "
                   f"| {r['rejected_pct_messages']} | {r['rejected_pct_claims']} |")
    out += [
        "", "## What enforcement would have done (pi_lab)", "",
        f"- Pitches blocked: {e['pitches_blocked']} of {e['pitches']} ({_pct(e['pitches_blocked'], e['pitches'])})",
        f"- Replies blocked: {e['replies_blocked']} of {e['replies']} ({_pct(e['replies_blocked'], e['replies'])}), "
        f"in {e['threads']} interview threads",
        f"- Threads with at least 2 blocked replies from the same lab (abandoned under F4 unless "
        f"the feedback changes the next draft): {e['threads_with_two_blocks']}",
        f"- Labs with no records: {', '.join(e['labs_without_records']) or 'none'}",
        "", "## Per lab", "",
        "| agent_id | bot | records (DOIs / PMIDs) | messages | claims | rejected | rejected % |",
        "|---|---|---|---|---|---|---|",
    ]
    for aid, r in report["per_lab"].items():
        out.append(f"| {aid} | {r['bot']} | {r['dois']} / {r['pmids']} | {r['messages']} "
                   f"| {r['claims']} | {r['rejected']} | {r['rejected_pct']} |")
    out += ["", "## Rejections by reason code", "", "| code | count | labs |", "|---|---|---|"]
    for code, r in report["reasons"].items():
        out.append(f"| {code} | {r['count']} | {', '.join(r['labs'])} |")
    out += ["", "## Samples", ""]
    for code, items in report["samples"].items():
        out.append(f"### {code}")
        for i in items:
            out.append(f"- run {i['run']}, {i['message_ts']}, {i['agent']}, {i['phase']}: {i['reason']}  ")
            out.append(f"  excerpt: {i['excerpt']}")
        out.append("")
    out += ["## Live rows", ""]
    if not report["live"]:
        out.append("none yet")
    else:
        out += ["| mode | phase | outcome | count | first | last |", "|---|---|---|---|---|---|"]
        out += [f"| {r['mode']} | {r['phase']} | {r['outcome']} | {r['count']} | {r['first']} | {r['last']} |"
                for r in report["live"]]
    return "\n".join(out) + "\n"


async def _main(args: argparse.Namespace) -> int:
    scope = Scope(
        runs=tuple(uuid.UUID(r) for r in args.run or ()),
        since=date.fromisoformat(args.since) if args.since else None,
        agents=tuple(args.agent or ()),
    )
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            report = await collect(db, scope, profiles_dir=Path(args.profiles_dir), samples=args.samples)
    except SQLAlchemyError as exc:
        print(f"replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(render_json(report) if args.format == "json" else render_markdown(report), end="")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="append", metavar="RUN_ID", help="Limit to this run (repeatable)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="Only rows created on or after this day")
    parser.add_argument("--agent", action="append", metavar="ID", help="Limit to this speaker (repeatable)")
    parser.add_argument("--samples", type=int, default=5, help="Samples per reason code (default 5)")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--profiles-dir", default="profiles", help="The persona tree (default profiles)")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
