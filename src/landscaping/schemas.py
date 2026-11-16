"""Structured-output JSON Schemas for every landscaping stage that returns JSON (spec §7.3-§7.10).

Each schema matches its prompt's output fields (``prompts/landscaping/*.md``) and goes to
``llm_client.build_request(..., schema=stage_schema(stage, rubric))``. They use only what
structured output accepts, as the repository's other schemas do
(``company_sources/coi_llm.CLAIMS_SCHEMA``): every object lists all its properties as
``required`` with ``additionalProperties: false``; values are constrained by ``enum`` and unions
by ``anyOf``. Length bounds ("at most 200 characters") and numeric ranges are left to the prompts
and to the parsing code, since structured output does not enforce them; integer scores are an
``enum`` of the rubric scale instead.

The vocabularies the criteria file owns (direction, stage, evidence grade, agent kind) are plain
strings here: the criteria file is an epoch input that may change them, and normalization reads
the values against it (§8.1). The six dimension keys and the score scale are the rubric's, and
the rubric is an epoch input too (§7.11, D61): the triage, deep and final schemas are built from
the epoch's own rubric (``stage_schema``), never from the file on disk. The gating keys are
fixed by the rubric parser.

S6's full-text re-extraction (``deep_extract``) reuses ``SCHEMAS["extract"]`` (§7.7 step 1).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from src.services.blackbird_rubric import _REQUIRED_GATING_KEYS, Rubric

#: The rubric's three structural gating keys, in its order (every parsed rubric has exactly
#: these; ``blackbird_rubric._parse_gating``).
GATING_KEYS: tuple[str, ...] = tuple(_REQUIRED_GATING_KEYS)
#: The three dimensions S4 scores (§7.5).
TRIAGE_DIMENSION_KEYS = (
    "differentiation_unmet_need", "scientific_credibility", "translational_path",
)
#: The verdict contract's six key-point groups, in its skeleton's order.
KEY_POINT_KEYS = (
    "indication_audience", "lab_background", "proposal", "clinical_actionability",
    "path_to_clinic", "commercial_opportunity",
)
#: The four S4 LLM flags (§7.5).
LLM_FLAG_KEYS = (
    "correlation_only", "model_unrepresentative_no_human_data", "key_molecule_owned_elsewhere",
    "literature_contradiction",
)
#: The seven S6 disease-profile fields (§7.7 step 3).
DISEASE_PROFILE_KEYS = (
    "indication", "prevalence", "standard_of_care", "approved_drugs", "clinical_pipeline",
    "competing_targets", "unmet_need",
)
RESEARCH_KINDS = (
    "target_discovery", "target_validation", "disease_mechanism", "medicinal_chemistry",
    "biologic_engineering", "oligonucleotide", "gene_or_cell_therapy", "repurposing",
    "clinical_study", "other",
)

_STRING: dict[str, Any] = {"type": "string"}
_BOOL: dict[str, Any] = {"type": "boolean"}
_STRINGS: dict[str, Any] = {"type": "array", "items": _STRING}
_GATE: dict[str, Any] = {"type": "string", "enum": ["met", "not_met", "unconfirmed"]}
_CONFIDENCE: dict[str, Any] = {"type": "string", "enum": ["High", "Moderate", "Speculative"]}
_RUBRIC_STATUS: dict[str, Any] = {"type": "string", "enum": ["present", "absent", "unclear"]}
#: A list of references, or the string "estimate" when a claim rests on the model's own
#: knowledge (L30, L31).
_SUPPORT: dict[str, Any] = {
    "anyOf": [_STRINGS, {"type": "string", "enum": ["estimate"]}],
}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _keyed(keys: tuple[str, ...], value: dict[str, Any]) -> dict[str, Any]:
    return _object({key: value for key in keys})


_QUOTE = _object({"quote": _STRING, "section": _STRING})

_SCREEN = _object({
    "in_scope": {"type": "string", "enum": ["yes", "no", "unsure"]},
    "research_kind": _array({"type": "string", "enum": list(RESEARCH_KINDS)}),
    "targets_mentioned": _STRINGS,
    "diseases_mentioned": _STRINGS,
    "modality_mentioned": {"anyOf": [_STRING, {"type": "null"}]},
    "reason": _STRING,
})

_EXTRACT_IDEA = _object({
    "idea_type": {"type": "string", "enum": ["biology", "chemistry"]},
    "targets": _array(_object({
        "as_written": _STRING,
        "kind": {"type": "string", "enum": [
            "gene", "protein", "rna", "complex", "pathway_node", "cell_population",
        ]},
        "species": {"type": "string", "enum": ["human", "mouse", "rat", "other", "unstated"]},
    })),
    "direction": _STRING,
    "agent": _object({"as_written": _STRING, "kind": _STRING}),
    "disease": _object({"as_written": _STRING}),
    "modality": _object({
        "as_written": _STRING,
        "basis": {"type": "string", "enum": ["proposed", "tested", "none"]},
    }),
    "stage": _STRING,
    "evidence_grade": _STRING,
    "evidence_summary": _STRING,
    "mechanism_hypothesis": _STRING,
    "target_rubric": _array(_object({
        "item": {"type": "integer"},
        "status": _RUBRIC_STATUS,
        "grade": {"anyOf": [_STRING, {"type": "null"}]},
        "quote": _STRING,
        "section": _STRING,
    })),
    "key_results": _array(_object({"claim": _STRING, "quote": _STRING, "section": _STRING})),
    "model_systems": _STRINGS,
    "patient_population": _STRING,
    "biomarkers": _STRINGS,
    "novelty_claims": _array(_QUOTE),
    "limitations": _array(_QUOTE),
    "unmet_need_statements": _array(_QUOTE),
    "funding_and_ip_mentions": _STRINGS,
})

_EXTRACT = _object({
    "ideas": _array(_EXTRACT_IDEA),
    "linked_publications": _array(_object({"doi": _STRING, "pmid": _STRING, "title": _STRING})),
})

_RERANK = _object({
    "winner": {"type": "string", "enum": ["A", "B"]},
    "reason": _STRING,
})

_SIMILARITY = _object({"same_program": _BOOL, "reason": _STRING})

_LABEL = _object({
    "label": _STRING,
    "theme": _STRING,
    "platform_signal": _object({"present": _BOOL, "reason": _STRING}),
})

SCHEMAS: dict[str, dict] = {
    "screen": _SCREEN,
    "extract": _EXTRACT,
    "rerank": _RERANK,
    "similarity": _SIMILARITY,
    "label": _LABEL,
}


#: The stages whose schema depends on the rubric (its dimension keys and scale).
_RUBRIC_STAGES = ("triage", "deep", "final")


@lru_cache(maxsize=8)
def _rubric_schemas(
    dimension_keys: tuple[str, ...], scale_min: int, scale_max: int
) -> dict[str, dict]:
    """The triage, deep and final schemas for a rubric's dimension keys and scale; cached on
    exactly the rubric content they read."""
    score: dict[str, Any] = {"type": "integer", "enum": list(range(scale_min, scale_max + 1))}
    triage = _object({
        "gating": _keyed(GATING_KEYS, _GATE),
        "gating_rationales": _keyed(GATING_KEYS, _STRING),
        "scores": _keyed(TRIAGE_DIMENSION_KEYS, score),
        "dimension_rationales": _keyed(TRIAGE_DIMENSION_KEYS, _STRING),
        "target_rubric": _array(_object({"item": {"type": "integer"}, "status": _RUBRIC_STATUS})),
        "evidence_grade": _STRING,
        "stage": _STRING,
        "llm_flags": _keyed(LLM_FLAG_KEYS, _object({"raised": _BOOL, "reason": _STRING})),
        "red_flags": _STRINGS,
        "confidence": _CONFIDENCE,
        "in_therapeutic_scope": _BOOL,
        "druggability_note": _STRING,
    })

    deep = _object({
        "summary": _STRING,
        "disease_profile": _keyed(
            DISEASE_PROFILE_KEYS, _object({"value": _STRING, "support": _SUPPORT})
        ),
        "target_biology": _array(_object({"line": _STRING, "support": _SUPPORT})),
        "outside_groups": _array(_object({"finding": _STRING, "support": _SUPPORT})),
        "gating": _keyed(GATING_KEYS, _GATE),
        "gating_rationales": _keyed(GATING_KEYS, _STRING),
        "flag_review": _array(_object({
            "flag": _STRING,
            "status": {"type": "string", "enum": ["confirmed", "cleared"]},
            "reason": _STRING,
        })),
        "scores": _keyed(dimension_keys, score),
        "dimension_rationales": _keyed(dimension_keys, _STRING),
        "red_flags": _STRINGS,
        "confidence": _CONFIDENCE,
    })

    # The verdict contract's object without ``subject_agent_id`` (no lab agent, §7.10), plus the
    # landscaping additions ``claim_support`` and ``member_notes``.
    final = _object({
        "company_or_project": _STRING,
        "headline": _STRING,
        "key_points": _keyed(KEY_POINT_KEYS, _STRINGS),
        "elevator_pitch": _STRING,
        "score_rationale": _STRING,
        "strengths": _STRINGS,
        "risks": _STRINGS,
        "competitive_landscape": _STRINGS,
        "evidence_maturity": _STRINGS,
        "gating": _keyed(GATING_KEYS, _GATE),
        "gating_rationales": _keyed(GATING_KEYS, _STRING),
        "scores": _keyed(dimension_keys, score),
        "dimension_rationales": _keyed(dimension_keys, _STRING),
        "red_flags": _STRINGS,
        "recommendation": {
            "type": "string", "enum": ["advance", "conditional", "pass", "route-to-incubation"],
        },
        "rationale": _STRING,
        "recommended_next_experiment": _STRING,
        "confidence": _CONFIDENCE,
        "claim_support": _keyed(KEY_POINT_KEYS, _array(_SUPPORT)),
        "member_notes": _array(_object({"idea": _STRING, "note": _STRING})),
    })
    return {"triage": triage, "deep": deep, "final": final}


def stage_schema(stage: str, rubric: Rubric) -> dict:
    """``stage``'s output schema. The triage, deep and final schemas are built from ``rubric``
    (an epoch's own, ``epochs.epoch_inputs(...).rubric``, §7.11); every other stage's is
    ``SCHEMAS[stage]``. ``KeyError`` for a stage with no schema."""
    if stage in _RUBRIC_STAGES:
        return _rubric_schemas(
            tuple(dim.key for dim in rubric.dimensions), rubric.scale_min, rubric.scale_max
        )[stage]
    return SCHEMAS[stage]
