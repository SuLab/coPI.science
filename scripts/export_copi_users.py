"""Export selected users (profile + publications + agent metadata) to a portable bundle.

Produces a directory another CoPI instance can ingest with
``scripts/import_copi_users.py``. Deliberately NOT a pg_dump: source UUIDs are
recorded for traceability but never reused, so the bundle survives schema drift
between instances and cannot collide with rows already on the target. ORCID is
the join key (``users_orcid_key`` is already unique).

Secrets are never written. ``AgentRegistry.slack_bot_token`` is excluded by
design — a bot token is bound to the source Slack workspace and is useless on
the target, which must provision its own app. Same convention as
scripts/export_agent_roster.py.

Run inside the app container (scripts/ is not mounted, so copy it in first):

    docker cp scripts/export_copi_users.py copi-python-app-1:/app/scripts/
    docker exec copi-python-app-1 python scripts/export_copi_users.py \\
        --orcid 0000-0003-1636-7766 --orcid 0000-0001-9309-6141 \\
        --out /tmp/copiuserexport
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from src.config import get_settings
from src.database import get_session_factory
from src.models import AgentRegistry, Cohort, CohortMembership, Publication, ResearcherProfile, User

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("export_copi_users")

BUNDLE_FORMAT = 1

# Columns carried per table. Anything not listed is intentionally dropped:
#   users:    id / created_at / updated_at  -> target generates its own
#             is_admin / claimed_at / last_login_at / access_status
#                                          -> account state is the target's call
#   profiles: id / user_id / created_at / updated_at -> re-keyed on import
#             private_profile_seed         -> an unreviewed LLM draft, see README
#   agents:   slack_bot_token              -> secret, workspace-bound
USER_FIELDS = [
    "name", "email", "orcid", "institution", "department",
    "email_notifications_enabled", "email_notification_frequency",
    "onboarding_complete",
]
PROFILE_FIELDS = [
    "research_summary", "techniques", "experimental_models", "disease_areas",
    "key_targets", "keywords", "grant_titles", "profile_version",
    "profile_generated_at", "raw_abstracts_hash", "synthesis_validated",
    "evidence_pmid_count", "evidence_pub_count",
]
PUB_FIELDS = [
    "pmid", "pmcid", "doi", "title", "abstract", "journal", "year",
    "author_position", "methods_text",
]
AGENT_FIELDS = ["agent_id", "bot_name", "pi_name", "role"]


def _plain(value):
    """JSON-safe scalar: datetimes to ISO-8601, enums to their value."""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, int, bool)):
        return value.value
    return value


def _row(obj, fields: list[str]) -> dict:
    return {f: _plain(getattr(obj, f)) for f in fields}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


README_TEMPLATE = """# CoPI user transfer bundle

You are reading this because you were asked to ingest a set of PIs exported from
another CoPI instance. Everything you need is in this directory — you do not need
the source repo.

## What is in here

{provenance}{roster}

| File | What it is |
|---|---|
| `manifest.json` | Provenance + a sha256 for every other file. The importer verifies these. |
| `users.json` | The data: users, researcher_profiles, publications, agent metadata. |
| `import_copi_users.py` | The idempotent importer. Run it inside this instance's app container. |
| `private/*.md` | PI-authored private profiles. Sensitive — see "Private profiles" below. |
| `profiles/public/*.md` | The rendered public profiles, for eyeballing. Not read on import. |

## Ingest

1. **Find the app container** on this instance:

   ```bash
   docker ps --format '{{{{.Names}}}}' | grep app
   ```

   Everything below writes `<app-container>`; substitute the real name. If the
   instance uses a split compose file set, no compose command is needed here —
   these are plain `docker exec` calls against a running container.

2. **Copy the bundle in.** `scripts/` is typically not bind-mounted, so copy both
   the data and the importer:

   ```bash
   docker cp {bundle_name} <app-container>:/tmp/{bundle_name}
   docker exec <app-container> mkdir -p /app/scripts
   docker cp {bundle_name}/import_copi_users.py <app-container>:/app/scripts/
   ```

3. **Dry run first.** This verifies checksums, prints exactly what would change,
   and rolls back. Nothing is written:

   ```bash
   docker exec -w /app <app-container> \\
       python scripts/import_copi_users.py --bundle /tmp/{bundle_name}
   ```

   Read the output. `CREATE user` means a new row; `UPDATE user` means an ORCID
   already present here and its fields will be overwritten from the bundle. If you
   see UPDATE and did not expect it, stop and ask the operator before applying.

