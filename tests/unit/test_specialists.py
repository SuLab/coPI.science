"""The eight specialist personas, the opinion contract, and the rule that
derives which domains a given verdict was obliged to consult.

Pure functions over plain data — no DB, no engine, no LLM. See
docs/specs/2026-08-07-nine-evaluator-panel-design.md §2, §4.
"""
import json

from src.agent.specialists import (
    SPECIALIST_DOMAINS,
    VERDICT_SIGNALS,
    parse_opinion,
    persona_path,
    required_domains_for,
)


def test_the_eight_domains_are_exactly_the_spec_table():
    assert set(SPECIALIST_DOMAINS) == {
        "scientific", "chemistry", "clinical", "commercial",
        "legal", "technologic", "talent", "budget",
    }


def test_blackbird_is_not_a_specialist():
    """The document lists Blackbird as the ninth evaluator, but it is the hub
    doing the integrating — not something the hub can consult about itself."""
    assert "blackbird" not in SPECIALIST_DOMAINS


def test_every_domain_declares_when_to_consult_it():
    for domain, spec in SPECIALIST_DOMAINS.items():
        assert spec.consult_when.strip(), domain
        assert spec.owns.strip(), domain
        assert spec.title.strip(), domain


def test_the_two_missing_personas_map_to_new_dimensions():
    """Scientific and Chemistry are the personas with no representation in the
    old rubric, and the reason the panel exists. Each must land somewhere."""
    assert SPECIALIST_DOMAINS["scientific"].maps_to_dimension == "experimental_rigor"
    assert SPECIALIST_DOMAINS["chemistry"].maps_to_dimension == "chemistry_dc_path"


def test_persona_path_is_under_prompts_specialists():
    p = persona_path("chemistry")
    assert p.as_posix().endswith("prompts/specialists/chemistry.md")


# --- parse_opinion -----------------------------------------------------------

def _raw(**over):
    body = {
        "verdict_signal": "caution",
        "concerns": ["in-family off-target risk at SK2"],
        "questions_to_ask": ["What selectivity margin over SK2 have you measured?"],
        "confidence": "moderate",
    }
    body.update(over)
    return json.dumps(body)


def test_parse_reads_the_contract():
    op = parse_opinion(_raw(), domain="chemistry")
    assert op.domain == "chemistry"
    assert op.verdict_signal == "caution"
    assert op.concerns == ("in-family off-target risk at SK2",)
    assert op.questions_to_ask == (
        "What selectivity margin over SK2 have you measured?",
    )
    assert op.confidence == "moderate"


def test_parse_keeps_the_raw_text_verbatim():
    """The hub sees the raw string; the parse is for us, not for it."""
    raw = _raw()
    assert parse_opinion(raw, domain="chemistry").raw == raw


def test_parse_of_prose_is_an_opinion_not_a_failure():
    """A specialist that answers in prose has still answered. Only a FAILED
    call must not satisfy the floor — see the engine, not here."""
    op = parse_opinion("The chemistry here is not close to a DC.", domain="chemistry")
    assert op.verdict_signal == "caution"
    assert op.concerns == ()
    assert op.raw == "The chemistry here is not close to a DC."


def test_parse_of_an_unknown_signal_degrades_to_caution():
    op = parse_opinion(_raw(verdict_signal="catastrophic"), domain="chemistry")
    assert op.verdict_signal == "caution"


def test_parse_of_a_fenced_block_still_works():
    """Models fence JSON by reflex. Do not let that be a parse failure."""
    op = parse_opinion("```json\\n" + _raw() + "\\n```", domain="chemistry")
    assert op.verdict_signal == "caution"


def test_every_signal_in_the_enum_round_trips():
    for sig in VERDICT_SIGNALS:
        assert parse_opinion(_raw(verdict_signal=sig), domain="legal").verdict_signal == sig


def test_non_list_concerns_degrade_to_empty():
    op = parse_opinion(_raw(concerns="a string, not a list"), domain="legal")
    assert op.concerns == ()


