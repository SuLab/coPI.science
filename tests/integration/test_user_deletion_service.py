"""delete_user_account, DB phase (audit F1/F4/F5, decisions D1/D2/D4/D5)."""
import logging
import os

import pytest
from sqlalchemy import select

from src.models import (
    AccessAllowlist,
    AgentRegistry,
    AppSetting,
    PiDmMessage,
    ProfileRevision,
    User,
)
from src.services import user_deletion
from src.services.jhu_rules import TENURE_KEY_PREFIX
from src.services.user_deletion import delete_user_account
from tests import factories

pytestmark = pytest.mark.asyncio


async def _seed_pi_with_agent(db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, status="active")
    db_session.add(
        ProfileRevision(
            agent_registry_id=agent.id,
            profile_type="public",
            content="full profile snapshot",
            mechanism="pipeline",
        )
    )
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        PiDmMessage(
            simulation_run_id=run.id,
            agent_id=agent.agent_id,
            pi_user_id=f"local:{user.id}",
            direction="inbound",
            content="my unpublished idea",
            ts="1.0",
            posted_at=1.0,
        )
    )
    db_session.add(
        AppSetting(key=f"{TENURE_KEY_PREFIX}{user.id}", value='{"year": 2019}')
    )
    await db_session.flush()
    return user, agent


async def test_teardown_suspends_agent_and_purges(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    user, agent = await _seed_pi_with_agent(db_session)
    user_id, agent_pk, agent_slug = user.id, agent.id, agent.agent_id

    report = await delete_user_account(db_session, user)

    assert (await db_session.get(User, user_id)) is None
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    assert refreshed.status == "suspended"
    assert refreshed.user_id is None
    assert (
        await db_session.scalar(
            select(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent_pk
            )
        )
    ) is None
    assert (
        await db_session.scalar(
            select(PiDmMessage).where(PiDmMessage.agent_id == agent_slug)
        )
    ) is None
    assert (
        await db_session.scalar(
            select(AppSetting).where(
                AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}"
            )
        )
    ) is None
    assert report.agent_id == agent_slug
    assert report.agent_suspended is True
    assert report.revisions_deleted == 1
    assert report.dms_deleted == 1
    assert report.tenure_key_deleted is True


async def test_teardown_without_agent_still_purges_user_keyed_rows(db_session):
    user = await factories.make_user(db_session)
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        PiDmMessage(
            simulation_run_id=run.id,
            agent_id="ghost",
            pi_user_id=f"local:{user.id}",
            direction="inbound",
            content="x",
            ts="1.0",
            posted_at=1.0,
        )
    )
    await db_session.flush()
    user_id = user.id

    report = await delete_user_account(db_session, user)

    assert (await db_session.get(User, user_id)) is None
    assert report.agent_id is None
    assert report.dms_deleted == 1


async def test_allowlist_removed_only_when_asked(db_session):
    u1 = await factories.make_user(db_session)
    u2 = await factories.make_user(db_session)
    db_session.add(AccessAllowlist(orcid=u1.orcid))
    db_session.add(AccessAllowlist(orcid=u2.orcid))
    await db_session.flush()
    o1, o2 = u1.orcid, u2.orcid

    await delete_user_account(db_session, u1)  # default: keep
    await delete_user_account(db_session, u2, remove_from_allowlist=True)

    kept = await db_session.scalar(
        select(AccessAllowlist).where(AccessAllowlist.orcid == o1)
    )
    gone = await db_session.scalar(
        select(AccessAllowlist).where(AccessAllowlist.orcid == o2)
    )
    assert kept is not None
    assert gone is None


async def test_files_are_deleted_post_commit(db_session, monkeypatch, tmp_path):
    pub = tmp_path / "pub"
    mem = tmp_path / "mem"
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", pub)
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", mem)
    user, agent = await _seed_pi_with_agent(db_session)
    slug = agent.agent_id
    pub.mkdir()
    (pub / f"{slug}.md").write_text("profile")
    (mem / slug / "private").mkdir(parents=True)
    (mem / f"{slug}.md").write_text("legacy memory")
    (mem / slug / "public.md").write_text("memory")
    (mem / slug / "private" / "C1.md").write_text("private memory")

    report = await delete_user_account(db_session, user)

    assert not (pub / f"{slug}.md").exists()
    assert not (mem / f"{slug}.md").exists()
    assert not (mem / slug).exists()
    assert len(report.files_deleted) == 3
    assert report.errors == []


