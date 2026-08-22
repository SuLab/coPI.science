"""US prior-art search via the USPTO Open Data Portal (api.uspto.gov).

US filings only — see the caveat in src/agent/tools.py where results are surfaced.

Endpoint history: this tool originally targeted PatentsView
(search.patentsview.org), which PatentsView decommissioned in its 2026-03-20
migration to the USPTO Open Data Portal. The current endpoint is the ODP Patent
File Wrapper (PFW) search at api.uspto.gov. The response is application-centric
(patentFileWrapperDataBag) rather than granted-patent-centric, so we map the
applicationMetaData fields that matter for prior-art scouting (title, publication
number, dates, applicant, inventor, status). Abstracts are not returned by the
search endpoint.

Two things beyond that mapping matter to a scouting hub and are handled here rather
than left to the caller: ODP's top-level ``count`` (how many filings matched, as
opposed to how many were returned — see ``PriorArtResult.truncation_note``), and
the evidentiary weight of each filing's status (see ``_prior_art_class``; an
expired provisional was never published and is not prior art at all).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"

# One search token: a run of letters/digits, optionally carrying the ONE piece of
# punctuation that survives tokenisation — a hyphen joining a symbol to a NUMERIC
# suffix (LOX-1, GDF-15, GS-441524, MIP-3alpha, gamma-8, COVID-19). Everything else
# in the caller's text is a token boundary, including a hyphen before a letter
# ("myeloid-derived" -> myeloid, derived), so every query without a SYMBOL-N in it
# is tokenised exactly as it was before.
#
# This still strips the simplified query syntax's special characters, so the query
# cannot be broken or injected: a match can only START on an alphanumeric, so a
# leading "-" — the syntax's NOT operator — can never reach USPTO.
#
# Splitting SYMBOL-N was root cause half 1 of run 8b64a0e0's H3: the numeric suffix
# became a token of its own, and _salience ranked a bare number above every 2-4
# letter gene symbol (half 2, below), so the narrowest backoff tier of
# "MIP-3alpha ..." was ['3alpha'], of "GDF-15 ..." was ['15'], of "GS-441524 ..."
# was ['441524'] and of "TARP gamma-8 ..." was ['8'].
_Q_TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[0-9][A-Za-z0-9]*)*")

# Greek spelled out, the way patent titles write it — this module's own comments
# above already cite MIP-3alpha and gamma-8, and live 2026-08-22
# `inventionTitle:(beta)` returns 13,640 while `inventionTitle:(alpha AND
# estrogen)` returns 112.
#
# This is here INSTEAD of widening `_Q_TOKEN` to accept non-ASCII, which is the
# obvious fix and the wrong one. ODP cannot represent non-ASCII at all: probed
# live, `inventionTitle:(β)`, `(Qβ)` and `("Qβ")` all return HTTP 404, and
# `_search_titles` maps 404 to `([], 0)` = "searched, matched nothing". Widening
# the class would therefore 404 every tier and report "No US filings matched
# this query" — fake novelty, which is the one thing this tool must never
# manufacture ("An empty title search is never FTO").
#
# What the ASCII-only class actually cost was not the term but its SPECIFICITY:
# for `Qβ malaria epitope` the tokens were ['Q', 'malaria', 'epitope'] — `Q` is
# ASCII — and `_salience('Q')` ranks it first, so the narrowest backoff tier was
# a one-letter title search. Live: `inventionTitle:(Q)` -> HTTP 200, count
# 1,862, i.e. ten arbitrary filings offered to the hub as adjacent prior art.
#
# Case is preserved (Γ -> "Gamma", γ -> "gamma") so `_salience`'s "this is a
# symbol, not prose" bonus survives the rewrite.
#
# THE WHOLE ALPHABET, not the letters some incident happened to name. A partial
# table reproduces the bug above through whichever letter is missing, and the
# first version of this table proved it: `π-π stacking` tokenised as
# ['stacking'], `CD3ζ signalling` as ['CD3', 'signalling'] and `Φ29 polymerase`
# as ['29', 'polymerase'] — the specific term deleted in all three, `broadened`
# reported False, and `dropped_or_rewritten` empty. The alphabet is finite and
# fits here; the judgement call about which letters "matter" does not.
#
# Compatibility duplicates are NOT listed, because `_to_ascii` runs NFKD FIRST
# and NFKD folds every one of them onto the base letter: U+00B5 MICRO SIGN and
# U+2126 OHM SIGN (the codepoints a units string and a PDF paste really carry),
# the ϐϑϒϕϖϰϱϵϴ variant letters, the mathematical alphanumerics (𝛽 …), and
# accented Greek (ά -> α + combining acute). Verified for each, 2026-08-22.
# U+03C2 FINAL SIGMA is the exception — NFKD leaves it alone — so it is listed.
#
# Known hazard, deliberately accepted: the capitals ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ are
# homoglyphs of Latin ABEZHIKMNOPTYX, so a paste artifact spells out as
# "Omicron" rather than reading as "O". Both readings are wrong for a paste and
# neither can be told from the other at this layer — but the spelled-out one is
# DISCLOSED (`dropped_or_rewritten`), where a silent drop was not, which is the
# property this whole path is being repaired for.
_GREEK_TO_ASCII = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "ο": "omicron",
    "π": "pi", "ρ": "rho", "ς": "sigma", "σ": "sigma", "τ": "tau",
    "υ": "upsilon", "φ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "Α": "Alpha", "Β": "Beta", "Γ": "Gamma", "Δ": "Delta", "Ε": "Epsilon",
    "Ζ": "Zeta", "Η": "Eta", "Θ": "Theta", "Ι": "Iota", "Κ": "Kappa",
    "Λ": "Lambda", "Μ": "Mu", "Ν": "Nu", "Ξ": "Xi", "Ο": "Omicron",
    "Π": "Pi", "Ρ": "Rho", "Σ": "Sigma", "Τ": "Tau", "Υ": "Upsilon",
    "Φ": "Phi", "Χ": "Chi", "Ψ": "Psi", "Ω": "Omega",
}

# Unicode dashes -> ASCII "-", so `_Q_TOKEN`'s SYMBOL-N protection (which is
# written against ASCII "-" only) still sees the join. An EXPLICIT table rather
# than NFKC, because NFKC does not fold U+2010/2011/2013/2014 to ASCII at all —
# so the measured `LOX‑1` (U+2011, the form a PDF-derived query carries)
# tokenised as ['LOX', '1'] and backed off to the numeral, the exact bug the
# hyphen rule was added to fix.
#
# This cannot open an injection: the folded "-" is still only ever a token
# CONTINUATION, because `_Q_TOKEN` can only START a match on an alphanumeric.
# A leading "−" (U+2212) becomes a leading "-" and is discarded the same way an
# ASCII one always was — pinned by
# `test_the_character_class_still_blocks_a_leading_dash`.
_DASH_TO_ASCII = dict.fromkeys(
    (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFF0D), "-"
)

_TRANSLITERATE = {
    **{ord(k): v for k, v in _GREEK_TO_ASCII.items()},
    **_DASH_TO_ASCII,
}

# ODP's boolean operators, which are SYNTAX and never search terms. `_q_term`
# quotes only hyphenated tokens, so a bare `NOT` survived tokenisation — and
# `_salience("NOT")` (uppercase, 3 chars) outranks all prose, so it could become
# a whole backoff tier of its own. Live: `inventionTitle:(TFEB AND NOT)` ->
# HTTP 200, count 0, which `_execute_search_prior_art` renders as "No US filings
# matched this query": clean novelty out of a syntax accident. `and`/`or` were
# already in `_GENERIC`, but that only affects RANKING — tier 1 is the query
# verbatim, which is where the damage was, so they are dropped from the token
# stream instead.
_QUERY_OPERATORS = frozenset({"and", "or", "not"})


def _to_ascii(text: str) -> str:
    """Fold one chunk of caller text to ASCII: Greek spelled out, Unicode dashes
    folded, combining marks stripped from accented Latin (Ångström -> Angstrom).

    NFKD runs FIRST, so the table only ever has to list BASE letters: every
    compatibility duplicate (U+00B5 MICRO SIGN, U+2126 OHM SIGN, the variant
    letters, the mathematical alphanumerics, accented Greek) decomposes onto one
    of them on the way in. It used to run second, which is how U+2126 reached
    `_Q_TOKEN` as U+03A9, was dropped, and was reported as `Ω29→Ω29` — a
    disclosure that rendered as no change at all. NFKD also folds U+2011 to
    U+2010 and U+FF0D to ASCII "-", both of which the dash table then covers.

    Anything still non-ASCII afterwards (CJK, Cyrillic, an arrow, ∆) is left
    alone here and discarded by `_Q_TOKEN` — `_prepare` is what makes sure the
    caller is TOLD about it.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in decomposed.translate(_TRANSLITERATE)
        if not unicodedata.combining(ch)
    )


