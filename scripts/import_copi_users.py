"""Ingest a bundle produced by scripts/export_copi_users.py into this CoPI instance.

Idempotent: ORCID is the join key, so re-running updates in place rather than
duplicating. Source UUIDs are never reused — this instance mints its own.

Runs inside the app container of the TARGET instance:

    docker cp import_copi_users.py <app-container>:/app/scripts/
    docker exec <app-container> python scripts/import_copi_users.py --bundle /tmp/copiuserexport
    docker exec <app-container> python scripts/import_copi_users.py --bundle /tmp/copiuserexport --apply

Without --apply nothing is written; the script prints the plan and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from src.database import get_session_factory
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.services.profile_export import export_private_profile, export_profile_to_markdown

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("import_copi_users")

SUPPORTED_BUNDLE_FORMAT = 1
DATETIME_FIELDS = {"profile_generated_at"}


def _dt(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def verify_manifest(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text())
    fmt = manifest.get("bundle_format")
    if fmt != SUPPORTED_BUNDLE_FORMAT:
        raise SystemExit(f"Unsupported bundle_format {fmt} (this importer speaks {SUPPORTED_BUNDLE_FORMAT})")
    bad = []
    for rel, want in manifest["sha256"].items():
        path = bundle / rel
        if not path.exists():
            bad.append(f"{rel}: missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
            bad.append(f"{rel}: checksum mismatch")
    if bad:
        raise SystemExit("Bundle failed verification:\n  " + "\n  ".join(bad))
    logger.info(
        "Bundle verified: %d files, exported %s from %s (alembic %s)",
        len(manifest["sha256"]), manifest["exported_at"],
        manifest["source_host"], manifest["source_alembic_revision"],
    )
    return manifest


def _pub_key(p) -> tuple:
    """Dedupe key: PMID wins, then DOI, then normalised title."""
    get = p.get if isinstance(p, dict) else lambda k: getattr(p, k)
    if get("pmid"):
        return ("pmid", str(get("pmid")))
    if get("doi"):
        return ("doi", str(get("doi")).lower())
    return ("title", (get("title") or "").strip().lower())


async def ingest(bundle: Path, apply: bool, access_status: str, skip_private: bool,
                 skip_publications: bool, export_md: bool) -> None:
    manifest = verify_manifest(bundle)
    data = json.loads((bundle / "users.json").read_text())
    session_factory = get_session_factory()

    async with session_factory() as db:
        for rec in data["users"]:
            u = rec["user"]
            orcid = u["orcid"]
            user = (await db.execute(select(User).where(User.orcid == orcid))).scalar_one_or_none()

            if user is None:
                action = "CREATE"
                user = User(orcid=orcid, name=u["name"], access_status=access_status)
                db.add(user)
            else:
                action = "UPDATE"
            for field in ("name", "email", "institution", "department",
                          "email_notifications_enabled", "email_notification_frequency",
                          "onboarding_complete"):
                if u.get(field) is not None:
                    setattr(user, field, u[field])
            await db.flush()
            logger.info("%s user %s (%s) -> %s", action, u["name"], orcid, user.id)

            # ---- profile -----------------------------------------------------
            if rec["profile"]:
                profile = (
                    await db.execute(
                        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
                    )
                ).scalar_one_or_none()
                if profile is None:
                    profile = ResearcherProfile(user_id=user.id, profile_version=0)
                    db.add(profile)
                for field, value in rec["profile"].items():
                    setattr(profile, field, _dt(value) if field in DATETIME_FIELDS else value)
                if not skip_private and rec.get("private_profile_file"):
                    # newline="" disables universal-newline translation: without it
                    # Path.read_text() rewrites CRLF to LF and the imported private
                    # profile no longer matches the source byte-for-byte.
                    with open(bundle / rec["private_profile_file"], encoding="utf-8",
                              newline="") as fh:
                        profile.private_profile_md = fh.read().rstrip("\r\n")
                await db.flush()
                logger.info("  profile v%s, private_md=%s",
                            profile.profile_version,
                            "yes" if profile.private_profile_md else "no")
            else:
                profile = None

            # ---- publications -------------------------------------------------
            added = 0
            if not skip_publications:
                existing = {
                    _pub_key(p): p
                    for p in (
                        await db.execute(select(Publication).where(Publication.user_id == user.id))
                    ).scalars().all()
                }
                for p in rec["publications"]:
                    if _pub_key(p) in existing:
                        continue
                    db.add(Publication(user_id=user.id, **p))
                    added += 1
                await db.flush()
            logger.info("  publications: %d in bundle, %d new", len(rec["publications"]), added)

            # ---- agent registry -------------------------------------------------
            # Created as 'pending' with NO token: a Slack bot token is bound to the
            # source workspace. Provision this instance's own app via /admin/agents.
            if rec["agent"]:
                a = rec["agent"]
                agent = (
                    await db.execute(
                        select(AgentRegistry).where(AgentRegistry.agent_id == a["agent_id"])
                    )
                ).scalar_one_or_none()
                if agent is None:
                    agent = AgentRegistry(
                        agent_id=a["agent_id"], bot_name=a["bot_name"],
                        pi_name=a["pi_name"], role=a.get("role") or "pi",
                        user_id=user.id, status="pending",
                    )
                    db.add(agent)
                    logger.info("  agent %s created status=pending (no token — provision on this instance)",
                                a["agent_id"])
                else:
                    if agent.user_id is None:
                        agent.user_id = user.id
                    logger.info("  agent %s already exists (status=%s) — left as-is",
                                agent.agent_id, agent.status)
                await db.flush()

            # ---- rendered markdown for the agent runtime -------------------------
            if apply and export_md and profile and rec["agent"]:
                pubs = (
                    await db.execute(select(Publication).where(Publication.user_id == user.id))
                ).scalars().all()
                export_profile_to_markdown(user, profile, rec["agent"]["agent_id"], list(pubs))
                export_private_profile(user, profile, rec["agent"]["agent_id"])

        if apply:
            await db.commit()
            logger.info("COMMITTED. Source bundle: %s", manifest["exported_at"])
        else:
            await db.rollback()
            logger.info("DRY RUN — rolled back, nothing written. Re-run with --apply to commit.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--apply", action="store_true", help="commit; omit for a dry run")
    ap.add_argument("--access-status", default="pending", choices=["pending", "allowed", "denied"],
                    help="access_status for users this run CREATES (default: pending)")
    ap.add_argument("--skip-private", action="store_true", help="do not import private profiles")
    ap.add_argument("--skip-publications", action="store_true")
    ap.add_argument("--no-export-md", action="store_true",
                    help="skip rewriting profiles/{public,private}/*.md (useful when testing "
                         "against a scratch DB from a container whose profiles/ is bind-mounted)")
    args = ap.parse_args()
    asyncio.run(ingest(args.bundle, args.apply, args.access_status,
                       args.skip_private, args.skip_publications, not args.no_export_md))


if __name__ == "__main__":
    main()
