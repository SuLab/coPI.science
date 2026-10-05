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


# The slug names files and cohorts that [a-z0-9_-] checks guard, and Slack restricts
# a bot's display name to ASCII (2026-10-05 Add-PI audit, F4/F5).
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Anna Müller", ("muller", "MullerBot")),
        ("José García López", ("lopez", "LopezBot")),
        ("Søren Kierkegaard", ("kierkegaard", "KierkegaardBot")),
        ("Lars Ørsted", ("orsted", "OrstedBot")),
        ("Liam O'Brien", ("obrien", "OBrienBot")),
        ("Ana Hamacher-Brady", ("hamacherbrady", "HamacherBradyBot")),
        ("John Smith Jr.", ("smith", "SmithBot")),
        ("Mary Jones III", ("jones", "JonesBot")),
        ("Jean-Luc Picard PhD", ("picard", "PicardBot")),
        ("Naoki Ii", ("ii", "IiBot")),
        ("Henry Ford II", ("ford", "FordBot")),
    ],
)
async def test_slugs_and_bot_names_are_ascii_letters_only(db_session, name, expected):
    assert await derive_agent_identity(db_session, name) == expected


@pytest.mark.parametrize("name", ["0000-0002-1825-009X", "王小明", "Иван Петров"])
async def test_a_name_without_a_latin_surname_falls_back_to_the_orcid_stem(db_session, name):
    """An ORCID record with a private name yields the iD itself as the name; the old
    derivation took its check digit and minted the slug ``x``."""
    agent_id, bot_name = await derive_agent_identity(
        db_session, name, orcid="0000-0002-1825-0097"
    )
    assert (agent_id, bot_name) == ("pi0097", "Pi0097Bot")


async def test_an_accented_initial_is_folded_in_the_prefix_tier(db_session):
    await factories.make_agent(db_session, agent_id="ruiz", bot_name="RuizBot")
    assert await derive_agent_identity(db_session, "Ángel Ruiz") == ("aruiz", "ARuizBot")


async def test_a_long_surname_keeps_the_bot_name_within_slacks_35_characters(db_session):
    agent_id, bot_name = await derive_agent_identity(
        db_session, "Ann Wolfeschlegelsteinhausenbergerdorff"
    )
    assert len(bot_name) <= 35
    assert agent_id == bot_name[:-3].lower()