def _prepare(query: str) -> tuple[list[str], tuple[str, ...]]:
    """Title-search tokens, plus a note for everything the caller's text had to
    have done to it to get there. Never raises; may return ``([], ...)``.

    The notes exist because the model judges the hits against the phrase it
    THINKS it searched: `Qbeta` results read as a clean answer about `Qβ` unless
    something says otherwise. They are carried on
    ``PriorArtResult.dropped_or_rewritten`` and rendered by
    ``src/agent/tools.py::_scope_note``.

    The disclosure is TOTAL, and the first version was not: it fired only when
    the fold CHANGED the chunk, so a chunk that folded to itself and then
    vanished at the ASCII token class reported nothing — `π-π stacking` reached
    the model as "SCOPE: searched titles for stacking." with `broadened` False, a
    term silently deleted and the note saying nothing had happened. That is the
    same class of damage the transliteration exists to stop. Three branches
    instead of one, per whitespace chunk:

    * nothing survived and the chunk was not ASCII -> say the term was dropped.
      An ASCII punctuation-only chunk ("/") is deliberately silent: nothing was
      ever there to lose, and a note per slash would drown the real ones. An
      all-operator chunk is silent here too, having already been noted below.
    * something survived but characters did not -> say what was actually
      searched, not just that "something changed".
    * a clean rewrite -> the arrow form.

    In every arrow-form note the right-hand side is what was SENT, and what is
    sent is always ASCII — so a note can never again render as an identity
    mapping (pinned by `test_a_disclosure_note_is_never_an_identity_mapping`).

    Chunk-at-a-time rather than whole-string, so a chunk and its rewrite always
    line up. `_Q_TOKEN` cannot span whitespace, so this tokenises identically to
    the joined form.
    """
    tokens: list[str] = []
    notes: list[str] = []
    for chunk in (query or "").split():
        folded = _to_ascii(chunk)
        kept: list[str] = []
        for token in _Q_TOKEN.findall(folded):
            if token.lower() in _QUERY_OPERATORS:
                notes.append(f"{token} (dropped — a query operator, not a title word)")
                continue
            kept.append(token)
        if not kept:
            if not chunk.isascii():
                notes.append(
                    f"{chunk} (dropped — no ASCII equivalent, so nothing was "
                    f"searched for it)"
                )
        elif not folded.isascii():
            notes.append(
                f"{chunk}→{' '.join(kept)} "
                f"(characters with no ASCII equivalent were removed)"
            )
        elif folded != chunk:
            notes.append(f"{chunk}→{folded}")
        tokens.extend(kept)
    return tokens, tuple(notes)


