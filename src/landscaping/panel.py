"""The S6 specialist panel: which domains consult, their personas and their questions
(spec §7.7 step 8, D12).

A persona is composed exactly as ``src/agent/tools.py::_load_persona_text`` composes it from
disk: the file at ``specialists.persona_path(domain)`` with ``{stage_bar}`` replaced by
``blackbird_rubric.render_stage_bar_markdown(domain)``
(``tests/unit/landscaping/test_panel.py`` holds the two equal). Consults go through the
landscaping ledgered client, never ``tools._call_specialist``, which books into the engine's
per-run call log that a worker process does not have.
"""
from __future__ import annotations

from src.agent.prompt_safety import delimit
from src.agent.specialists import persona_path
from src.services.blackbird_rubric import render_stage_bar_markdown

PANEL_BIOLOGY = ("scientific", "chemistry", "clinical", "commercial", "legal", "talent", "budget")
#: A chemistry idea adds the technologic consult (D12).
PANEL_CHEMISTRY = PANEL_BIOLOGY + ("technologic",)

_STAGE_BAR_PLACEHOLDER = "{stage_bar}"

#: Each domain's question about the idea; the dossier and the S6 summary follow it.
DOMAIN_QUESTIONS = {
    "scientific": (
        "How rigorous and reproducible is the evidence for this idea, and how well do its "
        "model systems predict human biology?"
    ),
    "chemistry": (
        "How tractable is the path from this target or chemical matter to a development "
        "candidate, and which modality fits it best?"
    ),
    "clinical": (
        "Is the unmet need real against the current standard of care, is the indication the "
        "right one, and what would the clinical development path look like?"
    ),
    "commercial": (
        "How differentiated is this idea against the named competing programs, and what is "
        "its venture potential?"
    ),
    "legal": (
        "What freedom-to-operate, licensing or ownership questions does the record raise?"
    ),
    "talent": (
        "Judged from the record of grants, publications, patents and technology listings, how "
        "likely is the team to complete the next stage of this work?"
    ),
    "budget": (
        "What is the smallest fundable experiment that would de-risk this idea, and does it "
        "fit Blackbird's grant bands and durations?"
    ),
    "technologic": (
        "Is the platform or technology behind this idea feasible, and would the proposed next "
        "work actually test that feasibility?"
    ),
}


def panel_domains(idea_type: str | None) -> tuple[str, ...]:
    """The consulted domains: ``PANEL_CHEMISTRY`` for a chemistry idea, else
    ``PANEL_BIOLOGY``."""
    return PANEL_CHEMISTRY if idea_type == "chemistry" else PANEL_BIOLOGY


def compose_persona(domain: str) -> str:
    """The domain's persona file with ``{stage_bar}`` replaced by the rubric's stage bar.

    Raises ``FileNotFoundError`` when the persona file is missing and ``RubricError`` for a
    domain the rubric has no stage bar for. ``str.replace``, not ``str.format``: persona files
    contain bare braces.
    """
    persona = persona_path(domain).read_text(encoding="utf-8")
    if _STAGE_BAR_PLACEHOLDER in persona:
        persona = persona.replace(_STAGE_BAR_PLACEHOLDER, render_stage_bar_markdown(domain))
    return persona


def consult_message(domain: str, *, dossier: str, summary: str) -> str:
    """A consult's user message: the domain question, then the dossier S6 read, then the S6
    summary (fenced as ``publication_abstract``, since it restates source material)."""
    return (
        f"## Question from Blackbird\n\n{DOMAIN_QUESTIONS[domain]}\n\n"
        f"## The analysis summary\n\n{delimit(summary, 'publication_abstract')}\n\n"
        f"## The dossier\n\n{dossier}"
    )
