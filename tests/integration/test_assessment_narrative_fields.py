"""The reviewer-facing narrative fields on an assessment (migrations 0043, 0048).

Task 2 covers the columns themselves; Task 8 extends this file with the engine
write path that fills them from the sidecar. `score_rationale` (sidecar item 10,
migration 0048) joined the set on 2026-09-14 — app-only, never published to
`#assessments-summary`.
"""

import pytest
from sqlalchemy import select

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


async def _delete_run(factory, run_id):
    """Clean up a run committed outside the rollback-scoped `db_session` fixture.

    The `engine`-based tests below open their own `async_sessionmaker` and
    call `.commit()` for real, so nothing rolls their `SimulationRun` back —
    unlike the rest of this file, which uses `db_session` and is cleaned up by
    its per-test transaction. `tests/integration/test_harness_smoke.py`'s
    `test_writes_are_rolled_back_*` pair is the canary that catches a leak
    like this (it sorts alphabetically after this file, so a leaked row here
    is still present when it runs); a future test copying this `engine`/
    `async_sessionmaker` harness needs its own cleanup too. Deleting the run
    is sufficient — `OpportunityAssessment.simulation_run_id` is
    ON DELETE CASCADE, so its row goes with it.
    """
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)  # cascades to the assessment
            await cleanup.commit()


async def _seed_run(db):
    run = SimulationRun()
    db.add(run)
    await db.flush()
    return run


async def test_narrative_fields_round_trip(db_session):
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        channel_name="general",
        company_or_project="Short label",
        headline="A blood test that says who responds to immunotherapy in liver cancer.",
        key_points=["The classifier does not exist yet", "Circadian confound unmeasured"],
        elevator_pitch="Hopkins has 39-plex cytokine data on 124 patients.",
    )
    db_session.add(row)
    await db_session.flush()
    db_session.expunge(row)

    stored = (
        await db_session.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == row.id)
        )
    ).scalar_one()
    assert stored.headline.startswith("A blood test")
    assert stored.key_points == [
        "The classifier does not exist yet",
        "Circadian confound unmeasured",
    ]
    assert stored.elevator_pitch.startswith("Hopkins has")


