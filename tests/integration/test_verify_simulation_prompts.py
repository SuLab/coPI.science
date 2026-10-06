import pytest

from scripts import verify_simulation_prompts as v
from tests import factories

PERSONA = (
    "# {aid} Lab — Public Profile\n\n## Key Methods and Technologies\n\n- cryo-EM\n\n"
    "## Recent Publications\n\n- P. https://pubmed.ncbi.nlm.nih.gov/{pmid}/ (PMID {pmid})\n"
)


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    (root / "public").mkdir(parents=True)
    (root / "memory").mkdir()
    for target in ("src.agent.agent.PROFILES_DIR", "src.agent.tools.PROFILES_DIR"):
        monkeypatch.setattr(target, root)
    return root


async def _seed(db, profiles):
    await factories.make_agent(db, agent_id="vhub", bot_name="VHubBot", pi_name="Hub",
                               role="scout_hub")
    for aid, pmid in (("vwang", "38980071"), ("vgordy", "38980072")):
        user = await factories.make_user(db)
        await factories.make_agent(db, user=user, agent_id=aid, bot_name=f"{aid}Bot",
                                   pi_name=f"PI {aid}", role="pi_lab")
        (profiles / "public" / f"{aid}.md").write_text(
            PERSONA.format(aid=aid, pmid=pmid), encoding="utf-8")


def _by_name(results):
    return {r.name: r for r in results}


async def test_every_check_passes_on_a_clean_tree(db_session, profiles):
    await _seed(db_session, profiles)
    results = _by_name(await v.run_checks(db_session))
    assert list(results) == list(v.CHECKS)
    failed = {n: r.details for n, r in results.items() if r.status == "FAIL"}
    assert failed == {}
    assert results["cohort_gate_production"].status in {"PASS", "WARN"}


async def test_a_hub_persona_left_in_place_fails(db_session, profiles):
    await _seed(db_session, profiles)
    (profiles / "public" / "vhub.md").write_text("# old hub profile\n", encoding="utf-8")
    assert _by_name(await v.run_checks(db_session))["hub_persona_archived"].status == "FAIL"


async def test_a_gate_that_lets_everything_through_fails(db_session, profiles, monkeypatch):
    from src.agent import tools

    await _seed(db_session, profiles)
    monkeypatch.setattr(tools, "_outside_gate", lambda *a, **k: False)
    assert _by_name(await v.run_checks(db_session))["cohort_gate_synthetic"].status == "FAIL"


async def test_an_unfenced_transcript_fails(db_session, profiles, monkeypatch):
    from src.agent import agent as agent_module

    await _seed(db_session, profiles)
    monkeypatch.setattr(agent_module, "delimit", lambda content, tag="x": str(content))
    assert _by_name(await v.run_checks(db_session))["hub_transcript"].status == "FAIL"


def test_the_channel_rows_compare_the_old_and_new_rule():
    persona = (
        "# Lab\n\n## Research Summary\n\nsmall molecule imaging\n\n"
        "## Key Methods and Technologies\n\n- cryo-EM\n"
    )
    rows = {r.agent_id: r for r in v.channel_rows([("lab", "pi_lab", persona),
                                                    ("vhub", "scout_hub", "")])}
    assert rows["lab"].before >= {"general", "drug-repurposing", "structural-biology",
                                  "aging-and-longevity"}
    assert rows["lab"].after == {"general", "structural-biology"}
    assert "chemical-biology" in rows["vhub"].after
