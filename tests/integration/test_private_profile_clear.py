"""Clear-after-write for the agent-page private-profile save route.

Companion to the ``export_private_profile`` unlink fix in
``src/services/profile_export.py`` (Unit H item 3, #22 COR-23 / #29). The
22.6/22.11 re-review found that ``POST /agent/{agent_id}/profile/save`` wrote
a blank submission's raw (whitespace) content straight to
``profiles/private/{agent_id}.md`` instead of removing the file, so
``src/agent/agent.py``'s ``private_profile`` property — which falls back to
"No private instructions yet." only when the file is ABSENT — kept honouring
instructions the PI had just deleted.

Real ASGI requests, real Postgres — same harness as ``test_agent_page.py``,
kept self-contained here (rather than importing its fixtures) because that
file is owned by another implementer working concurrently in this tree.
"""

import base64
import json

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import ResearcherProfile
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture(autouse=True)
def profiles_dir(tmp_path, monkeypatch):
    """Keep the profile-save route off the repo's real profiles/ directory."""
    monkeypatch.setattr("src.routers.agent_page.PROFILES_DIR", tmp_path / "profiles")
    return tmp_path / "profiles"


@pytest.fixture
async def pi_and_agent(db_session):
    pi = await factories.make_user(db_session, name="Clear PI", email="clearpi@example.org")
    agent = await factories.make_agent(
        db_session, user=pi, agent_id="tstclear", bot_name="ClearBot", pi_name="Clear PI",
    )
    await factories.make_profile(
        db_session,
        user=pi,
        private_profile_md=None,
        private_profile_seed="Model-authored seed from admin onboarding.",
    )
    await db_session.flush()
    return pi, agent


async def test_saving_then_blanking_the_private_profile_deletes_the_exported_file(
    client, db_session, profiles_dir, pi_and_agent
):
    pi, agent = pi_and_agent
    path = profiles_dir / "private" / f"{agent.agent_id}.md"

    # Save real content first — the positive control for the deletion below.
    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": "Always cite the 2019 paper."},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302
    assert path.exists()
    assert "Always cite the 2019 paper." in path.read_text(encoding="utf-8")

    # Now clear it with a whitespace-only submission.
    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": "   "},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302
    assert not path.exists(), (
        "a blank private-profile save must delete the exported file, not write "
        "whitespace to it — agent.py's private_profile property only falls back "
        "to 'No private instructions yet.' when the file is absent"
    )

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md is None
    assert profile.private_profile_seed is None, (
        "a blank save must also clear private_profile_seed — otherwise the next "
        "profile_pipeline export (`content = md or seed`) resurrects a "
        "model-authored seed the PI never approved, undoing the clear"
    )


async def test_blanking_a_private_profile_that_was_never_written_does_not_raise(
    client, profiles_dir, pi_and_agent
):
    """Tolerate absence: clearing with nothing on disk yet must 302, not 500."""
    pi, agent = pi_and_agent
    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": "   "},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302
    assert not (profiles_dir / "private" / f"{agent.agent_id}.md").exists()


async def test_a_truly_empty_content_field_clears_the_private_profile(
    client, db_session, profiles_dir, pi_and_agent
):
    """An emptied textarea submits ``content=``, and that must clear.

    Starlette's form parser hands an empty value to FastAPI as a MISSING field, so this
    route's original ``content: str = Form(...)`` returned a raw
    ``422 {"type":"missing","loc":["body","content"]}`` and cleared nothing — meaning the
    clear path (and with it the seed-clearing of #22 COR-23 / #29) was unreachable from a
    browser unless the PI happened to leave whitespace in the box. Found by driving the
    real route against a copy of production. The onboarding twin has always used
    ``Form("")``; this is that parity, pinned.
    """
    pi, agent = pi_and_agent
    path = profiles_dir / "private" / f"{agent.agent_id}.md"

    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": "Always cite the 2019 paper."},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302
    assert path.exists()

    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": ""},          # exactly what an emptied textarea sends
        headers=_auth(pi.id),
    )
    assert r.status_code == 302, r.text
    assert not path.exists(), "the private instructions file must be gone"

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md is None
    assert profile.private_profile_seed is None
