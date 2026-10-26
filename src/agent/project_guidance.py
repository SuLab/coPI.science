"""Per-turn guidance in a project thread (spec §5.8, D55).

A project thread is the thread under a lab's reply to one of BlackbirdBot's projects
(``MessageLog.is_project_thread``). Its turns are banded by the ordinal of the message about to
be written, as interview turns are by ``thread_guidance.phase4_guidance``, but with project
strings: BlackbirdBot refines the plan with the lab and closes it on its last turn (or earlier,
once the lab's latest reply starts with ✅ and it agrees).

Dependency-free apart from ``role_capabilities`` (no DB, no Agent import), like
``thread_guidance``, so the bands are unit-testable in isolation. ``log_phase`` is written to
``llm_call_logs.thread_phase`` (String(20)).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agent.role_capabilities import hub_role_names

THREAD_KIND_INTERVIEW = "interview"
THREAD_KIND_PROJECT = "project"

#: The last ordinal of the open band (both roles).
OPEN_UNTIL = 4
#: The first ordinal of BlackbirdBot's close band: it must close there.
HUB_CLOSE_FROM = 11
#: The first ordinal of the lab's final-word band.
LAB_FINAL_FROM = 9

_HUB_EARLY_CLOSE = (
    "The lab's latest reply starts with ✅: it agrees the plan as it stands is feasible and "
    "complete. If you agree too, close now: start with ✅, state the final plan and add the "
    "<project_json> block. If you do not, say what is still missing and carry on."
)


@dataclass(frozen=True)
class ProjectTurn:
    """One turn's band in a project thread. ``must_close``: BlackbirdBot's last turn, whose
    reply closes the project whatever it holds. ``may_close_early``: a ``<project_json>``
    block in this reply counts as a close (the hub, after the lab's ✅)."""

    band: str
    label: str
    guidance: str
    instructions: str
    must_close: bool
    may_close_early: bool
    log_phase: str


def _hub_turn(ordinal: int, lab_agrees: bool) -> ProjectTurn:
    if ordinal >= HUB_CLOSE_FROM:
        return ProjectTurn(
            band="close",
            label="MUST CLOSE",
            guidance="This is your last turn: you MUST close the project now.",
            instructions=(
                "Start the reply with ✅ if you agree the plan is feasible and complete, or ⚠️ "
                "if you do not; state the final plan; then add the <project_json> block with "
                "every field, the gaps and the disagreements."
            ),
            must_close=True,
            may_close_early=False,
            log_phase="project_close",
        )
    if ordinal <= OPEN_UNTIL:
        band, label, log_phase = "open", "OPEN", "project_open"
        guidance = (
            "Open: confirm what the lab has for this project — its models, samples, data and "
            "methods — and ask what it would change in the opening plan."
        )
        instructions = (
            "Write a reply that asks one or two specific questions to fill the plan's open "
            "fields."
        )
    else:
        band, label, log_phase = "refine", "REFINE", "project_refine"
        guidance = (
            "Refine: test the controls, the standard of care, the readout, and the time and "
            "budget. Consult the scientific and clinical specialists on the controls and the "
            "standard of care if you have not yet."
        )
        instructions = "Write a reply that settles the next open field or disagreement."
    if lab_agrees:
        guidance += "\n\n" + _HUB_EARLY_CLOSE
    return ProjectTurn(
        band=band, label=label, guidance=guidance, instructions=instructions,
        must_close=False, may_close_early=lab_agrees, log_phase=log_phase,
    )


def _lab_turn(ordinal: int) -> ProjectTurn:
    if ordinal <= OPEN_UNTIL:
        band, label, log_phase = "open", "OPEN", "project_open"
        guidance = (
            "Open: say what your lab would use for this project and what you would change in "
            "the opening plan."
        )
        instructions = (
            "Answer BlackbirdBot's questions specifically, naming the models, samples and "
            "methods your lab has actually used."
        )
    elif ordinal < LAB_FINAL_FROM:
        band, label, log_phase = "refine", "REFINE", "project_refine"
        guidance = (
            "Refine: settle the open fields with BlackbirdBot, and correct anything it has "
            "wrong about your lab's work."
        )
        instructions = (
            "Answer specifically. Start the reply with ✅ only if you agree the plan as it now "
            "stands is feasible and complete."
        )
    else:
        band, label, log_phase = "final", "FINAL WORD", "project_final"
        guidance = "This may be your last turn before BlackbirdBot closes."
        instructions = (
            "Start the reply with ✅ if you agree the plan is feasible and complete, or ⚠️ "
            "followed by what is still missing or disputed."
        )
    return ProjectTurn(
        band=band, label=label, guidance=guidance, instructions=instructions,
        must_close=False, may_close_early=False, log_phase=log_phase,
    )


def project_turn(role: str, ordinal: int, *, lab_agrees: bool) -> ProjectTurn:
    """The band of the message ``ordinal`` (1-based, the reply about to be written) that an
    agent of ``role`` writes in a project thread. A role in ``hub_role_names()`` is
    BlackbirdBot; any other is the lab. ``lab_agrees`` (the lab's latest reply starts with
    ✅) matters only to the hub's open and refine bands."""
    if role in hub_role_names():
        return _hub_turn(ordinal, lab_agrees)
    return _lab_turn(ordinal)
