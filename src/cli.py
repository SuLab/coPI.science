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


if __name__ == "__main__":
    app()
