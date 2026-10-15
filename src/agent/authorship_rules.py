"""Grounding authorship claims in a lab's publication records (main SPEC GATE-1 to GATE-13).

``check_draft_authorship`` decides whether a draft's first-person authorship claims
("we published …", "our 2023 Nature paper", "the Su lab published it with us") are
backed by the speaker's records, and whether every lab named as a co-author holds the
same citations. ``strip_ungrounded_authorship_lines`` applies the same test to a
working-memory synthesis, line by line.

Design rules (main's, kept): fail closed — a lab without records cannot verify any
claim; a co-authorship claim is checked against the named partner's records too; and
the rules are deterministic and conservative, because a false rejection costs one
regenerated draft and a false pass puts a fabricated credential in a real PI's name.

Pure functions. Imports C2 (``src.agent.dois``), ``src.agent.mentions`` and C12's
``LabPublicationRecord``; never ``src.agent.agent`` (GATE-1).
"""

from __future__ import annotations

import bisect
import functools
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field

from src.agent.dois import Citation, find_citations
from src.agent.mentions import COAUTHOR_STEM_RE, LabMention, RosterEntry, find_lab_mentions
from src.agent.publication_records import LabPublicationRecord

__all__ = [
    "REASON_CODES", "AuthorshipVerdict", "ClaimReason", "LabPublicationRecord",
    "check_draft_authorship", "claims_coauthorship", "lab_self_names",
    "makes_first_person_authorship_claim", "split_sentences",
    "strip_ungrounded_authorship_lines",
]

REASON_CODES = ("no_records", "uncited", "not_held", "partner_no_records", "partner_not_held")
REASON_MAX_CHARS = 500

_NO_RECORDS = (
    "first-person authorship claim, but this lab has no publication records to verify "
    "against (fail closed)"
)
_UNCITED = (
    "first-person authorship claim without a DOI or PubMed link attached to it — "
    "unverifiable (cite the paper's DOI from your publication list)"
)
_NOT_HELD = "first-person authorship claim for {ids} not in this lab's publication records"
_PARTNER_NO_RECORDS = (
    "co-authorship claimed with @{label}, whose lab has no publication records to verify "
    "against (fail closed)"
)
_PARTNER_NOT_HELD = (
    "co-authorship claimed with @{label}, but {ids} is not in that lab's publication records"
)


@dataclass(frozen=True)
class ClaimReason:
    code: str
    text: str


@dataclass(frozen=True)
class AuthorshipVerdict:
    ok: bool
    reason: str | None = None
    has_claim: bool = False
    reasons: tuple[ClaimReason, ...] = ()


# --- GATE-2 pre-processing ------------------------------------------------------------

_UNICODE_FOLD = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"',
})
_EMPHASIS_RUN_RE = re.compile(r"[*~_]+")

# Placeholders: private-use characters, never \w, never emphasis or sentence punctuation.
_C0, _C1 = "", ""  # citation i
_S0, _S1 = "", ""  # a mention of the speaker's own lab
_O0, _O1 = "", ""  # a mention of another lab
CITE = rf"{_C0}(\d+){_C1}"
SELFM = rf"{_S0}\d+{_S1}"
OTHM = rf"{_O0}(\d+){_O1}"
_CITE_RE = re.compile(CITE)
_SLACK_LABEL_RE = re.compile(rf"<({_C0}\d+{_C1})\|([^<>\n]*)>")
_SLACK_BARE_RE = re.compile(rf"<({_C0}\d+{_C1})>")

# --- GATE-3 word classes ----------------------------------------------------------------

_AUX = r"(?:have|has|had|are|were|is|was|am|be|been|'ve|'re|'m|'d|'s)"
_ADV = (r"(?:(?!(?:on|app|fami|ita|ear)ly\b)\w+ly|just|also|both|all|first|together|"
        r"already|recently|previously|actually|jointly)")
_INS = r"(?:—[^—.!?\n]{0,80}—|\([^()!?\n]{0,80}\)|,[^,.!?\n]{0,80},)"
_JOIN = rf"(?:(?:and|&)\s+(?:the\s+)?(?:{_O0}\d+{_O1}|{SELFM}))"
_FILL = rf"(?:\s*(?:{_AUX}|{_ADV})\b|\s*{_JOIN}|\s*{_INS})"
_FP_SUBJ = (rf"(?:\b(?:we|I)\b|\bours\b|\b(?:our|my)\s+(?:own\s+)?(?:labs?|team|group)\b(?:'s)?"
            rf"|(?:\bthe\s+)?{SELFM})")
