"""CoPI CLI — seed-profile, seed-profiles, admin:grant, admin:revoke."""

import asyncio

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="copi", help="CoPI / LabAgent management CLI")
console = Console()


def _run(coro):
    """Run an async coroutine from a synchronous context."""
    return asyncio.run(coro)


async def _get_db():
    """Get an async database session."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.config import get_settings
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def _seed_one_orcid(orcid: str, run_pipeline: bool = True) -> None:
    """Create user record and optionally enqueue profile generation for one ORCID."""
    from sqlalchemy import select

    from src.models import Job, User
    from src.services.orcid import fetch_orcid_profile

    engine, factory = await _get_db()
    async with factory() as db:
        # Check if user already exists
        result = await db.execute(select(User).where(User.orcid == orcid))
        user = result.scalar_one_or_none()

        if user:
            console.print(f"[yellow]User with ORCID {orcid} already exists: {user.name}[/yellow]")
        else:
            # Fetch ORCID profile
            console.print(f"Fetching ORCID profile for {orcid}...")
            try:
                profile_data = await fetch_orcid_profile(orcid)
            except Exception as exc:
                console.print(f"[red]Failed to fetch ORCID profile: {exc}[/red]")
                profile_data = {"name": orcid, "orcid": orcid}

            user = User(
                orcid=orcid,
                name=profile_data.get("name", orcid),
                email=profile_data.get("email"),
                institution=profile_data.get("institution"),
                department=profile_data.get("department"),
            )
            db.add(user)
            await db.flush()
            console.print(f"[green]Created user: {user.name} ({orcid})[/green]")

        if run_pipeline:
            job = Job(
                type="generate_profile",
                user_id=user.id,
                payload={"user_id": str(user.id), "orcid": orcid},
            )
            db.add(job)
            console.print(f"[green]Enqueued profile generation job for {user.name}[/green]")

        await db.commit()
    await engine.dispose()


@app.command(name="seed-profile")
def seed_profile(
    orcid: str = typer.Option(..., "--orcid", help="ORCID ID (format: 0000-0000-0000-0000)"),
    no_pipeline: bool = typer.Option(False, "--no-pipeline", help="Skip profile generation"),
):
    """Create a user record and enqueue profile generation for one ORCID."""
    _run(_seed_one_orcid(orcid, run_pipeline=not no_pipeline))


@app.command(name="seed-profiles")
def seed_profiles(
    file: str = typer.Option(..., "--file", help="Text file with one ORCID per line"),
    no_pipeline: bool = typer.Option(False, "--no-pipeline", help="Skip profile generation"),
):
    """Create user records for all ORCIDs in a file."""
    import pathlib
    path = pathlib.Path(file)
    if not path.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    orcids = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    console.print(f"Processing {len(orcids)} ORCIDs...")

    for orcid in orcids:
        if not orcid or orcid.startswith("#"):
            continue
        _run(_seed_one_orcid(orcid, run_pipeline=not no_pipeline))


@app.command(name="admin:grant")
def admin_grant(
    orcid: str = typer.Option(..., "--orcid", help="ORCID ID to grant admin to"),
):
    """Grant admin privileges to a user by ORCID."""
    async def _grant() -> bool:
        from sqlalchemy import select

        from src.models import USER_ROLE_ADMIN, User
        engine, factory = await _get_db()
        try:
            async with factory() as db:
                result = await db.execute(select(User).where(User.orcid == orcid))
                user = result.scalar_one_or_none()
                if not user:
                    console.print(f"[red]User with ORCID {orcid} not found[/red]")
                    return False
                user.user_role = USER_ROLE_ADMIN
                await db.commit()
                console.print(f"[green]Granted admin to {user.name} ({orcid})[/green]")
                return True
        finally:
            await engine.dispose()

    if not _run(_grant()):
        # Exit nonzero so a provisioning script checking $? can tell a typo'd ORCID
        # from a successful grant. The message above says which; a bare exit code
        # with no explanation would be worse than the silent success it replaces.
        raise typer.Exit(1)


@app.command(name="admin:revoke")
def admin_revoke(
    orcid: str = typer.Option(..., "--orcid", help="ORCID ID to revoke admin from"),
):
    """Revoke admin privileges from a user by ORCID."""
    async def _revoke() -> bool:
        from sqlalchemy import select

        from src.models import USER_ROLE_PI, User
        engine, factory = await _get_db()
        try:
            async with factory() as db:
                result = await db.execute(select(User).where(User.orcid == orcid))
                user = result.scalar_one_or_none()
                if not user:
                    console.print(f"[red]User with ORCID {orcid} not found[/red]")
                    return False
                user.user_role = USER_ROLE_PI
                await db.commit()
                console.print(f"[green]Revoked admin from {user.name} ({orcid})[/green]")
                return True
        finally:
            await engine.dispose()

    if not _run(_revoke()):
        raise typer.Exit(1)


@app.command(name="list-users")
def list_users():
    """List all users in the database."""
    async def _list():
        from sqlalchemy import select

        from src.models import User
        engine, factory = await _get_db()
        async with factory() as db:
            result = await db.execute(select(User).order_by(User.created_at.desc()))
            users = result.scalars().all()

        table = Table(title="Users")
        table.add_column("Name", style="cyan")
        table.add_column("ORCID", style="green")
        table.add_column("Institution")
        table.add_column("Role", style="red")
        table.add_column("Onboarded")

        for user in users:
            table.add_row(
                user.name,
                user.orcid,
                user.institution or "—",
                user.user_role,
                "Yes" if user.onboarding_complete else "No",
            )
        console.print(table)
        await engine.dispose()

    _run(_list())


@app.command(name="regenerate-profiles")
def regenerate_profiles():
    """Enqueue profile regeneration jobs for all users with an ORCID."""
    async def _regenerate():
        from sqlalchemy import select

        from src.models import Job, User
        engine, factory = await _get_db()
        async with factory() as db:
            result = await db.execute(select(User).where(User.orcid.isnot(None)))
            users = result.scalars().all()
            count = 0
            for user in users:
                job = Job(
                    type="generate_profile",
                    user_id=user.id,
                    payload={"user_id": str(user.id), "orcid": user.orcid},
                )
                db.add(job)
                count += 1
                console.print(f"[green]Enqueued regeneration for {user.name} ({user.orcid})[/green]")
            await db.commit()
            console.print(f"\n[bold green]Enqueued {count} profile regeneration jobs.[/bold green]")
        await engine.dispose()

    _run(_regenerate())


@app.command(name="backfill-profile-revisions")
def backfill_profile_revisions():
    """Create initial ProfileRevision rows from existing profile files on disk."""
    async def _backfill():
        from pathlib import Path

        from sqlalchemy import select

        from src.models import AgentRegistry
        from src.services.profile_versioning import create_revision, latest_revision

        engine, factory = await _get_db()
        async with factory() as db:
            # Load all agents
            result = await db.execute(select(AgentRegistry))
            agents = {a.agent_id: a for a in result.scalars().all()}

            count = 0
            for profile_type, subdir in [
                ("public", "profiles/public"),
                ("memory", "profiles/memory"),
            ]:
                dirpath = Path(subdir)
                if not dirpath.exists():
                    continue
                for filepath in sorted(dirpath.glob("*.md")):
                    agent_id = filepath.stem
                    agent_reg = agents.get(agent_id)
                    if not agent_reg:
                        console.print(
                            f"[yellow]Skipping {filepath} — no agent '{agent_id}' in registry[/yellow]"
                        )
                        continue
                    content = filepath.read_text(encoding="utf-8")
                    if not content.strip():
                        continue
                    # `create_revision` also refuses to duplicate an unchanged body, so
                    # this is not what makes the command idempotent. It is what makes
                    # the command *say so*: without it the tally below would report
                    # creating rows a re-run did not create.
                    previous = await latest_revision(
                        db, agent_registry_id=agent_reg.id, profile_type=profile_type
                    )
                    if previous is not None and previous.content == content:
                        console.print(
                            f"[yellow]Unchanged {profile_type} profile for {agent_id} "
                            f"— no new revision[/yellow]"
                        )
                        continue
                    await create_revision(
                        db,
                        agent_registry_id=agent_reg.id,
                        profile_type=profile_type,
                        content=content,
                        mechanism="pipeline",
                        change_summary="Initial backfill from existing file",
                    )
                    count += 1
                    console.print(f"[green]Backfilled {profile_type} revision for {agent_id}[/green]")

            await db.commit()
            console.print(f"\n[bold green]Created {count} profile revisions.[/bold green]")
        await engine.dispose()

    _run(_backfill())


if __name__ == "__main__":
    app()