4. **Apply:**

   ```bash
   docker exec -w /app <app-container> \\
       python scripts/import_copi_users.py --bundle /tmp/{bundle_name} --apply
   ```

   Useful flags: `--access-status allowed` (grant access to users this run creates;
   default is `pending`), `--skip-private`, `--skip-publications`, `--no-export-md`
   (skip rewriting `profiles/{{public,private}}/*.md` — use this if you are rehearsing
   against a scratch database from a container whose `profiles/` is bind-mounted,
   or the rehearsal will overwrite the live instance's profile files).

5. **Verify:**

   ```bash
   docker exec <postgres-container> psql -U copi -d copi -c "
   SELECT u.name, u.orcid, u.access_status,
          (SELECT count(*) FROM publications p WHERE p.user_id=u.id) AS pubs,
          length(rp.research_summary) AS summary_len, rp.profile_version
   FROM users u LEFT JOIN researcher_profiles rp ON rp.user_id=u.id
   WHERE u.orcid IN ({orcid_sql});"
   ```

   Expected: {expected}

## What is deliberately NOT in the bundle

- **Slack bot tokens.** A token is bound to the source Slack workspace and is
  useless here. Agent rows import as `status='pending'` with no token. To bring a
  bot online on this instance, provision a new Slack app via **/admin/agents ->
  the pending agent -> Provision**, then **Approve & Activate**. A running
  simulation picks it up on its next roster sync (~30s); no restart needed.
- **Account state** — `is_admin`, `claimed_at`, `last_login_at`, `access_status`.
  Whether these PIs have access to *this* instance is this instance's decision,
  so imported users default to `access_status='pending'`.
- **Source UUIDs.** This instance mints its own. ORCID is the join key, which is
  what makes re-running the importer safe.
- **`private_profile_seed`.** See below.

## Private profiles

`private/<agent_id>.md` is private guidance the PI wrote themselves. It is read by
that PI's agent and is not public. Treat it as confidential: do not paste it into
a channel, a proposal, or any other instance. If the operator does not want it,
run the importer with `--skip-private` and `rm -rf private/`.

Where a PI had only an unreviewed LLM-generated draft (`private_profile_seed`)
rather than a saved private profile, **that draft was intentionally excluded** —
importing it would present synthesized text as the PI's own words. Those agents
start with no private instructions, which is the correct state until the PI
writes one during onboarding.{seed_note}

## Re-running

The importer is idempotent. Publications dedupe on PMID, then DOI, then
normalised title. Profiles and users upsert on ORCID. Existing agent rows are
left alone — the importer never overwrites a status or a token that is already
here.
"""


def _readme(records: list[dict], skipped_seeds: list[str], bundle_name: str,
            cohort: str | None) -> str:
    roster_lines = ["| PI | ORCID | Agent | Publications | Private profile |",
                    "|---|---|---|---|---|"]
    expected = []
    for r in records:
        u = r["user"]
        agent = r["agent"]["agent_id"] if r["agent"] else "—"
        roster_lines.append(
            f"| {u['name']} | {u['orcid']} | `{agent}` | {len(r['publications'])} | "
            f"{'yes' if r['private_profile_file'] else 'no'} |"
        )
        expected.append(f"{u['name']} with {len(r['publications'])} publications")
    seed_note = ""
    if skipped_seeds:
        seed_note = ("\n\nExcluded unreviewed seeds in this bundle: "
                     + ", ".join(f"`{s}`" for s in skipped_seeds) + ".")
    return README_TEMPLATE.format(
        roster="\n".join(roster_lines),
        bundle_name=bundle_name,
        provenance=(f"These are the members of the `{cohort}` cohort on the source "
                    f"instance. Cohort *membership itself is not transferred* — only the "
                    f"users, profiles and publications.\n\n" if cohort else ""),
        orcid_sql=", ".join(f"'{r['user']['orcid']}'" for r in records),
        expected="; ".join(expected) + ".",
        seed_note=seed_note,
    )


async def resolve_cohort(db, cohort_name: str) -> list[str]:
    """Cohort membership is keyed by agent_id, not user_id. Resolve to ORCIDs.

    Members without an AgentRegistry row or without a linked user are skipped
    with a warning: service bots such as ``grantbot`` sit in cohorts but are not
    people and have nothing to export.
    """
    cohort = (
        await db.execute(select(Cohort).where(Cohort.name == cohort_name))
    ).scalar_one_or_none()
    if cohort is None:
        names = (await db.execute(select(Cohort.name).order_by(Cohort.name))).scalars().all()
        raise SystemExit(f"No cohort named {cohort_name!r}. Known cohorts: {', '.join(names)}")

    agent_ids = (
        await db.execute(
            select(CohortMembership.agent_id)
            .where(CohortMembership.cohort_id == cohort.id)
            .order_by(CohortMembership.agent_id)
        )
    ).scalars().all()

    orcids, skipped = [], []
    for agent_id in agent_ids:
        orcid = (
            await db.execute(
                select(User.orcid)
                .join(AgentRegistry, AgentRegistry.user_id == User.id)
                .where(AgentRegistry.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        if orcid is None:
            skipped.append(agent_id)
        else:
            orcids.append(orcid)

    logger.info("Cohort %r: %d members -> %d exportable users", cohort_name, len(agent_ids), len(orcids))
    if skipped:
        logger.warning("Skipped %d cohort member(s) with no linked user: %s",
                       len(skipped), ", ".join(skipped))
    return orcids


async def build_bundle(orcids: list[str], out: Path, cohort: str | None = None) -> None:
    settings = get_settings()
    session_factory = get_session_factory()

    out.mkdir(parents=True, exist_ok=True)
    (out / "private").mkdir(exist_ok=True)
    (out / "profiles" / "public").mkdir(parents=True, exist_ok=True)

    records = []
    private_written: list[str] = []
    skipped_seeds: list[str] = []

    async with session_factory() as db:
        skipped_members: list[str] = []
        if cohort:
            resolved = await resolve_cohort(db, cohort)
            orcids = list(dict.fromkeys(resolved + orcids))
        if not orcids:
            raise SystemExit("Nothing to export: no ORCIDs resolved")

        for orcid in orcids:
            user = (
                await db.execute(select(User).where(User.orcid == orcid))
            ).scalar_one_or_none()
            if user is None:
                logger.error("No user with ORCID %s — aborting, bundle would be incomplete", orcid)
                raise SystemExit(1)

            profile = (
                await db.execute(
                    select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
                )
            ).scalar_one_or_none()
            pubs = (
                (await db.execute(select(Publication).where(Publication.user_id == user.id)))
                .scalars()
                .all()
            )
            agent = (
                await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user.id))
            ).scalar_one_or_none()

            agent_id = agent.agent_id if agent else None
            rec = {
                "source_user_id": str(user.id),
                "user": _row(user, USER_FIELDS),
                "source_access_status": user.access_status,
                "profile": _row(profile, PROFILE_FIELDS) if profile else None,
                "publications": [_row(p, PUB_FIELDS) for p in pubs],
                "agent": _row(agent, AGENT_FIELDS) if agent else None,
                "private_profile_file": None,
            }

            # Private profile: only a PI-saved private_profile_md travels.
            # private_profile_seed is an unreviewed LLM draft — shipping it would
            # present synthesized text as the PI's own words on the target.
            if profile and profile.private_profile_md and agent_id:
                pf = out / "private" / f"{agent_id}.md"
                pf.write_text(profile.private_profile_md + "\n", encoding="utf-8")
                rec["private_profile_file"] = f"private/{agent_id}.md"
                private_written.append(agent_id)
            elif profile and profile.private_profile_seed:
                skipped_seeds.append(agent_id or orcid)

            # Rendered public profile, for human review of the bundle.
            if agent_id:
                src_md = Path("profiles/public") / f"{agent_id}.md"
                if src_md.exists():
                    (out / "profiles" / "public" / f"{agent_id}.md").write_text(
                        src_md.read_text(encoding="utf-8"), encoding="utf-8"
                    )

            records.append(rec)
            logger.info(
                "%s (%s): profile=%s pubs=%d agent=%s private_md=%s",
                user.name, orcid, "yes" if profile else "NO", len(pubs),
                agent_id or "none",
                "yes" if rec["private_profile_file"] else "no",
            )

        alembic_rev = (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()

    (out / "users.json").write_text(
        json.dumps({"bundle_format": BUNDLE_FORMAT, "users": records}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Ship the importer alongside the data so the target needs nothing from this
    # repo, and write the agent-facing instructions. Both land before the manifest
    # is computed, so the checksums cover the whole bundle including the importer.
    importer_src = Path(__file__).resolve().parent / "import_copi_users.py"
    (out / "import_copi_users.py").write_text(
        importer_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (out / "README.md").write_text(_readme(records, skipped_seeds, out.name, cohort), encoding="utf-8")

    files = sorted(
        str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and p.name != "manifest.json"
    )
    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_host": os.environ.get("COPI_SOURCE_HOST", socket.gethostname()),
        "source_alembic_revision": alembic_rev,
        "source_database": settings.database_url.rsplit("/", 1)[-1].split("?")[0],
        "counts": {
            "users": len(records),
            "profiles": sum(1 for r in records if r["profile"]),
            "publications": sum(len(r["publications"]) for r in records),
            "private_profiles": len(private_written),
        },
        "source_cohort": cohort,
        "orcids": orcids,
        "excluded": {
            "slack_bot_token": "secret; target must provision its own Slack app",
            "private_profile_seed": skipped_seeds,
            "account_state": ["is_admin", "claimed_at", "last_login_at", "access_status"],
        },
        "sha256": {f: _sha256(out / f) for f in files},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info("Bundle written to %s (%d files)", out, len(files) + 1)
    if skipped_seeds:
        logger.info("Excluded unreviewed private seeds for: %s", ", ".join(skipped_seeds))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orcid", action="append", default=[], help="repeatable")
    ap.add_argument("--cohort", help="export every member of this cohort (by cohorts.name)")
    ap.add_argument("--out", type=Path, default=Path("/tmp/copiuserexport"))
    args = ap.parse_args()
    if not args.orcid and not args.cohort:
        ap.error("give --cohort and/or at least one --orcid")
    asyncio.run(build_bundle(args.orcid, args.out, args.cohort))


if __name__ == "__main__":
    main()