_STRONG_V = (r"(?:co-?)?(?:(?:first|senior|last|lead|corresponding|joint)-?)?(?:authored|published)"
             r"|co-?wrote|co-?first-?authored")
_WEAK_V = (r"(?:wrote|led|co-?led|released|posted|put\s+out|put\s+together|submitted|"
           r"contributed\s+to|preprinted|deposited)")
_PUB_N = r"(?:papers?|publications?|preprints?|articles?|manuscripts?)"
_WEAK_N = r"(?:study|studies|reviews?|chapters?)"
_COMPOUND = (r"(?:discussions?|clubs?|readings?|lists?|trail|queue|pile|feed|digest|"
             r"recommendations?|picks|selection|count|search|alerts?)")
_POSS = rf"(?:\b(?:our|my)\s+(?:(?:own|labs?|team|group)(?:'s)?\s+)?|(?:\bthe\s+)?{SELFM}(?:'s)?\s+)"
_MOD_STOP = (r"(?:the|a|an|your|their|his|her|its|this|that|these|those|of|in|on|at|for|from|to|with|"
             r"by|about|and|or|but|than|as|which|who|what|favou?rite|go-to|reading|colleagues?|"
             r"friends?|collaborators?|partners?|we|i|you|they|he|she|it|is|are|was|were)")
_PUB_ADJ = r"(?:published|co-?authored|preprinted|peer-reviewed|accepted|submitted|joint)"
_MOD = rf"(?:(?!{_MOD_STOP}\b)(?:{_PUB_ADJ}|(?![\w-]+(?:ed|ing)\b)[\w'-]+))"
_ATTRIB = rf"\s+(?:from|by)\s+(?:the\s+)?(?:{_O0}\d+{_O1}|(?-i:[A-Z])[\w'-]+\s+et\s+al)"
_NOUN_EXCLUSIONS = rf"\b(?!-)(?!\s+{_COMPOUND}\b)(?!{_ATTRIB})"
_CUE = r"(?:(?:19|20)\d\d|recent|previous|earlier|latest|published|prior|joint)"

