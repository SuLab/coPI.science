"""GrantBot's LLM selection step must never post unvetted opportunities on a parse failure
(issue #23 COR-26a/COR-26b)."""

from src.agent import grantbot


async def test_a_non_list_selection_response_hard_fails_instead_of_falling_back(monkeypatch):
    """The old fallback posted every opportunity's key on ANY exception — exactly what a
    selection step exists to prevent."""
    async def fake_generate(**kwargs):
        return '"PAR-24-293"'  # a bare JSON string, not a list

    monkeypatch.setattr("src.services.llm.generate_agent_response", fake_generate)
    opps = {"PAR-24-293": {"title": "T1"}, "RFA-AI-27-019": {"title": "T2"}}
    assert await grantbot._select_opportunities(opps) == []


async def test_a_dict_element_in_the_selection_is_rejected_not_crashed(monkeypatch):
    """A dict/list element in the LLM's JSON array used to reach `num in all_opps` in
    run_grantbot uncaught, raising TypeError: unhashable type."""
    async def fake_generate(**kwargs):
        return '["PAR-24-293", {"number": "RFA-AI-27-019"}]'

    monkeypatch.setattr("src.services.llm.generate_agent_response", fake_generate)
    opps = {"PAR-24-293": {"title": "T1"}, "RFA-AI-27-019": {"title": "T2"}}
    assert await grantbot._select_opportunities(opps) == []


async def test_a_well_formed_selection_still_works(monkeypatch):
    async def fake_generate(**kwargs):
        return '["PAR-24-293"]'

    monkeypatch.setattr("src.services.llm.generate_agent_response", fake_generate)
    opps = {"PAR-24-293": {"title": "T1"}, "RFA-AI-27-019": {"title": "T2"}}
    assert await grantbot._select_opportunities(opps) == ["PAR-24-293"]


async def test_a_number_the_llm_invented_is_dropped_not_a_hard_fail(monkeypatch):
    """A str/int element that names no real opportunity is not a type violation — it's the LLM
    citing something filtered upstream or mis-cased. Drop it; don't fail the whole batch."""
    async def fake_generate(**kwargs):
        return '["PAR-24-293", "NOT-REAL-99-999"]'

    monkeypatch.setattr("src.services.llm.generate_agent_response", fake_generate)
    opps = {"PAR-24-293": {"title": "T1"}}
    assert await grantbot._select_opportunities(opps) == ["PAR-24-293"]


def test_dead_profile_search_helpers_are_removed():
    """COR-26d: these three helpers and PROFILES_DIR had zero callers anywhere in src/, scripts/
    or tests/ — grantbot's profile-driven keyword search was superseded and never removed. 83 dead
    lines plus a dead module constant."""
    for name in (
        "_load_researcher_profiles", "_extract_list_section",
        "_build_search_queries", "PROFILES_DIR",
    ):
        assert not hasattr(grantbot, name), f"{name} should have been removed"