async def test_archived_working_memory_purged_on_deletion(db_session, monkeypatch, tmp_path):
    # --fresh archives whole memory trees under archive/<stamp>/<agent_id>;
    # a deleted PI's synthesized memory must not survive in those snapshots.
    pub = tmp_path / "pub"
    mem = tmp_path / "mem"
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", pub)
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", mem)
    user, agent = await _seed_pi_with_agent(db_session)
    slug = agent.agent_id
    (mem / "archive" / "20260828T120000Z" / slug).mkdir(parents=True)
    (mem / "archive" / "20260828T120000Z" / slug / "public.md").write_text(
        "archived memory"
    )
    (mem / "archive" / "20260828T120000Z" / "other").mkdir()

    report = await delete_user_account(db_session, user)

    assert not (mem / "archive" / "20260828T120000Z" / slug).exists()
    assert (mem / "archive" / "20260828T120000Z" / "other").exists()
    assert report.errors == []


async def test_unsafe_agent_id_skips_files_but_reports(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    user = await factories.make_user(db_session)
    await factories.make_agent(
        db_session, user=user, agent_id="../escape", status="active"
    )

    report = await delete_user_account(db_session, user)

    assert report.files_deleted == []
    assert any("unsafe agent_id" in e for e in report.errors)


async def test_token_revoked_and_cleared_on_success(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    calls = []

    async def _fake_revoke(token):
        calls.append(token)
        return True

    monkeypatch.setattr(
        "src.services.user_deletion.revoke_token_async", _fake_revoke
    )
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(
        db_session, user=user, status="active", slack_bot_token="xoxb-live"
    )
    agent_pk = agent.id

    report = await delete_user_account(db_session, user)

    assert calls == ["xoxb-live"]
    assert report.slack_token_revoked is True
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.slack_bot_token is None


async def test_token_kept_when_revocation_fails(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")

    async def _fake_revoke(token):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(
        "src.services.user_deletion.revoke_token_async", _fake_revoke
    )
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(
        db_session, user=user, status="active", slack_bot_token="xoxb-live"
    )
    agent_pk = agent.id

    report = await delete_user_account(db_session, user)

    assert report.slack_token_revoked is False
    assert any("slack revoke" in e for e in report.errors)
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    # Failure keeps the token recorded so an operator can revoke it manually.
    assert refreshed.slack_bot_token == "xoxb-live"
    # The agent is still suspended — the state that actually stops activity.
    assert refreshed.status == "suspended"


async def test_a_non_bot_token_is_never_revoked(db_session, monkeypatch, tmp_path):
    """auth.revoke kills whatever it is handed: an ``xoxp-`` value stored by
    mistake would revoke a HUMAN's user token, so it is skipped and reported."""
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    calls = []

    async def _fake_revoke(token):
        calls.append(token)
        return True

    monkeypatch.setattr(
        "src.services.user_deletion.revoke_token_async", _fake_revoke
    )
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(
        db_session, user=user, status="active", slack_bot_token="xoxp-human-secret"
    )
    agent_pk = agent.id

    report = await delete_user_account(db_session, user)

    assert calls == []
    assert report.slack_token_revoked is None
    assert any("not a bot token" in e for e in report.errors)
    # Redacted to its prefix: the secret never reaches the report.
    assert not any("human-secret" in e for e in report.errors)
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    await db_session.refresh(refreshed)
    assert refreshed.slack_bot_token == "xoxp-human-secret"
    assert refreshed.status == "suspended"


STAMP = "20261015T000000000000Z"


@pytest.fixture
def teardown_dirs(tmp_path, monkeypatch):
    dirs = {name: tmp_path / name for name in ("pub", "mem", "companies", "orphaned", "outside")}
    monkeypatch.setattr(user_deletion, "_PUBLIC_DIR", dirs["pub"])
    monkeypatch.setattr(user_deletion, "_MEMORY_DIR", dirs["mem"])
    monkeypatch.setattr(user_deletion, "_COMPANIES_DIR", dirs["companies"])
    monkeypatch.setattr(user_deletion, "_ORPHANED_DIR", dirs["orphaned"])
    for path in dirs.values():
        path.mkdir()
    return dirs


def _revision(agent, owner, content):
    return ProfileRevision(agent_registry_id=agent.id, profile_type="public", content=content,
                           mechanism="pipeline",
                           owner_user_id=owner.id if owner is not None else None)


async def _revision_ids(db, agent_pk):
    return set((await db.execute(select(ProfileRevision.id).where(
        ProfileRevision.agent_registry_id == agent_pk))).scalars())


async def _relinked_smith(db):
    """``smith`` was A's (revision r1), then linked to B (revision r2)."""
    a = await factories.make_user(db)
    b = await factories.make_user(db)
    smith = await factories.make_agent(db, user=a, agent_id="smith", status="active")
    r1 = _revision(smith, a, "A's persona")
    db.add(r1)
    await db.flush()
    smith.user_id = b.id
    await db.flush()
    r2 = _revision(smith, b, "B's persona")
    db.add(r2)
    await db.flush()
    return a, b, smith.id, r1.id, r2.id


async def test_deletion_purges_owned_revisions_on_a_relinked_agent(db_session, teardown_dirs):
    a, _b, smith_pk, _r1, r2 = await _relinked_smith(db_session)
    report = await delete_user_account(db_session, a)
    assert await _revision_ids(db_session, smith_pk) == {r2}
    assert report.revisions_deleted == 1


async def test_deletion_keeps_another_owners_revisions(db_session, teardown_dirs):
    _a, b, smith_pk, r1, _r2 = await _relinked_smith(db_session)
    await delete_user_account(db_session, b)
    assert await _revision_ids(db_session, smith_pk) == {r1}


async def test_deletion_purges_legacy_null_revisions_of_owned_agent(db_session, teardown_dirs):
    b = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=b, agent_id="smith", status="active")
    agent_pk = agent.id
    db_session.add(_revision(agent, None, "legacy persona"))
    await db_session.flush()
    await delete_user_account(db_session, b)
    assert await _revision_ids(db_session, agent_pk) == set()


async def test_deletion_purges_tagged_archived_personas(db_session, teardown_dirs):
    orphaned = teardown_dirs["orphaned"]
    a = await factories.make_user(db_session)
    b = await factories.make_user(db_session)
    mine = orphaned / f"smith.{STAMP}.owner-{a.id.hex}.md"
    untagged = orphaned / "smith.20261014T000000000000Z.md"
    theirs = orphaned / f"jones.{STAMP}.owner-{b.id.hex}.md"
    for path in (mine, untagged, theirs):
        path.write_text("persona", encoding="utf-8")
    report = await delete_user_account(db_session, a)
    assert not mine.exists() and untagged.exists() and theirs.exists()
    assert str(mine) in report.files_deleted


async def test_archive_purge_runs_without_an_agent(db_session, teardown_dirs):
    a = await factories.make_user(db_session)
    copy = teardown_dirs["orphaned"] / f"smith.{STAMP}.owner-{a.id.hex}.md"
    copy.write_text("persona", encoding="utf-8")
    report = await delete_user_account(db_session, a)
    assert report.agent_id is None
    assert not copy.exists()


async def _owner_of_smith(db):
    pi = await factories.make_user(db)
    await factories.make_agent(db, user=pi, agent_id="smith", status="active")
    return pi


async def test_symlinked_archive_stamp_is_not_followed(db_session, teardown_dirs, caplog):
    mem, outside = teardown_dirs["mem"], teardown_dirs["outside"]
    (outside / "smith").mkdir()
    (outside / "smith" / "x.md").write_text("not ours", encoding="utf-8")
    (mem / "archive").mkdir()
    stamp_link = mem / "archive" / "S"
    stamp_link.symlink_to(outside, target_is_directory=True)
    pi = await _owner_of_smith(db_session)
    with caplog.at_level(logging.ERROR, logger="src.services.user_deletion"):
        report = await delete_user_account(db_session, pi)
    assert (outside / "smith" / "x.md").exists()
    assert any(str(stamp_link) in e for e in report.errors)
    assert "is a symbolic link; not followed" in caplog.text


async def test_symlinked_persona_file_is_unlinked_not_followed(db_session, teardown_dirs):
    target = teardown_dirs["outside"] / "t.md"
    target.write_text("not ours", encoding="utf-8")
    link = teardown_dirs["pub"] / "smith.md"
    link.symlink_to(target)
    await delete_user_account(db_session, await _owner_of_smith(db_session))
    assert not os.path.lexists(link)
    assert target.exists()


async def test_symlinked_memory_dir_is_unlinked(db_session, teardown_dirs):
    elsewhere = teardown_dirs["outside"] / "m"
    elsewhere.mkdir()
    (elsewhere / "public.md").write_text("not ours", encoding="utf-8")
    link = teardown_dirs["mem"] / "smith"
    link.symlink_to(elsewhere, target_is_directory=True)
    await delete_user_account(db_session, await _owner_of_smith(db_session))
    assert not os.path.lexists(link)
    assert (elsewhere / "public.md").exists()


async def test_dangling_link_is_removed(db_session, teardown_dirs):
    link = teardown_dirs["companies"] / "smith.md"
    link.symlink_to(teardown_dirs["outside"] / "missing.md")
    report = await delete_user_account(db_session, await _owner_of_smith(db_session))
    assert not os.path.lexists(link)
    assert str(link) in report.files_deleted


async def test_symlinked_orphaned_dir_is_reported_not_followed(
    db_session, teardown_dirs, monkeypatch,
):
    a = await factories.make_user(db_session)
    target = teardown_dirs["outside"] / "orphaned"
    target.mkdir()
    copy = target / f"smith.{STAMP}.owner-{a.id.hex}.md"
    copy.write_text("not ours to follow", encoding="utf-8")
    link = teardown_dirs["outside"].parent / "orphaned-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(user_deletion, "_ORPHANED_DIR", link)
    report = await delete_user_account(db_session, a)
    assert any(f"{link} is a symbolic link; not followed" in e for e in report.errors)
    assert os.path.islink(link) and target.is_dir() and copy.exists()


async def test_symlinked_memory_archive_root_is_reported_not_followed(db_session, teardown_dirs):
    target = teardown_dirs["outside"] / "archive"
    (target / "S" / "smith").mkdir(parents=True)
    (target / "S" / "smith" / "x.md").write_text("not ours", encoding="utf-8")
    (target / "S" / "smith.md").write_text("not ours", encoding="utf-8")
    root = teardown_dirs["mem"] / "archive"
    root.symlink_to(target, target_is_directory=True)
    report = await delete_user_account(db_session, await _owner_of_smith(db_session))
    assert any(f"{root} is a symbolic link; not followed" in e for e in report.errors)
    assert os.path.islink(root)
    assert (target / "S" / "smith" / "x.md").exists() and (target / "S" / "smith.md").exists()


async def test_symlinked_tagged_archived_persona_is_unlinked_not_followed(
    db_session, teardown_dirs,
):
    a = await factories.make_user(db_session)
    target = teardown_dirs["outside"] / "t.md"
    target.write_text("not ours", encoding="utf-8")
    link = teardown_dirs["orphaned"] / f"smith.{STAMP}.owner-{a.id.hex}.md"
    link.symlink_to(target)
    report = await delete_user_account(db_session, a)
    assert not os.path.lexists(link)
    assert target.read_text(encoding="utf-8") == "not ours"
    assert str(link) in report.files_deleted


async def test_tagged_archived_persona_directory_is_reported_and_kept(db_session, teardown_dirs):
    a = await factories.make_user(db_session)
    entry = teardown_dirs["orphaned"] / f"smith.{STAMP}.owner-{a.id.hex}.md"
    entry.mkdir()
    (entry / "inner.md").write_text("kept", encoding="utf-8")
    report = await delete_user_account(db_session, a)
    assert any(f"{entry} is a directory; not removed" in e for e in report.errors)
    assert entry.is_dir() and (entry / "inner.md").exists()
    assert str(entry) not in report.files_deleted
