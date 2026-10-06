"""Reproduce one PI's profile synthesis without storing anything (spec 2026-10-05 §6.3,
"the plan reproduces Davis's synthesis").

Builds the synthesis context as profile_pipeline step 7 does, from what is already
stored: the PI's stored, non-excluded publications (the corpus as of the last run, before
any regeneration; nothing is re-resolved), tenure-filtered, the newest
``SYNTHESIS_WINDOW``, those with an abstract; methods from the rows' stored
``methods_text`` (the first 10 with text; PMC is not fetched); the persona's grant
sections. Then calls ``llm.synthesize_profile`` once (one Opus call, about $0.15) and
prints the parsed reply, the type check's verdict and ``_validate_profile``'s. On a parse
failure it prints the exception; the full model text is in llm's ERROR log line.

Usage::

    python scripts/synthesis_dry_run.py --orcid 0000-0000-0000-0000 [--show-context]

Exit 0 when the reply parsed and type-checked, 2 otherwise. The session is read-only
(``SET TRANSACTION READ ONLY``).
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, Publication, User  # noqa: E402
from src.services.grant_sections import load_grant_sections  # noqa: E402
from src.services.jhu_rules import export_tenure_start, tenure_filter  # noqa: E402
from src.services.llm import synthesize_profile  # noqa: E402
from src.services.profile_pipeline import (  # noqa: E402
    SYNTHESIS_WINDOW,
    SynthesisOutputError,
    _build_synthesis_context,
    _researcher_info,
    _validate_profile,
    checked_synthesis,
)
from src.services.tenure_scope import publication_in_use, publication_order_by  # noqa: E402

#: Stored methods sections offered, as step 5 fetches at most this many.
METHODS_LIMIT = 10


async def build_context(db: AsyncSession, user: User) -> str:
    """The step-7 context from the stored corpus (no resolve, no network, no write)."""
    agent_id = await db.scalar(
        select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user.id)
    )
    tenure_start = await export_tenure_start(db, user.id, agent_id)
    rows = (await db.execute(
        select(Publication)
        .where(Publication.user_id == user.id, publication_in_use())
        .order_by(*publication_order_by())
    )).scalars().all()
    by_pmid = {p.pmid: p for p in rows}
    records: list[dict[str, Any]] = [
        {"pmid": p.pmid, "pmcid": p.pmcid, "title": p.title, "abstract": p.abstract,
         "journal": p.journal, "year": p.year, "doi": p.doi, "id": str(p.id)}
        for p in rows
    ]
    in_tenure = tenure_filter(records, tenure_start)
    pubs = [r for r in in_tenure[:SYNTHESIS_WINDOW] if r.get("abstract")]
    methods: dict[str, str] = {}
    for rec in pubs:
        row = by_pmid.get(rec["pmid"])
        if row is not None and row.methods_text and len(methods) < METHODS_LIMIT:
            methods[rec["pmid"]] = row.methods_text
    grants = await load_grant_sections(db, user.id)
    return _build_synthesis_context(
        orcid_profile=_researcher_info(user, {}),
        grants=grants,
        publications=pubs,
        methods_by_pmid=methods,
    )


async def run(db: AsyncSession, orcid: str, *, show_context: bool = False) -> int:
    """Synthesize once for the PI with ``orcid`` and print the verdicts. Exit code: 0 when
    the reply parsed and type-checked, 2 otherwise (also for an unknown ORCID iD)."""
    user = await db.scalar(select(User).where(User.orcid == orcid))
    if user is None:
        print(f"No user with ORCID iD {orcid}")
        return 2
    context = await build_context(db, user)
    if show_context:
        print("=== context ===")
        print(context)
    try:
        raw = await synthesize_profile(context, user.name)
    except Exception as exc:  # noqa: BLE001 — the dry run reports every failure
        print(f"parsed: no ({exc!r}); the full model text is in the ERROR log above")
        return 2
    print("=== reply ===")
    print(json.dumps(raw, indent=2, ensure_ascii=False))
    print("parsed: yes")
    try:
        checked = checked_synthesis(raw)
    except SynthesisOutputError as exc:
        print(f"type check: FAIL ({exc})")
        return 2
    print("type check: ok")
    print(f"validation: {'ok' if _validate_profile(checked) else 'FAIL'}")
    return 0


async def _main(args: argparse.Namespace) -> int:
    async with get_session_factory()() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return await run(db, args.orcid, show_context=args.show_context)
        finally:
            await db.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--orcid", required=True, help="the PI's ORCID iD")
    parser.add_argument("--show-context", action="store_true",
                        help="print the synthesis context before calling the model")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
