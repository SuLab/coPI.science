"""Print the projects prototype's measures for one simulation run (read-only).

Renders ``src.services.projects_report.build_report``: projects requested per lab,
outcomes and not-finished reasons, research and refinement cost, the most common gaps
and disagreements, anchors per profile section, sources by kind, project reviews beside
the run's assessment reviews, and the run time from request to finish. Writes nothing to
the database.

Usage:
    python scripts/projects_report.py --run <run id>                   # Markdown to stdout
    python scripts/projects_report.py --run <run id> --format json
    python scripts/projects_report.py --run <run id> --out /path/outside/the/checkout.md

``--out`` refuses a path inside the repository, so a report never lands in a commit or,
through the builder stage, in an image. On the production host, run it in a one-off
worker container and redirect stdout to a file outside the checkout (this repo's stack
only, never the other deployment's containers):

    $DC run --rm --no-deps -T worker python scripts/projects_report.py --run <run id> > ~/backups-blackbird/projects-report.md

with ``DC="docker compose -f docker-compose.prod.yml"``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.database import get_session_factory  # noqa: E402
from src.services.projects_report import build_report  # noqa: E402


def out_path_allowed(path: Path) -> bool:
    """False for a path inside the repository (symlinks resolved)."""
    return not path.expanduser().resolve().is_relative_to(REPO_ROOT)


def _usd(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:.2f}"


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _share(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render_markdown(report: dict[str, Any]) -> str:
    """The report as Markdown, one section per measure."""
    lines = [
        f"# Projects report — run {report['run_id']}",
        "",
        f"Run time {_num(report['run_hours'])} h; "
        f"{'finalized' if report['run_finalized'] else 'live' if report['run_live'] else 'not live'}; "
        f"{report['project_count']} projects.",
        "",
        "## Projects per lab",
        "",
        "| Lab | Requested | Per run day |",
        "|---|---|---|",
    ]
    for lab in report["labs"]:
        lines.append(f"| {lab['agent_id']} | {lab['requested']} | {_num(lab['per_run_day'], 2)} |")
    lines += ["", *(f"- {note}" for note in report["notes"]), "", "## Outcomes", ""]
    for kind, n in sorted(report["outcome_counts"].items()):
        share = report["outcome_shares"].get(kind)
        lines.append(f"- {kind}: {n}" + (f" ({_share(share)} of finished)" if share is not None else ""))
    if report["not_finished_reasons"]:
        lines += ["", "Not finished, by reason:", ""]
        lines += [f"- {r['label']} (`{r['code']}`): {r['count']}" for r in report["not_finished_reasons"]]
    cost = report["cost"]
    lines += [
        "",
        "## Cost",
        "",
        f"Research {_usd(cost['research_usd'])}, refinement {_usd(cost['refinement_usd'])}, "
        f"total {_usd(cost['total_usd'])}.",
    ]
    if cost["unpriced_models"]:
        lines.append(f"Unpriced models (counted as $0): {', '.join(cost['unpriced_models'])}.")
    lines += [
        "",
        "| Lab | Slot | Title | Outcome | Research | Refinement | Hours to finish |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in report["projects"]:
        title = (p["title"] or "—").replace("|", "\\|")
        outcome = p["outcome_label"] + (f" — {p['reason_label']}" if p["reason_label"] else "")
        lines.append(
            f"| {p['agent_id']} | {p['slot']} | {title} | {outcome} | {_usd(p['research_usd'])} "
            f"| {_usd(p['refinement_usd'])} | {_num(p['hours_to_finish'])} |"
        )
    for heading, key in (("Most common gaps", "top_gaps"), ("Most common disagreements", "top_disagreements")):
        lines += ["", f"## {heading}", ""]
        lines += [f"- {t['count']} × {t['text']}" for t in report[key]] or ["- none"]
    lines += ["", "## Anchors per profile section", ""]
    lines += [f"- {s}: {n}" for s, n in report["anchor_sections"].items()] or ["- none"]
    lines += ["", "## Sources by kind", ""]
    lines += [f"- {k}: {n}" for k, n in report["sources_by_kind"].items()] or ["- none"]
    lines.append(f"- per project with a choice: {_num(report['sources_per_project'])}")
    rv = report["reviews"]
    hours = report["hours_to_finish"]
    lines += [
        "",
        "## Reviews",
        "",
        f"- project reviews: {rv['project_review_count']}, mean rating "
        f"{_num(rv['project_mean_rating'])}, fund share {_share(rv['project_fund_share'])}",
        f"- assessment reviews (labs' own pitches): {rv['assessment_review_count']}, mean score "
        f"{_num(rv['assessment_mean_score'])}",
        "",
        "## Run time from request to finish",
        "",
        f"- {hours['count']} finished; mean {_num(hours['mean'])} h, median {_num(hours['median'])} h",
        "",
    ]
    return "\n".join(lines)


async def _build(run_id: uuid.UUID) -> dict[str, Any]:
    async with get_session_factory()() as db:
        return await build_report(db, run_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, type=uuid.UUID, help="simulation run id")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--out", type=Path, help="a file outside the repository")
    args = parser.parse_args(argv)
    if args.out is not None and not out_path_allowed(args.out):
        print(f"refusing --out {args.out}: it is inside the repository", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(_build(args.run))
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    text = (
        json.dumps(report, indent=2, default=str)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.out is None:
        print(text)
    else:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
