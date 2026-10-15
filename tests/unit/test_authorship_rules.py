"""The authorship-claim rules (spec 2026-10-15 §11 R6.2 to R6.6): GATE-2 to GATE-12,
main SPEC §11's acceptance corpus ported verbatim, blackbird rows b01-b06, and GATE-15."""
import ast
import inspect
import time
from pathlib import Path

import pytest

from src.agent import authorship_rules as rules
from src.agent.authorship_rules import (
    ClaimReason,
    LabPublicationRecord,
    check_draft_authorship,
    makes_first_person_authorship_claim,
    split_sentences,
    strip_ungrounded_authorship_lines,
)
from src.agent.mentions import RosterEntry, _roster_pattern, find_lab_mentions

# Shorthands (main SPEC §11, "DOI shorthands"); {K} in a text is https://doi.org/ + K.
D = "10.1093/bioadv/vbag036"
B = "10.1093/bioinformatics/btad570"
P = "10.1016/S0092-8674(02)00722-5"
S = "10.1002/1097-0134(20010201)42:2<279::AID-PROT150>3.0.CO;2-U"
U = "10.1007/978-1-4939-7514-3_14"
PS = "10.1016/S0092-8674(02)00999-9"
X = "10.9999/fabricated.1"
C = "10.5555/chin.2023.1"
PMID = "31234567"
SHORT = {"D": D, "B": B, "P": P, "S": S, "U": U, "PS": PS, "X": X, "C": C}


def K(text: str) -> str:
    """Expand {K} to https://doi.org/<DOI of K>."""
    for key in sorted(SHORT, key=len, reverse=True):
        text = text.replace("{" + key + "}", "https://doi.org/" + SHORT[key])
    return text


def _rec(*dois, pmids=()):
    return LabPublicationRecord(
        dois=frozenset(d.lower() for d in dois), has_records=bool(dois or pmids),
        pmids=frozenset(pmids),
    )


MAIN_ROSTER = (
    RosterEntry("good", "GoodBot", "Benjamin Good"),
    RosterEntry("wu", "WuBot", "Chunlei Wu"),
    RosterEntry("su", "SuBot", "Andrew Su"),
)
R = (*MAIN_ROSTER, RosterEntry("chin", "ChinBot", "Jason Chin"),
     RosterEntry("blackbird", "BlackbirdBot", None))
REC = {
    "good": LabPublicationRecord(),
    "wu": _rec(D, B, P, S, U, pmids=(PMID,)),
    "su": _rec(D),
    "chin": _rec(C),
    "blackbird": LabPublicationRecord(),
}
BOT = {e.agent_id: e.bot_name for e in R}


def _check(speaker, text, *, roster=R, rec=REC, **kw):
    return check_draft_authorship(
        K(text), own=rec[speaker], speaker_id=speaker, roster=roster,
        record_for=rec.__getitem__, label_for=BOT.__getitem__, **kw,
    )


# --- R6.5: the acceptance corpus, verbatim (62 main rows), plus b01-b06 ------------------

