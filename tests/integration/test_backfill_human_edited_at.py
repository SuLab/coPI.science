"""The D55 human_edited_at backfill (migration 0062 and scripts/backfill_human_edited_at.py):
only newer web edits of the summary or tag sections stamp; it is idempotent and never moves
the value backward."""
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from src.models import ProfileRevision
from tests import factories

pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]
_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sql() -> str:
    return _load(_ROOT / "scripts/backfill_human_edited_at.py", "_bfh").HUMAN_EDITED_AT_BACKFILL_SQL


def _persona(summary: str, tags: str = "- organoids", header: str = "Jane Wang",
             pubs: str = "- Paper A.") -> str:
    return (f"# {header} Lab — Public Profile\n\n**PI:** {header}\n\n"
            f"## Research Summary\n\n{summary}\n\n## Key Methods and Technologies\n\n{tags}\n\n"
            f"## Recent Publications\n\n{pubs}\n")


async def _pi(db_session, generated_at=_T0):
    user = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=user, profile_generated_at=generated_at)
    agent = await factories.make_agent(db_session, user=user)
    return user, profile, agent


def _rev(agent, content, mechanism, at):
    return ProfileRevision(agent_registry_id=agent.id, profile_type="public", content=content,
                           mechanism=mechanism, created_at=at)


async def _stamp(db_session, profile):
    await db_session.execute(text(_sql()))
    await db_session.refresh(profile)
    return profile.human_edited_at


def test_the_migration_and_the_script_run_the_same_sql():
    mig = _load(_ROOT / "alembic/versions/0062_corpus_provenance_profile_drafts.py", "_m0062b")
    assert mig.HUMAN_EDITED_AT_BACKFILL_SQL == _sql()


async def test_backfill_stamps_only_newer_web_edits_of_summary_or_tags(db_session):
    user, profile, agent = await _pi(db_session)
    edit_at = _T0 + timedelta(days=2)
    db_session.add_all([
        _rev(agent, _persona("Generated."), "pipeline", _T0 - timedelta(minutes=1)),
        _rev(agent, _persona("Generated.", header="Jane Q. Wang"), "web", _T0 + timedelta(days=1)),
        _rev(agent, _persona("Edited by Jane."), "web", edit_at),
        _rev(agent, _persona("Edited by Jane.", pubs="- Paper B."), "pipeline", edit_at + timedelta(days=1)),
    ])
    await db_session.flush()
    assert await _stamp(db_session, profile) == edit_at


async def test_header_or_publication_only_changes_and_old_edits_do_not_stamp(db_session):
    user, profile, agent = await _pi(db_session)
    db_session.add_all([
        _rev(agent, _persona("Old edit."), "web", _T0 - timedelta(days=5)),
        _rev(agent, _persona("Old edit."), "pipeline", _T0 - timedelta(minutes=1)),
        _rev(agent, _persona("Old edit.", header="Jane Q. Wang"), "web", _T0 + timedelta(days=1)),
        _rev(agent, _persona("Old edit.", pubs="- New."), "web_impersonated", _T0 + timedelta(days=2)),
    ])
    await db_session.flush()
    assert await _stamp(db_session, profile) is None


async def test_a_never_generated_profile_counts_every_web_edit(db_session):
    user, profile, agent = await _pi(db_session, generated_at=None)
    at = _T0 + timedelta(days=3)
    db_session.add(_rev(agent, _persona("Hand-written."), "web_impersonated", at))
    await db_session.flush()
    assert await _stamp(db_session, profile) == at


async def test_backfill_is_idempotent_and_never_moves_backward(db_session):
    user, profile, agent = await _pi(db_session)
    edit_at = _T0 + timedelta(days=2)
    db_session.add_all([
        _rev(agent, _persona("Generated."), "pipeline", _T0),
        _rev(agent, _persona("Edited."), "web", edit_at),
    ])
    await db_session.flush()
    assert await _stamp(db_session, profile) == edit_at
    assert await _stamp(db_session, profile) == edit_at
    later = edit_at + timedelta(days=7)
    profile.human_edited_at = later
    await db_session.flush()
    assert await _stamp(db_session, profile) == later
