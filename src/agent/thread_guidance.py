"""Per-role phase-4 thread guidance.

The EXPLORE/DECIDE/CONCLUDE strings used to be hardcoded in
src/agent/agent.py with no role branch, which meant the Blackbird scouting hub —
an agent with no lab and no collaborations to propose — was told to pitch its
lab's capabilities and to close every interview with a :memo: collaboration
proposal. See docs/plans/2026-08-06-blackbird-rubric-alignment.md (F3).

Dependency-free on purpose (no DB, no Agent import) so the branching is
unit-testable in isolation.

The ``pi_lab`` strings are BYTE-IDENTICAL to the pre-refactor literals and are
pinned by tests/characterization/__snapshots__/test_agent_turn_gm.ambr. Do not
reword them.
"""

from __future__ import annotations

EXPLORE = "EXPLORE"
DECIDE = "DECIDE"
CONCLUDE = "MUST CONCLUDE"

_PI_LAB = {
    EXPLORE: (
        "You are in the EXPLORE phase. Share relevant specifics from your lab's recent work. "
        "Ask clarifying questions about the other lab's capabilities. Use retrieve_profile and "
        "retrieve_abstract tools to learn more. Do NOT propose a full collaboration yet.",
        "Write a reply that shares specific details from your lab and asks a clarifying "
        "question. Use tools proactively to research the other lab.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Narrow the scope: is there genuine complementarity? "
        "Can you name a specific first experiment? If yes, build toward a :memo: Summary proposal. "
        "If no, start your reply with ⏸️ and explain graciously why there's no viable collaboration. "
        "It is OK to conclude with no proposal — not every conversation leads to one.",
        "Write a reply that moves toward a conclusion. Either build toward a specific "
        ":memo: Summary proposal or acknowledge insufficient overlap.",
    ),
    CONCLUDE: (
        "This is message 12 — you MUST conclude the thread now. Either post a :memo: Summary "
        "with a collaboration proposal, or close gracefully acknowledging insufficient overlap.",
        "This is the final message. You MUST either:\n"
        "1. Post a :memo: Summary with a specific collaboration proposal, OR\n"
        "2. If the other agent already posted a :memo: Summary you agree with AS-IS, reply with ✅ "
        "(no modifications — if you want changes, post your own revised :memo: Summary instead), OR\n"
        "3. Start your reply with ⏸️ and close gracefully explaining why there's no good proposal.\n\n"
        "Option 3 is perfectly acceptable — not every conversation should end in a proposal.",
    ),
}

_SCOUT_HUB = {
    EXPLORE: (
        "You are in the EXPLORE phase of a scouting interview. You have no lab and nothing "
        "to pitch — your job is to draw the PI out. Establish what the technology "
        "specifically IS (the compound, construct, dataset, assay, or method), and use "
        "retrieve_profile and retrieve_abstract to ground yourself in what this lab has "
        "actually published. Form a provisional read on where it sits on the Blackbird "
        "funnel (incubation / pre-seed / seed / follow-on), because that sets the evidence "
        "bar for everything after. Do NOT score it yet and do NOT offer an assessment.",
        "Write a reply that asks one specific question about the technology itself — what "
        "makes it different, what stage the evidence is at. Use tools proactively to ground "
        "yourself in this lab's publications before you ask.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Work the gating criteria explicitly — a 'no' on any "
        "of them blocks or heavily discounts the opportunity:\n"
        "- **Baltimore commitment.** ASK whether the PI would anchor a NewCo in Baltimore "
        "(ideally Blackbird BioHub) and keep forward activities there. A JHU address is NOT "
        "a Baltimore commitment — the institution is not the answer to this question, the "
        "founder is. Treat it as unconfirmed until the PI says it.\n"
        "- **Credible technology source** with a path to license the underlying IP.\n"
        "- **Freedom-to-operate** — any known encumbrance, co-ownership, or third-party "
        "blockade. Run search_prior_art with 2-4 specific terms (a gene/target symbol, a "
        "compound, a modality) — never a sentence — and read an empty title search as "
        "nothing more than an empty title search.\n"
        "Then probe the heaviest scoring dimensions: differentiation (first/best-in-class, "
        "not incremental), market size and actionable unmet need, team/founder quality, and "
        "external signals (VC interest, big-pharma interest or deal comps, a KOL who "
        "validates it). Ask about platform breadth versus single-asset risk. For a "
        "therapeutic or target proposal, work the target-level scientific checklist in "
        "your private instructions — clinical genetic evidence, animal-model rescue, "
        "in vitro functional data, available tool reagents and pharmacologic probes, "
        "whether selective modulation is achievable and by what modality, and whether "
        "proof of mechanism is established. If the idea clearly cannot clear the bar, "
        "start your reply with ⏸️ and say so specifically — an honest 'no' is more "
        "useful to Blackbird than an inflated maybe.\n\n"
        "Consult the panel as you go, with consult_specialist — not at the end. Their "
        "questions_to_ask become your next question to the PI, which is the whole value; "
        "asking after you have formed a view wastes them. Consult `scientific` whenever "
        "the PI makes an experimental claim and `chemistry` whenever chemical matter or a "
        "modality comes up: those two decide most real Blackbird rejections and are the "
        "two this rubric historically had no way to ask about.",
        "Write a reply that closes the biggest gap in your screen. Ask about the gating "
        "criteria you still cannot answer — Baltimore commitment, licensable IP, FTO — or "
        "about differentiation, market, or external validation. One or two specific "
        "questions, not a questionnaire.",
    ),
    CONCLUDE: (
        "This is message 12 — you MUST conclude the interview now. Do NOT propose a "
        "collaboration; you are not a party to the science. Close with your verdict stated "
        "inline so nothing is lost: the funnel stage, which gating criteria are met, not "
        "met, or unconfirmed, your recommendation (advance / conditional / pass / "
        "route-to-incubation), the red flags you saw, and a confidence label. If the idea "
        "warrants a standalone :mag: Opportunity Assessment, say that it will follow as its "
        "own post. If it does not, start your reply with ⏸️ and say specifically what would "
        "need to change.",
        "This is the final message. You MUST either:\n"
        "1. Close the interview with your inline verdict — funnel stage, gating status, "
        "recommendation (advance / conditional / pass / route-to-incubation), red flags, "
        "confidence label — noting that a standalone :mag: Opportunity Assessment will "
        "follow, OR\n"
        "2. Start your reply with ⏸️ and close gracefully, naming the specific missing "
        "piece that would make this assessable.\n\n"
        "Option 2 is perfectly acceptable — most interviews should end there. Never close "
        "by proposing that the two labs work together.\n\n"
        "If you are heading for advance or conditional, the domains this idea touches must "
        "ALREADY have been consulted — the assessment turn has no tools, so a verdict whose "
        "panel was never convened is refused and nothing is persisted. If you have not "
        "consulted them by now, either consult them in this reply or conclude at pass.",
    ),
}

_BY_ROLE = {"pi_lab": _PI_LAB, "scout_hub": _SCOUT_HUB}


def phase4_guidance(role: str, message_count: int) -> tuple[str, str, str]:
    """Return ``(thread_phase, phase_guidance, instructions)`` for ``role``.

    An unknown role degrades to ``pi_lab`` — the same "absence of overrides is
    pi_lab" rule src/agent/roles.py uses for prompt resolution.
    """
    if message_count <= 4:
        phase = EXPLORE
    elif message_count <= 11:
        phase = DECIDE
    else:
        phase = CONCLUDE
    guidance, instructions = _BY_ROLE.get(role, _PI_LAB)[phase]
    return phase, guidance, instructions
