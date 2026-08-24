"""The eight specialist personas, the opinion contract, and the rule that
derives which domains a given verdict was obliged to consult.

Pure functions over plain data — no DB, no engine, no LLM. See
docs/specs/2026-08-07-nine-evaluator-panel-design.md §2, §4.
"""
import itertools
import json
import logging
from pathlib import Path

from src.agent.specialists import (
    MIN_CLEAR_RATE,
    PANEL_NOTE_QUESTION_CHARS,
    SPECIALIST_DOMAINS,
    VERDICT_SIGNALS,
    clear_rate_warning,
    clip_question,
    format_panel_note,
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
    old rubric, and the reason the panel exists. Each must land somewhere.

    Both now own two dimensions rather than one — see
    `test_every_dimension_above_5_percent_incubation_weight_has_an_owning_specialist`
    for why. The first entry stays the original 1:1 mapping so a reader can
    still tell which dimension the persona was written against.
    """
    assert SPECIALIST_DOMAINS["scientific"].maps_to_dimensions == (
        "experimental_rigor", "mechanism_validation",
    )
    assert SPECIALIST_DOMAINS["chemistry"].maps_to_dimensions == (
        "chemistry_dc_path", "toxicity_selectivity",
    )


def test_no_dimension_has_two_owning_specialists():
    """`src/services/directory.py` inverts this table into a dict keyed by
    dimension, so two owners would silently mean "last one in the table wins"
    on the page that tells a reader who to ask about a badly-scoring
    dimension."""
    seen: dict[str, str] = {}
    for domain, spec in SPECIALIST_DOMAINS.items():
        for dimension in spec.maps_to_dimensions:
            assert dimension not in seen, (
                f"{dimension!r} is claimed by both {seen[dimension]!r} and {domain!r}"
            )
            seen[dimension] = domain


def test_every_dimension_above_5_percent_incubation_weight_has_an_owning_specialist():
    """A dimension with no owner is a dimension no specialist can be required
    for, so a bad score there is never sourced to an opinion.

    Measured 2026-08-22: 5 of 13 dimensions were unowned — 25% of the
    incubation weight — including `mechanism_validation` (10) and
    `toxicity_selectivity` (8), which are the two most-cited rejection reasons
    in the stakeholder document that justified the panel in the first place
    (mechanism validation 5 of 15 rejections, toxicity/selectivity 4).

    The bar is *above* 5, not "all thirteen": `external_signals` (2),
    `dev_regulatory_feasibility` (3) and `exit_thesis` (2) are deliberately
    left unowned — inventing a specialist for a 2-point dimension would buy a
    required consult, and a `panel_incomplete` risk, for nothing.
    """
    from src.services.blackbird_rubric import RUBRIC_WEIGHTS_INCUBATION

    owned = {
        dimension
        for spec in SPECIALIST_DOMAINS.values()
        for dimension in spec.maps_to_dimensions
    }
    unowned = sorted(
        dimension
        for dimension, weight in RUBRIC_WEIGHTS_INCUBATION.items()
        if weight > 5 and dimension not in owned
    )
    assert unowned == [], f"heavy dimensions with no owning specialist: {unowned}"


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


# --- parse_opinion, the tolerant extractor -----------------------------------
#
# 6 of 168 consults in run 8b64a0e0 were laundered into `caution`/`low`/no
# concerns by a `json.loads` that saw the whole reply or nothing. The recovery
# line is clean and was measured: `end_turn` replies carry a COMPLETE object
# plus trailing prose and recover; `refusal` replies are cut mid-array and do
# not. See docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, H5.

def test_json_with_trailing_prose_keeps_its_blocking_signal():
    """The chute/scientific consult, in the shape it actually arrived.

    Before the tolerant extractor this parsed as `caution`/`low`/no concerns —
    and `caution` is what was published into the PI's own thread, as ⚠️, for a
    specialist who had said ⛔ with high confidence. The inversion is the whole
    reason this test exists; a merely-lost signal would have been survivable.
    """
    raw = (
        _raw(verdict_signal="blocking", confidence="high",
             concerns=["A single-antibody ICC cannot establish translocation"],
             questions_to_ask=[])
        + "\n\nNote: I have marked this blocking rather than caution because the "
          "claim rests entirely on that one stain."
    )
    op = parse_opinion(raw, domain="scientific")
    assert op.verdict_signal == "blocking"
    assert op.confidence == "high"
    assert len(op.concerns) == 1
    assert op.raw == raw, "the hub still sees every byte, prose included"


def test_a_fenced_object_with_trailing_prose_parses():
    """`_strip_fence` anchors the closing fence at the end of the string, so a
    model that adds one sentence after the fence defeats it. Two of the six."""
    raw = (
        "```json\n" + _raw(verdict_signal="clear", confidence="high") + "\n```\n\n"
        "Happy to go deeper on selectivity if that would help."
    )
    op = parse_opinion(raw, domain="chemistry")
    assert op.verdict_signal == "clear"
    assert op.confidence == "high"


def test_a_truncated_object_still_falls_back_to_caution():
    """The `refusal` half: unrecoverable, and it must stay `caution`.

    `clear` here would turn a specialist we could not read into an approval —
    the exact failure `has_usable_content` was written to prevent. The floor,
    not the parser, is what must refuse to credit this consult.
    """
    op = parse_opinion(
        '{"verdict_signal": "blocking", "concerns": ["The dose-response is not',
        domain="scientific",
    )
    assert op.verdict_signal == "caution"
    assert op.confidence == "low"
    assert op.concerns == ()


def test_a_fenced_braceless_blocking_opinion_is_not_laundered():
    """`_strip_fence` used to run BEFORE `extract_json`, which defeated the one
    recovery branch written for exactly this shape.

    `json_extract` carries a branch whose own comment says "Claude sometimes
    drops the opening brace inside the fence" — and it tests for the ```json
    fence that `_strip_fence` had already removed. So this reply parsed
    correctly from `raw` and RAISED after stripping, landing on the
    `caution`/`low`/no-concerns default: the same laundering the branch exists
    to prevent, reintroduced by the call that was meant to help it.
    """
    raw = (
        '```json\n'
        '"verdict_signal": "blocking", '
        '"concerns": ["The mouse line is third-party encumbered"], '
        '"questions_to_ask": [], '
        '"confidence": "high"\n'
        '```'
    )
    op = parse_opinion(raw, domain="legal")
    assert op.verdict_signal == "blocking"
    assert op.confidence == "high"
    assert op.concerns == ("The mouse line is third-party encumbered",)
    assert op.raw == raw


def test_a_fenced_empty_object_still_reads_as_no_content():
    """The trap on the other side of the same change: `_strip_fence` stays
    load-bearing in `has_usable_content`, where a fenced `{}` must still read as
    "says nothing". Only `parse_opinion` stopped pre-stripping."""
    assert has_usable_content("```json\n{}\n```") is False
    assert has_usable_content("```\n{}\n```") is False


def test_a_defaulted_signal_logs_a_warning_naming_the_domain(caplog):
    """The six were invisible: a laundered opinion looks exactly like a genuine
    cautious one in every log line and every stored row. One WARNING naming the
    domain is what makes the next six greppable."""
    with caplog.at_level(logging.WARNING, logger="src.agent.specialists"):
        parse_opinion("I would rather not answer that.", domain="legal")
    assert any(
        record.levelno == logging.WARNING and "legal" in record.getMessage()
        for record in caplog.records
    ), caplog.text


def test_a_signal_that_was_read_logs_nothing(caplog):
    """The warning has to stay rare enough to be worth reading: 141 of 168
    consults parsed fine, and a line per consult is a line nobody greps."""
    with caplog.at_level(logging.WARNING, logger="src.agent.specialists"):
        parse_opinion(_raw(), domain="legal")
    assert caplog.records == []


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


def test_a_high_ip_fto_score_requires_legal():
    """Since the third gate's rename (fto_achievable -> translational_potential,
    rubric v2.1.0) new verdicts carry no FTO gating key at all, so the legal
    trigger re-anchors on the score: claiming a strong IP position (ip_fto >= 4)
    is a legal assertion and must be sourced — the same shape as platform >= 4
    summoning technologic."""
    v = {"recommendation": "advance", "scores": {"ip_fto": 4}}
    assert "legal" in required_domains_for(v)


def test_a_low_ip_fto_score_alone_does_not_require_legal():
    v = {"recommendation": "advance", "scores": {"ip_fto": 3}}
    assert "legal" not in required_domains_for(v)


def test_an_fto_claim_in_text_requires_legal():
    """FTO talk in the verdict's own prose summons legal even when the score
    stays modest — a rationale resting on freedom-to-operate is a legal claim."""
    v = {
        "recommendation": "advance",
        "rationale": "Freedom-to-operate looks clean; no blocking filings found.",
    }
    assert "legal" in required_domains_for(v)


def test_translational_potential_met_does_not_require_legal():
    """The renamed third gate is a scientific judgement, not an FTO claim —
    marking it met must not summon counsel."""
    v = {"recommendation": "advance", "gating": {"translational_potential": "met"}}
    assert "legal" not in required_domains_for(v)


def test_a_platform_claim_requires_technologic():
    v = {"scores": {"platform": 5}, "rationale": "A reusable editing platform."}
    assert "technologic" in required_domains_for(v)


def test_commercial_is_required_when_a_differentiation_claim_is_made():
    """`commercial` owns `differentiation` — 16 of 100 incubation weight, the
    heaviest single dimension — and until now NO input could require it. So the
    heaviest dimension in the rubric was the one dimension whose specialist the
    floor could never demand.
    """
    for text in (
        "First-in-class against a target nobody else is drugging.",
        "The differentiation from the two clinical-stage competitors is unclear.",
        "A crowded competitive landscape with three named competitors.",
        "Best-in-class potency, but the comparables are weak.",
    ):
        assert "commercial" in required_domains_for(_verdict(text)), text


def test_budget_is_required_when_a_workplan_claim_is_made():
    """`budget` owns `workplan_capital_efficiency`, which the v2 incubation
    scale re-weighted from 1 to 8 — the single largest weight change in the
    re-baseline — while leaving the domain unrequirable."""
    for text in (
        "The workplan is a 24-month effort at roughly $750k.",
        "A milestone-driven budget with a 12-month timeline.",
        "Burn rate is the binding constraint on this scope.",
    ):
        assert "budget" in required_domains_for(_verdict(text)), text


def test_the_new_cues_do_not_fire_on_ordinary_verdict_prose():
    """The cost model here is inverted from what it looks like: this runs AFTER
    the interview has ended, so a false positive cannot be repaired by asking
    one more question — it marks a finished verdict `panel_incomplete`. The two
    new cue sets are held to the same false-positive discipline as the four
    that preceded them.
    """
    for text in (
        "The marketing of this idea outran the data.",
        "We discussed the costume of scientific rigor, not rigor itself.",
        "A timely response from the PI, but no new data.",
        "Deal-breaking is not the same as blocking.",
    ):
        required = required_domains_for(_verdict(text))
        assert "commercial" not in required, text
        assert "budget" not in required, text


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


def test_the_legal_conditions_are_stated():
    """required_domains_for adds 'legal' on ip_fto >= 4 or FTO language — the
    post-rename triggers — and the prompt block states both, so the model can
    aim at the floor it is graded against."""
    verdict = {"recommendation": "advance", "scores": {"ip_fto": 4}}
    assert "legal" in required_domains_for(verdict)
    block = _mandatory_block()
    assert "legal" in block
    assert "ip_fto" in block
    assert "freedom-to-operate" in block


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

    assert reachable == set(SPECIALIST_DOMAINS), (
        "every one of the eight domains must be reachable by some input; the "
        "floor cannot demand a specialist no verdict can ever trigger"
    )
    # The history this assertion replaces: for as long as the panel existed,
    # `commercial` and `budget` were unreachable — finding F5, deferred by D6
    # because closing it needed a hub prompt change. `commercial` owns
    # `differentiation`, the heaviest dimension on both scales (15 investment /
    # 16 incubation), so the floor could not demand an opinion on the thing it
    # weighted most. Re-measured and closed 2026-08-22 (M7).
    assert {"commercial", "budget"} <= reachable


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


# ---------------------------------------------------------------
# format_panel_note — the one line the hub posts into a workspace-
# visible interview thread when a consult succeeds.
# ---------------------------------------------------------------


def test_the_note_carries_the_domain_the_signal_and_the_question():
    assert format_panel_note(
        domain="legal",
        verdict_signal="blocking",
        question="Who owns the mouse line?",
    ) == '🧪 Panel · legal — ⛔ blocking — asked: "Who owns the mouse line?"'


def test_every_signal_has_its_own_emoji():
    seen = {}
    for signal in sorted(VERDICT_SIGNALS):
        note = format_panel_note(domain="legal", verdict_signal=signal, question="q?")
        assert f" {signal} " in note, signal
        emoji = note.split(" — ")[1].split(" ")[0]
        assert emoji not in seen, f"{signal} and {seen.get(emoji)} share {emoji!r}"
        seen[emoji] = signal
    assert seen == {"⛔": "blocking", "⚠️": "caution", "✅": "clear"}


def test_an_unknown_signal_renders_bare_rather_than_reassuringly():
    """`parse_opinion` already degrades an unreadable signal to "caution", so
    this is belt-and-braces — but if something ever reaches here off-contract,
    the note must not dress it up with a ✅."""
    note = format_panel_note(domain="legal", verdict_signal="probably fine", question="q?")
    assert note == '🧪 Panel · legal — probably fine — asked: "q?"'
    assert "✅" not in note


def test_the_panel_note_question_clip_is_600_chars():
    """Pinned literally, not just derived from the constant, so a silent
    future change to ``PANEL_NOTE_QUESTION_CHARS`` fails a test instead of
    just reshaping notes in production. See the constant's comment for the
    production measurement (n=134 consults) that set this value."""
    assert PANEL_NOTE_QUESTION_CHARS == 600


def test_the_question_is_clipped_on_a_word_boundary():
    question = "Is the animal model encumbered " * 25  # 800 chars
    note = format_panel_note(domain="legal", verdict_signal="clear", question=question)
    quoted = note.split('asked: "', 1)[1].rstrip('"')
    assert quoted.endswith("…")
    assert len(quoted) <= PANEL_NOTE_QUESTION_CHARS + 1  # + the ellipsis
    assert not quoted[:-1].endswith(" "), "trailing space before the ellipsis"
    assert question.startswith(quoted[:-1])


def test_a_short_question_is_untouched():
    assert clip_question("Who owns the mouse line?") == "Who owns the mouse line?"
    assert clip_question("x" * PANEL_NOTE_QUESTION_CHARS) == "x" * PANEL_NOTE_QUESTION_CHARS


def test_a_single_unbroken_token_is_still_bounded():
    """A word-boundary search that finds nothing must not return the whole
    string — the bound has to hold whatever the text looks like."""
    clipped = clip_question("x" * 5000)
    assert len(clipped) == PANEL_NOTE_QUESTION_CHARS + 1
    assert clipped.endswith("…")


# ---------------------------------------------------------------
# clear_rate_warning — the panel-discrimination alarm.
#
# It used to be `total >= 50 and not counts.get("clear")`: a ZERO test. Run
# 8b64a0e0 returned 141 caution / 26 blocking / 1 clear over 168 consults and
# was the first run ever to silence it — that single `clear` is the only one in
# the whole database. A zero-test is silenced by one outlier; a rate test is
# not.
# ---------------------------------------------------------------


def test_a_single_clear_does_not_silence_the_discrimination_alarm():
    """The exact production shape, minus the blocking column: 167 caution and
    one clear must still warn."""
    message = clear_rate_warning({"caution": 167, "clear": 1})
    assert message is not None
    assert "clear" in message
    assert "168" in message, "the operator needs the denominator, not just the rate"


def test_the_run_that_motivated_the_change_still_warns():
    assert clear_rate_warning({"caution": 141, "blocking": 26, "clear": 1}) is not None


def test_a_panel_that_never_clears_anything_still_warns():
    """The original zero case must not regress out of coverage."""
    assert clear_rate_warning({"caution": 100, "blocking": 60}) is not None


def test_a_discriminating_panel_is_silent():
    """A panel clearing comfortably above the floor is the whole point; it must
    not produce a warning nobody can act on."""
    clears = 40
    assert clear_rate_warning({"caution": 100, "blocking": 20, "clear": clears}) is None
    assert clears / 160 > MIN_CLEAR_RATE


def test_the_alarm_stays_quiet_below_its_sample_floor():
    """Fifty is the smallest sample the alarm has ever spoken on, and it is kept
    deliberately: at n=10 a zero `clear` rate is ordinary luck, and an alarm
    that cries at the start of every run is an alarm that gets muted."""
    assert clear_rate_warning({"caution": 49}) is None
    assert clear_rate_warning({"caution": 50}) is not None


def test_the_alarm_never_divides_by_zero():
    for counts in ({}, {"caution": 0}, {"clear": 0}):
        assert clear_rate_warning(counts) is None


def test_the_clear_rate_floor_is_pinned():
    """Pinned literally so a future loosening is a diff, not a drift. 5% is the
    first floor this alarm has ever had: run 8b64a0e0's 0.6% (1 of 168) is an
    order of magnitude under it, while a genuinely selective panel clearing one
    idea in twenty stays silent."""
    assert MIN_CLEAR_RATE == 0.05


def test_the_alarm_names_its_denominator_as_counted_consults():
    """The tally excludes TRUNCATED consults — recorded durably, deliberately
    never counted (tools.py: an unread specialist has cleared nothing) — so
    `specialist_consults` can hold more rows for a run than the alarm's total.
    Run ee419dd3 measured the gap: 229 rows, "2 of 228" in the alarm, and the
    one-row mismatch was written up as an unexplained open question. The
    message must say "counted consults" so an operator reconciling it against
    the table knows the difference is by design, not a lost count."""
    # ee419dd3's exact counted shape: 202 caution + 24 blocking + 2 clear.
    message = clear_rate_warning({"caution": 202, "blocking": 24, "clear": 2})
    assert message is not None
    assert "2 of 228 counted consults" in message


def test_nothing_but_the_three_publishable_fields_can_reach_a_note():
    """The privacy rule is enforced by the SIGNATURE, not by discipline at the
    call site: an interview thread is visible to every lab in the workspace, so
    there must be no parameter through which the opinion body, the concerns,
    the questions_to_ask or the confidence could be published."""
    import inspect

    params = inspect.signature(format_panel_note).parameters
    assert set(params) == {"domain", "verdict_signal", "question"}
    assert all(p.kind is p.KEYWORD_ONLY for p in params.values()), (
        "keyword-only, so a positional call cannot silently pass the wrong field"
    )
