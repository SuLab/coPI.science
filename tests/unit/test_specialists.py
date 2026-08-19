"""The eight specialist personas, the opinion contract, and the rule that
derives which domains a given verdict was obliged to consult.

Pure functions over plain data — no DB, no engine, no LLM. See
docs/specs/2026-08-07-nine-evaluator-panel-design.md §2, §4.
"""
import itertools
import json
from pathlib import Path

from src.agent.specialists import (
    SPECIALIST_DOMAINS,
    VERDICT_SIGNALS,
    has_usable_content,
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


# --- the floor's obligations must be stated where the hub can read them ------
#
# required_domains_for() refuses an advance/conditional verdict whose panel is
# incomplete, and _persist_assessment drops that verdict entirely. So every rule
# the function can enforce has to be stated in the prompt the hub actually reads;
# otherwise the hub is held to a contract it was never given, and the verdict is
# lost with the reply already posted. See tests/unit/test_specialist_floor.py.

_SCOUT_HUB_PHASE4 = Path("prompts/roles/scout_hub/phase4-thread-reply.md")


def _mandatory_block() -> str:
    """The prompt's mandatory-consult section, lowercased."""
    text = _SCOUT_HUB_PHASE4.read_text(encoding="utf-8").lower()
    start = text.index("mandatory consults")
    return text[start:start + 2000]


def test_the_prompt_has_a_mandatory_consults_section():
    assert "mandatory consults" in _SCOUT_HUB_PHASE4.read_text(encoding="utf-8").lower()


def test_every_always_required_domain_is_named_as_always_required():
    """_ALWAYS = {scientific, talent}. 'talent' in particular appeared only as one
    name in a list of eight before this — nothing told the hub it was mandatory."""
    block = _mandatory_block()
    for domain in required_domains_for({"recommendation": "advance"}):
        assert domain in block, f"{domain} is always required but unstated"


def test_the_platform_condition_that_requires_technologic_is_stated():
    """required_domains_for adds 'technologic' when scores.platform >= 4 — so a
    STRONGER verdict needs strictly more consults than a weak one."""
    verdict = {"recommendation": "advance", "scores": {"platform": 4}}
    assert "technologic" in required_domains_for(verdict)
    block = _mandatory_block()
    assert "technologic" in block
    assert "platform" in block


def test_the_fto_condition_that_requires_legal_is_stated():
    """required_domains_for adds 'legal' when gating.fto_achievable == 'met'."""
    verdict = {"recommendation": "advance", "gating": {"fto_achievable": "met"}}
    assert "legal" in required_domains_for(verdict)
    block = _mandatory_block()
    assert "legal" in block
    assert "fto_achievable" in block


def test_the_cue_driven_domains_are_stated():
    """chemistry/clinical are added by free-text cue matching over the verdict."""
    block = _mandatory_block()
    assert "chemistry" in block
    assert "clinical" in block


def _verdict(text, **over):
    v = {
        "recommendation": "advance", "subject_agent_id": "x",
        "rationale": text, "company_or_project": "", "funnel_stage": "",
        "red_flags": [], "suggested_derisking_milestones": [],
    }
    v.update(over)
    return v


def test_reasons_does_not_summon_the_chemistry_specialist():
    """'aso' (antisense oligonucleotide) must not match 'reasons'.

    Fired on 7 of 18 production verdicts. On `hart` it was the ONLY chemistry
    cue present, so it alone decided the requirement.
    """
    assert "chemistry" not in required_domains_for(
        _verdict("We passed for several reasons, none of them chemical.")
    )


def test_architecture_does_not_summon_the_chemistry_specialist():
    """'hit' (a screening hit) must not match 'architecture'. Fired on 6/18."""
    assert "chemistry" not in required_domains_for(
        _verdict("The granuloma architecture is not recapitulated in this model.")
    )


def test_also_and_signals_do_not_summon_the_clinical_specialist():
    """'als' (the disease) must not match 'also', 'signals', 'animals',
    'journals'. Fired on 9/18; on `mcmeniman` it was the only clinical cue."""
    for word in ("also", "signals", "animals", "journals"):
        assert "clinical" not in required_domains_for(
            _verdict(f"There are {word} to consider here.")
        ), f"{word!r} must not read as ALS"


def test_compounding_reasons_does_not_summon_the_chemistry_specialist():
    """'compound' must not match 'compounding'.

    On the real `coller` production verdict, "several compounding reasons"
    was the ONLY chemistry cue present, so it alone decided the requirement.
    Fix round 1: moved "compound" from the prefix tier into
    `_WORD_ONLY_CUES` because the false-positive class this task exists to
    close survived there via a prefix cue.
    """
    assert "chemistry" not in required_domains_for(
        _verdict(
            "This does not clear the bar for several compounding reasons."
        )
    )


def test_the_prefix_tier_is_anchored_at_a_word_boundary():
    """The ~30 cues OUTSIDE `_WORD_ONLY_CUES` are anchored too, on the left.

    Every other false-positive test above exercises a cue in
    `_WORD_ONLY_CUES` ("aso", "hit", "als", "compound"), which is matched by
    the OTHER branch of `_cue_pattern`. So deleting the `(?<![a-z0-9])`
    lookbehind from the prefix branch reverted every remaining cue to raw
    substring containment and shipped green. Verified by mutation: remove that
    lookbehind and this test fails, while the four tests above still pass.

    The prefix tier deliberately has no RIGHT-hand anchor — that is what lets
    "medicinal chem" reach "medicinal chemistry" and "neurodegener" reach
    "neurodegeneration" — so the left-hand one is the only thing standing
    between it and substring matching.
    """
    # "clinical" inside "nonclinical": the whole point of the tier is that a
    # cue must START a word.
    assert "clinical" not in required_domains_for(
        _verdict("A nonclinical readout in a rodent model.")
    ), "'nonclinical' must not summon the clinical specialist"
    # "peptide" inside "polypeptide" — same tier, a different domain, so this
    # cannot pass by accident of one cue's spelling.
    assert "chemistry" not in required_domains_for(
        _verdict("A polypeptide fold prediction benchmark.")
    ), "'polypeptide' must not summon the chemistry specialist"
    # And the tier still does its job: a prefix cue matches its own stems.
    assert "clinical" in required_domains_for(_verdict("Neurodegenerative decline."))
    assert "chemistry" in required_domains_for(_verdict("A peptide binder."))


def test_an_unrecognised_but_populated_json_object_is_an_opinion():
    """A specialist that answered in a shape we did not name still answered.

    `has_usable_content` used to require one of four known keys, so
    {"signal": ..., "analysis": "<500 words>"} was reported to the hub as "the
    specialist returned an empty response" — a false statement that threw the
    analysis away and denied the domain its credit, while the SAME words sent
    as bare prose were kept. Only `{}` says nothing.
    """
    assert has_usable_content(
        '{"signal": "blocking", "analysis": "The assay has no orthogonal control, '
        'and the effect size is within run-to-run variance."}'
    ) is True
    assert has_usable_content('{"opinion": "proceed"}') is True
    assert has_usable_content('```json\n{"unexpected_key": ["a", "b"]}\n```') is True
    # The line the change does not cross: an object with no keys at all.
    assert has_usable_content("{}") is False


def test_genuine_cues_still_match():
    """Narrowing must not become blindness."""
    assert "chemistry" in required_domains_for(_verdict("We have ASOs in hand."))
    assert "chemistry" in required_domains_for(_verdict("An aso-based approach."))
    assert "chemistry" in required_domains_for(_verdict("A known-compound series."))
    assert "chemistry" in required_domains_for(_verdict("We have a lead compound."))
    assert "chemistry" in required_domains_for(_verdict("Several compounds in hand."))
    assert "chemistry" in required_domains_for(
        _verdict("Medicinal chemistry is tractable.")
    )
    assert "clinical" in required_domains_for(_verdict("An ALS indication."))
    assert "clinical" in required_domains_for(_verdict("A clinical-stage asset."))
    assert "clinical" in required_domains_for(_verdict("Patient-derived organoids."))
    assert "clinical" in required_domains_for(_verdict("Neurodegeneration broadly."))


def test_only_the_documented_domains_are_reachable():
    """Which domains the floor can EVER require, asserted rather than assumed.

    `commercial` and `budget` cannot be required by any input — proven
    exhaustively here rather than trusted. That is finding F5, and this test is
    what would have caught it. F5 and F6a are deferred by D6 (the fixes need a
    hub prompt change), so this pins the deferred state honestly instead of
    leaving five-of-eight as a fact remembered only in a design doc.
    """
    reachable: set[str] = set()
    cue_texts = [
        "small molecule compound inhibitor antibody peptide modality scaffold",
        "disease patient indication clinical therapeutic cancer tumor",
        "platform pipeline reusable multiple shots",
        "commercial competitor landscape deal comps investor budget cost timeline",
        "",
    ]
    for r in range(len(cue_texts) + 1):
        for combo in itertools.combinations(cue_texts, r):
            text = " ".join(combo)
            for fto in ("met", "not_met", "unconfirmed", None):
                for platform in (1, 3, 4, 5, None):
                    reachable |= required_domains_for(
                        _verdict(
                            text,
                            company_or_project=text,
                            red_flags=[text],
                            suggested_derisking_milestones=[text],
                            gating={"fto_achievable": fto},
                            scores={"platform": platform},
                        )
                    )

    assert reachable == {
        "scientific", "talent", "chemistry", "clinical", "technologic", "legal",
    }
    assert {"commercial", "budget"}.isdisjoint(reachable), (
        "commercial maps to `differentiation`, the heaviest dimension at 15%, "
        "and the floor still cannot demand it — F5, deferred by D6"
    )


def test_an_empty_reply_is_not_an_opinion():
    """A call that returned nothing must not satisfy the floor."""
    for raw in ("", "   ", "\n\n", "null", "[]", "{}", "3", '"x"'):
        assert has_usable_content(raw) is False, f"{raw!r} carries no opinion"


def test_prose_is_still_an_opinion():
    """Deliberate: a specialist answering in sentences has still answered.
    Only a call that returned NOTHING is excluded."""
    assert has_usable_content("The controls here are inadequate.") is True
    assert has_usable_content("I cannot assess this without the assay.") is True


def test_a_partial_json_opinion_counts():
    assert has_usable_content('{"verdict_signal": "clear"}') is True
    assert has_usable_content('{"concerns": ["off-target risk"]}') is True
    assert has_usable_content('```json\n{"confidence": "high"}\n```') is True