async def test_narrative_fields_default_to_sql_null_not_json_null(db_session):
    """`key_points` is JSONB, and `none_as_null=True` is what keeps Python None
    a real SQL NULL. Without it, `WHERE key_points IS NULL` misses the row —
    the exact defect 0031 and 0036 each had to repair once (A14)."""
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        key_points=None,
    )
    db_session.add(row)
    await db_session.flush()

    found = (
        await db_session.execute(
            select(OpportunityAssessment.id).where(
                OpportunityAssessment.id == row.id,
                OpportunityAssessment.key_points.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == row.id


async def test_persist_assessment_stores_the_four_narrative_fields(engine):
    """Sidecar items 6-8 and item 10 (`score_rationale`, 0048) reach their
    columns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "subject_agent_id": "wang",
            "company_or_project": "Short label",
            "headline": "A blood test that says who responds to immunotherapy.",
            "key_points": ["No classifier exists yet", "Circadian confound unmeasured"],
            "elevator_pitch": "Hopkins has cytokine data on 124 patients.",
            "score_rationale": "Feasibility carries the score; novelty is thin.",
            "recommendation": "conditional",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.headline.startswith("A blood test")
        assert row.key_points == ["No classifier exists yet", "Circadian confound unmeasured"]
        assert row.elevator_pitch.startswith("Hopkins has")
        assert row.score_rationale == "Feasibility carries the score; novelty is thin."
    finally:
        await _delete_run(factory, run_id)


async def test_a_non_list_key_points_degrades_to_null_and_keeps_raw_verdict(engine):
    """A20. A model that answers `key_points` with a string must not DataError
    the row out of existence — the row IS the archive. It degrades to NULL and
    raw_verdict keeps what was actually emitted, exactly like `red_flags` and
    `derisking_milestones` already do."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "key_points": "not a list at all",
            "recommendation": "pass",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.key_points is None
        assert row.raw_verdict["key_points"] == "not a list at all"
    finally:
        await _delete_run(factory, run_id)


async def test_a_wrong_typed_score_rationale_degrades_to_null_and_keeps_raw_verdict(
    engine,
):
    """A20 again, for 0048's column: a `score_rationale` that is not a string
    degrades to NULL and `raw_verdict` keeps what was emitted. A malformed
    narrative field must never cost the verdict."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "score_rationale": {"why": "an object, not a string"},
            "recommendation": "pass",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.score_rationale is None
        assert row.raw_verdict["score_rationale"] == {"why": "an object, not a string"}
    finally:
        await _delete_run(factory, run_id)


async def test_an_overlong_headline_or_project_label_warns_but_still_stores(
    engine, caplog,
):
    """Shape checks are WARNINGS, never drops (A4). The row IS the archive, so a
    headline over `_HEADLINE_SOFT_LIMIT` or a `company_or_project` over
    `_PROJECT_SOFT_LIMIT` is stored verbatim and merely logged — the project
    warning naming the 120-character #assessments-summary clip, which is the
    consequence an operator reading the log acts on."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    long_headline = "H" * 300
    long_project = "P" * 200
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": long_project,
                "headline": long_headline,
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.headline == long_headline
        assert row.company_or_project == long_project

        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "headline is 300 chars" in warnings
        assert "company_or_project is 200 chars" in warnings
        assert "clips it at 120" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_missing_key_point_group_is_stored_and_warned(engine, caplog):
    """D5: a partial grouped object stores rather than NULLing the whole field,
    and the omission is named in one WARNING — otherwise the relaxation would
    trade a loud failure for total silence."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    partial = {"significance": ["s"], "innovation": ["i"]}
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "key_points": partial,
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.key_points == partial

        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "key_points omits" in warnings
        assert "clinical_actionability" in warnings
    finally:
        await _delete_run(factory, run_id)


def test_normalize_rejects_an_unknown_group_key_and_says_so():
    """2026-09-14 audit. `normalize_key_points` rejects a dict carrying an
    unknown group key, which stores `key_points = NULL` and leaves the value
    only in `raw_verdict`. The per-group bounds check cannot see that case —
    with all five known keys present plus a sixth, nothing is absent and every
    value is a list — so the whole field used to be dropped in SILENCE. That is
    exactly the prompt-newer-than-image skew the 0048 deploy note describes."""
    from src.services.assessment_detail import normalize_key_points

    five = {k: ["x"] for k in (
        "significance", "innovation", "clinical_actionability",
        "key_questions", "commercial_potential",
    )}
    assert normalize_key_points({**five, "open_questions": ["y"]}) is None
    assert normalize_key_points({"significance": "not a list"}) is None


def test_normalize_accepts_legacy_list_and_the_five_group_object():
    """C1/C2: the flat list (scout_hub <= 1.2.0), the three-group object (1.3.0)
    and the five-group object (1.4.0) all round-trip unchanged."""
    from src.services.assessment_detail import normalize_key_points

    assert normalize_key_points(["a", "b", "c"]) == ["a", "b", "c"]
    five = {
        "significance": ["s"],
        "innovation": ["i1", "i2"],
        "clinical_actionability": ["c"],
        "key_questions": ["q"],
        "commercial_potential": ["p"],
    }
    assert normalize_key_points(five) == five
    three = {"significance": ["s"], "innovation": ["i"], "commercial_potential": ["c"]}
    assert normalize_key_points(three) == three


def test_normalize_rejects_wrong_shapes():
    """C2. A SUBSET of the known group keys round-trips (D5 / finding K1): exact
    set equality made one omitted group store NULL and lose the whole field to
    `raw_verdict`, the strictest possible reaction to the mildest possible
    defect. An UNKNOWN key is still rejected outright — that is real shape
    drift — and so are `{}` and a non-list value."""
    from src.services.assessment_detail import normalize_key_points

    assert normalize_key_points("not a list") is None
    assert normalize_key_points({}) is None                                # empty dict
    partial = {"significance": ["s"]}
    assert normalize_key_points(partial) == partial                        # D5: accepted
    assert normalize_key_points(
        {"significance": "s", "innovation": [], "commercial_potential": []}
    ) is None
    assert normalize_key_points(
        {"significance": [], "innovation": [], "commercial_potential": [], "extra": []}
    ) is None


async def test_persist_assessment_stores_strengths_and_risks(engine):
    """Sidecar items 11/12 (0049): valid bullet lists reach their columns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "strengths": ["S one", "S two"],
            "risks": ["R one", "R two"],
            "recommendation": "conditional",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.strengths == ["S one", "S two"]
        assert row.risks == ["R one", "R two"]
    finally:
        await _delete_run(factory, run_id)


async def test_wrong_typed_strengths_and_risks_degrade_to_null_and_keep_raw_verdict(
    engine,
):
    """A20: a `strengths` that is not a list, and a `risks` list that holds a
    non-string element, both degrade to NULL and `raw_verdict` keeps what was
    actually emitted."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "strengths": "not a list",
            "risks": [1, 2],
            "recommendation": "pass",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.strengths is None
        assert row.risks is None
        assert row.raw_verdict["strengths"] == "not a list"
        assert row.raw_verdict["risks"] == [1, 2]
    finally:
        await _delete_run(factory, run_id)


async def test_a_five_bullet_strengths_list_still_stores_and_warns(engine, caplog):
    """Shape checks are WARNINGS, never drops (A4): a count outside the 2-4
    contract bound is stored verbatim and merely logged."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    five = ["S one", "S two", "S three", "S four", "S five"]
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "strengths": five,
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.strengths == five

        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "strengths carries 5 bullets" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_persist_assessment_stores_landscape_and_evidence_maturity(engine):
    """Sidecar items 13/14 (0050): valid bullet lists reach their columns."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
            "company_or_project": "Short label",
            "competitive_landscape": ["Program A is Phase I.", "Program B lapsed."],
            "evidence_maturity": ["Biology: settled.", "Chemistry: unverified."],
            "recommendation": "conditional",
            "scores": {},
        })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.competitive_landscape == ["Program A is Phase I.", "Program B lapsed."]
        assert row.evidence_maturity == ["Biology: settled.", "Chemistry: unverified."]
    finally:
        await _delete_run(factory, run_id)


async def test_wrong_typed_landscape_degrades_to_null_and_keeps_raw_verdict(
    engine, caplog
):
    """A20: a malformed narrative field costs the field, never the verdict.

    The caplog assertions are what pin the SOFT-BOUND LOOP (step 3). Without
    them the loop could be reverted to ("strengths", "risks") and every other
    test here would still pass, because the column values come from step 4's
    `normalize_bullets` kwargs, not from the loop.
    """
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "competitive_landscape": "not a list",
                "evidence_maturity": ["Biology: settled.", ""],
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.competitive_landscape is None
        assert row.evidence_maturity is None
        assert row.raw_verdict["competitive_landscape"] == "not a list"
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "competitive_landscape was DROPPED" in warnings
        assert "evidence_maturity was DROPPED" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_five_bullet_evidence_maturity_still_stores_and_warns(engine, caplog):
    """The out-of-range bullet count is a WARNING, never a drop (spec §8), and
    this is the second test pinning the soft-bound loop's extension."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        five = ["one", "two", "three", "four", "five"]
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "evidence_maturity": five,
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.evidence_maturity == five
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "evidence_maturity carries 5 bullets" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_120_character_headline_warns_against_the_110_bound(engine, caplog):
    """The prose bound (scout_hub 1.7.0) and `_HEADLINE_SOFT_LIMIT` must not part
    company: at 140 the alarm was silent for exactly the headlines the 110 bound
    exists to catch."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "headline": "A" * 120,
                "recommendation": "conditional",
                "scores": {},
            })
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "contract asks for <=110" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_an_overlong_landscape_bullet_still_stores_and_warns(engine, caplog):
    """Spec §8's third case for the 0050 fields: the `_HUB_BULLET_CHARS` (200)
    bound is a WARNING, never a drop (A4). Exercised for `strengths` already;
    the extended loop must do the same for the two new fields."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    long_bullet = "x" * 260
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "competitive_landscape": [long_bullet, "Program B lapsed."],
                "recommendation": "conditional",
                "scores": {},
            })

        async with factory() as db:
            row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().one()
        assert row.competitive_landscape == [long_bullet, "Program B lapsed."]
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "competitive_landscape bullet is 260 chars" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_citation_outside_the_published_excerpt_warns(engine, caplog):
    """scout_hub 1.7.0's citation budget is otherwise unalarmed.

    Item 8 moved the provenance citation to sentence four and asks that
    sentences 1-4 END within ~550 chars so it lands inside the 600 that
    `#assessments-summary` publishes. A pitch that cites a source the
    published excerpt will not carry must say so at write time: the Slack
    post cannot be retracted, and staff would otherwise find out by reading
    the channel.
    """
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    # Citation pushed past the 600-char publish window: sentences 1-3 alone
    # overrun the budget, so the clipper cuts before sentence four.
    late = "A" * 580 + ". The work builds on https://doi.org/10.7554/eLife.94488. Tail."
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "elevator_pitch": late,
                "recommendation": "conditional",
                "scores": {},
            })
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "does not carry" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_citation_inside_the_published_excerpt_is_silent(engine, caplog):
    """The alarm's negative control: a compliant pitch must not warn, or the
    warning becomes noise staff learn to ignore."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    early = (
        "A" * 200 + ". " + "B" * 140
        + ". The work builds on https://doi.org/10.7554/eLife.94488.\n\n"
        + "C" * 300
    )
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "elevator_pitch": early,
                "recommendation": "conditional",
                "scores": {},
            })
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "does not carry" not in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_a_bare_doi_outside_the_excerpt_warns(engine, caplog):
    """Item 8 permits "DOI or PubMed link", so a citation legally carries no
    scheme. An alarm testing for `http` alone stays silent for exactly the
    citation style most likely to be written without one."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    late = "A" * 580 + ". The work builds on doi:10.7554/eLife.94488. Tail."
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "elevator_pitch": late,
                "recommendation": "conditional",
                "scores": {},
            })
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "does not carry" in warnings
    finally:
        await _delete_run(factory, run_id)


async def test_an_early_url_does_not_mask_a_dropped_later_citation(engine, caplog):
    """The alarm compares citation SETS. A trial-registry URL in sentence one
    survives the cut; the sentence-four DOI does not. Testing only "is any
    citation left" would call that fine — and it is the exact shape the alarm
    exists to catch."""
    import logging

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import SimulationEngine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    pitch = (
        "Patients today enrol at https://clinicaltrials.gov/study/NCT01234567. "
        + "A" * 520
        + ". The work builds on https://doi.org/10.7554/eLife.94488. Tail."
    )
    try:
        stub = SimulationEngine(
            agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
        )
        with caplog.at_level(logging.WARNING):
            await SimulationEngine._persist_assessment(stub, "blackbird", "general", {
                "company_or_project": "Short label",
                "elevator_pitch": pitch,
                "recommendation": "conditional",
                "scores": {},
            })
        warnings = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "does not carry" in warnings, (
            "the surviving NCT URL must not mask the dropped eLife DOI"
        )
        assert "eLife.94488" in warnings, "the warning names what was lost"
    finally:
        await _delete_run(factory, run_id)
