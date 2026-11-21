"""Admin actions for the Scripps landscaping test stack (spec 2026-11-20 §6.6, §7, C7, C9).

    python scripts/scripps_admin.py grant --orcid <iD> --role <role> --name <name>
    python scripts/scripps_admin.py start-backfill
    python scripts/scripps_admin.py override-epoch --epoch <uuid> --actor-orcid <iD> --reason <text>
    python scripts/scripps_admin.py disable-source <name> --reason <text>
    python scripts/scripps_admin.py batch-probe

Run inside a one-off container of the stack's app image, so the stack's database and keys are
used; the website's login is ORCID only, so these stand in for the admin controls. Each command
commits its own change and exits 0; a refused one (bad ORCID iD, role, name, reason, an
unknown or non-admin actor, an unknown source, a backfill the route would refuse, an epoch that is not held or a candidate)
writes nothing, prints its message on stderr and exits 2.

Commands:
  grant           create or update an allowed account by ORCID iD; <role> is admin, manager
                  or reviewer
  start-backfill  move ``landscape_phase`` from ``pilot`` to ``backfill`` with the route's checks
  override-epoch  record an admin's override of a held epoch (``epochs.override``)
  disable-source  set a ``landscape_sources`` row's ``enabled`` to false
  batch-probe     whether the key's account accepts Message Batches: prints ``api <batch id>``
                  (the one-request batch is cancelled) or ``sync <error>``
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs, intake
from src.models import User
from src.models.landscape import LandscapeSource
from src.models.user import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from src.routers.landscape import begin_backfill
from src.services.pi_onboarding import validate_orcid
from src.services.session_epoch import bump_session_epoch

#: The roles ``grant`` gives: the owner is ``admin``, the remote colleague ``manager`` (C7).
GRANT_ROLES = (USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER)
#: The ``custom_id`` of ``batch_probe``'s single request.
BATCH_PROBE_ID = "scripps-batch-probe"
#: Status codes by which an account refuses Message Batches; ``batch_probe`` reads them as
#: "use sync mode".
_BATCH_REFUSED = frozenset({400, 403, 404})


def _session_factory():
    """The application's session factory: a seam for tests."""
    from src.database import get_session_factory
    return get_session_factory()