MAIN_CORPUS = [
    ("c01", "wu", "Our 2023 Nature paper showed that chemical probes transfer across species.", "Y/REJ"),
    ("c02", "wu", "Our study in Cell described the same pipeline.", "Y/REJ"),
    ("c03", "wu", "Our previous paper already benchmarked this.", "Y/REJ"),
    ("c04", "wu", "Our earlier publication covers the method in detail.", "Y/REJ"),
    ("c05", "wu", "We're authors on the Desiderata paper, so happy to discuss.", "Y/REJ"),
    ("c06", "wu", "I'm an author on that review.", "Y/REJ"),
    ("c07", "wu", "We co-first-authored the benchmark paper last year.", "Y/REJ"),
    ("c08", "wu", "My group put out a preprint on this in March.", "Y/REJ"),
    ("c09", "wu", "The Desiderata paper, which we led, frames the problem.", "Y/REJ"),
    ("c10", "su", "Our lab's 2022 paper with the Wu lab is relevant here.", "Y/REJ"),
    ("c11", "wu", "Our 2023 Nature paper ({B}) showed this.", "Y/ok"),
    ("c12", "wu", "The Desiderata paper, which we led, is here: {D}", "Y/ok"),
    ("c13", "wu", "Our paper with @GoodBot is {B}.", "Y/REJ"),
    ("c14", "wu", "@GoodBot and I published {B}.", "Y/REJ"),
    ("c15", "wu", "We published {B} alongside @GoodBot.", "Y/REJ"),
    ("c16", "wu", "We and @GoodBot co-published {B}.", "Y/REJ"),
    ("c17", "wu", "We and @SuBot co-published {D}.", "Y/ok"),
    ("c18", "wu", "Our paper with @SuBot ({B}) extends this.", "Y/REJ"),
    ("c19", "wu", "We published {P} in Cell.", "Y/ok"),
    ("c20", "wu", "We published ({P}).", "Y/ok"),
    ("c21", "wu", "We published <{P}|our Cell paper>.", "Y/ok"),
    ("c22", "wu", "We published {S} back in 2001.", "Y/ok"),
    ("c23", "wu", "We published {PS}.", "Y/REJ"),
    ("c24", "wu", "We published [our Cell paper]({P}).", "Y/ok"),
    ("c25", "wu", "We co-authored the Desiderata paper and our lab also published {B}.", "Y/REJ"),
    ("c26", "wu", "Our 2023 paper ({B}) and our 2024 preprint ({D}) both apply.", "Y/ok"),
    ("c27", "wu", "Our lab recently published our paper on this: {B}", "Y/ok"),
    ("c28", "wu", "Our paper {B}: see Fig. 2.", "Y/ok"),
    ("c29", "wu", "Have you seen our paper {B}?", "Y/ok"),
    ("c30", "wu", "We just published {B}!", "Y/ok"),
    ("c31", "wu", "We published `10.1093/bioinformatics/btad570` last year.", "Y/ok"),
    ("c33", "wu", "We published a chapter: {U}", "Y/ok"),
    ("c34", "wu", "We published https://doi.org/10.1016%2FS0092-8674%2802%2900722-5 in Cell.", "Y/ok"),
    ("c35", "wu", "We published the benchmark with Chen et al. in 2023 ({B}).", "Y/ok"),
    ("c36", "wu", "Our paper *Is knowledge graph reasoning enough? A benchmark* (Bioinformatics, 2023) — {B}", "Y/ok"),
    ("c37", "wu", "We published *BioThings Explorer. Part 2* {B}", "Y/ok"),
    ("c38", "wu", "We published this in 2019 — https://pubmed.ncbi.nlm.nih.gov/31234567/", "Y/ok"),
    ("c39", "wu", "Our paper (PMID: 31234567) covers this.", "Y/ok"),
    ("c40", "wu", "Dr. Su and I published {D}.", "Y/ok"),
    ("c41", "wu", "We co-authored {D} with the Good and Su labs.", "Y/REJ"),
    ("c42", "wu", "We co-authored {D} with Benjamin Good.", "Y/REJ"),
    ("c43", "wu", "We co-authored it with Benjamin Good's group: {D}", "Y/REJ"),
    ("c44", "wu", "We co-authored {D} with the Good team.", "Y/REJ"),
    ("c45", "wu", "We co-authored {D} with the lab of Benjamin Good.", "Y/REJ"),
    ("c46", "wu", "We co-authored {D} with GoodBot.", "Y/REJ"),
    ("c48", "su", "The Su lab published {X} last year.", "Y/REJ"),
    ("n02", "good", "Our paper discussion yesterday was great.", "N/ok"),
    ("n04", "good", "We read the paper published in Cell last year.", "N/ok"),
    ("n05", "good", "As I wrote above, the assay needs a control.", "N/ok"),
    ("n06", "good", "We haven't published on this yet.", "N/ok"),
    ("n11", "good", "Our favorite paper from the Su lab is {D}.", "N/ok"),
    ("n14", "good", "Our lab studies kinase signaling.", "N/ok"),
    ("n16", "good", "That paper was published by the Wu lab ({D}).", "N/ok"),
    ("n19", "good", "We discussed the preprint our colleagues at Scripps put out.", "N/ok"),
    ("n20", "good", "Our paper trail on this project is thin.", "N/ok"),
    ("n23", "good", "Our team reviewed papers for the workshop.", "N/ok"),
    ("n25", "good", "Together with @SuBot we could publish a follow-up.", "N/ok"),
    ("n26", "good", "We wrote to the Su lab last week about reagents.", "N/ok"),
    ("n27", "good", 'Quoting the Wu lab paper: "we published the first atlas of X" ({D}).', "N/ok"),
    ("n28", "good", "Our lab would co-author with anyone who has good data.", "N/ok"),
    ("n29", "good", "Our Nature paper club meets Fridays.", "N/ok"),
    ("n30", "good", "In our paper-reading group we covered @SuBot's preprint.", "N/ok"),
]
BLACKBIRD_ROWS = [
    ("b01", "wu", "@BlackbirdBot We published {B} and it underpins this pitch.", "Y/ok"),
    ("b02", "wu", "@BlackbirdBot Our 2023 Nature paper showed the effect.", "Y/REJ"),
    ("b03", "wu", "We co-authored {B} with @BlackbirdBot.", "Y/REJ"),
    ("b04", "blackbird", "Your 2023 paper ({B}) is the key evidence here.", "N/ok"),
    ("b05", "blackbird", "We published {B}.", "Y/REJ"),
    ("b06", "wu", "The Wu lab published {B} last year.", "Y/ok"),
]


