import itertools
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    LlmCallLog,
    OpportunityAssessment,
    SpecialistConsult,
)
from src.services import assessment_detail as ad
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, load_rubric
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
    PROVENANCE_UNSTAMPED,
)
from tests import factories
from tests.characterization.freeze.fixtures import seed_frozen_interview
from tests.unit import _frozen_assessment_detail as frozen
from tests.unit._seeded_assessments import seeded_assessments  # noqa: F401

_REV = SimpleNamespace(scale_max=5, scale_min=1)
_A = [
    SimpleNamespace(gating={"ip_clear": "met", "x": "not_met", "y": "unconfirmed", "z": True}, red_flags=["f"],
                    dimension_rationales={"team": "why"}, scores={"team": 5}),
    SimpleNamespace(gating=None, red_flags="nope", dimension_rationales=None, scores=None),
    SimpleNamespace(gating={}, red_flags=[], dimension_rationales={}, scores={}),
]
_DIMS = [None, [], [{"key": "team", "title": "Team", "score": 4.8}, {"key": "b", "score": 1.0},
                    {"key": "c", "score": None}, {"key": "d", "score": 3}, "junk"]]
_CONSULTS = [
    None, [],
    [{"domain": "legal", "verdict_signal": "blocking", "concerns": ["a", "b"], "created_at": 2},
     {"domain": "legal", "verdict_signal": "adequate", "established": ["c"], "created_at": 1},
     {"domain": "clinical", "verdict_signal": "high", "created_at": 3},
     {"domain": "ip", "verdict_signal": "gap", "reply_truncated": True},
     {"domain": "reg", "verdict_signal": "gap", "read_state": "defaulted"}],
]


@pytest.mark.parametrize("a,dims,consults,rev,prov", list(itertools.product(
    _A, _DIMS, _CONSULTS, [_REV, None], [PROVENANCE_LIVE, PROVENANCE_UNKNOWN, None])))
def test_strengths_and_risks_equal(a, dims, consults, rev, prov):
    kw = dict(dimensions=dims, consults=consults, revision=rev, revision_provenance=prov)
    assert ad.derive_strengths_and_risks(a, **kw) == frozen.derive_strengths_and_risks(a, **kw)


HUB = "blackbird"
# An archived revision in prompts/rubric/revisions.toml (not the live 3.5.0).
ARCHIVED_STAMP = ("3.2.0", "42aec0479ac6")


