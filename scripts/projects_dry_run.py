"""Research BlackbirdBot projects for some labs without storing anything (spec §8, dry run).

Runs the worker's own request builders, tool calls, screening and choice checks
(``src.services.project_research``) in memory, lab after lab: no ``hub_projects`` or
``hub_project_calls`` row is written and nothing is posted. It stops starting calls once its
own priced total reaches ``--max-usd``, and writes ONE JSON file::

    {"generated_at": <ISO time>, "model": ..., "total_usd": "<dollars>",
     "labs": [{"agent_id", "pi_name", "cost_usd", "sessions": [{"domain", "status",
               "candidates", "dropped"}], "choice_failures", "outcome", "choice",
               "title", "posting_text"}, ...]}

``outcome`` is ``ready_to_post``, ``no_idea``, ``research_failed``, ``choice_failed`` or
``stopped_at_cap``. The file holds research text about the labs: keep it out of the
checkout (the script refuses a path inside the repository, which is ``/app`` in a container).

Run it on the host in a ONE-OFF worker container, never in a live one, with a host
directory outside the checkout mounted for the output (the worker service mounts only
``profiles/`` and ``prompts/``)::

    DC="docker compose -f docker-compose.prod.yml"
    mkdir -p ~/backups-blackbird/projects-dry-run
    $DC run --rm --no-deps -T -v ~/backups-blackbird/projects-dry-run:/out worker \\
        python scripts/projects_dry_run.py --out /out/2026-10-27.json [--lab <agent_id> ...] \\
        [--max-usd 40]

Without ``--lab`` it researches every active agent whose role is a lab role
(``role_capabilities.spoke_role_names()``) and that has a persona file. The model, the tool
round limit and the prompts are the worker's (``LLM_PROJECT_RESEARCH_MODEL``,
``PROJECT_RESEARCH_MAX_TOOL_ROUNDS``, ``prompts/projects/``). Exit 0 when the file was
written, 2 when the arguments or the prompts are unusable.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src.agent.role_capabilities import spoke_role_names  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.models import AgentRegistry  # noqa: E402
from src.services import profile_export  # noqa: E402
from src.services import project_research as pr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The checkout's path inside the images.
CONTAINER_ROOT = Path("/app")
#: Uncounted failures (a rate limit, a 5xx, PubMed down) one lab may wait out before it is
#: given up as ``research_failed``.
MAX_WAITS_PER_LAB = 3
#: The longest single wait the dry run sleeps.
MAX_WAIT_SECONDS = 120.0


def check_out_path(out: Path) -> Path:
    """The resolved output path; ``ValueError`` when it lies inside the repository."""
    resolved = out.expanduser().resolve()
    for root in (REPO_ROOT, CONTAINER_ROOT):
        if resolved == root or root in resolved.parents:
            raise ValueError(f"refusing to write inside the repository ({root}): {resolved}")
    return resolved


async def _lab_ids(db, requested: list[str] | None) -> list[str]:
    if requested:
        return list(dict.fromkeys(requested))
    rows = (
        await db.execute(
            select(AgentRegistry.agent_id)
            .where(
                AgentRegistry.status == "active",
                AgentRegistry.role.in_(spoke_role_names()),
            )
            .order_by(AgentRegistry.agent_id)
        )
    ).scalars().all()
    return [a for a in rows if (profile_export.PROFILES_DIR / f"{a}.md").is_file()]


def _lab_entry(agent_id: str, pi_name: str | None) -> dict[str, Any]:
    return {
        "agent_id": agent_id, "pi_name": pi_name, "cost_usd": "0", "sessions": [],
        "choice_failures": [], "outcome": "research_failed", "choice": None,
        "title": None, "posting_text": None,
    }


async def research_lab(
    lab: pr.LabContext,
    *,
    model: str,
    prompts: pr.Prompts,
    max_rounds: int,
    roster: list,
    hub_agent_ids: frozenset[str],
    budget: Decimal,
) -> tuple[dict[str, Any], Decimal]:
    """One lab's research and choice in memory: ``(its <lab> entry, dollars spent)``.
    No call starts once ``budget`` is spent."""
    entry = _lab_entry(lab.agent_id, lab.pi_name)
    state = pr.new_state(lab, prompt_shas=prompts.shas)
    spent = Decimal(0)
    waits = 0
    outcome: str | None = None
    while outcome is None:
        step = pr.plan_next_step(
            state, lab, model=model, prompts=prompts, max_rounds=max_rounds
        )
        if step.kind == pr.STEP_NO_IDEA:
            outcome = "no_idea"
            break
        if spent >= budget:
            outcome = "stopped_at_cap"
            break
        message, exc = await pr.call_model(step.request or {})
        result = await pr.apply_result(
            state, step, lab, message=message, exc=exc, model=model,
            roster=roster, hub_agent_ids=hub_agent_ids,
        )
        spent += pr.ledger_row_cost(result.entries, model, step.step)
        if result.choice is not None:
            outcome = "ready_to_post"
            entry.update(
                choice=result.choice, title=result.choice["title"],
                posting_text=result.posting_text,
            )
        elif result.end_reason is not None:
            outcome = result.end_reason
        elif result.wait == pr.TRANSIENT_RETRY_AFTER or result.error_code == "pubmed_unavailable":
            # An uncounted failure: the worker would wait and run the same step again.
            waits += 1
            if waits > MAX_WAITS_PER_LAB:
                outcome = "research_failed"
            else:
                await asyncio.sleep(min(result.wait.total_seconds(), MAX_WAIT_SECONDS))
    entry.update(
        outcome=outcome,
        cost_usd=str(spent),
        sessions=[
            {k: s.get(k) for k in ("domain", "status", "candidates", "dropped")}
            for s in state["sessions"]
        ],
        choice_failures=state["choice_failures"],
    )
    return entry, spent


async def dry_run(
    session_factory, *, labs: list[str] | None, max_usd: Decimal, out: Path
) -> dict[str, Any]:
    """Research each lab in turn and write the report to ``out``; returns the report.
    Reads the registry and earlier projects; writes nothing to the database."""
    target = check_out_path(out)
    settings = get_settings()
    model = settings.llm_project_research_model
    prompts = pr.load_prompts()
    if prompts is None:
        raise ValueError("prompts/projects/research.md or choose.md is missing or empty")
    max_rounds = int(settings.project_research_max_tool_rounds)
    async with session_factory() as db:
        roster, hub_agent_ids = await pr.load_roster(db)
        contexts: list[tuple[str, pr.LabContext | None, str | None]] = []
        for agent_id in await _lab_ids(db, labs):
            pi_name = await db.scalar(
                select(AgentRegistry.pi_name).where(AgentRegistry.agent_id == agent_id)
            )
            contexts.append((agent_id, await pr.load_lab(db, agent_id), pi_name))
    total = Decimal(0)
    entries = []
    for agent_id, lab, pi_name in contexts:
        if lab is None:
            entries.append(_lab_entry(agent_id, pi_name))
            continue
        entry, spent = await research_lab(
            lab, model=model, prompts=prompts, max_rounds=max_rounds, roster=roster,
            hub_agent_ids=hub_agent_ids, budget=max_usd - total,
        )
        total += spent
        entries.append(entry)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "total_usd": str(total),
        "labs": entries,
    }
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lab", action="append", dest="labs", metavar="AGENT_ID",
                        help="a lab's agent_id (repeatable); default every active lab")
    parser.add_argument("--max-usd", type=Decimal, default=Decimal("40.0"),
                        help="stop starting calls once this many dollars are spent")
    parser.add_argument("--out", type=Path, required=True,
                        help="the JSON file to write, outside the checkout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from src.database import get_session_factory

    try:
        report = asyncio.run(
            dry_run(get_session_factory(), labs=args.labs, max_usd=args.max_usd, out=args.out)
        )
    except ValueError as exc:
        print(f"projects_dry_run: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {len(report['labs'])} lab(s), ${report['total_usd']}, to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
