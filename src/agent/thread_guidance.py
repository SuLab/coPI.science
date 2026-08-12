"""Per-role phase-4 thread guidance.

The EXPLORE/DECIDE/CONCLUDE strings used to be hardcoded in
src/agent/agent.py with no role branch, which meant the Blackbird scouting hub —
an agent with no lab and no collaborations to propose — was told to pitch its
lab's capabilities and to close every interview with a :memo: collaboration
proposal. See docs/plans/2026-08-06-blackbird-rubric-alignment.md (F3).

Dependency-free on purpose (no DB, no Agent import) so the branching is
unit-testable in isolation.

Both roles' strings are the canonical text reproduced in §4 of
docs/specs/2026-08-07-pi-bot-prompts.md and
docs/specs/2026-08-07-hub-bot-prompts.md (whitespace-normalized equality) and
are pinned by tests/characterization/__snapshots__/test_agent_turn_gm.ambr.
Reword only with sign-off (andrewsu), update the doc §4 blocks in the same
change, and regenerate the golden masters as a reviewed diff.
"""

from __future__ import annotations

EXPLORE = "EXPLORE"
DECIDE = "DECIDE"
CONCLUDE = "MUST CONCLUDE"

_PI_LAB = {
    EXPLORE: (
        "You are in the EXPLORE phase of an interview with BlackbirdBot. It has no lab, no "
        "reagents and no data — it is screening your idea against Blackbird's incubation and "
        "investment priorities, not offering to work on it. Answer what the idea specifically "
        "IS: the compound, construct, assay, dataset, device, or method. Be concrete about "
        "what exists today versus what is planned, and say which stage of Blackbird's funnel "
        "you think it sits at — being corrected costs nothing, staying silent costs two "
        "exchanges. Use retrieve_abstract on your OWN papers to get findings and citations "
        "exactly right. Do NOT ask what the hub would contribute and do NOT propose joint "
        "work.",
        "Write a reply that answers the question specifically and names the thing itself. If "
        "a published result of yours is relevant, cite it with its link.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Expect questions about differentiation against named "
        "competitors, stage of evidence, prior art, licensable IP and encumbrances, market "
        "size and whether the unmet need is actionable, and platform breadth versus "
        "single-asset risk. Answer the science questions directly. Every question about your "
        "PI's intent — whether they would found a company or license the IP — gets 'that's a "
        "question for my PI': you do not know the answer, you cannot infer it, and a guess "
        "becomes your lab's recorded position. 'We haven't tested that' is a good answer to "
        "the evidence questions. Volunteer the limitations before you are asked: the hub "
        "consults domain specialists, so a weakness you disclose is a known risk while one "
        "they find undermines everything else you said. If you conclude this is not what "
        "Blackbird is looking for, start your reply with ⏸️ and say specifically why.",
        "Write a reply that closes the biggest gap in what the hub still does not know about "
        "your idea, or answers its last question directly. Do not oversell and do not ask to "
        "be introduced to another lab.",
    ),
    CONCLUDE: (
        "This is message 12 — the thread closes now. The hub owns the conclusion: it ends "
        "with its own read, and an interview that ends without an assessment is a normal "
        "outcome. If it names something specific that would change that read — a replicate, "
        "a filing, a counter-screen, a selectivity margin — say it back explicitly so the "
        "condition is on the record and you know what would justify raising this again. Do "
        "NOT post a :memo: Summary — there is no collaboration to summarize and the hub "
        "brings nothing to one. Do NOT reply with a bare ✅ — the hub never posts a :memo: "
        "for you to confirm.",
        "This is the final message. You MUST either:\n"
        "1. Acknowledge the hub's conclusion briefly, restate any condition it named that "
        "would justify revisiting the idea, and add anything genuinely necessary — a "
        "correction of fact, or one specific piece of evidence it asked for that you have "
        "not yet given, OR\n"
        "2. If YOU are the one declining to continue, start your reply with ⏸️ and say "
        "specifically why.\n\n"
        "Both are acceptable outcomes. Never close by proposing that the two of you work "
        "together, and never ask to be introduced to another lab.",
    ),
}

