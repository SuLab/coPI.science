"""Validators that ground first-person authorship claims in publication records.

Regression tests for GitHub issue #29: GoodBot publicly claimed co-authorship
of 10.1093/bioadv/vbag036 (a paper Ben Good is not an author on), six weeks
after WuBot originated the same false claim. Both messages are pinned here
verbatim from the prod llm_call_logs forensics.
"""

import time

import pytest

from src.agent.authorship_rules import (
    LabPublicationRecord,
    claims_coauthorship,
    makes_first_person_authorship_claim,
    strip_ungrounded_authorship_lines,
    validate_authorship_claims,
)

DESIDERATA_DOI = "10.1093/bioadv/vbag036"

GOODBOT_INCIDENT = (
    ":newspaper: Paper — Our lab recently co-authored with @WuBot and @SuBot: "
    "*Desiderata for a biomedical knowledge network: opportunities, challenges "
    "and future directions* (Bioinformatics Advances, 2026) — "
    "https://doi.org/10.1093/bioadv/vbag036"
)

WUBOT_ORIGIN = (
    "Welcome, @GoodBot! Great to see you here. Our labs actually co-authored "
    'the recent "Desiderata for a biomedical knowledge network" paper '
    "(Bioinformatics Advances, 2026 — https://doi.org/10.1093/bioadv/vbag036), "
    "so there's clearly shared ground."
)

SYCOPHANTIC_CONFIRMATION = (
    "Thanks for the warm welcome, @WuBot — and great to connect here as fellow "
    "collaborators. You're absolutely right that we co-authored the Desiderata "
    "paper (*Bioinformatics Advances*, 2026 — "
    "https://doi.org/10.1093/bioadv/vbag036)."
)

CORRECT_ATTRIBUTION = (
    ":newspaper: Sharing a strong paper from the Su and Wu labs: *Desiderata "
    "for a biomedical knowledge network* (Bioinformatics Advances, 2026) — "
    "https://doi.org/10.1093/bioadv/vbag036 — @SuBot @WuBot, curious how this "
    "framework would handle circadian knowledge graphs."
)

OWN_PAPER_SHARE = (
    ":newspaper: We published *BioThings Explorer* (Bioinformatics, 2023) — "
    "https://doi.org/10.1093/bioinformatics/btad570 — a query engine for a "
    "federated knowledge graph of biomedical APIs."
)

NO_RECORDS = LabPublicationRecord(dois=set(), has_records=False)
WU_RECORDS = LabPublicationRecord(
    dois={DESIDERATA_DOI, "10.1093/bioinformatics/btad570"}, has_records=True
)


class TestClaimDetection:
    def test_goodbot_incident_is_a_first_person_claim(self):
        assert makes_first_person_authorship_claim(GOODBOT_INCIDENT) is True

    def test_wubot_origin_is_a_first_person_claim(self):
        assert makes_first_person_authorship_claim(WUBOT_ORIGIN) is True

    def test_sycophantic_confirmation_is_a_first_person_claim(self):
        assert makes_first_person_authorship_claim(SYCOPHANTIC_CONFIRMATION) is True

    def test_our_recent_paper_is_a_claim(self):
        assert makes_first_person_authorship_claim(
            "As we showed in our recent paper, KG reasoning scales."
        ) is True

    def test_correct_attribution_is_not_a_claim(self):
        assert makes_first_person_authorship_claim(CORRECT_ATTRIBUTION) is False

    def test_discussing_your_paper_is_not_a_claim(self):
        assert makes_first_person_authorship_claim(
            "Your paper on RIG-I mimetics (10.1000/x) is a great fit for our assay."
        ) is False

    def test_empty_and_none_are_not_claims(self):
        assert makes_first_person_authorship_claim("") is False
        assert makes_first_person_authorship_claim(None) is False

    def test_coauthorship_stem(self):
        assert claims_coauthorship(GOODBOT_INCIDENT) is True
        assert claims_coauthorship(WUBOT_ORIGIN) is True
        assert claims_coauthorship(OWN_PAPER_SHARE) is False


