"""CoPI CLI — seed-profile, seed-profiles, admin:grant, admin:revoke."""

import asyncio
import uuid

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
    async def _grant():
        from sqlalchemy import select
        from src.models import User
        engine, factory = await _get_db()
        async with factory() as db:
            result = await db.execute(select(User).where(User.orcid == orcid))
            user = result.scalar_one_or_none()
            if not user:
                console.print(f"[red]User with ORCID {orcid} not found[/red]")
                return
            user.is_admin = True
            await db.commit()
            console.print(f"[green]Granted admin to {user.name} ({orcid})[/green]")
        await engine.dispose()

    _run(_grant())


@app.command(name="admin:revoke")
def admin_revoke(
    orcid: str = typer.Option(..., "--orcid", help="ORCID ID to revoke admin from"),
):
    """Revoke admin privileges from a user by ORCID."""
    async def _revoke():
        from sqlalchemy import select
        from src.models import User
        engine, factory = await _get_db()
        async with factory() as db:
            result = await db.execute(select(User).where(User.orcid == orcid))
            user = result.scalar_one_or_none()
            if not user:
                console.print(f"[red]User with ORCID {orcid} not found[/red]")
                return
            user.is_admin = False
            await db.commit()
            console.print(f"[green]Revoked admin from {user.name} ({orcid})[/green]")
        await engine.dispose()

    _run(_revoke())


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
        table.add_column("Admin", style="red")
        table.add_column("Onboarded")

        for user in users:
            table.add_row(
                user.name,
                user.orcid,
                user.institution or "—",
                "Yes" if user.is_admin else "No",
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


@app.command(name="seed-pilot-labs")
def seed_pilot_labs(
    agent_id: str = typer.Option(None, "--agent-id", help="Seed only this agent (e.g. 'su'). Omit for all."),
    run_pipeline: bool = typer.Option(False, "--run-pipeline", help="Enqueue profile generation jobs"),
):
    """Create User + AgentRegistry rows for all pilot labs (or one), bypassing ORCID login."""
    from src.agent.simulation import PILOT_LABS

    labs = PILOT_LABS
    if agent_id:
        labs = [lab for lab in PILOT_LABS if lab["id"] == agent_id]
        if not labs:
            console.print(f"[red]Unknown agent-id '{agent_id}'. Valid IDs: {[l['id'] for l in PILOT_LABS]}[/red]")
            raise typer.Exit(1)

    async def _seed():
        from datetime import datetime, timezone
        from sqlalchemy import select
        from src.models import AgentRegistry, Job, User

        engine, factory = await _get_db()
        async with factory() as db:
            created_users = 0
            created_agents = 0
            skipped = 0

            for lab in labs:
                synthetic_orcid = f"synthetic:{lab['id']}"

                # --- User ---
                result = await db.execute(select(User).where(User.orcid == synthetic_orcid))
                user = result.scalar_one_or_none()

                if user:
                    console.print(f"[yellow]User already exists for {lab['pi']} ({synthetic_orcid})[/yellow]")
                else:
                    user = User(
                        orcid=synthetic_orcid,
                        name=lab["pi"],
                        access_status="allowed",
                        onboarding_complete=True,
                    )
                    db.add(user)
                    await db.flush()
                    created_users += 1
                    console.print(f"[green]Created user: {lab['pi']} ({synthetic_orcid})[/green]")

                # --- AgentRegistry ---
                result = await db.execute(
                    select(AgentRegistry).where(AgentRegistry.agent_id == lab["id"])
                )
                agent_reg = result.scalar_one_or_none()

                if agent_reg:
                    if agent_reg.user_id is None:
                        agent_reg.user_id = user.id
                        console.print(f"[yellow]{lab['name']} already exists — linked to user[/yellow]")
                    else:
                        console.print(f"[yellow]{lab['name']} already exists — skipping[/yellow]")
                    skipped += 1
                else:
                    agent_reg = AgentRegistry(
                        agent_id=lab["id"],
                        bot_name=lab["name"],
                        pi_name=lab["pi"],
                        user_id=user.id,
                        status="active",
                        approved_at=datetime.now(timezone.utc),
                    )
                    db.add(agent_reg)
                    created_agents += 1
                    console.print(f"[green]Created agent: {lab['name']} (status=active)[/green]")

                # --- Optional profile job ---
                if run_pipeline:
                    job = Job(
                        type="generate_profile",
                        user_id=user.id,
                        payload={"user_id": str(user.id), "orcid": synthetic_orcid},
                    )
                    db.add(job)
                    console.print(f"  [dim]Enqueued profile generation for {lab['pi']}[/dim]")

            await db.commit()
            console.print(
                f"\n[bold green]Done.[/bold green] "
                f"Created {created_users} user(s), {created_agents} agent(s), skipped {skipped}."
            )
        await engine.dispose()

    _run(_seed())


@app.command(name="backfill-profile-revisions")
def backfill_profile_revisions():
    """Create initial ProfileRevision rows from existing profile files on disk."""
    async def _backfill():
        from pathlib import Path
        from sqlalchemy import select
        from src.models import AgentRegistry
        from src.services.profile_versioning import create_revision

        engine, factory = await _get_db()
        async with factory() as db:
            # Load all agents
            result = await db.execute(select(AgentRegistry))
            agents = {a.agent_id: a for a in result.scalars().all()}

            count = 0
            for profile_type, subdir in [
                ("public", "profiles/public"),
                ("private", "profiles/private"),
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