def _tokenise(query: str) -> list[str]:
    """Split caller text into title-search tokens. Never raises; may return []."""
    return _prepare(query)[0]


def _q_term(term: str) -> str:
    """Render one token for the simplified query syntax.

    A hyphen inside an *unquoted* term is read as OR, not as part of the word.
    Live-verified 2026-08-22 against api.uspto.gov:

        inventionTitle:(LOX-1)    -> count 34,680  (= LOX 111 + "1" 34,595 - 26 both)
        inventionTitle:("LOX-1")  -> count 26      (= LOX AND 1, exactly)

    So keeping the hyphen without quoting it would be strictly worse than the bug
    it fixes: the most specific token in the query would become the least specific
    clause in it, and ``sort: filingDate desc`` + ``limit: 10`` would hand the hub
    ten unrelated recent filings as prior art. Only hyphenated tokens are quoted,
    so every other query is byte-identical to what it was before.
    """
    return f'"{term}"' if "-" in term else term


@dataclass(frozen=True)
class PriorArtResult:
    """A completed title search. ``None`` from search_prior_art still means the
    search could not run at all — see that function's contract.

    ``terms_used`` is the breadth that produced ``hits``: when it is shorter than
    ``total_terms`` the query was broadened because the full phrase matched nothing,
    and the caller MUST say so rather than presenting the result as on-point.

    ``total_terms`` is the number of TOKENS the search was built from, and stays
    that. It is deliberately NOT the caller's whitespace-split word count:
    ``TFEB / TFE3 fusion`` splits to 4 against 3 tokens, so a whitespace-derived
    total would make tier 1 — the query exactly as asked — report
    ``broadened=True`` and claim "your full phrase matched no title" about a
    search that was never broadened. What the caller's text lost on the way to
    those tokens is reported separately, by ``dropped_or_rewritten``.

    ``dropped_or_rewritten`` names every piece of the caller's text that had to
    change before it could be sent (see ``_prepare``): a Greek letter spelled
    out, a Unicode dash folded, a boolean operator dropped. Empty on the ordinary
    query. The caller MUST surface it — the model judges the hits against the
    phrase it thinks it searched.

    ``total_count`` is ODP's own ``count`` for the tier that produced ``hits`` —
    how many filings matched, as opposed to how many were returned — or ``None``
    when the response carried no count. ``hits`` is only ever a page (``limit``,
    default 10, most-recently-filed first), so a caller that reports it without
    ``truncation_note`` is disclaiming the search's SCOPE while silently implying
    its COMPLETENESS. That was H2 in run 8b64a0e0: 49 of 109 broadened searches
    returned exactly 10, of match sets as large as 27,906, and nothing in the
    output distinguished "10 of 10" from "10 of 27,906".
    """

    hits: list[dict[str, Any]]
    terms_used: list[str]
    total_terms: int
    total_count: int | None = None
    dropped_or_rewritten: tuple[str, ...] = ()

    @property
    def broadened(self) -> bool:
        return len(self.terms_used) < self.total_terms

    @property
    def truncated(self) -> bool:
        """More filings matched than were returned.

        Derived from ``total_count`` rather than from ``len(hits) == limit``, so an
        exactly-full page of an exactly-``limit``-sized match set is not accused of
        hiding anything, and an unknown count is never *asserted* to be complete.
        """
        return self.total_count is not None and self.total_count > len(self.hits)

    @property
    def truncation_note(self) -> str:
        """One sentence disclosing incompleteness, or "" when there is none.

        Lives here rather than in the caller so the number and the sentence about
        it cannot drift apart. ``src/agent/tools.py::_scope_note`` is the intended
        consumer, and it owns the SPACING: this used to end in ``\\n\\n``, which
        that caller interpolated straight after a full stop and produced
        "...for TFEB AND melanoma.COMPLETENESS: showing..." plus a trailing
        ``\\n\\n\\n\\n``. A disclosure that reads as a typo is one a model can
        discount.
        """
        if not self.truncated:
            return ""
        return (
            f"COMPLETENESS: showing the {len(self.hits)} most-recently-filed of "
            f"{self.total_count} filings matching at this breadth. The absence of a "
            f"close match below is NOT evidence that there isn't one."
        )