def test_the_corpus_is_complete():
    assert len(MAIN_CORPUS) == 62
    assert len({row[0] for row in MAIN_CORPUS + BLACKBIRD_ROWS}) == 68


@pytest.mark.parametrize(("cid", "spk", "text", "exp"), MAIN_CORPUS + BLACKBIRD_ROWS,
                         ids=[r[0] for r in MAIN_CORPUS + BLACKBIRD_ROWS])
def test_corpus(cid, spk, text, exp):
    expect_claim, expect_ok = exp.startswith("Y"), exp.endswith("ok")
    rosters = (R, MAIN_ROSTER) if cid[0] != "b" else (R,)
    for roster in rosters:  # the hub and chin change no main expectation
        assert makes_first_person_authorship_claim(K(text), speaker_id=spk, roster=roster) is expect_claim
        assert _check(spk, text, roster=roster).ok is expect_ok


@pytest.mark.parametrize("text", [
    "We have a question about your paper.",
    "I have a comment on the Su lab's preprint.",
    "We have a preprint from the Su lab on our list.",
    "We had a paper by Chen et al. in mind.",
    "We have a paper club on Fridays.",
])
def test_further_sentences_are_not_claims(text):
    assert makes_first_person_authorship_claim(text, speaker_id="good", roster=R) is False
    assert _check("good", text).ok is True


# --- R6.2 -------------------------------------------------------------------------------

@pytest.mark.parametrize(("pattern", "sentence"), [
    (4, "Our 2019 study showed the same effect."),
    (7, "We are both authors on that preprint."),
    (8, "We are proud authors of that atlas."),
    (9, "The Su lab published it with us."),
    (10, "This is a paper from the Good lab."),
    (11, "We have a Nature paper on exactly this."),
    (12, "Our findings were published last year."),
    (13, "It was published by our lab in 2021."),
    (14, "Dr. Good and colleagues published the method."),
    (15, "It is a paper of ours."),
])
def test_gate3_forms_are_claims(pattern, sentence):
    v = _check("good", sentence)
    assert (v.has_claim, v.ok) == (True, False), pattern


def test_hedges_drop_the_match():
    assert _check("su", "I can't confirm that we published that paper.").has_claim is False
    assert _check("good", "Not sure whether we published that.").has_claim is False
    v = _check("good", "If you look at our 2023 paper, the method is there.")
    assert (v.has_claim, v.ok) == (True, False)


def test_attributed_quote_with_first_person_cue_stays_a_claim():
    v = _check("good", 'As we wrote: "we published the first atlas of X" ({D}).')
    assert (v.has_claim, v.ok) == (True, False)


