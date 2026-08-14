import pytest

from src.agent.thread_guidance import phase4_guidance


@pytest.mark.parametrize("count,expected", [(1, "EXPLORE"), (4, "EXPLORE"),
                                           (5, "DECIDE"), (11, "DECIDE"),
                                           (12, "MUST CONCLUDE"), (99, "MUST CONCLUDE")])
def test_phase_boundaries_are_unchanged(count, expected):
    for role in ("pi_lab", "scout_hub"):
        assert phase4_guidance(role, count)[0] == expected


def test_pi_lab_strings_are_byte_identical_to_the_pinned_snapshot():
    # Spot-anchors of the current §4 text (docs/specs/2026-08-07-pi-bot-prompts.md),
    # pinned in tests/characterization/__snapshots__/test_agent_turn_gm.ambr. Any
    # drift here changes every PI bot's behaviour.
    _, decide_guidance, decide_instructions = phase4_guidance("pi_lab", 5)
    assert "that's a question for my PI" in decide_guidance
    assert "differentiation" in decide_guidance
    assert decide_instructions == (
        "Write a reply that closes the biggest gap in what the hub still does not know about "
        "your idea, or answers its last question directly. Do not oversell and do not ask to "
        "be introduced to another lab."
    )

    _, conclude_guidance, conclude_instructions = phase4_guidance("pi_lab", 12)
    assert "Do NOT post a :memo: Summary" in conclude_guidance
    assert "Do NOT reply with a bare ✅" in conclude_guidance
    assert "Never close by proposing that the two of you work together" in conclude_instructions


def test_unknown_role_falls_back_to_pi_lab():
    assert phase4_guidance("nonexistent", 5) == phase4_guidance("pi_lab", 5)


def test_scout_hub_never_asks_for_a_collaboration_proposal():
    for count in (1, 5, 12):
        _, guidance, instructions = phase4_guidance("scout_hub", count)
        blob = guidance + instructions
        assert ":memo:" not in blob
        assert "collaboration proposal" not in blob
        assert "your lab's recent work" not in blob
        assert "complementarity" not in blob


def test_scout_hub_decide_phase_works_the_gating_criteria():
    # 3-criteria gating contract (Baltimore location gating was dropped, dcc5212):
    # credible technology source, freedom-to-operate, differentiation.
    _, guidance, instructions = phase4_guidance("scout_hub", 5)
    blob = (guidance + instructions).lower()
    assert "credible" in blob
    assert "freedom-to-operate" in blob or "fto" in blob
    assert "differentiation" in blob
    assert "baltimore" not in blob
    # Part C.4 of the rubric — the target-level scientific checklist.
    assert "proof of mechanism" in blob


def test_scout_hub_conclusion_carries_the_verdict_and_names_the_artifact():
    phase, guidance, instructions = phase4_guidance("scout_hub", 12)
    assert phase == "MUST CONCLUDE"
    blob = guidance + instructions
    assert ":mag:" in blob
    assert "⏸️" in blob


def test_scout_hub_decide_phase_directs_the_panel():
    _, guidance, instructions = phase4_guidance("scout_hub", 5)
    both = guidance + instructions
    assert "consult_specialist" in both
    # The two personas the rubric never had must be named explicitly, or the
    # hub will keep consulting only the ones it already thinks in terms of.
    assert "scientific" in both
    assert "chemistry" in both


def test_scout_hub_conclude_warns_that_the_floor_bites_later():
    _, guidance, instructions = phase4_guidance("scout_hub", 12)
    both = (guidance + instructions).lower()
    assert "refus" in both or "reject" in both


def test_pi_lab_guidance_is_untouched_by_the_panel():
    for count in (2, 8, 12):
        _, guidance, instructions = phase4_guidance("pi_lab", count)
        assert "consult_specialist" not in guidance + instructions
