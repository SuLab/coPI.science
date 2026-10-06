"""scripts/persona_file_audit.py (spec 2026-10-05 §6.4 one-off, §9 row 4)."""
import pytest

from scripts.persona_file_audit import run
from src.services import persona_lifecycle, profile_export
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    public, orphaned = tmp_path / "public", tmp_path / "orphaned"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", public)
    monkeypatch.setattr(persona_lifecycle, "ORPHANED_DIR", orphaned)
    public.mkdir()
    return public, orphaned


def _file(public, stem, *, summary=True):
    body = "# X Lab\n\n" + ("## Research Summary\n\nWords.\n" if summary else "")
    (public / f"{stem}.md").write_text(body, encoding="utf-8")


async def test_check_passes_when_every_lab_has_a_summary_file(db_session, dirs, capsys):
    public, _ = dirs
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    await factories.make_agent(db_session, user=pi, agent_id="auditok", status="active")
    _file(public, "auditok")
    _file(public, "blackbird")
    assert await run(db_session, "check", apply=False, keep=frozenset({"blackbird"})) == 0
    out = capsys.readouterr().out
    assert "PASS persona_files" in out and "PASS orphans" in out and "KEPT" in out


async def test_check_fails_on_a_missing_file_and_an_orphan(db_session, dirs, capsys):
    public, _ = dirs
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    await factories.make_agent(db_session, user=pi, agent_id="auditmiss", status="pending")
    _file(public, "nobodyhere")
    assert await run(db_session, "check", apply=False, keep=frozenset()) == 1
    out = capsys.readouterr().out
    assert "MISSING auditmiss" in out and "ORPHAN" in out and "nobodyhere.md" in out


async def test_archive_orphans_moves_only_files_without_an_agent_row(db_session, dirs):
    public, orphaned = dirs
    for status in ("active", "pending", "suspended"):
        await factories.make_agent(db_session, agent_id=f"keep{status}", status=status)
        _file(public, f"keep{status}")
    _file(public, "orphanone")
    assert await run(db_session, "archive-orphans", apply=False, keep=frozenset()) == 0
    assert (public / "orphanone.md").exists()             # a dry run moves nothing
    assert await run(db_session, "archive-orphans", apply=True, keep=frozenset()) == 0
    assert not (public / "orphanone.md").exists()
    assert len(list(orphaned.glob("orphanone.*.md"))) == 1
    assert all((public / f"keep{s}.md").exists() for s in ("active", "pending", "suspended"))


async def test_blackbird_is_kept_by_default(db_session, dirs):
    public, _ = dirs
    _file(public, "blackbird")
    await run(db_session, "archive-orphans", apply=True, keep=frozenset({"blackbird"}))
    assert (public / "blackbird.md").exists()


async def test_reexport_missing_writes_the_file(db_session, dirs, capsys):
    public, _ = dirs
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, research_summary="Audit words.")
    await factories.make_agent(db_session, user=pi, agent_id="auditre", status="active")
    assert await run(db_session, "reexport-missing", apply=True, keep=frozenset()) == 0
    assert "Audit words." in (public / "auditre.md").read_text(encoding="utf-8")
    assert "REEXPORTED auditre" in capsys.readouterr().out