def _required(text: str, what: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{what} is required")
    return text


async def grant(db: AsyncSession, *, orcid: str, role: str, name: str) -> User:
    """Create or update the account for ``orcid`` as an allowed ``role`` and commit.

    A new account gets ``name``, ``access_status="allowed"`` and ``onboarding_complete`` set
    only for an admin (an admin is otherwise sent to onboarding, which needs a worker this
    stack does not run; spec §7). An existing account keeps its name, gets the role and
    ``allowed``, gets ``onboarding_complete=True`` when made admin (never reset to False), and
    has its sessions ended (``bump_session_epoch``) when its role changed. Raises ValueError,
    writing nothing, for an invalid ORCID iD, a role outside ``GRANT_ROLES`` or a blank name."""
    canonical = validate_orcid(orcid)
    if role not in GRANT_ROLES:
        raise ValueError(f"role must be one of {', '.join(GRANT_ROLES)}, not {role!r}")
    name = _required(name, "A name")
    user = await db.scalar(select(User).where(User.orcid == canonical))
    if user is None:
        user = User(
            orcid=canonical, name=name, user_role=role, access_status="allowed",
            onboarding_complete=role == USER_ROLE_ADMIN,
        )
        db.add(user)
        await db.commit()
        return user
    role_changed = user.user_role != role
    user.user_role = role
    user.access_status = "allowed"
    if role == USER_ROLE_ADMIN:
        user.onboarding_complete = True
    if role_changed:
        await bump_session_epoch(db, user.id)
    await db.commit()
    return user


async def start_backfill(db: AsyncSession) -> None:
    """``begin_backfill`` (the route's checks; raises its ``BackfillRefused``, a ValueError),
    then commit."""
    await begin_backfill(db)
    await db.commit()


async def override_epoch(
    db: AsyncSession, *, epoch_id: uuid.UUID, actor_orcid: str, reason: str
) -> None:
    """Override the held (or not yet regressed) epoch ``epoch_id`` as the account with
    ``actor_orcid``, through ``epochs.override``, which commits. The actor must be an admin, as
    for the route this stands in for. Raises ValueError for a blank reason, an invalid,
    unknown or non-admin actor iD, or an epoch ``epochs.override`` refuses."""
    reason = _required(reason, "A reason")
    canonical = validate_orcid(actor_orcid)
    actor = await db.scalar(select(User).where(User.orcid == canonical))
    if actor is None:
        raise ValueError(f"no account has the ORCID iD {canonical}")
    if actor.user_role != USER_ROLE_ADMIN:
        raise ValueError(f"{canonical} is {actor.user_role}; only an admin overrides an epoch")
    await epochs.override(db, epoch_id, actor_id=actor.id, reason=reason)


async def disable_source(db: AsyncSession, *, source: str, reason: str) -> None:
    """Set the ``landscape_sources`` row of ``source`` to disabled and commit, after
    ``intake.ensure_sources`` (which commits the missing rows), so it works before the
    worker's first start. Created that way, the first row's time is the pilot's anchor
    (``intake._pilot_anchor``), so run it before the worker only on the day the worker
    starts. Raises ValueError for a source outside ``intake.SOURCES`` or a blank reason.
    Prints ``source <name> disabled: <reason>`` for the run log."""
    reason = _required(reason, "A reason")
    if source not in intake.SOURCES:
        raise ValueError(f"unknown source {source!r}; known: {', '.join(intake.SOURCES)}")
    await intake.ensure_sources(db)
    row = await db.scalar(select(LandscapeSource).where(LandscapeSource.source == source))
    row.enabled = False
    await db.commit()
    print(f"source {source} disabled: {reason}")


async def batch_probe(client, model: str) -> tuple[str, str]:
    """Create a one-request Message Batch (``max_tokens`` 1, custom id ``BATCH_PROBE_ID``) on
    ``model`` and cancel it: ``("api", batch_id)``. An ``anthropic.APIStatusError`` with status
    400, 403 or 404 gives ``("sync", message)``; any other error RAISES."""
    try:
        batch = await client.messages.batches.create(requests=[{
            "custom_id": BATCH_PROBE_ID,
            "params": {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ok"}],
            },
        }])
    except anthropic.APIStatusError as exc:
        if exc.status_code in _BATCH_REFUSED:
            return "sync", str(exc)
        raise
    await client.messages.batches.cancel(batch.id)
    return "api", batch.id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grant")
    g.add_argument("--orcid", required=True)
    g.add_argument("--role", required=True, choices=GRANT_ROLES)
    g.add_argument("--name", required=True)
    sub.add_parser("start-backfill")
    o = sub.add_parser("override-epoch")
    o.add_argument("--epoch", required=True, type=uuid.UUID)
    o.add_argument("--actor-orcid", required=True)
    o.add_argument("--reason", required=True)
    d = sub.add_parser("disable-source")
    d.add_argument("name")
    d.add_argument("--reason", required=True)
    sub.add_parser("batch-probe")
    return parser


async def _batch_probe_command() -> None:
    from src.config import get_settings
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    mode, detail = await batch_probe(client, settings.landscape_screen_model)
    print(f"{mode} {detail}")


async def _run(args: argparse.Namespace) -> None:
    if args.command == "batch-probe":
        await _batch_probe_command()
        return
    async with _session_factory()() as db:
        if args.command == "grant":
            user = await grant(db, orcid=args.orcid, role=args.role, name=args.name)
            print(f"{user.orcid} is {user.user_role}, allowed")
        elif args.command == "start-backfill":
            await start_backfill(db)
            print("landscape_phase is backfill")
        elif args.command == "override-epoch":
            await override_epoch(
                db, epoch_id=args.epoch, actor_orcid=args.actor_orcid, reason=args.reason
            )
            print(f"epoch {args.epoch} overridden")
        elif args.command == "disable-source":
            await disable_source(db, source=args.name, reason=args.reason)


def main(argv: list[str] | None = None) -> int:
    """Run one command: 0 on success, 2 on a ValueError (message on stderr). Bad arguments
    exit 2 through argparse."""
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