def test_emphasis_and_unicode_hyphens_are_folded():
    text = "We *recently co‐authored* {B}."
    assert _check("wu", text).ok is True
    assert _check("good", text).ok is False


def test_split_sentences_keeps_abbreviations_and_spans():
    assert split_sentences("Dr. Su and I wrote it. See Fig. 2 and Eq. 3. Done!") == [
        "Dr. Su and I wrote it.", "See Fig. 2 and Eq. 3.", "Done!",
    ]
    assert len(split_sentences(K("We published *BioThings Explorer. Part 2* {B}"))) == 1


def test_rules_module_does_not_import_the_agent_module():
    tree = ast.parse(Path(rules.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "src.agent.agent" not in imported


# --- R6.3 -------------------------------------------------------------------------------

def test_reason_codes_and_texts():
    assert _check("good", "We published {B}.").reasons == (ClaimReason(
        "no_records",
        "first-person authorship claim, but this lab has no publication records to verify "
        "against (fail closed)",
    ),)
    assert _check("wu", "We published {X}.").reasons == (ClaimReason(
        "not_held",
        "first-person authorship claim for 10.9999/fabricated.1 not in this lab's publication records",
    ),)
    assert _check("wu", "Our 2023 Nature paper showed this.").reasons == (ClaimReason(
        "uncited",
        "first-person authorship claim without a DOI or PubMed link attached to it — "
        "unverifiable (cite the paper's DOI from your publication list)",
    ),)
    assert _check("wu", "We co-authored {B} with @GoodBot.").reasons == (ClaimReason(
        "partner_no_records",
        "co-authorship claimed with @GoodBot, whose lab has no publication records to verify "
        "against (fail closed)",
    ),)
    v = _check("wu", "Our paper with @SuBot ({B}) extends this.")
    assert v.reasons == (ClaimReason(
        "partner_not_held",
        "co-authorship claimed with @SuBot, but 10.1093/bioinformatics/btad570 is not in that "
        "lab's publication records",
    ),)
    assert v.reason == v.reasons[0].text


def test_pmid_rendering_in_reasons():
    v = _check("wu", "Our paper (PMID: 99999999) covers this.")
    assert v.reasons[0].code == "not_held" and "PMID 99999999" in v.reasons[0].text


def test_citation_only_next_sentence_attaches():
    assert _check("wu", "We published our benchmark last year. {B}").ok is True


def test_namesake_partners_are_pooled():
    roster = (*R, RosterEntry("pwu", "PwuBot", "Peng Wu"))
    rec = {**REC, "pwu": LabPublicationRecord(), "su": _rec(B, X)}
    bots = {**BOT, "pwu": "PwuBot"}

    def check(text):
        return check_draft_authorship(K(text), own=rec["su"], speaker_id="su", roster=roster,
                                      record_for=rec.__getitem__, label_for=bots.__getitem__)

    assert check("We co-authored {B} with the Wu lab.").ok is True
    v = check("We co-authored {X} with the Wu lab.")
    assert v.reasons[0].code == "partner_not_held" and "@PwuBot/WuBot" in v.reasons[0].text


def test_unresolved_mentions_are_ignored():
    assert _check("wu", "We co-authored {B} with @NobodyBot.").ok is True


def test_extra_partners_join_the_coauthorship_check():
    v = _check("wu", "We co-authored {B}.", extra_partners={"good"})
    assert v.reasons[0].code == "partner_no_records" and "@GoodBot" in v.reasons[0].text


def test_lab_without_records_fails_every_claim():
    v = _check("blackbird", "We published {B}.")
    assert (v.has_claim, v.ok, v.reasons[0].code) == (True, False, "no_records")


def test_check_signature_is_keyword_only():
    params = list(inspect.signature(check_draft_authorship).parameters.values())
    assert [p.name for p in params] == [
        "text", "own", "speaker_id", "roster", "record_for", "bot_uid_to_agent", "label_for",
        "extra_partners", "context_text",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:])


# --- R6.4: thread-root grounding -------------------------------------------------------

R_WU = K(":bulb: Pitch — Our query engine ({B}) can screen rare-disease cohorts. @BlackbirdBot")
R_WU_DATED = R_WU.replace(f"(https://doi.org/{B})", f"(Bioinformatics, 2023 — https://doi.org/{B})")
R_WU_TWO = K(":bulb: Pitch — Our query engine ({B}) and our graph ({D}) can screen cohorts.")
R_CHIN = K(":bulb: Pitch — Our base editor ({C}) corrects the variant in organoids. @BlackbirdBot")
CHIN_ACK = "Plainly: yes, that paper is ours — Prof. Chin is an author on it."


@pytest.mark.parametrize("text", [
    "We published a second Nature paper on this last month.",
    "Our 2024 Cell paper builds on that.",
    "We also published a follow-up on ubiquitin chains that extends it.",
    "Yes, that paper is ours. We also published three more papers on this in Cell.",
    "We co-authored that paper with @GoodBot.",
])
def test_root_must_reject(text):
    assert _check("wu", text, context_text=R_WU).ok is False


def test_noun_phrase_anywhere_in_the_sentence_grounds_f3():
    v = _check("chin", CHIN_ACK, context_text=R_CHIN)
    assert (v.has_claim, v.ok) == (True, True)


def test_foreign_root_does_not_ground():
    v = _check("chin", CHIN_ACK, context_text=R_CHIN.replace(C, X))
    assert (v.ok, v.reasons[0].code) == (False, "uncited")


def test_missing_root_does_not_ground():
    assert _check("chin", CHIN_ACK, context_text=None).ok is False


def test_root_with_two_citations_does_not_ground():
    assert _check("wu", "Yes — Dr. Wu is an author on that paper.", context_text=R_WU_TWO).ok is False


def test_year_must_appear_in_the_root():
    text = "Yes — Dr. Wu is an author on that 2023 paper."
    assert _check("wu", text, context_text=R_WU).ok is False
    assert _check("wu", text, context_text=R_WU_DATED).ok is True


def test_venue_must_appear_in_the_root():
    text = "Yes — Dr. Wu is an author on that paper in Nature."
    assert _check("wu", text, context_text=R_WU_DATED).ok is False


def test_marker_in_the_noun_phrase_bars_grounding():
    text = "Yes — Dr. Wu is an author on that new paper."
    assert _check("wu", text, context_text=R_WU).ok is False


def test_unit_follow_up_marker():
    """F32 accepts this false pass: "That paper" is a definite noun phrase in the second
    unit's sentence, and "follow-up" does not precede its noun."""
    v = _check("wu", "That paper is ours, and we also published a follow-up.", context_text=R_WU)
    assert (v.has_claim, v.ok) == (True, True)


# --- R6.6: GATE-15 performance ---------------------------------------------------------

BIG = (RosterEntry("wu", "WuBot", "Chunlei Wu"),
       *(RosterEntry(f"agent{i}", f"Agent{i}Bot", f"Pat Name{i}") for i in range(126)))
BIG_REC = {e.agent_id: LabPublicationRecord() for e in BIG} | {"wu": REC["wu"]}


@pytest.mark.parametrize("text", [
    "Wu " * 4000, "our " * 5000 + "paper", "10.1234/" + "(" * 5000, "(a). " * 3000, '"a". ' * 3000,
], ids=["wu", "our", "parens", "paren-sentences", "quote-sentences"])
def test_adversarial_inputs_finish_within_a_second(text):
    for run in (
        lambda: find_lab_mentions(text, BIG),
        lambda: check_draft_authorship(text, own=BIG_REC["wu"], speaker_id="wu", roster=BIG,
                                       record_for=BIG_REC.__getitem__),
        lambda: strip_ungrounded_authorship_lines(text, BIG_REC["wu"], ("WuBot", "Chunlei Wu"),
                                                  speaker_id="wu", roster=BIG),
    ):
        start = time.perf_counter()
        run()
        assert time.perf_counter() - start < 1.0


def test_roster_pattern_is_compiled_once():
    _roster_pattern.cache_clear()
    find_lab_mentions("the Wu lab", BIG)
    find_lab_mentions("the Wu lab", tuple(BIG))
    assert _roster_pattern.cache_info().hits >= 1
