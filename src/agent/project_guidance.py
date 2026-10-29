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
#: The first ordinal at which one ✅ from the lab lets BlackbirdBot close early. Before it,
#: the lab must have opened ✅ on its two most recent consecutive posts (owner decision D77,
#: 2026-10-29). Run b70d0cff closed at a median ordinal of 6 of 12 on a single ✅, and plan
#: depth tracked the close turn: ~5,700 characters at ordinal 4 against ~10,200 at 8.
HUB_EARLY_CLOSE_FROM = 8
#: Consecutive turns that produced nothing and reported a refusal or a truncation before a
#: project thread is abandoned (``ThreadState.refusal_count``). Four, not the empty reply's
#: two: in run d54d7386 antiochos lost two consecutive turns to refusals at ordinal 8 of 12
#: and the thread was abandoned three turns from its close.
PROJECT_REFUSALS_MAX = 4
#: The longest a refused project turn waits before its retry. The four waits are the tail a
#: Projects wind-down sits through before the thread abandons, and a refusal is deterministic,
#: so the interview lane's half-hour ceiling buys nothing here.
PROJECT_REFUSAL_BACKOFF_CAP_S = 300.0

_HUB_EARLY_CLOSE = (
    "The lab's latest reply starts with ✅: it agrees the plan as it stands is feasible and "
    "complete. If you agree too, close now: start with ✅, state the final plan and add the "
    "<project_json> block. If you do not, say what is still missing and carry on."
)
#: Shown when the lab agrees but D77 holds the close. A ✅ with no instruction attached is how
#: run b70d0cff closed half its threads at ordinal 6: the hub read agreement as permission.
_HUB_HOLD_CLOSE = (
    "The lab's latest reply starts with ✅, but it is too early to close and you have turns "
    "left. Spend this one on the science: test a control, the readout, the analysis or a "
    "number in the design that has not been pinned down yet. Close only once the lab has "
    "agreed twice running, or from your eighth turn."
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


def _hub_turn(ordinal: int, lab_agrees: bool, lab_agreed_before: bool) -> ProjectTurn:
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
    may_close_early = lab_agrees and (
        ordinal >= HUB_EARLY_CLOSE_FROM or lab_agreed_before
    )
    if may_close_early:
        guidance += "\n\n" + _HUB_EARLY_CLOSE
    elif lab_agrees:
        guidance += "\n\n" + _HUB_HOLD_CLOSE
    return ProjectTurn(
        band=band, label=label, guidance=guidance, instructions=instructions,
        must_close=False, may_close_early=may_close_early, log_phase=log_phase,
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


def project_turn(
    role: str, ordinal: int, *, lab_agrees: bool, lab_agreed_before: bool = False,
) -> ProjectTurn:
    """The band of the message ``ordinal`` (1-based, the reply about to be written) that an
    agent of ``role`` writes in a project thread. A role in ``hub_role_names()`` is
    BlackbirdBot; any other is the lab. ``lab_agrees`` (the lab's latest reply starts with
    ✅) and ``lab_agreed_before`` (the post before that one did too) matter only to the hub's
    open and refine bands, where together they decide whether it may close early (D77).

    ``lab_agreed_before`` defaults to False so a caller that only wants ``log_phase``, which
    no position changes, need not read the thread twice."""
    if role in hub_role_names():
        return _hub_turn(ordinal, lab_agrees, lab_agreed_before)
    return _lab_turn(ordinal)