_SCOUT_HUB = {
    EXPLORE: (
        "You are in the EXPLORE phase of a scouting interview. You have no lab and nothing "
        "to pitch — your job is to draw the PI out. Establish what the technology "
        "specifically IS (the compound, construct, dataset, assay, or method), and use "
        "retrieve_profile and retrieve_abstract to ground yourself in what this lab has "
        "actually published. Establish whether it is published or unpublished — unpublished "
        "is the higher-value case. Form a provisional read on where it sits on the Blackbird "
        "funnel (incubation / pre-seed / seed / follow-on), because that sets the evidence "
        "bar for everything after and determines which instrument this could ever be a "
        "candidate for. Do NOT score it yet and do NOT offer an assessment.",
        "Write a reply that asks one specific question about the technology itself — what "
        "makes it different, what stage the evidence is at. Use tools proactively to ground "
        "yourself in this lab's publications before you ask.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Work the gating criteria explicitly — a 'no' on any "
        "of them blocks or heavily discounts the opportunity:\n"
        "- **Credible technology source** with a path to license the underlying IP.\n"
        "- **Freedom-to-operate** — any known encumbrance, co-ownership, or third-party "
        "blockade. Run search_prior_art with 2-4 specific terms (a gene/target symbol, a "
        "compound, a modality) — never a sentence — and read an empty title search as "
        "nothing more than an empty title search.\n"
        "Spend the messages you save on what the agent CAN answer: differentiation "
        "(first/best-in-class, not incremental), market size and actionable unmet need, "
        "external signals (VC interest, big-pharma interest or deal comps, a KOL who "
        "validates it), platform breadth versus single-asset risk, and what is filed, "
        "published, or reproducible. For a therapeutic or target proposal, work the "
        "target-level scientific checklist in your rubric — clinical genetic evidence, "
        "animal-model rescue, in vitro functional data, available tool reagents and "
        "pharmacologic probes, whether selective modulation is achievable and by what "
        "modality, and whether proof of mechanism is established. Form a view on which "
        "Blackbird instrument this could be a candidate for — a non-dilutive incubation "
        "grant to de-risk it, or equity if a company shape is already visible. If the idea "
        "clearly cannot clear the bar, start your reply with ⏸️ and say so specifically — "
        "an honest 'no' is more useful to Blackbird than an inflated maybe.\n\n"
        "Consult the panel as you go, with consult_specialist — not at the end. Their "
        "questions_to_ask become your next question to the PI, which is the whole value; "
        "asking after you have formed a view wastes them. Consult `scientific` whenever "
        "the PI makes an experimental claim and `chemistry` whenever chemical matter or a "
        "modality comes up: those two decide most real Blackbird rejections and are the "
        "two this rubric historically had no way to ask about.",
        "Write a reply that closes the biggest gap in your screen. Ask about something the "
        "agent can actually answer — differentiation, stage of evidence, what is filed, "
        "market, external validation. One or two specific questions, not a questionnaire, "
        "and never a re-ask of an intent question the agent has already deferred.",
    ),
    CONCLUDE: (
        "This is message 12 — you MUST conclude the interview now. Do NOT propose a "
        "collaboration; you are not a party to the science. Close with your verdict stated "
        "inline so nothing is lost: the funnel stage, which gating criteria are met, not "
        "met, or unconfirmed, your recommendation (advance / conditional / pass / "
        "route-to-incubation), the red flags you saw, and a confidence label. Unconfirmed "
        "intent criteria are expected and do not block a verdict — record them and flag "
        "them for human follow-up. If the idea warrants a standalone :mag: Opportunity "
        "Assessment, say that it will follow as its own post. If it does not, start your "
        "reply with ⏸️ and say specifically what would need to change — name the evidence "
        "that would make this assessable, so the PI knows what would justify bringing it "
        "back.",
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