#: GATE-3's claim patterns. Each comment names the pattern number in main SPEC GATE-3.
_CLAIM_PATTERNS = [
    # 1: subject + ≤ 4 fillers + strong verb ("we have just published", "we and @X co-authored")
    rf"{_FP_SUBJ}(?:{_FILL}){{0,4}}\s*(?:{_STRONG_V})\b",
    # 2: subject + ≤ 4 fillers + weak verb, a publication noun within 60 characters
    rf"{_FP_SUBJ}(?:{_FILL}){{0,4}}\s*{_WEAK_V}\b[^.;:!?\n]{{0,60}}?\b{_PUB_N}\b",
    # 3: possessive + ≤ 4 modifiers + publication noun, minus compounds and attributions
    rf"{_POSS}(?:{_MOD}\s+){{0,4}}?{_PUB_N}{_NOUN_EXCLUSIONS}",
    # 4: possessive + study/review/chapter with a cue, or "(published) in <Capitalized>"
    rf"{_POSS}(?:{_MOD}\s+){{0,3}}?{_CUE}\s+(?:{_MOD}\s+){{0,2}}?{_WEAK_N}\b",
    rf"{_POSS}(?:{_MOD}\s+){{0,3}}?{_WEAK_N}\s+(?:published\s+)?in\s+(?-i:[A-Z])",
    # 5: "<pub noun> (≤ 3 tokens) ,? which/that <subject> (≤ 2 fillers) <verb>"
    rf"\b{_PUB_N}\b(?:\s+[^\s.;!?]+){{0,3}}?\s*,?\s*(?:which|that)\s+{_FP_SUBJ}(?:{_FILL}){{0,2}}"
    rf"\s*(?:{_STRONG_V}|{_WEAK_V})\b",
    # 6: "<pub noun> <subject> …" (reduced relative: "the preprint we put out")
    rf"\b{_PUB_N}\s+{_FP_SUBJ}(?:{_FILL}){{0,2}}\s*(?:{_STRONG_V}|{_WEAK_V})\b",
    # 7: subject + ≤ 3 fillers + (all/both)? (a/an/the/one of the)? (co-)(first/…)? author(s)
    rf"{_FP_SUBJ}(?:{_FILL}){{0,3}}\s*(?:all\s+|both\s+)?(?:a\s+|an\s+|the\s+|one\s+of\s+the\s+)?"
    rf"(?:co-?)?(?:(?:first|senior|last|lead|corresponding|joint|co-first)[\s-]+)?authors?\b",
    # 8: "(proud) authors of" — literal, as GATE-3 states it
    r"\b(?:proud\s+)?authors\s+of\b",
    # 9: strong or weak verb + ≤ 60 characters + with/alongside/together with + us/me/our lab/self
    rf"\b(?:{_STRONG_V}|{_WEAK_V})\b[^.;!?\n]{{0,60}}?\b(?:with|alongside|together\s+with)\s+"
    rf"(?:us|me|our\s+(?:labs?|team|group)|(?:the\s+)?{SELFM})",
    # 10: "<pub noun> from/by (the) self"
    rf"\b{_PUB_N}\s+(?:from|by)\s+(?:the\s+)?{SELFM}",
    # 11: subject + have/had/got + a/an/another + ≤ 4 modifiers + publication noun
    rf"{_FP_SUBJ}\s*(?:have|has|had|got|'ve|'d)\s+(?:a|an|another)\s+(?:{_MOD}\s+){{0,4}}?"
    rf"{_PUB_N}{_NOUN_EXCLUSIONS}",
    # 12: "our work/findings/results/data (was/were) published/appeared"
    r"\b(?:our|my)\s+(?:work|findings|results|data)\s+(?:(?:was|were|has\s+been|have\s+been)\s+)?"
    r"(?:published|appeared)\b",
    # 13: "published/authored by us/our lab/self"
    rf"\b(?:published|authored)\s+by\s+(?:us|me|our\s+(?:labs?|team|group)|(?:the\s+)?{SELFM})",
    # 14: "self and colleagues" + strong verb
    rf"{SELFM}\s+and\s+colleagues(?:{_FILL}){{0,4}}\s*(?:{_STRONG_V})\b",
    # 15: today's forms kept: "as/being … authors on/of", "behind the … paper", "a paper of
    # ours", "privilege to co-author", "contribution to the … paper", "our published …"
    r"\b(?:as|being)\s+(?:a\s+|the\s+)?(?:co-?authors?|(?:senior|first|last|corresponding|lead)\s+authors?)"
    r"\s+(?:on|of)\b",
    rf"{_FP_SUBJ}\s+(?:is|are|was|were|am)\s+behind\s+[^.!?\n]{{0,80}}?{_PUB_N}\b",
    rf"\b{_PUB_N}\s+of\s+(?:ours|mine)\b",
    r"\b(?:our|my)\s+(?:privilege|honou?r|pleasure)\s+to\s+(?:\w+\s+)?"
    r"(?:co-?author|co-?write|author|publish|write)\b",
    rf"\b(?:our|my)\s+(?:(?:labs?|team|group)(?:'s)?\s+)?contributions?\s+to\s+[^.!?\n]{{0,80}}?{_PUB_N}\b",
    r"\b(?:our|my)\s+(?:\w+ly\s+)?(?:co-?authored|co-?wrote|co-?published|authored|published)\b",
]
_CLAIM_RES = [re.compile(p, re.IGNORECASE) for p in _CLAIM_PATTERNS]

# --- GATE-4 negation and doubt; GATE-5 quotations -------------------------------------