# Adversarial-audit probes (2026-08-11, finding C1): realistic paraphrases the
# original verb-anchored grammar failed OPEN on — every one bypassed the whole
# deterministic layer. Pinned verbatim from the audit.
C1_PARAPHRASE_PROBES = [
    "We're co-authors on the *Desiderata* paper — https://doi.org/10.1093/bioadv/vbag036",
    "Our lab has co-authored the *Desiderata* paper with @WuBot",
    "We've co-authored the *Desiderata* paper",
    # The pinned incident text with Slack *emphasis* wrapping the verb.
    ":newspaper: Paper — Our lab *recently co-authored* with @WuBot and @SuBot: "
    "*Desiderata* — https://doi.org/10.1093/bioadv/vbag036",
    "As co-authors of the Desiderata paper, we'd be glad to discuss",
    "Happy to weigh in as a co-author on that",
    "Our lab is behind the recent Desiderata paper",
    "Our team published the Desiderata paper",
    "Our group co-authored it",
    "we have published, together with the Su lab, the Desiderata paper",
    "It was our privilege to co-author that",
    "I was senior author on the Desiderata paper",
    "our lab's contribution to the Desiderata paper",
    "a paper of ours",
    "We co‐authored the Desiderata paper",  # U+2010 unicode hyphen
    "We’ve co-authored the Desiderata paper",  # U+2019 unicode apostrophe
]


class TestClaimDetectionParaphrases:
    @pytest.mark.parametrize("text", C1_PARAPHRASE_PROBES)
    def test_paraphrase_is_detected(self, text):
        assert makes_first_person_authorship_claim(text) is True

    def test_correct_attribution_still_not_a_claim(self):
        assert makes_first_person_authorship_claim(CORRECT_ATTRIBUTION) is False

    def test_third_party_attribution_still_not_a_claim(self):
        assert makes_first_person_authorship_claim(
            "The Su lab published a strong KG paper — worth reading."
        ) is False

    def test_your_paper_still_not_a_claim(self):
        assert makes_first_person_authorship_claim(
            "Your paper on RIG-I mimetics (10.1000/x) is a great fit for our assay."
        ) is False


class TestValidateAuthorshipClaims:
    def test_no_claim_passes_regardless_of_records(self):
        v = validate_authorship_claims(CORRECT_ATTRIBUTION, NO_RECORDS)
        assert v.ok is True

    def test_goodbot_incident_rejected_fail_closed(self):
        # Good has zero publication records → cannot verify → reject.
        v = validate_authorship_claims(GOODBOT_INCIDENT, NO_RECORDS)
        assert v.ok is False
        assert "no publication records" in v.reason

    def test_claimed_doi_not_in_own_records_rejected(self):
        own = LabPublicationRecord(dois={"10.1000/other"}, has_records=True)
        v = validate_authorship_claims(GOODBOT_INCIDENT, own)
        assert v.ok is False
        assert DESIDERATA_DOI in v.reason

    def test_own_paper_share_passes(self):
        v = validate_authorship_claims(OWN_PAPER_SHARE, WU_RECORDS)
        assert v.ok is True

    def test_claim_without_any_doi_rejected(self):
        v = validate_authorship_claims(
            "We recently published a major paper on knowledge networks.",
            WU_RECORDS,
        )
        assert v.ok is False
        assert "without a DOI" in v.reason

    def test_wubot_origin_rejected_via_tagged_coauthor(self):
        # The DOI IS in Wu's own records — the false part is the claimed
        # co-author (@GoodBot), whose lab has no records. Fail closed.
        v = validate_authorship_claims(
            WUBOT_ORIGIN, WU_RECORDS, tagged={"GoodBot": NO_RECORDS}
        )
        assert v.ok is False
        assert "GoodBot" in v.reason

    def test_coauthorship_with_real_coauthor_passes(self):
        su = LabPublicationRecord(dois={DESIDERATA_DOI}, has_records=True)
        v = validate_authorship_claims(
            "We co-authored *Desiderata* (https://doi.org/10.1093/bioadv/vbag036) "
            "with @SuBot — happy to discuss extensions.",
            WU_RECORDS,
            tagged={"SuBot": su},
        )
        assert v.ok is True

    def test_non_coauthorship_claim_ignores_tagged_labs(self):
        # "We published X" + a tag elsewhere in the message is not a
        # co-authorship claim about the tagged lab.
        v = validate_authorship_claims(
            OWN_PAPER_SHARE + " @LairsonBot this may help your screen.",
            WU_RECORDS,
            tagged={"LairsonBot": NO_RECORDS},
        )
        assert v.ok is True


# Audit finding I2: joint-authorship phrasings that dodge the literal
# "co-author" stem. Each tags a lab with no records while the sender (Wu)
# genuinely owns the DOI — exactly the WUBOT_ORIGIN shape, reworded.
I2_JOINT_AUTHORSHIP_PROBES = [
    "We wrote the Desiderata paper together with @GoodBot — "
    "https://doi.org/10.1093/bioadv/vbag036",
    "Our joint paper with @GoodBot: *Desiderata* — "
    "https://doi.org/10.1093/bioadv/vbag036",
    "We published the Desiderata paper with @GoodBot "
    "(https://doi.org/10.1093/bioadv/vbag036).",
]


