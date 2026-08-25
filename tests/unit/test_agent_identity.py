"""derive_agent_identity as a shared service (moved out of agent_page).

The web flow's copy had no third collision tier — a second collision on the
initial-prefixed id (``pwu``) raised IntegrityError on the unique
``agents.agent_id`` — and an all-digit display name produced an EMPTY
``agent_id`` silently. Both matter now that the manager Add-PI flow mints
agents automatically. Casing behavior is the web flow's (``McCarthyBot``),
not the backfill script's ``.capitalize()`` (``MccarthyBot``).
"""

import pytest

from src.services.agent_identity import derive_agent_identity
from tests import factories

pytestmark = pytest.mark.integration


async def test_no_collision_preserves_the_display_casing(db_session):
    agent_id, bot_name = await derive_agent_identity(db_session, "Mary McCarthy")
    assert (agent_id, bot_name) == ("mccarthy", "McCarthyBot")


async def test_first_collision_gets_the_initial_prefix_pair(db_session):
    await factories.make_agent(db_session, agent_id="wu", bot_name="WuBot")
    agent_id, bot_name = await derive_agent_identity(db_session, "Peng Wu")
    assert (agent_id, bot_name) == ("pwu", "PWuBot")


async def test_double_collision_falls_to_a_numeric_tier_instead_of_raising(
    db_session,
):
    await factories.make_agent(db_session, agent_id="wu", bot_name="WuBot")
    await factories.make_agent(db_session, agent_id="pwu", bot_name="PWuBot")
    agent_id, bot_name = await derive_agent_identity(db_session, "Ping Wu")
    assert (agent_id, bot_name) == ("wu2", "Wu2Bot")


async def test_a_name_with_no_alphabetic_characters_never_yields_an_empty_id(
    db_session,
):
    agent_id, bot_name = await derive_agent_identity(
        db_session, "1234 5678", orcid="0000-0001-2345-6789"
    )
    assert agent_id
    assert agent_id == agent_id.lower()
    assert "6789" in agent_id
    assert bot_name.endswith("Bot")