# --- required_domains_for ----------------------------------------------------

def test_scientific_and_talent_are_always_required():
    assert {"scientific", "talent"} <= required_domains_for({})


def test_chemical_matter_requires_chemistry():
    v = {"company_or_project": "A small molecule SCAP inhibitor"}
    assert "chemistry" in required_domains_for(v)


def test_a_modality_requires_chemistry():
    v = {"rationale": "An antisense oligonucleotide for ALS."}
    assert "chemistry" in required_domains_for(v)


def test_a_disease_claim_requires_clinical():
    v = {"rationale": "For the treatment of schizophrenia in adults."}
    assert "clinical" in required_domains_for(v)


def test_met_fto_requires_legal():
    """Claiming FTO is achievable is a legal assertion; it must be sourced."""
    v = {"gating": {"fto_achievable": "met"}}
    assert "legal" in required_domains_for(v)


def test_unconfirmed_fto_does_not_require_legal():
    v = {"gating": {"fto_achievable": "unconfirmed"}}
    assert "legal" not in required_domains_for(v)


def test_a_platform_claim_requires_technologic():
    v = {"scores": {"platform": 5}, "rationale": "A reusable editing platform."}
    assert "technologic" in required_domains_for(v)


def test_a_bare_verdict_requires_only_the_always_pair():
    assert required_domains_for({}) == frozenset({"scientific", "talent"})


def test_requirement_derivation_never_raises_on_junk():
    """This runs inside _persist_assessment's try block; an exception here
    would be logged as 'Failed to persist assessment' after the row committed."""
    for junk in (None, [], "string", {"gating": "not a dict"}, {"scores": 7}):
        assert isinstance(required_domains_for(junk), frozenset)


# --- the persona files -------------------------------------------------------

def test_every_domain_has_a_persona_file_on_disk():
    for domain in SPECIALIST_DOMAINS:
        p = persona_path(domain)
        assert p.is_file(), f"missing persona file for {domain}: {p}"
        assert p.read_text(encoding="utf-8").strip(), f"empty persona file: {p}"


def test_no_orphan_persona_files():
    """A file with no domain is a file nothing can ever load."""
    from src.agent.specialists import SPECIALISTS_DIR

    on_disk = {p.stem for p in SPECIALISTS_DIR.glob("*.md")}
    assert on_disk == set(SPECIALIST_DOMAINS)


def test_every_persona_states_the_opinion_contract():
    """Each persona must ask for the structured fields, or parse_opinion
    degrades every answer to caution/low and the panel is decoration."""
    for domain in SPECIALIST_DOMAINS:
        body = persona_path(domain).read_text(encoding="utf-8")
        for field in ("verdict_signal", "concerns", "questions_to_ask", "confidence"):
            assert field in body, f"{domain} persona omits {field}"
        for signal in ("blocking", "caution", "clear"):
            assert signal in body, f"{domain} persona omits the {signal} signal"


def test_the_science_personas_carry_the_vocabulary_the_rubric_lacked():
    """The audit found these words absent from every scout_hub prompt and from
    the rubric. They are the reason these two personas exist."""
    sci = persona_path("scientific").read_text(encoding="utf-8").lower()
    for term in ("control", "power", "interpretab", "translatab"):
        assert term in sci, f"scientific persona omits {term!r}"

    chem = persona_path("chemistry").read_text(encoding="utf-8").lower()
    for term in ("development candidate", "off-target", "tolerab", "selectivit"):
        assert term in chem, f"chemistry persona omits {term!r}"


def test_no_persona_claims_to_decide():
    """A specialist advises; the hub integrates. A persona that says 'reject'
    invites the hub to outsource a judgement it owns."""
    for domain in SPECIALIST_DOMAINS:
        body = persona_path(domain).read_text(encoding="utf-8").lower()
        assert "you do not decide" in body, f"{domain} persona omits the advisory boundary"
