"""Clear-after-write for the agent-page private-profile save route.

Companion to the ``export_private_profile`` unlink behaviour in
``src/services/profile_export.py``. ``POST /agent/{agent_id}/profile/save`` must
remove ``profiles/private/{agent_id}.md`` on a blank submission rather than
writing its raw (whitespace) content straight to disk, so
``src/agent/agent.py``'s ``private_profile`` property — which falls back to
"No private instructions yet." only when the file is ABSENT — does not keep
honouring instructions the PI just deleted.

Real ASGI requests, real Postgres — same harness as ``test_agent_page.py``,
kept self-contained here rather than importing its fixtures.
"""

import base64
import json

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import ProfileRevision, ResearcherProfile
from src.services import profile_export, profile_pipeline
from tests import factories
from tests.fakes import FakeAnthropic

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
    route must declare ``content: str = Form("")``, not ``Form(...)`` -- otherwise it
    returns a raw ``422 {"type":"missing","loc":["body","content"]}`` and clears
    nothing, making the clear path (and with it the seed-clearing) unreachable from a
    browser unless the PI happened to leave whitespace in the box. The onboarding twin
    already uses ``Form("")``; this pins that same parity here.
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


async def test_omitting_content_entirely_is_rejected_and_survives(
    client, db_session, profiles_dir, pi_and_agent
):
    """A request that OMITS `content` entirely
    (as opposed to submitting it present-but-empty) must not blank anything.
    Not reachable from the browser (templates/agent/profile.html's textarea is
    always submitted), so this only matters for a hand-crafted or scripted
    POST — but the route must not treat "absent" and
    "present and empty" identically the way plain `Form("")` does, or an
    omitted field would 302 and destroy both private columns and the disk
    file where a 422 that destroys nothing is correct.
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
        data={"unrelated": "1"},  # `content` omitted entirely
        headers=_auth(pi.id),
    )
    assert r.status_code == 400, r.text

    assert path.exists(), "an omitted content field deleted the exported file"
    assert "Always cite the 2019 paper." in path.read_text(encoding="utf-8")
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md == "Always cite the 2019 paper."
    assert profile.private_profile_seed == "Model-authored seed from admin onboarding.", (
        "an omitted content field must not touch private_profile_seed either"
    )


async def test_onboarding_twin_also_rejects_an_omitted_content_field(
    client, db_session, monkeypatch, tmp_path, pi_and_agent
):
    """onboarding.py's save_private_profile must not use the same plain
    `Form("")` shape as the agent_page.py route above for this case; it needs
    the same `content: str | None = Form(None)` handling."""
    from src.services import profile_export

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    monkeypatch.setattr(profile_export, "PRIVATE_PROFILES_DIR", tmp_path / "private")
    pi, agent = pi_and_agent
    path = tmp_path / "private" / f"{agent.agent_id}.md"

    r = await client.post(
        "/onboarding/private-profile",
        data={"content": "Real onboarding instructions."},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302
    assert path.exists()

    r = await client.post(
        "/onboarding/private-profile",
        data={"unrelated": "1"},  # `content` omitted entirely
        headers=_auth(pi.id),
    )
    assert r.status_code == 400, r.text

    assert path.exists(), "an omitted content field deleted the exported file"
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md == "Real onboarding instructions."


# ---------------------------------------------------------------------------
# The clear must survive the next profile_pipeline run
# ---------------------------------------------------------------------------

# A public synthesis that passes profile_pipeline._validate_profile (100-350 word
# summary, 3+ techniques, 1+ disease area), so the run below stays on the happy
# path and spends exactly ONE LLM call on the public profile. Anything the
# pipeline asks for after that is the private-seed call this file is about.
_VALID_SYNTHESIS = {
    "research_summary": " ".join(["chemoproteomics"] * 120),
    "techniques": ["mass spectrometry", "click chemistry", "activity-based probes"],
    "experimental_models": ["cell lines"],
    "disease_areas": ["cancer"],
    "key_targets": ["serine hydrolases"],
    "keywords": ["proteomics"],
}


def _install_pipeline_fakes(monkeypatch, profiles_dir):
    """Neutralize every external boundary run_profile_pipeline reaches.

    The ORCID stubs return nothing, so steps 3-5 make no PubMed/PMC calls at
    all and the run takes the shortest path to step 9. The export directories
    are repointed at the SAME tree the save route above writes to — otherwise
    the pipeline's disk-adoption step would read a different (empty) directory
    and the test would prove nothing about the file the PI just cleared.
    """
    async def _orcid_profile(orcid_id):
        return {"name": "Clear PI", "orcid": orcid_id}

    async def _nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", _orcid_profile)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_grants", _nothing)
    monkeypatch.setattr(profile_pipeline, "fetch_orcid_works", _nothing)
    monkeypatch.setattr(profile_export, "PROFILES_DIR", profiles_dir / "public")
    monkeypatch.setattr(profile_export, "PRIVATE_PROFILES_DIR", profiles_dir / "private")

    # Only the public synthesis is scripted. A second call (the private seed)
    # falls through to FakeAnthropic's default_text, so a resurrected seed shows
    # up as a stored "OK" rather than as an IndexError.
    fake_llm = FakeAnthropic([json.dumps(_VALID_SYNTHESIS)])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake_llm)
    return fake_llm


@pytest.fixture
async def never_onboarded_pi_and_agent(db_session):
    """A PI who never completed onboarding — the common case, not the corner.

    Every admin-seeded pilot lab starts with ``onboarding_complete = false``.
    The onboarding flag is therefore no proxy at all for "this PI has made a
    private-instructions decision".
    """
    pi = await factories.make_user(
        db_session,
        name="Never Onboarded PI",
        email="neveronboarded@example.org",
        onboarding_complete=False,
    )
    agent = await factories.make_agent(
        db_session, user=pi, agent_id="tstnoonb", bot_name="NoOnbBot",
        pi_name="Never Onboarded PI",
    )
    await factories.make_profile(
        db_session, user=pi, private_profile_md=None, private_profile_seed=None,
    )
    await db_session.flush()
    return pi, agent


async def test_a_clear_through_the_agent_page_survives_the_next_pipeline_run(
    client, db_session, profiles_dir, monkeypatch, never_onboarded_pi_and_agent
):
    """The seed-resurrection guard must key on the
    cleared state, not on ``user.onboarding_complete``.

    ``POST /agent/{agent_id}/profile/save`` is the other clear route (usable by
    the PI *or* a delegate). It nulls both private columns, unlinks the exported
    file and records an empty private revision — but it never touches
    ``onboarding_complete``. For an agent whose PI never
    finished onboarding, a guard keyed on that flag would let the very next pipeline
    run (a regenerate, an admin re-enqueue, a monthly refresh) synthesize a
    fresh model-authored seed and export it to disk, undoing the clear — which
    is exactly what the guard exists to prevent.

    Driven end to end: the real route clears real content, then the real
    pipeline runs over the state the route left behind.
    """
    pi, agent = never_onboarded_pi_and_agent
    path = profiles_dir / "private" / f"{agent.agent_id}.md"

    # 1. Real private content, written through the real route (the positive
    #    control: without this the clear below would be a no-op and the test
    #    would pass for the wrong reason).
    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": "Never contact this lab on Fridays."},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302, r.text
    assert path.exists()

    # 2. The PI clears it.
    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": ""},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302, r.text
    assert not path.exists()

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md is None
    assert profile.private_profile_seed is None
    # The route leaves the DB-side record of the clear this fix keys on: an
    # EMPTY private revision. Compared as a sorted set, not in created_at
    # order — created_at defaults to now(), which in Postgres is the
    # TRANSACTION timestamp, and the whole test runs inside one rolled-back
    # transaction (tests/conftest.py), so both rows share it and "newest" is
    # arbitrary between them. That tie is why the guard keys on the existence
    # of an empty revision rather than on the latest one being empty.
    revisions = (await db_session.execute(
        select(ProfileRevision.content).where(
            ProfileRevision.agent_registry_id == agent.id,
            ProfileRevision.profile_type == "private",
        )
    )).scalars().all()
    assert sorted(revisions) == ["", "Never contact this lab on Fridays."], revisions
    assert pi.onboarding_complete is False, (
        "the clear route must not have set the onboarding flag — if it did, this "
        "test no longer covers the defect"
    )

    # 3. The next pipeline run must not put model-authored instructions back.
    fake_llm = _install_pipeline_fakes(monkeypatch, profiles_dir)
    profile = await profile_pipeline.run_profile_pipeline(pi.id, db_session)

    assert profile.private_profile_seed is None, (
        "the pipeline regenerated a private seed for a PI who cleared their "
        "instructions through /agent/{id}/profile/save"
    )
    assert profile.private_profile_md is None
    assert not path.exists(), "a regenerated seed was exported over the cleared file"
    assert len(fake_llm.calls) == 1, (
        "exactly one LLM call (the public synthesis) — a second call is the "
        "private-seed synthesis this PI's clear must have suppressed"
    )


# Audit D3. The guard asked Postgres `btrim(content, ' \t\n\r\f\v')` while the route
# decided with Python's Unicode-aware `str.strip()` and then recorded the RAW body, so the
# two disagreed on every whitespace code point outside that six-character class. Measured
# against this Postgres 15: exactly seven disagree. NBSP is the realistic one -- it is what
# any paste from Word, Outlook, Google Docs or a PDF leaves behind -- and U+3000 is what a
# CJK input method leaves. For each, the route DID clear (nulled both columns, unlinked the
# export) while the guard read the revision as non-empty and answered "never cleared", so
# the next pipeline run synthesized a fresh model-authored seed straight back over it.
_WHITESPACE_CLEARS = [
    pytest.param("\u00a0", id="NBSP-U+00A0"),
    pytest.param("\u0085", id="NEL-U+0085"),
    pytest.param("\u001c", id="FILESEP-U+001C"),
    pytest.param("\u2000", id="ENQUAD-U+2000"),
    pytest.param("\u2028", id="LINESEP-U+2028"),
    pytest.param("\u202f", id="NNBSP-U+202F"),
    pytest.param("\u3000", id="IDEOGRAPHIC-U+3000"),
    # Controls: these six the old SQL class already handled, so they must keep working.
    pytest.param(" ", id="SPACE-control"),
    pytest.param("\t", id="TAB-control"),
    pytest.param("\n", id="NEWLINE-control"),
]


@pytest.mark.parametrize("blank", _WHITESPACE_CLEARS)
async def test_a_whitespace_clear_survives_the_next_pipeline_run(
    blank, client, db_session, profiles_dir, monkeypatch, never_onboarded_pi_and_agent
):
    """A clear is a clear whatever whitespace the PI left behind.

    Same end-to-end shape as
    ``test_a_clear_through_the_agent_page_survives_the_next_pipeline_run`` above -- real
    route, then real pipeline -- but the PI leaves a single whitespace character in the
    textarea instead of an empty string. A textarea can submit any Unicode whitespace,
    which is why enumerating a SQL character class was the wrong mechanism.
    """
    pi, agent = never_onboarded_pi_and_agent
    path = profiles_dir / "private" / f"{agent.agent_id}.md"

    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": "Never contact this lab on Fridays."},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302, r.text
    assert path.exists()

    r = await client.post(
        f"/agent/{agent.agent_id}/profile/save",
        data={"content": blank},
        headers=_auth(pi.id),
    )
    assert r.status_code == 302, r.text
    assert not path.exists(), f"the route did not clear for {blank!r}"

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md is None
    assert profile.private_profile_seed is None

    fake_llm = _install_pipeline_fakes(monkeypatch, profiles_dir)
    profile = await profile_pipeline.run_profile_pipeline(pi.id, db_session)

    assert profile.private_profile_seed is None, (
        f"the pipeline regenerated a private seed after a clear of {blank!r} -- the guard "
        "read the recorded revision as non-empty"
    )
    assert profile.private_profile_md is None
    assert not path.exists(), "a regenerated seed was exported over the cleared file"
    assert len(fake_llm.calls) == 1, (
        "a second LLM call is the private-seed synthesis this clear must have suppressed"
    )


async def test_an_admin_seeded_pi_who_never_cleared_still_gets_a_seed(
    db_session, profiles_dir, monkeypatch, never_onboarded_pi_and_agent
):
    """The control for the test above, in the same fixture state.

    Same PI, same never-onboarded flag, same empty private columns — the only
    difference is that nothing was ever cleared. An admin-seeded PI who has no
    private instructions yet must still get a generated seed, exported to disk,
    or this fix breaks onboarding for every new lab.
    """
    pi, agent = never_onboarded_pi_and_agent
    path = profiles_dir / "private" / f"{agent.agent_id}.md"

    fake_llm = _install_pipeline_fakes(monkeypatch, profiles_dir)
    profile = await profile_pipeline.run_profile_pipeline(pi.id, db_session)

    assert profile.private_profile_seed == "OK", profile.private_profile_seed
    assert len(fake_llm.calls) == 2, "public synthesis + private seed"
    assert path.exists(), "the generated seed must reach profiles/private/"
    assert path.read_text(encoding="utf-8").strip() == "OK"