# Domain-generic words. A title search ANDing these in is what made every
# production query return zero: "inhibitor", "treatment" and "disease" are in
# almost no patent TITLE even when the patent is squarely on point.
_GENERIC = frozenset({
    "a", "an", "and", "for", "from", "in", "of", "on", "or", "the", "to", "via", "with",
    "using", "use", "uses", "based", "novel", "new", "improved", "method", "methods",
    "system", "systems", "approach", "approaches", "treatment", "treating", "therapy",
    "therapeutic", "therapeutics", "disease", "diseases", "disorder", "disorders",
    "patient", "patients", "human", "clinical", "cell", "cells", "protein", "proteins",
    "inhibitor", "inhibitors", "inhibiting", "inhibition", "modulator", "modulators",
    "agent", "agents", "targeting", "target", "targets", "assay", "assays", "platform",
    "expression", "activity", "function",
})

# Floor on how narrow a *multi*-term backoff goes before the final single-term
# tier (see _tiers). Two ANDed specific terms is still worth trying on its own
# before dropping to one, since a pairing of two real signal terms is more
# specific than either alone when both survive generics-filtering.
_MIN_TERMS = 2


def _salience(token: str) -> tuple[int, int, str]:
    """Rank key: gene/target symbols beat prose. Deterministic (ties break on the
    token itself) so the query sent to USPTO is reproducible across runs.

    An **all-digit** token earns neither symbol bonus. It used to earn both — root
    cause half 2 of run 8b64a0e0's H3: a pure number contains a digit (+3) *and*
    ``"1".islower()`` is ``False`` (+2), so it scored 5 before the length bonus,
    against GDF=3 and LOX=3. Every SYMBOL-N query therefore backed off to its own
    numeric suffix. Note this is a bonus the *number* loses, not a bonus digits
    lose: the +3 is what promotes C9orf72 / MARK2 / PE38 / HER3, and it stays.

    Deliberately NOT fixed by adding numbers to ``_GENERIC``: ``_rank_terms`` is
    ``pool = specific or tokens``, so a blocklist wide enough to empty the specific
    pool falls back to the *unfiltered* phrase — the guaranteed-zero-hit bug the
    whole backoff exists to prevent.
    """
    score = 0
    if not token.isdigit():
        if any(ch.isdigit() for ch in token):
            score += 3  # C9orf72, MARK2, PE38, HER3, LOX-1
        if token.isupper() and len(token) >= 2:
            score += 3  # TFEB, BRAF, ALS
        elif not token.islower():
            score += 2  # MiP, mCherry
    score += min(len(token) // 4, 2)
    return (score, len(token), token)


def _rank_terms(tokens: list[str]) -> list[str]:
    specific = [t for t in tokens if t.lower() not in _GENERIC]
    pool = specific or tokens  # an all-generic query still gets searched
    return sorted(pool, key=_salience, reverse=True)


def _tiers(tokens: list[str]) -> list[list[str]]:
    """Breadths to try, widest first, at most four HTTP calls (the ODP
    rate-limits aggressively; a 429 is now paced against, then retried, and only
    then costs us the whole search — see ``_pace`` and ``_search_titles``).

    Tier 1 is the query EXACTLY as asked, in the caller's own order: that is
    the precise search, and preserving it means the backoff only ever widens.
    Later tiers drop generic words and keep the most specific terms, ending at
    a single term: measured against the live USPTO API, a lone specific term
    (TFEB, BRAF, C9orf72) reliably returns hits where a 2-term floor returned
    zero for exactly the queries this backoff exists to fix. A long query can
    now cost up to four calls, paced apart; a 429 that outlives its retries on
    any of them still returns ``None`` (see search_prior_art) rather than a
    clean-looking empty result.

    Widths are gated on the candidate being a new term *set*, NOT on the input
    token count — gating on ``width >= len(tokens)`` (the pre-fix behaviour)
    skipped every backoff tier for a query of 2 tokens or fewer, which is
    exactly the breadth the prompt now asks the model to use.

    The set (rather than list) comparison is M13: ODP's AND is commutative —
    live-verified, ``deoxyhypusine AND synthase`` and ``synthase AND
    deoxyhypusine`` both return 27 — but salience ranking reorders a short query,
    and the old list comparison read the permutation as a new tier. That was 17
    provably-wasted POSTs in run 8b64a0e0, each of which also brought the ladder
    one step closer to the 429 that eventually killed it.

    ``_MIN_TERMS`` is a floor on how narrow a *multi*-term backoff goes before
    the final single-term tier below it, not a minimum on how much specific
    signal a tier must contain: when the specific pool is narrower than a
    given width (e.g. only one gene symbol survives among several generic
    words), that narrower pool is still used, in full, as its own tier.
    Skipping it there would silently reproduce the guaranteed zero-hit bug
    this backoff exists to fix — a single specific term is more informative
    than the full generic-laden phrase it's paired with.
    """
    ranked = _rank_terms(tokens)
    tiers = [list(tokens)]
    seen = {frozenset(tokens)}
    for width in (3, _MIN_TERMS, 1):
        candidate = ranked[:width] if width <= len(ranked) else list(ranked)
        key = frozenset(candidate)
        if key not in seen:
            seen.add(key)
            tiers.append(candidate)
    return tiers


# Rate limiting. Every one of run 8b64a0e0's 10 429s landed on the *third* POST of
# its own tier ladder, issued back-to-back with no spacing at all — so the burst was
# ours, and pacing matters more here than retrying (the same lesson as
# pubmed._ncbi_get, where `await _pace()` before every attempt is the load-bearing
# half). 1.0s spacing = 60 requests/minute, which is the limit USPTO documents for
# the sibling TSDR API; ODP's own rate-limit page is JS-rendered and could not be
# read, so this is the closest published number plus the observation that three
# back-to-back POSTs on one key were enough to trip it. A whole four-tier ladder
# costs 3s of spacing, against consults that take 25-40s.
_PACE_INTERVAL = 1.0
_next_slot: float = 0.0

# 429 retry. Transient: one of the run's 429s succeeded 41ms later on the same key.
_ODP_ATTEMPTS = 3
_ODP_BACKOFF = 1.0


async def _pace() -> None:
    """Space ODP request starts at least ``_PACE_INTERVAL`` apart, process-wide.

    Lifted from ``pubmed._pace``, including the reason it holds no lock: the
    read-modify-write of ``_next_slot`` has no ``await`` between the read and the
    write, so it is atomic on the event loop, and a module-level asyncio primitive
    would bind to the first event loop that touched it (which breaks under
    pytest's per-test loops).
    """
    global _next_slot
    loop = asyncio.get_running_loop()
    now = loop.time()
    wait = _next_slot - now
    _next_slot = max(now, _next_slot) + _PACE_INTERVAL
    if wait > 0:
        await asyncio.sleep(wait)


# Full-text (abstract + first claim) enrichment. Each pre-grant-publication XML is
# ~1 MB, so we only fetch it for the top few hits and cap the extracted text to keep
# the Phase-4 prompt lean. Best-effort: a failed fetch leaves the hit title-level.
_FULLTEXT_MAX = 5
_ABSTRACT_LEN = 1200
_CLAIM_LEN = 600
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ABSTRACT_RE = re.compile(r"<abstract\b[^>]*>(.*?)</abstract>", re.DOTALL | re.IGNORECASE)
_CLAIM_RE = re.compile(r"<claim\b[^>]*>(.*?)</claim>", re.DOTALL | re.IGNORECASE)


def _api_key() -> str:
    s = get_settings()
    return s.uspto_api_key or s.patentsview_api_key


def _plaintext(fragment: str, limit: int) -> str:
    # Strip XML tags, then decode entities (the pgpub XML uses &#x3e; etc.).
    return html.unescape(_WS.sub(" ", _TAG.sub(" ", fragment)).strip())[:limit]


async def _fetch_fulltext(client: httpx.AsyncClient, uri: str) -> tuple[str, str]:
    """Fetch a pre-grant-publication XML and extract (abstract, first claim).

    Best-effort: returns ("", "") on any failure. The fileLocationURI 302-redirects
    to a signed data.uspto.gov download, so the client must follow redirects.

    Paced like the search itself: enrichment was 57% of run 8b64a0e0's USPTO
    request budget (493 of 865), so pacing only the searches would leave the
    majority of our traffic unpaced.
    """
    try:
        await _pace()
        r = await client.get(uri, headers={"X-API-KEY": _api_key()})
        r.raise_for_status()
        xml = r.text
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("[patents] full-text fetch failed for %s: %s", uri, exc)
        return "", ""
    abstract = ""
    m = _ABSTRACT_RE.search(xml)
    if m:
        abstract = _plaintext(m.group(1), _ABSTRACT_LEN)
    claim = ""
    cm = _CLAIM_RE.search(xml)
    if cm:
        claim = _plaintext(cm.group(1), _CLAIM_LEN)
    return abstract, claim


def _prior_art_class(status: str, *, published: bool) -> str:
    """How much evidentiary weight one ODP filing carries as prior art.

    Of the 624 hit rows shown to the hub in run 8b64a0e0, only 12% were granted
    patents, **17.4% were expired provisionals** — never published, and so not
    prior art at all — and 30% were unexamined 2026 filings; all three rendered
    identically. Rows are LABELLED, not filtered: a published pending application
    *is* prior art under 35 USC 102(a)(2), and an abandoned one stays prior art as
    of its publication date.

    ``published`` comes from the presence of a publication number/date, which is
    what makes the "not yet published" answer sound rather than inferred from
    status text alone.
    """
    s = (status or "").lower()
    if "provisional" in s:
        # A provisional is never published and never examined. It can support a
        # later filing's priority date, but on its own it discloses nothing.
        return "unpublished provisional — NOT prior art (provisionals are never published)"
    if "patented" in s:
        return "granted patent — prior art"
    if not published:
        return "filed but not yet published — not yet prior art"
    if "abandoned" in s or "expired" in s:
        return "published application, since abandoned — prior art as of its publication date"
    # Deliberately silent about examination: ODP's status vocabulary distinguishes
    # "Docketed New Case" from "Non Final Action Mailed", and publication — not
    # examination — is what makes an application prior art.
    return "published pending application — prior art under 35 USC 102(a)(2)"


async def _search_titles(
    client: httpx.AsyncClient, terms: list[str], limit: int, key: str
) -> tuple[list[dict[str, Any]], int | None] | None:
    """One title search, paced and retried. ``None`` == rate limited even after
    retries (caller must treat as unavailable); ``([], n)`` == searched, matched
    nothing. The second element is ODP's own ``count`` — how many filings matched,
    not how many were returned — or ``None`` when the response carried no count.
    Each hit carries ``_pgpub_uri`` for the optional full-text enrichment, which
    the caller pops before returning.

    ``limit`` is deliberately capped rather than raised: the largest match sets are
    five figures (27,906 for a title search on "resistance"), which is not pageable
    at any limit, and every extra hit multiplies the enrichment traffic that is
    already the majority of our USPTO budget. Incompleteness is disclosed instead —
    see ``PriorArtResult.truncation_note``.
    """
    anded = " AND ".join(_q_term(t) for t in terms)
    body = {
        "q": f"applicationMetaData.inventionTitle:({anded})",
        "pagination": {"offset": 0, "limit": max(1, min(limit, 50))},
        "sort": [{"field": "applicationMetaData.filingDate", "order": "desc"}],
    }
    for attempt in range(_ODP_ATTEMPTS):
        await _pace()
        resp = await client.post(SEARCH_URL, json=body, headers={"X-API-KEY": key})
        if resp.status_code != 429:
            break
        if attempt < _ODP_ATTEMPTS - 1:
            logger.info("[patents] 429 on attempt %d — backing off", attempt + 1)
            await asyncio.sleep(_ODP_BACKOFF * (2 ** attempt))
    if resp.status_code == 429:
        logger.warning(
            "[patents] rate limited (429) after %d attempts — treating as unavailable",
            _ODP_ATTEMPTS,
        )
        return None
    # ODP answers 404 (not 200-with-empty) when a valid search matches nothing.
    if resp.status_code == 404:
        return [], 0
    resp.raise_for_status()
    data = resp.json()

    hits: list[dict[str, Any]] = []
    for entry in data.get("patentFileWrapperDataBag", []) or []:
        meta = entry.get("applicationMetaData", {}) or {}
        pub_number = meta.get("earliestPublicationNumber")
        pub_date = meta.get("earliestPublicationDate")
        raw_status = meta.get("applicationStatusDescriptionText", "")
        prior_art = _prior_art_class(raw_status, published=bool(pub_number or pub_date))
        hits.append({
            "patent_id": pub_number or entry.get("applicationNumberText") or "",
            "title": meta.get("inventionTitle", ""),
            "date": pub_date or meta.get("filingDate", ""),
            "applicant": meta.get("firstApplicantName", ""),
            "inventor": meta.get("firstInventorName", ""),
            # `status` carries BOTH because it is the only one of the two that
            # src/agent/tools.py renders into the <patent> block the model reads,
            # and ODP's own wording is worth keeping alongside the classification.
            # `prior_art_status` is the bare machine-readable class.
            "status": f"{raw_status} [{prior_art}]" if raw_status else prior_art,
            "prior_art_status": prior_art,
            "abstract": "",
            "claim": "",
            "_pgpub_uri": (entry.get("pgpubDocumentMetaData") or {}).get("fileLocationURI"),
        })
    count = data.get("count")
    return hits, (count if isinstance(count, int) else None)


async def _enrich(client: httpx.AsyncClient, hits: list[dict[str, Any]]) -> None:
    """Add abstract + first claim to the top few published hits, in place. Bounded
    and best-effort so a slow or missing XML never fails the search.

    ``_pgpub_uri`` is internal bookkeeping (which pre-grant XML to fetch) and must
    never survive into a returned hit — it is popped unconditionally here, on every
    hit, even past ``_FULLTEXT_MAX`` where it is never fetched.
    """
    for i, hit in enumerate(hits):
        uri = hit.pop("_pgpub_uri", None)
        if uri and i < _FULLTEXT_MAX:
            hit["abstract"], hit["claim"] = await _fetch_fulltext(client, uri)


# Per-run result memo. 109 searches in run 8b64a0e0 resolved to only 91 distinct
# term-sets, so 18 of them (16.5%) re-ran the whole tier ladder AND re-paid the
# full-text enrichment behind it — about 24% of the run's USPTO request budget, and
# 18 more chances at the rate limit. Keyed on the SORTED token multiset, because
# ODP's AND is commutative (live-verified: `deoxyhypusine AND synthase` ==
# `synthase AND deoxyhypusine` == 27), so a permutation is the same search; the
# only thing that differs is the term order reported back, which is the order the
# search that actually ran used.
#
# Only successful results are stored — never a ``None``. "Could not search" is not
# a result, and a rate limit is transient: caching one would poison the rest of the
# run with an answer that reads as novelty.
_CACHE: dict[tuple[Any, ...], PriorArtResult] = {}
_CACHE_MAX = 512


def clear_prior_art_cache() -> None:
    """Drop the memo. The agent process is one run, so nothing calls this in
    production; tests do, to keep one case's HTTP mocks out of the next one's."""
    _CACHE.clear()


def _memo_copy(
    result: PriorArtResult, dropped_or_rewritten: tuple[str, ...] | None = None
) -> PriorArtResult:
    """Hand out a copy. ``hits`` is a list of MUTABLE dicts and a cached result is
    shared by every later caller in the run, so returning the stored object would
    let one caller's edit become another's input.

    ``dropped_or_rewritten`` is overridable because it describes THIS caller's
    text, not the cached search: the memo is keyed on the token multiset, and
    ``Qβ malaria`` and ``Qbeta malaria`` produce the same tokens from different
    words. Serving the stored note to the second caller would tell it its query
    was rewritten when it was not.
    """
    return PriorArtResult(
        hits=[dict(h) for h in result.hits],
        terms_used=list(result.terms_used),
        total_terms=result.total_terms,
        total_count=result.total_count,
        dropped_or_rewritten=(
            result.dropped_or_rewritten
            if dropped_or_rewritten is None
            else dropped_or_rewritten
        ),
    )


async def search_prior_art(query: str, limit: int = 10) -> PriorArtResult | None:
    """Search US patent filings (USPTO ODP) by invention title. Never raises.

    Tries the full phrase first, then progressively fewer, more specific terms
    (see ``_tiers``). This matters more than it looks: a free-text query ANDed on
    the title is a guaranteed zero-hit — measured 12/12 in production before the
    backoff existed — and a scouting hub reads a zero-hit as novelty.

    - ``None``             — the search could NOT be performed: no API key, or the
      endpoint was unreachable / errored / rate-limited / returned unparseable JSON.
    - ``PriorArtResult``   — the search ran. ``.hits`` may be empty (genuinely no
      title match at the narrowest breadth tried). ``.broadened`` tells the caller
      the query was widened and the hits may be adjacent rather than on point,
      ``.truncation_note`` tells it whether ``.hits`` is the whole match set or
      just the most-recently-filed page of it, and ``.dropped_or_rewritten``
      tells it which of its own words are not what was actually searched.

    Every ODP request is paced (``_pace``) and repeated queries are served from a
    per-run memo (``_CACHE``), so the request count is bounded by the number of
    *distinct* searches a run performs rather than by the number of tool calls.
    """
    key = _api_key()
    if not key:
        logger.info("[patents] no USPTO API key configured — cannot search")
        return None
    tokens, rewrites = _prepare(query)
    if not tokens:
        return PriorArtResult(
            hits=[], terms_used=[], total_terms=0, dropped_or_rewritten=rewrites
        )

    cache_key = (limit, tuple(sorted(tokens)))
    cached = _CACHE.get(cache_key)
    if cached is not None:
        logger.info("[patents] cache hit for %s (%d hits)", sorted(tokens), len(cached.hits))
        return _memo_copy(cached, rewrites)

    tiers = _tiers(tokens)
    hits: list[dict[str, Any]] = []
    terms_used = tiers[-1]
    total_count: int | None = None
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for terms in tiers:
                attempt = await _search_titles(client, terms, limit, key)
                if attempt is None:
                    return None
                total_count = attempt[1]
                if attempt[0]:
                    hits, terms_used = attempt[0], terms
                    break
            if hits:
                await _enrich(client, hits)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("[patents] search unavailable (endpoint unreachable or error): %s", exc)
        return None

    if len(terms_used) < len(tokens):
        logger.info(
            "[patents] broadened %d terms -> %s (%d hits)",
            len(tokens), terms_used, len(hits),
        )
    result = PriorArtResult(
        hits=hits,
        terms_used=list(terms_used),
        total_terms=len(tokens),
        total_count=total_count,
        dropped_or_rewritten=rewrites,
    )
    if len(_CACHE) < _CACHE_MAX:
        _CACHE[cache_key] = result
    return _memo_copy(result)