class TestJointAuthorshipSynonyms:
    @pytest.mark.parametrize("text", I2_JOINT_AUTHORSHIP_PROBES)
    def test_joint_phrasing_is_a_coauthorship_claim(self, text):
        assert claims_coauthorship(text) is True

    @pytest.mark.parametrize("text", I2_JOINT_AUTHORSHIP_PROBES)
    def test_joint_phrasing_enforces_tagged_lab_records(self, text):
        v = validate_authorship_claims(
            text, WU_RECORDS, tagged={"GoodBot": NO_RECORDS}
        )
        assert v.ok is False
        assert "GoodBot" in v.reason

    def test_own_paper_share_is_still_not_a_coauthorship_claim(self):
        assert claims_coauthorship(OWN_PAPER_SHARE) is False


class TestClaimDoiSentenceScoping:
    # Audit finding I3: a fabricated-title claim must not be satisfied by an
    # own-DOI attached to DIFFERENT work elsewhere in the message.
    I3_DOI_GAMING_PROBE = (
        "We co-authored the *Desiderata* knowledge-network paper — building "
        "on our earlier BioThings work "
        "(https://doi.org/10.1093/bioinformatics/btad570)."
    )

    def test_doi_for_other_work_does_not_ground_the_claim(self):
        # Wu owns btad570, but the co-authorship claim is about a paper whose
        # DOI is never cited — unverifiable, rejected.
        v = validate_authorship_claims(self.I3_DOI_GAMING_PROBE, WU_RECORDS)
        assert v.ok is False
        assert "without a DOI" in v.reason

    def test_own_paper_share_with_dash_delimited_doi_still_passes(self):
        v = validate_authorship_claims(OWN_PAPER_SHARE, WU_RECORDS)
        assert v.ok is True

    def test_claim_followed_by_bare_doi_sentence_passes(self):
        v = validate_authorship_claims(
            "We published BioThings Explorer. "
            "https://doi.org/10.1093/bioinformatics/btad570",
            WU_RECORDS,
        )
        assert v.ok is True


# The exact poisoned row from GoodBot's prod working memory (issue #29).
POISONED_MEMORY_ROW = (
    '| Wu Lab (@WuBot) | ❌ No proposal | Co-authored "Desiderata" paper. '
    "Core blocker. (x3 threads) |"
)


class TestStripUngroundedAuthorshipLines:
    def test_poisoned_row_is_stripped_when_no_records(self):
        memory = f"## Working Memory\n{POISONED_MEMORY_ROW}\n- Other note.\n"
        cleaned, stripped = strip_ungrounded_authorship_lines(memory, NO_RECORDS)
        assert stripped == [POISONED_MEMORY_ROW]
        assert "Desiderata" not in cleaned
        assert "Other note." in cleaned

    def test_explicitly_attributed_line_is_kept(self):
        line = "Wu Lab (@WuBot) co-authored the Desiderata paper with the Su Lab."
        cleaned, stripped = strip_ungrounded_authorship_lines(line, NO_RECORDS)
        assert stripped == []
        assert cleaned == line

    def test_own_grounded_line_is_kept(self):
        line = (
            "We published BioThings Explorer "
            "(https://doi.org/10.1093/bioinformatics/btad570) — cite in intros."
        )
        cleaned, stripped = strip_ungrounded_authorship_lines(line, WU_RECORDS)
        assert stripped == []

    def test_own_ungrounded_doi_line_is_stripped(self):
        line = "We published the KG paper (https://doi.org/10.9999/not-ours)."
        cleaned, stripped = strip_ungrounded_authorship_lines(line, WU_RECORDS)
        assert stripped == [line]

    def test_lines_without_authorship_verbs_untouched(self):
        memory = "1. Resume outreach.\n2. Monitor Liu response.\n"
        cleaned, stripped = strip_ungrounded_authorship_lines(memory, NO_RECORDS)
        assert cleaned == memory.rstrip("\n")
        assert stripped == []

    def test_pathological_capitalized_run_returns_quickly(self):
        # Audit finding M1: _OTHER_LAB_SUBJECT_RE backtracked quadratically —
        # this 4000-token line took ~1.9s pre-fix (~85s at 20k tokens), on the
        # event loop. Post-fix it completes in milliseconds; the 1s bound
        # leaves lots of headroom for a loaded CI machine.
        line = "Co-authored notes: " + ("Wu " * 4000) + "end"
        start = time.perf_counter()
        strip_ungrounded_authorship_lines(line, NO_RECORDS)
        assert time.perf_counter() - start < 1.0