async def _seed_interview(db, *, run, subject, channel, stamp, base, users):
    """One interview in the chat-parity shape (tests/integration/
    test_assessment_chat_parity.py::_seed): a transcript with hub, human and
    other-agent replies, an earlier same-domain consult the latest supersedes, a
    truncated consult, another thread's consult, a logged tool turn that places on
    the hub's question and one that places nowhere, plus every human-review table
    (a review recorded by an admin, status events, an assignment)."""
    version, content_hash = stamp
    root = f"{base:.6f}"
    question_ts = f"{base + 60:.6f}"
    verdict_ts = f"{base + 240:.6f}"
    for fields in (
        dict(agent_id=subject, message_ts=root, thread_ts=None, phase="new_post", posted_at=base,
             content="Our isogenic panel.", sender_name="LabBot"),
        dict(agent_id=HUB, message_ts=question_ts, thread_ts=root, phase="thread_reply",
             posted_at=base + 60, content="What are the controls?"),
        dict(agent_id=None, message_ts=f"{base + 120:.6f}", thread_ts=root, phase="thread_reply",
             posted_at=base + 120, content="A human comment", sender_name="A Person", is_bot=False),
        dict(agent_id="otherlab", message_ts=f"{base + 180:.6f}", thread_ts=root,
             phase="thread_reply", posted_at=base + 180, content="Another agent's reply"),
        dict(agent_id=HUB, message_ts=verdict_ts, thread_ts=root, phase="thread_reply",
             posted_at=base + 240, content="Verdict: conditional."),
    ):
        await factories.make_agent_message(db, run=run, channel_name=channel, **fields)
    other_root = f"{base + 1000:.6f}"
    await factories.make_agent_message(db, run=run, agent_id="otherlab", channel_name=channel,
                                       message_ts=other_root, phase="new_post",
                                       posted_at=base + 1000, content="Another interview")

    def consult(offset, thread=root, **fields):
        data = dict(simulation_run_id=run.id, agent_id=HUB, subject_agent_id=subject,
                    thread_id=thread, channel_name=channel, raw_opinion="raw opinion",
                    context_excerpt="excerpt", read_state="parsed",
                    created_at=datetime.fromtimestamp(base + offset, UTC))
        data.update(fields)
        return SpecialistConsult(**data)

    db.add_all([
        consult(70, domain="clinical", question="early", verdict_signal="adequate",
                confidence="high", concerns=["early concern"], questions_to_ask=["q"],
                established=["not latest"]),
        # Same timestamp as the hub's question: the stable sort's message-then-consult rule.
        consult(60, domain="ip", question="tie", verdict_signal="blocking", confidence="low",
                concerns=["ip concern"], questions_to_ask=[]),
        consult(90, domain="clinical", question="latest", verdict_signal="adequate",
                confidence="moderate", concerns=["latest concern"], questions_to_ask=["q2"],
                established=["one", "two", "three", "four"]),
        consult(100, domain="legal", question="cut", verdict_signal="gap", confidence="moderate",
                concerns=["cut concern"], questions_to_ask=[], truncated=True,
                read_state="truncated"),
        consult(1010, thread=other_root, domain="clinical", question="other thread",
                verdict_signal="gap", confidence="low", concerns=["x"], questions_to_ask=[]),
    ])

    def tool_log(offset, response_text, tool="consult_specialist"):
        return LlmCallLog(
            simulation_run_id=run.id, agent_id=HUB, phase="thread_reply", channel=channel,
            model="claude-test", system_prompt="system",
            messages_json=[
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": tool,
                 "input": {"domain": "clinical", "question": "Is n enough?"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                 "content": "opinion text"}]},
            ],
            response_text=response_text,
            created_at=datetime.fromtimestamp(base + offset, UTC),
        )

    db.add_all([
        tool_log(60, "<slack_message>\nWhat are the controls?\n</slack_message>"),
        tool_log(200, "<slack_message>\nA reply that was edited away\n</slack_message>",
                 tool="search_prior_art"),
    ])
    dims = [d.key for d in load_rubric().dimensions]
    assessment = OpportunityAssessment(
        id=uuid.uuid4(), simulation_run_id=run.id, agent_id=HUB, subject_agent_id=subject,
        channel_name=channel, slack_ts=verdict_ts, thread_id=root,
        company_or_project=f"Interview {subject}", headline="A headline",
        elevator_pitch="A pitch.", key_points={"significance": ["kp"]},
        strengths=["hub strength"], risks=["hub risk"],
        recommendation="conditional", confidence="Moderate", weighted_score=3.2,
        band="conditional",
        gating={"life_sciences_domain": "met", "credible_science": "not_met",
                "translational_potential": "unconfirmed"},
        scores={dims[0]: 4, dims[1]: 2, dims[2]: 3, "legacy_dim": 1},
        dimension_rationales={dims[0]: "reason one", dims[1]: "reason two"},
        red_flags=["A red flag. Second sentence."], rationale="Rationale.",
        panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True,
        rubric_version=version, rubric_content_hash=content_hash,
    )
    db.add(assessment)
    await db.flush()
    admin, manager, reviewer = users
    db.add_all([
        AssessmentReview(assessment_id=assessment.id, reviewer_user_id=reviewer.id,
                         reviewer_name=reviewer.name, score=4, dimension_scores={dims[0]: 5},
                         rubric_version=RUBRIC_VERSION, rubric_content_hash=RUBRIC_CONTENT_HASH,
                         comment="looks fine", feedback_mode="learn",
                         recorded_by_user_id=admin.id,
                         created_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC)),
        AssessmentReview(assessment_id=assessment.id, reviewer_user_id=None,
                         reviewer_name="Old reviewer", score=2, dimension_scores=None,
                         rubric_version=None, rubric_content_hash=None, comment="",
                         feedback_mode="log_only",
                         created_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC)),
        AssessmentReviewEvent(assessment_id=assessment.id, action="approved",
                              actor_user_id=manager.id, actor_name=manager.name,
                              created_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC)),
        AssessmentReviewEvent(assessment_id=assessment.id, action="disapproved",
                              actor_user_id=None, actor_name="Gone",
                              created_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC)),
        AssessmentReviewAssignment(assessment_id=assessment.id, assignee_user_id=reviewer.id,
                                   assignee_name=reviewer.name, assigned_by_user_id=manager.id,
                                   assigned_by_name=manager.name),
    ])
    await db.flush()
    return assessment.id