_HEDGE_BEFORE_RE = re.compile(
    r"(?:\b(?:can't|cannot|couldn't|could\s+not|unable\s+to|not\s+sure)"
    r"(?:\s+(?:confirm|verify|say|tell|claim))?(?:\s+(?:that|whether|if))?|\b(?:whether|if))\s*$",
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(r'"[^"\n]{1,300}"')
_QUOTE_LEADIN_RE = re.compile(
    r"(?:quot(?:e|es|ed|ing)|writes?|wrote|says?|said|states?|stated|notes?|noted|:)\s*$",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(rf"\b(?:we|us|our|ours|I|me|my|mine)\b|{SELFM}", re.IGNORECASE)

# --- GATE-6 units and anchors; GATE-7 partners --------------------------------------------

_REANCHOR_RE = re.compile(r"\b(?:our|my)\s+\w+", re.IGNORECASE)
_THIRD_PARTY_ANCHOR_RE = re.compile(
    rf"{_O0}\d+{_O1}(?:'s\b|\s+(?:\w+ly\s+)?(?:published|showed|reported|found|developed|"
    rf"described|released|posted|has|have|is|are|was|were)\b)",
    re.IGNORECASE,
)
_UNIT_SEP_RE = re.compile(rf"[;:—]|{CITE}|\b(?:and|but|while|whereas|although|plus)\b", re.IGNORECASE)
_PARTNER_PREP_RE = re.compile(
    r"(?<!consistent )(?<!compared )(?<!comparable )(?<!line )(?<!along )(?<!agrees )(?<!agree )"
    rf"\b(?:with|alongside|together\s+with|jointly\s+with|in\s+collaboration\s+with)\s+(?:the\s+)?"
    rf"{OTHM}(?!'s)",
    re.IGNORECASE,
)
_PARTNER_SUBJ_RE = re.compile(
    rf"{OTHM}\s+(?:and|&)\s+(?:I|me|we|us)\b"
    rf"|\b(?:we|I|our\s+labs?|{SELFM})\s+(?:and|&)\s+(?:the\s+)?{OTHM}",
    re.IGNORECASE,
)

# --- GATE-9 sentence splitting --------------------------------------------------------------

_ABBREV_RE = re.compile(
    r"(?:\bet\s+al|\be\.g|\bi\.e|\bcf|\bvs|\bDr|\bProf|\bFigs?|\bEqs?|\bNo|\bVol|\bapprox|\bca"
    r"|\bInc|\bLtd|\bJr|\bSr|\bSt|\bMr|\bMs|\bMrs|\bRef|\bSupp|\b[A-Z])\.$"
)
_PROTECT_RES = [
    re.compile(r"<[^<>\n]{1,400}>"),
    re.compile(r"\[[^\[\]\n]{1,300}\]\([^()\n]{1,400}\)"),
    re.compile(r"\*[^*\n]{1,300}\*"),
    re.compile(r"(?<!\w)_[^_\n]{1,300}_(?!\w)"),
    re.compile(r'"[^"\n]{1,300}"'),
    re.compile(r"\([^()\n]{0,300}\)"),
]
_SPLIT_RE = re.compile(r"[.!?]+(?=\s)|\n+")

# --- GATE-12 thread-root grounding -------------------------------------------------------

_DEFINITE_NP_RE = re.compile(
    r"\b(?:that|this|the|your)\s+((?:[\w'-]+\s+){0,4}?)"
    r"(?:papers?|preprints?|articles?|publications?|study|studies|manuscripts?)\b",
    re.IGNORECASE,
)
_MARKER_WORDS = frozenset({
    "a", "an", "another", "second", "new", "follow-up", "more",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
})
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_VENUE_RE = re.compile(r"\bin\s+((?-i:[A-Z])[\w-]*(?:\s+(?-i:[A-Z])[\w-]*)*)", re.IGNORECASE)

# --- GATE-13 memory lines ---------------------------------------------------------------------

_ANY_AUTHORSHIP_LINE_RE = re.compile(
    r"\b(?:co-?(?:first-?)?author(?:ed|s|ship)?|co-?wrote|co-?publish(?:ed)?|co-?first"
    r"|jointly\s+(?:co-?)?(?:authored|wrote|published)"
    r"|(?:authored|published|wrote)\b[^\n]{0,80}?\b(?:together|with)"
    r"|joint\s+(?:paper|publication|preprint|article|manuscript|work|study)"
    r"|shared\s+(?:authorship|byline)|authorship"
    r"|our\s+(?:joint\s+)?paper|we\s+(?:published|wrote|authored))\b",
    re.IGNORECASE,
)
_SELF_REFERENCE_RE = re.compile(r"\b(?:we|us|ours|our|my|mine)\b", re.IGNORECASE)
_OTHER_LAB_SUBJECT_RE = re.compile(
    r"\b(?!(?:Our|My)\s)[A-Z][\w.'-]*(?:\s+(?:and\s+)?[A-Z][\w.'-]*){0,6}\s+[Ll]abs?\b"
    r"(?:\s*\(@\w+\))?\s+(?:recently\s+)?(?:co-?(?:authored|wrote|published)|(?:are|were)\s+co-?authors)"
)


def split_sentences(text: str) -> list[str]:
    """GATE-9: split at ``[.!?]+`` before whitespace, and at newlines, except inside a
    protected span or after an abbreviation. Linear: the protected spans are merged once
    and each break is looked up with ``bisect``."""
    spans = sorted((m.start(), m.end()) for rx in _PROTECT_RES for m in rx.finditer(text))
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    starts = [s for s, _ in merged]
    out, last = [], 0
    for m in _SPLIT_RE.finditer(text):
        if m.group(0)[0] != "\n":
            i = bisect.bisect_right(starts, m.start()) - 1
            if i >= 0 and merged[i][0] < m.start() < merged[i][1] - 1:
                continue
            if m.group(0) == "." and _ABBREV_RE.search(text[max(0, m.start() - 8):m.end()]):
                continue
        segment = text[last:m.end()].strip()
        if segment:
            out.append(segment)
        last = m.end()
    tail = text[last:].strip()
    if tail:
        out.append(tail)
    return out


@dataclass
class _Prepared:
    sentences: list[str]
    cites: list[Citation]
    mentions: list[LabMention]


def _mask_attributed_quotes(sentence: str) -> str:
    """GATE-5: a quoted span whose 60-character lead-in ends in a quoting cue and holds
    no first-person or self token is reduced to its citations."""
    def repl(m: re.Match[str]) -> str:
        lead = sentence[max(0, m.start() - 60):m.start()]
        if _QUOTE_LEADIN_RE.search(lead) and not _FIRST_PERSON_RE.search(lead):
            return '"' + " ".join(f"{_C0}{i}{_C1}" for i in _CITE_RE.findall(m.group(0))) + '"'
        return m.group(0)
    return _QUOTE_RE.sub(repl, sentence)


def _prepare(text: str, speaker_id: str | None, roster: Sequence[RosterEntry],
             uid_map: Mapping[str, str] | None) -> _Prepared:
    """GATE-2 steps 1-7, in order."""
    cites = find_citations(text)
    parts, last = [], 0
    for i, c in enumerate(cites):
        parts.append(text[last:c.start])
        parts.append(f"{_C0}{i}{_C1}")
        last = c.end
    parts.append(text[last:])
    t = "".join(parts).translate(_UNICODE_FOLD)
    t = _SLACK_LABEL_RE.sub(r"\2 \1", t)
    t = _SLACK_BARE_RE.sub(r"\1", t)
    mentions = find_lab_mentions(t, roster, uid_map) if (roster or uid_map) else []
    parts, last = [], 0
    for k, m in enumerate(mentions):
        is_self = speaker_id is not None and speaker_id in m.agent_ids
        a, b = (_S0, _S1) if is_self else (_O0, _O1)
        parts.append(t[last:m.start])
        parts.append(f"{a}{k}{b}")
        last = m.end
    parts.append(t[last:])
    sentences = [
        _EMPHASIS_RUN_RE.sub("", _mask_attributed_quotes(s)) for s in split_sentences("".join(parts))
    ]
    return _Prepared(sentences, cites, mentions)


@functools.lru_cache(maxsize=512)
def _claim_units(sentence: str) -> tuple[tuple[int, int], ...]:
    """GATE-6: claim spans (GATE-4 hedges dropped), merged into units unless a separator
    lies between them. Cached: ``check_draft_authorship`` reads each sentence twice (once
    through ``makes_first_person_authorship_claim``), and GATE-15 bounds the total."""
    spans = sorted({
        (m.start(), m.end()) for rx in _CLAIM_RES for m in rx.finditer(sentence)
        if not _HEDGE_BEFORE_RE.search(sentence[max(0, m.start() - 60):m.start()])
    })
    units: list[list[int]] = []
    for a, b in spans:
        if units and (a <= units[-1][1] or not _UNIT_SEP_RE.search(sentence, units[-1][1], a)):
            units[-1][1] = max(units[-1][1], b)
        else:
            units.append([a, b])
    return tuple((a, b) for a, b in units)


def _is_citation_only(sentence: str) -> bool:
    return bool(_CITE_RE.search(sentence)) and not re.search(r"\w", _CITE_RE.sub("", sentence))


@dataclass
class _Claim:
    sentence: int
    cites: list[int] = field(default_factory=list)


def _attach(sentence: str, units: Sequence[tuple[int, int]]) -> dict[tuple[int, int], list[int]]:
    """GATE-6: each citation goes to its containing anchor, else the nearest preceding,
    else the following one. Only claim-unit anchors keep their citations."""
    anchors = [(a, b, True) for a, b in units]
    for rx in (_REANCHOR_RE, _THIRD_PARTY_ANCHOR_RE):
        for m in rx.finditer(sentence):
            if not any(a <= m.start() < b for a, b in units):
                anchors.append((m.start(), m.end(), False))
    anchors.sort()
    attached: dict[tuple[int, int], list[int]] = {u: [] for u in units}
    for cm in _CITE_RE.finditer(sentence):
        pos = cm.start()
        inside = [x for x in anchors if x[0] <= pos < x[1]]
        before = [x for x in anchors if x[1] <= pos]
        after = [x for x in anchors if x[0] > pos]
        target = inside[0] if inside else (before[-1] if before else (after[0] if after else None))
        if target and target[2]:
            attached[(target[0], target[1])].append(int(cm.group(1)))
    return attached


def _analyse(p: _Prepared) -> tuple[list[_Claim], list[tuple[int, set[int]]]]:
    """The claim units with their attached citations, and the sentence-level partners
    as ``(mention index, citations grounded in that sentence)``."""
    claims: list[_Claim] = []
    partners: list[tuple[int, set[int]]] = []
    for si, sentence in enumerate(p.sentences):
        units = _claim_units(sentence)
        if not units:
            continue
        attached = _attach(sentence, units)
        last_unit = units[-1]
        if (not attached[last_unit] and si + 1 < len(p.sentences)
                and _is_citation_only(p.sentences[si + 1])):
            attached[last_unit] = [int(x) for x in _CITE_RE.findall(p.sentences[si + 1])]
        sentence_cites: set[int] = set()
        for u in units:
            claims.append(_Claim(si, list(attached[u])))
            sentence_cites |= set(attached[u])
        for rx in (_PARTNER_PREP_RE, _PARTNER_SUBJ_RE):
            for m in rx.finditer(sentence):
                k = next(g for g in m.groups() if g is not None)
                partners.append((int(k), sentence_cites))
    return claims, partners


def makes_first_person_authorship_claim(
    text: str | None, *, speaker_id: str | None = None, roster: Sequence[RosterEntry] = (),
) -> bool:
    """True when ``text`` holds at least one GATE-3 claim unit."""
    if not text:
        return False
    p = _prepare(text, speaker_id, roster, None)
    return any(_claim_units(s) for s in p.sentences)


def claims_coauthorship(text: str | None) -> bool:
    """True when ``text`` carries one of GATE-7's co-authorship stems."""
    return bool(text) and bool(COAUTHOR_STEM_RE.search(text.translate(_UNICODE_FOLD).replace("*", "")))


def _render_ids(cites: Sequence[Citation]) -> str:
    rendered = {c.value: (f"PMID {c.value}" if c.kind == "pmid" else c.value) for c in cites}
    return ", ".join(rendered[v] for v in sorted(rendered))


def _reason(code: str, template: str, **values: str) -> ClaimReason:
    return ClaimReason(code, template.format(**values)[:REASON_MAX_CHARS])


def _root_grounds(sentence: str, root_plain: str) -> bool:
    """GATE-12 conditions 1 and 2 (F3, F32) for one claim unit's sentence."""
    if not any(
        not any(tok.lower() in _MARKER_WORDS or (tok.isdigit() and not _YEAR_RE.fullmatch(tok))
                for tok in m.group(1).split())
        for m in _DEFINITE_NP_RE.finditer(sentence)
    ):
        return False
    root_lower = root_plain.lower()
    if any(y not in root_lower for y in _YEAR_RE.findall(sentence)):
        return False
    return all(v.group(1).lower() in root_lower for v in _VENUE_RE.finditer(sentence))


def _ground_on_root(p: _Prepared, claims: list[_Claim], own: LabPublicationRecord,
                    context_text: str | None) -> None:
    """GATE-12 condition 3 and the attachment: an uncited unit whose sentence passes
    ``_root_grounds`` takes the root's one citation when this lab holds it."""
    if not context_text:
        return
    root_cites = find_citations(context_text)
    if len(root_cites) != 1 or not own.holds(root_cites[0]):
        return
    c = root_cites[0]
    root_plain = context_text[:c.start] + " " + context_text[c.end:]
    index = None
    for claim in claims:
        if not claim.cites and _root_grounds(p.sentences[claim.sentence], root_plain):
            if index is None:
                p.cites.append(c)
                index = len(p.cites) - 1
            claim.cites = [index]


def _pooled(agent_ids: frozenset[str], record_for: Callable[[str], LabPublicationRecord]
            ) -> LabPublicationRecord:
    records = [record_for(a) for a in sorted(agent_ids)]
    return LabPublicationRecord(
        dois=frozenset().union(*(r.dois for r in records)),
        has_records=any(r.has_records for r in records),
        pmids=frozenset().union(*(r.pmids for r in records)),
    )


def _partner_reasons(
    p: _Prepared, partners: list[tuple[int, set[int]]], claimed: list[int], text: str,
    speaker_id: str, record_for: Callable[[str], LabPublicationRecord],
    label_for: Callable[[str], str], extra_partners: Collection[str] | None,
) -> list[ClaimReason]:
    """GATE-7: every sentence partner must hold the citations grounded in its sentence;
    with a co-authorship stem, every resolved other lab (plus ``extra_partners``) must
    hold every claimed citation. Namesakes are pooled; unresolved mentions ignored."""
    others = {k: m.agent_ids for k, m in enumerate(p.mentions)
              if m.agent_ids and speaker_id not in m.agent_ids}
    checks: list[tuple[frozenset[str], list[int]]] = [
        (others[k], sorted(cites) or claimed) for k, cites in partners if k in others
    ]
    if claims_coauthorship(text):
        checks += [(ids, claimed) for ids in others.values()]
        checks += [(frozenset({a}), claimed) for a in sorted(extra_partners or ())]
    reasons: list[ClaimReason] = []
    for ids, cites in checks:
        label = "/".join(label_for(a) for a in sorted(ids))
        record = _pooled(ids, record_for)
        if not record.has_records:
            r = _reason("partner_no_records", _PARTNER_NO_RECORDS, label=label)
        else:
            missing = [p.cites[i] for i in cites if not record.holds(p.cites[i])]
            if not missing:
                continue
            r = _reason("partner_not_held", _PARTNER_NOT_HELD, label=label, ids=_render_ids(missing))
        if r not in reasons:
            reasons.append(r)
    return reasons


def check_draft_authorship(
    text: str | None, *, own: LabPublicationRecord, speaker_id: str,
    roster: Sequence[RosterEntry], record_for: Callable[[str], LabPublicationRecord],
    bot_uid_to_agent: Mapping[str, str] | None = None,
    label_for: Callable[[str], str] | None = None,
    extra_partners: Collection[str] | None = None,
    context_text: str | None = None,
) -> AuthorshipVerdict:
    """Check every first-person authorship claim in ``text`` (GATE-1 to GATE-12).

    ``own`` is the speaker's record; ``record_for(agent_id)`` gives any other lab's.
    ``label_for(agent_id)`` names a lab in a reason (default: its id).
    ``extra_partners`` are agent ids added to the co-authorship check. ``context_text``
    is the thread root, which may ground an uncited claim (GATE-12). The verdict's
    ``reasons`` are in check order; ``reason`` is the first one's text.
    """
    if not makes_first_person_authorship_claim(text, speaker_id=speaker_id, roster=roster):
        return AuthorshipVerdict(ok=True)
    p = _prepare(text or "", speaker_id, roster, bot_uid_to_agent)
    claims, partners = _analyse(p)
    if not claims:
        return AuthorshipVerdict(ok=True)
    if not own.has_records:
        r = _reason("no_records", _NO_RECORDS)
        return AuthorshipVerdict(ok=False, reason=r.text, has_claim=True, reasons=(r,))
    _ground_on_root(p, claims, own, context_text)
    reasons: list[ClaimReason] = []
    if any(not c.cites for c in claims):
        reasons.append(_reason("uncited", _UNCITED))
    claimed = sorted({i for c in claims for i in c.cites})
    unknown = [p.cites[i] for i in claimed if not own.holds(p.cites[i])]
    if unknown:
        reasons.append(_reason("not_held", _NOT_HELD, ids=_render_ids(unknown)))
    reasons += _partner_reasons(p, partners, claimed, text, speaker_id, record_for,
                                label_for or (lambda a: a), extra_partners)
    if reasons:
        return AuthorshipVerdict(ok=False, reason=reasons[0].text, has_claim=True,
                                 reasons=tuple(reasons))
    return AuthorshipVerdict(ok=True, has_claim=True)


# --- GATE-13 working memory ----------------------------------------------------------------


def lab_self_names(agent_id: str | None, bot_name: str | None, pi_name: str | None
                   ) -> tuple[str, ...]:
    """Names of an agent's own lab, e.g. ``("GoodBot", "Benjamin Good", "Good")`` for
    good: the bot name, the PI's name, the PI's surname and the title-cased agent id."""
    pi = (pi_name or "").strip()
    names: list[str] = []
    for candidate in ((bot_name or "").strip(), pi, pi.split()[-1] if pi else "",
                      (agent_id or "").strip().title()):
        if candidate and candidate not in names:
            names.append(candidate)
    return tuple(names)


def _compile_self_lab_re(self_names: Sequence[str]) -> re.Pattern[str] | None:
    """The agent's own lab named as a third party: a lab noun in any case after a
    case-sensitive name, "(lab|group|team) of (Dr./Prof.) NAME", coordinated forms, the
    full PI name, and the bot name in any case."""
    group = r"(?i:labs?|laborator(?:y|ies)|groups?|teams?)"
    alts: list[str] = []
    for name in (n.strip() for n in self_names if n and n.strip()):
        esc = re.escape(name)
        if name.lower().endswith("bot"):
            alts.append(rf"(?i:@?{esc})\b")
            continue
        alts.append(rf"\b{esc}(?:['’]s)?(?:\s*(?:,|and|&|/)\s*[A-Z][\w-]*){{0,4}}\s+{group}\b")
        alts.append(rf"\b{group}\s+of\s+(?:(?:Dr|Prof)\.?\s+)?{esc}\b")
        alts.append(rf"\b[A-Z][\w-]*(?:\s*(?:,|and|&|/)\s*[A-Z][\w-]*){{0,4}}\s*(?:,|and|&|/)\s*"
                    rf"{esc}(?:['’]s)?\s+{group}\b")
        if " " in name:
            alts.append(rf"\b{esc}\b")
    return re.compile("|".join(alts)) if alts else None


def _normalize_line(line: str) -> str:
    """Unicode fold and emphasis strip outside citation spans (DOIs keep their '_')."""
    out, last = [], 0
    for c in find_citations(line):
        out.append(_EMPHASIS_RUN_RE.sub("", line[last:c.start].translate(_UNICODE_FOLD)))
        out.append(line[c.start:c.end])
        last = c.end
    out.append(_EMPHASIS_RUN_RE.sub("", line[last:].translate(_UNICODE_FOLD)))
    return "".join(out)


def strip_ungrounded_authorship_lines(
    memory_text: str, own: LabPublicationRecord, self_names: Sequence[str] = (), *,
    speaker_id: str | None = None, roster: Sequence[RosterEntry] = (),
) -> tuple[str, list[str]]:
    """GATE-13: drop each memory line that asserts authorship the lab's records cannot
    back. A line is an authorship line when ``_ANY_AUTHORSHIP_LINE_RE`` or the claim
    grammar matches. It is kept when another lab is its explicit subject and it does not
    also point back at this lab, or when its citations are non-empty and all held.
    Returns ``(kept text, stripped lines)``."""
    self_lab_re = _compile_self_lab_re(self_names)
    kept: list[str] = []
    stripped: list[str] = []
    for line in memory_text.splitlines():
        norm = _normalize_line(line)
        if _ANY_AUTHORSHIP_LINE_RE.search(norm) or makes_first_person_authorship_claim(
            line, speaker_id=speaker_id, roster=roster,
        ):
            exempt = (_OTHER_LAB_SUBJECT_RE.search(norm) and not _SELF_REFERENCE_RE.search(norm)
                      and not (self_lab_re and self_lab_re.search(norm)))
            if not exempt:
                cites = find_citations(line)
                if not (cites and all(own.holds(c) for c in cites)):
                    stripped.append(line)
                    continue
        kept.append(line)
    return "\n".join(kept), stripped
