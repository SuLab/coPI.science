"""scripts/replay_authorship_claims.py (the 2026-10-15 publication-handling design §11
R6.15): counts, the thread root as context, the hub apart, C12/F9 records, and that the
replay writes nothing."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

import scripts.replay_authorship_claims as replay
from src.database import Base
from src.models import Publication
from tests import factories

pytestmark = pytest.mark.integration
B = "10.1093/bioinformatics/btad570"
D = "10.1093/bioadv/vbag036"
T = "1700000100.000000"


async def _msg(db_session, run, ts, agent_id, phase, content, thread_ts=None):
    await factories.make_agent_message(
        db_session, run=run, agent_id=agent_id, message_ts=ts, thread_ts=thread_ts, phase=phase,
        content=content, posted_at=float(ts), is_bot=True, channel_name="general",
        message_length=len(content),
    )


async def _seed(db_session, tmp_path, *, second_wu_block=False):
    run = await factories.make_simulation_run(db_session)
    wu_user, good_user = await factories.make_user(db_session), await factories.make_user(db_session)
    await factories.make_agent(db_session, user=wu_user, agent_id="wu", bot_name="WuBot",
                               pi_name="Chunlei Wu", role="pi_lab")
    await factories.make_agent(db_session, user=good_user, agent_id="good", bot_name="GoodBot",
                               pi_name="Benjamin Good", role="pi_lab")
    await factories.make_agent(db_session, agent_id="blackbird", bot_name="BlackbirdBot",
                               pi_name="Blackbird", role="scout_hub")
    db_session.add(Publication(user_id=wu_user.id, pmid="40000001", doi=B, title="BTE", year=2023))
    await db_session.flush()
    (tmp_path / "public").mkdir(exist_ok=True)
    await _msg(db_session, run, T, "wu", "new_post", f"We published https://doi.org/{B}.")
    await _msg(db_session, run, "1700000101.000000", "blackbird", "thread_reply", "Thanks.", T)
    await _msg(db_session, run, "1700000102.000000", "wu", "thread_reply",
               "Plainly: yes, that paper is ours — Dr. Wu is an author on it.", T)
    await _msg(db_session, run, "1700000103.000000", "wu", "thread_reply",
               "Our 2023 Nature paper showed this.", T)
    await _msg(db_session, run, "1700000104.000000", "good", "new_post",
               "Our lab published a Nature paper on this.")
    await _msg(db_session, run, "1700000105.000000", "blackbird", "panel_note", "consult: gap", T)
    await _msg(db_session, run, "1700000106.000000", "blackbird", "thread_reply", "We published 10.1/x.", T)
    if second_wu_block:
        await _msg(db_session, run, "1700000107.000000", "wu", "thread_reply",
                   "Our 2024 Cell paper builds on that.", T)
    return run


async def _report(db_session, tmp_path, run):
    return await replay.collect(db_session, replay.Scope(runs=(run.id,)), profiles_dir=tmp_path)


async def test_replay_reports_per_agent_counts_and_rates(db_session, tmp_path):
    report = await _report(db_session, tmp_path, await _seed(db_session, tmp_path))
    wu, good = report["per_lab"]["wu"], report["per_lab"]["good"]
    assert (wu["messages"], wu["claims"], wu["rejected"]) == (3, 3, 1)
    assert (good["messages"], good["claims"], good["rejected"]) == (1, 1, 1)


async def test_replay_uses_the_thread_root_as_context(db_session, tmp_path):
    report = await _report(db_session, tmp_path, await _seed(db_session, tmp_path))
    # wu's pitch and its "Plainly …" acknowledgement, grounded by the pitch as the root.
    assert report["totals"]["pi_lab"]["grounded"] == 2


async def test_replay_excludes_panel_notes_and_counts_them(db_session, tmp_path):
    report = await _report(db_session, tmp_path, await _seed(db_session, tmp_path))
    assert report["scope"]["not_replayed"] == {"panel_note": 1}


async def test_replay_separates_the_hub(db_session, tmp_path):
    report = await _report(db_session, tmp_path, await _seed(db_session, tmp_path))
    hub = report["totals"]["scout_hub"]
    assert (hub["messages"], hub["rejected"]) == (2, 1)
    assert "blackbird" not in report["per_lab"]
    assert report["enforcement"]["replies"] == 2 and report["enforcement"]["pitches"] == 2


async def test_would_abandon_counts_threads_with_two_lab_blocks(db_session, tmp_path):
    run = await _seed(db_session, tmp_path, second_wu_block=True)
    report = await _report(db_session, tmp_path, run)
    assert report["enforcement"]["threads_with_two_blocks"] == 1


async def test_replay_writes_nothing(db_session, tmp_path):
    run = await _seed(db_session, tmp_path)

    async def counts():
        return {t.name: (await db_session.execute(text(f'SELECT count(*) FROM "{t.name}"'))).scalar_one()
                for t in Base.metadata.sorted_tables}

    before = await counts()
    await _report(db_session, tmp_path, run)
    after = await counts()
    assert after == before
    assert after["authorship_claim_checks"] == 0


async def test_markdown_and_json_agree(db_session, tmp_path):
    report = await _report(db_session, tmp_path, await _seed(db_session, tmp_path))
    md = replay.render_markdown(report)
    for key, label in (("pi_lab", "pi_lab"), ("scout_hub", "scout_hub (log only)")):
        t = report["totals"][key]
        assert (f"| {label} | {t['messages']} | {t['claims']} | {t['grounded']} | {t['rejected']} |"
                in md)
    assert replay.render_json(report)


@pytest.mark.parametrize(("reason", "rejected"), [("staff", 1), ("superseded", 0)])
async def test_replay_records_follow_c12_and_f9(db_session, tmp_path, reason, rejected):
    """good's persona lists D and a second DOI; good's only row holding D is excluded."""
    run = await factories.make_simulation_run(db_session)
    user = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=user, agent_id="good", bot_name="GoodBot",
                               pi_name="Benjamin Good", role="pi_lab")
    db_session.add(Publication(user_id=user.id, pmid="40000002", doi=D, title="KG", year=2024,
                               excluded_at=datetime.now(UTC), excluded_reason=reason))
    await db_session.flush()
    (tmp_path / "public").mkdir(exist_ok=True)
    (tmp_path / "public" / "good.md").write_text(
        "# Benjamin Good Lab\n\n## Recent Publications\n\n"
        f"- KG. https://doi.org/{D}\n- Other. https://doi.org/10.5555/good.1\n", encoding="utf-8")
    await _msg(db_session, run, "1700000200.000000", "good", "new_post", f"We published https://doi.org/{D}.")
    report = await _report(db_session, tmp_path, run)
    assert report["per_lab"]["good"]["rejected"] == rejected
    if rejected:
        assert report["reasons"]["not_held"]["labs"] == ["good"]