@pytest_asyncio.fixture
async def seeded_detail_assessments(db_session, seeded_assessments):  # noqa: F811
    """The three bare rows, the chat golden's superseded-verdict interview
    (tests/characterization/freeze/fixtures.py: live stamp, consult, transcript,
    review), and full interviews stamped live, archived, unknown and NULL."""
    users = (
        await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Ada Admin"),
        await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Max Manager"),
        await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Rae Reviewer"),
    )
    pi = await factories.make_user(db_session, name="Pat PI")
    await factories.make_agent(db_session, user=pi, agent_id="detailpi")
    golden = await seed_frozen_interview(db_session)
    run = await factories.make_simulation_run(db_session)
    # Whole seconds: a sub-microsecond base would break the consult/message tie below.
    base = float(int(time.time()) - 7200)
    ids = list(seeded_assessments) + [golden]
    for i, (subject, stamp) in enumerate((
        ("detailpi", (RUBRIC_VERSION, RUBRIC_CONTENT_HASH)),
        ("lab-archived", ARCHIVED_STAMP),
        ("lab-unknown", ("0.0.1-unknown", "0badc0de0000")),
        ("lab-null", (None, None)),
    )):
        ids.append(await _seed_interview(
            db_session, run=run, subject=subject, channel=f"detail-split-{i}", stamp=stamp,
            base=base + i * 5000, users=users,
        ))
    return ids


async def _detail(build, db, assessment_id, **kw):
    """The detail dict plus the per-row attributes `_load_review_feedback` sets on
    the (identity-mapped, shared) review rows, captured before the other build
    overwrites them."""
    out = await build(db, assessment_id, **kw)
    extras = [
        (r.id, r.dimension_rows, r.dimension_provenance, r.recorded_by_name)
        for r in out["review_feedback"]
    ]
    return out, extras


@pytest.mark.integration
@pytest.mark.parametrize("admin_view,viewer_is_staff", [(True, True), (False, False), (False, True)])
async def test_build_assessment_detail_equal(db_session, seeded_detail_assessments, admin_view,
                                            viewer_is_staff):
    seen = set()
    for assessment_id in seeded_detail_assessments:
        kw = dict(admin_view=admin_view, viewer_is_staff=viewer_is_staff)
        new, new_extras = await _detail(ad.build_assessment_detail, db_session, assessment_id, **kw)
        old, old_extras = await _detail(frozen.build_assessment_detail, db_session, assessment_id,
                                        **kw)
        assert new == old, assessment_id
        assert new_extras == old_extras, assessment_id
        kinds = {entry["kind"] for entry in new["timeline"]}
        seen.add(new["revision_provenance"])
        if "consult" in kinds and "message" in kinds:
            seen.add("timeline")
        if any(entry.get("tool_turns") for entry in new["timeline"]):
            seen.add("placed-turn")
        if new["unplaced_turns"]:
            seen.add("unplaced-turn")
        if new["review_feedback"] and new["review_status_history"] and new["review_assignments"]:
            seen.add("reviews")
        if new["review_capable_users"]:
            seen.add("roster")
        if new["pi_user_id"]:
            seen.add("pi-link")
    # The seed reaches every path the split moved, so equality is not vacuous.
    expected = {PROVENANCE_LIVE, PROVENANCE_ARCHIVED, PROVENANCE_UNKNOWN, PROVENANCE_UNSTAMPED,
                "timeline", "reviews", "pi-link"}
    if admin_view:
        expected |= {"placed-turn", "unplaced-turn"}
    if viewer_is_staff:
        expected.add("roster")
    assert expected <= seen
    if not viewer_is_staff:
        assert "roster" not in seen
