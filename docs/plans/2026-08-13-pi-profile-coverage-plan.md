# PI Profile Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every PI profile grounded in the 50 most recent PubMed-indexed papers that PI actually published, and make it impossible for a new PI to reach `status='active'` without that being true.

**Architecture:** Introduce one corpus resolver (`src/services/corpus.py`) that unions ORCID, OpenAlex, and two PubMed searches, disambiguates the name-search hits **inside the resolver** (mandatory for every caller; skipped-source, never skipped-filter), dates any still-undated PMIDs via esummary, then dedupes, ranks by date, and caps at 50 — in that order. Both existing ingestion paths (`run_profile_pipeline` for new PIs, `generate_sparsedata_user.py` for bulk seeding) delegate to it, replacing two divergent, differently-broken retrieval implementations. A **persisted** coverage flag (new `researcher_profiles.coverage_suspect` column, migration 0026) and the existing-but-unwired `ResearcherProfile.evidence_state` become an activation gate covering **both** status-write branches of `admin_approve_agent`.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, httpx, pytest, alembic, Typer CLI. External APIs (all unauthenticated): ORCID v3.0, OpenAlex, NCBI E-utilities.

**Spec:** `docs/specs/2026-08-13-pi-profile-coverage-design.md`

**Revised:** 2026-08-13 after an adversarial audit; the revision closes the audit's
findings: in-resolver disambiguation (no unfiltered S4, no name-only S4), a real
characterization-test seam for Task 5, persisted+gated `coverage_suspect`, year-dating
before ranking (replaces a mitigation that could not work), kept-row re-fetch in the
backfill (repairs the 112 truncated titles), Tier A demoted before any apply, gate on both
activation branches with a `pi_lab` scope, CI-gate fixes (imports at top of file, test
lint), and a deploy-before-verify ordering fix.

## Global Constraints

- **The 50-publication cap is retained everywhere.** `PUBMED_FETCH_CAP = 50`. No PI ends with more than 50 stored publications. The cap is applied **last**, after ranking — never as a retrieval limit.
- **`retmax` on author searches is 200**, so ranking has a real corpus to work with. This is not a change to the cap.
- Ranking order is **publication year DESC, then PMID DESC** (numeric). Works with no year sort last.
- **PubMed `efetch` is the only source of stored content** (title, abstract, journal, year). OpenAlex and ORCID are discovery/estimation only.
- **Do not touch `reconcile_pub_doi`** (`src/services/profile_pipeline.py:196-210`) — it guards a prior incident.
- **Do not tighten disambiguation.** The audit found zero false attributions; the measured risk is under-retrieval.
- Tests run on the host: `.venv-test/bin/python -m pytest tests/ -v`. Full gate before commit: `./scripts/ci.sh`.
- `ruff check` must report zero findings on `tests/`, and must not raise the ratcheted ceiling on `src/`. **The ratchet has one finding of headroom (259 findings against a ceiling of 260, measured 2026-08-13), so treat it as zero-new-findings.** In particular: E402 is enabled — every `import` in `corpus.py` goes at the **top of the file**, regardless of which task adds it; F401 and B015 are enabled and `tests/` is held to zero.
- Never run `pytest --snapshot-update` against `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` — those strings are pinned and must not change. The **profile-pipeline** golden master is different: Task 5 legitimately changes the pipeline's observable behaviour, so its snapshot is regenerated *deliberately* in Task 5 step 5, scoped to that one file, with the diff reviewed hunk by hunk before commit.
- **Code changes require an agent-image rebuild + restart to take effect** (`$DC --profile agent build agent`). Flag this to the user; do not restart the running simulation unasked.

---

### Task 1: Stop truncating titles and abstracts at inline markup

Closes D6. Must land before any backfill, or refetched titles arrive damaged.

**Files:**
- Modify: `src/services/pubmed.py:207-218`
- Test: `tests/unit/test_pubmed_parsing.py` (create)

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `_parse_pubmed_xml(xml_text: str) -> list[dict]` — unchanged signature; `record["title"]` and `record["abstract"]` now retain text that follows inline elements.

- [ ] **Step 1: Write the failing test**

```python
"""Titles and abstracts must survive inline markup (<i>, <sup>, <b>).

PubMed italicises species binomials. Element.text returns only the character
data before the first child element, so "Batch Rearing <i>Anopheles</i>"
was stored as "Batch Rearing ". 112 of 1835 stored titles were truncated.
"""

from src.services.pubmed import _parse_pubmed_xml

_XML_WITH_MARKUP = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID>36223988</PMID>
   <Article>
    <ArticleTitle>Batch Rearing <i>Anopheles gambiae</i> for Behavioral Assays.</ArticleTitle>
    <Journal><Title>Cold Spring Harbor protocols</Title></Journal>
    <Abstract>
     <AbstractText>We rear <i>An. gambiae</i> at 27<sup>o</sup>C throughout.</AbstractText>
    </Abstract>
   </Article>
  </MedlineCitation>
 </PubmedArticle>
</PubmedArticleSet>"""


def test_title_retains_text_after_inline_markup():
    (record,) = _parse_pubmed_xml(_XML_WITH_MARKUP)
    assert record["title"] == "Batch Rearing Anopheles gambiae for Behavioral Assays."


def test_abstract_retains_text_after_inline_markup():
    (record,) = _parse_pubmed_xml(_XML_WITH_MARKUP)
    assert record["abstract"] == "We rear An. gambiae at 27oC throughout."


def test_labelled_abstract_sections_still_prefixed():
    xml = _XML_WITH_MARKUP.replace(
        "<AbstractText>", '<AbstractText Label="METHODS">'
    )
    (record,) = _parse_pubmed_xml(xml)
    assert record["abstract"].startswith("METHODS: We rear An. gambiae")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_pubmed_parsing.py -v`
Expected: FAIL — `assert 'Batch Rearing ' == 'Batch Rearing Anopheles gambiae for Behavioral Assays.'`

- [ ] **Step 3: Write the implementation**

In `src/services/pubmed.py`, add this helper next to `_parse_pubmed_xml`:

```python
def _element_text(el) -> str:
    """Full text of an element, flattening inline markup.

    PubMed <ArticleTitle> and <AbstractText> contain <i>/<sup>/<b> for species
    names and formulae. Element.text stops at the first child, silently
    truncating the field, so itertext() is the only correct read.
    """
    if el is None:
        return ""
    return "".join(el.itertext()).strip()
```

Replace line 207-208:

```python
        # Title
        title_el = article.find(".//ArticleTitle")
        record["title"] = _element_text(title_el)
```

Replace the abstract loop at 210-218:

```python
        # Abstract
        abstract_parts = []
        for abstract_el in article.findall(".//AbstractText"):
            label = abstract_el.get("Label")
            text = _element_text(abstract_el)
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        record["abstract"] = " ".join(abstract_parts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_pubmed_parsing.py tests/unit/test_doi_validation.py -v`
Expected: PASS (including the pre-existing DOI tests, which parse the same XML).

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_pubmed_parsing.py src/services/pubmed.py
git commit -m "fix(pubmed): flatten inline markup instead of truncating titles and abstracts"
```

---

### Task 2: Pure corpus core — normalise, dedupe, rank, cap

Closes D3 and D7. Pure functions, no I/O, so they are exhaustively testable.

**Files:**
- Create: `src/services/corpus.py`
- Test: `tests/unit/test_corpus_ranking.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `@dataclass CorpusCandidate` with fields `pmid: str`, `year: int | None`, `title: str`, `source: str`
  - `normalize_title(title: str) -> str`
  - `merge_and_rank(candidates: list[CorpusCandidate], cap: int = 50) -> list[CorpusCandidate]`

- [ ] **Step 1: Write the failing test**

```python
"""The cap is applied LAST, to a deduped, date-ranked corpus.

Regression guard for two production defects:
  - generate_sparsedata_user.py:702 sliced ORCID's group order, so SalzbergBot
    held 50 papers spanning 2009-2013 while its PI published through 2026.
  - Dedup was keyed on PMID alone, so preprint/journal pairs each took a slot.
"""

from src.services.corpus import CorpusCandidate, merge_and_rank, normalize_title


def _c(pmid, year, title="t", source="orcid"):
    return CorpusCandidate(pmid=pmid, year=year, title=title, source=source)


def test_keeps_the_most_recent_when_over_cap():
    cands = [_c(str(i), 1990 + i, title=f"paper {i}") for i in range(60)]
    kept = merge_and_rank(cands, cap=50)
    assert len(kept) == 50
    assert kept[0].year == 2049
    assert min(c.year for c in kept) == 2000


def test_ranks_year_desc_then_pmid_desc():
    kept = merge_and_rank(
        [_c("100", 2020, "a"), _c("300", 2024, "b"), _c("200", 2024, "c")], cap=50
    )
    assert [c.pmid for c in kept] == ["300", "200", "100"]


def test_undated_works_sort_last_and_are_not_dropped():
    kept = merge_and_rank([_c("1", None, "a"), _c("2", 1999, "b")], cap=50)
    assert [c.pmid for c in kept] == ["2", "1"]


def test_dedupes_by_pmid_across_sources():
    kept = merge_and_rank(
        [_c("55", 2024, "same", "orcid"), _c("55", 2024, "same", "openalex")], cap=50
    )
    assert len(kept) == 1


def test_dedupes_preprint_and_journal_by_normalised_title_keeping_later():
    kept = merge_and_rank(
        [
            _c("36994162", 2023, "Preserving Derivative Information While Transforming Neuronal Curves."),
            _c("38036915", 2024, "Preserving derivative information while transforming neuronal curves"),
        ],
        cap=50,
    )
    assert [c.pmid for c in kept] == ["38036915"]


def test_erratum_is_not_collapsed_into_its_article():
    kept = merge_and_rank(
        [
            _c("1", 2023, "A study of things"),
            _c("2", 2024, "Erratum: A study of things"),
        ],
        cap=50,
    )
    assert len(kept) == 2


def test_normalize_title_ignores_case_punctuation_and_spacing():
    assert normalize_title("A Study, of  Things.") == normalize_title("a study of things")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_ranking.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.corpus'`

- [ ] **Step 3: Write the implementation**

Create `src/services/corpus.py`:

```python
"""Corpus resolution: gather from every source, then dedupe, rank, and cap.

The ordering is the whole design. Both historical ingestion paths capped or
short-circuited *during* retrieval, which is what let a PI with 786 indexed
papers be stored with 20, and let another be stored with 50 papers that all
predate 2014. Gathering never stops early; the cap is a presentation decision
applied to a complete, ranked corpus.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_CAP = 50

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class CorpusCandidate:
    """One candidate work, from one source. `pmid` is the identity."""

    pmid: str
    year: int | None
    title: str
    source: str


def normalize_title(title: str) -> str:
    """Casefold + strip punctuation so preprint/journal pairs collide.

    Deliberately does NOT strip "Erratum:"/"Correction to:" prefixes — doing so
    would collapse an erratum into its article and, because the erratum is the
    later of the two, would keep the erratum and discard the real paper.
    """
    lowered = _PUNCT.sub(" ", (title or "").lower())
    return _WS.sub(" ", lowered).strip()


def merge_and_rank(
    candidates: list[CorpusCandidate], cap: int = DEFAULT_CAP
) -> list[CorpusCandidate]:
    """Dedupe by PMID then by normalised title, rank by date, then cap."""
    by_pmid: dict[str, CorpusCandidate] = {}
    for c in candidates:
        existing = by_pmid.get(c.pmid)
        if existing is None or (existing.year is None and c.year is not None):
            by_pmid[c.pmid] = c

    by_title: dict[str, CorpusCandidate] = {}
    for c in by_pmid.values():
        key = normalize_title(c.title)
        if not key:
            by_title[f"__pmid__{c.pmid}"] = c
            continue
        prior = by_title.get(key)
        if prior is None or _rank_key(c) > _rank_key(prior):
            by_title[key] = c

    ranked = sorted(by_title.values(), key=_rank_key, reverse=True)
    if len(ranked) > cap:
        logger.info("Corpus capped: %d candidates -> %d kept", len(ranked), cap)
    return ranked[:cap]


def _rank_key(c: CorpusCandidate) -> tuple[int, int]:
    """Year DESC, then PMID DESC. Undated works sort last."""
    year = c.year if c.year is not None else -1
    try:
        pmid = int(c.pmid)
    except (TypeError, ValueError):
        pmid = -1
    return (year, pmid)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_ranking.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/services/corpus.py tests/unit/test_corpus_ranking.py
git commit -m "feat(corpus): dedupe, date-rank, then cap — cap applied last"
```

---

### Task 3: Source clients — OpenAlex, the two PubMed author searches, and the disambiguator

Adds the retrieval the pipeline has never had (D2), the independent estimate P2 needs, and
**moves the S4 disambiguator into the resolver's module** so no caller can skip it (§4.3).

**Files:**
- Modify: `src/services/corpus.py`, `scripts/generate_sparsedata_user.py` (import the moved
  disambiguator back; delete the local copy)
- Test: `tests/unit/test_corpus_sources.py` (create)

**Interfaces:**
- Consumes: `CorpusCandidate` from Task 2.
- Produces:
  - `build_author_query(name: str, affiliations: list[str]) -> str`
  - `async fetch_openalex_by_orcid(orcid: str) -> list[CorpusCandidate]`
  - `async fetch_pubmed_by_orcid(orcid: str) -> list[str]` (PMIDs)
  - `async fetch_pubmed_by_author(name: str, affiliations: list[str], retmax: int = 200) -> list[str]` (PMIDs)
  - `parse_openalex_works(payload: dict) -> list[CorpusCandidate]` (pure — this is what the test drives)
  - `async disambiguate_pmids(pmids: list[str], name: str, affiliations: list[str]) -> list[str]`
    — a **byte-compatible port** of the seeder's `_disambiguate` (with
    `_author_first_name_matches`, `_author_affiliations_from_xml`, and
    `INSTITUTION_STOPWORDS`, `scripts/generate_sparsedata_user.py:329` and `:76-90`),
    moved so both ingestion paths share it. The seeder then imports these names from
    `src.services.corpus` instead of defining them; behaviour is identical (do **not**
    tighten the matcher — spec §6). Port the logic, don't rewrite it.

- [ ] **Step 1: Write the failing test**

```python
"""Source clients. The pure parser and query builder are tested directly; the
HTTP wrappers are thin and covered by tests/live_api.
"""

import pytest

from src.services.corpus import build_author_query, parse_openalex_works

_OPENALEX_PAGE = {
    "results": [
        {
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/38036915"},
            "publication_year": 2024,
            "title": "Preserving derivative information",
        },
        {   # no PMID -> not PubMed-indexed -> must be skipped
            "ids": {"doi": "https://doi.org/10.1101/2024.01.01.000000"},
            "publication_year": 2024,
            "title": "A bioRxiv preprint",
        },
        {   # null title must not crash the parser
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/12345"},
            "publication_year": None,
            "title": None,
        },
    ],
    "meta": {"next_cursor": None},
}


def test_parse_openalex_keeps_only_pubmed_indexed_works():
    cands = parse_openalex_works(_OPENALEX_PAGE)
    assert [c.pmid for c in cands] == ["38036915", "12345"]


def test_parse_openalex_strips_the_pmid_url_prefix():
    (first, _) = parse_openalex_works(_OPENALEX_PAGE)
    assert first.pmid == "38036915"
    assert first.year == 2024
    assert first.source == "openalex"


def test_parse_openalex_tolerates_null_title_and_year():
    last = parse_openalex_works(_OPENALEX_PAGE)[-1]
    assert last.title == ""
    assert last.year is None


@pytest.mark.parametrize(
    "name,affs,expected",
    [
        ("Carl Wu", ["Johns Hopkins University"],
         '(Wu Carl[Author]) AND ("Johns Hopkins University"[Affiliation])'),
        ("Carl Wu", [], "Wu Carl[Author]"),
        ("C. Wu", ["Johns Hopkins University"],
         '(Wu C[Author]) AND ("Johns Hopkins University"[Affiliation])'),
    ],
)
def test_build_author_query(name, affs, expected):
    assert build_author_query(name, affs) == expected


def test_build_author_query_uses_at_most_two_affiliations():
    q = build_author_query("Carl Wu", ["A", "B", "C"])
    assert '"C"[Affiliation]' not in q
    assert '"A"[Affiliation] OR "B"[Affiliation]' in q
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_sources.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_author_query'`

- [ ] **Step 3: Write the implementation**

Add to `src/services/corpus.py`. **The imports go at the top of the file with Task 2's
imports** — E402 is enabled and the src/ ratchet has no headroom (Global Constraints):

```python
# top of file, merged into the existing import block:
import httpx

from src.services.pubmed import _ncbi_get, EUTILS_BASE
```

Then the module-level code:

```python
OPENALEX_BASE = "https://api.openalex.org"
OPENALEX_MAILTO = "blackbird@copi.science"
AUTHOR_SEARCH_RETMAX = 200


def build_author_query(name: str, affiliations: list[str]) -> str:
    """PubMed ESearch term for a PI. Mirrors the seeder's proven form."""
    parts = name.replace(".", " ").split()
    if not parts:
        return ""
    last, first = parts[-1], (parts[0] if len(parts) > 1 else "")
    if first and len(first) > 1:
        author_term = f"{last} {first}[Author]"
    elif first:
        author_term = f"{last} {first[0]}[Author]"
    else:
        author_term = f"{last}[Author]"
    if affiliations:
        aff_terms = " OR ".join(f'"{a}"[Affiliation]' for a in affiliations[:2])
        return f"({author_term}) AND ({aff_terms})"
    return author_term


def parse_openalex_works(payload: dict) -> list[CorpusCandidate]:
    """Keep only works OpenAlex maps to a PMID — those are fetchable content."""
    out: list[CorpusCandidate] = []
    for w in (payload.get("results") or []):
        pmid_url = (w.get("ids") or {}).get("pmid")
        if not pmid_url:
            continue
        out.append(
            CorpusCandidate(
                pmid=str(pmid_url).rstrip("/").rsplit("/", 1)[-1],
                year=w.get("publication_year"),
                title=w.get("title") or "",
                source="openalex",
            )
        )
    return out


async def fetch_openalex_by_orcid(orcid: str) -> list[CorpusCandidate]:
    """Every PubMed-indexed work OpenAlex attributes to this ORCID."""
    if not orcid:
        return []
    out: list[CorpusCandidate] = []
    cursor: str | None = "*"
    async with httpx.AsyncClient(timeout=60) as client:
        while cursor and len(out) < 800:
            # params= so httpx URL-encodes the cursor: OpenAlex cursors are
            # base64 and can carry '+'/'=', which an f-string URL would corrupt.
            params = {
                "filter": f"author.orcid:{orcid}",
                "per-page": 200,
                "cursor": cursor,
                "mailto": OPENALEX_MAILTO,
                "select": "ids,publication_year,title",
            }
            try:
                resp = await client.get(
                    f"{OPENALEX_BASE}/works", params=params,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.warning("OpenAlex lookup failed for %s: %s", orcid, exc)
                break
            out.extend(parse_openalex_works(payload))
            cursor = (payload.get("meta") or {}).get("next_cursor")
    return out


async def _esearch(term: str, retmax: int, sort: str | None = None) -> list[str]:
    if not term:
        return []
    params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": retmax}
    if sort:
        params["sort"] = sort
    try:
        resp = await _ncbi_get(f"{EUTILS_BASE}/esearch.fcgi", params)
        return (resp.json().get("esearchresult") or {}).get("idlist") or []
    except Exception as exc:
        logger.warning("PubMed ESearch failed (%s): %s", term, exc)
        return []


async def fetch_pubmed_by_orcid(orcid: str) -> list[str]:
    """Papers PubMed itself has tagged with this ORCID. High precision."""
    if not orcid:
        return []
    return await _esearch(f"{orcid}[auid]", AUTHOR_SEARCH_RETMAX)


async def fetch_pubmed_by_author(
    name: str, affiliations: list[str], retmax: int = AUTHOR_SEARCH_RETMAX
) -> list[str]:
    """Name + affiliation search — the only source that finds work the PI
    never added to ORCID. Over-fetches (200) so ranking has a real corpus.

    Returns [] when no affiliation is known: a bare name-only search is
    prohibited (spec §4.3 — "Hardwick" alone returns 1,779 hits)."""
    if not affiliations:
        return []
    return await _esearch(build_author_query(name, affiliations), retmax, sort="pub date")
```

- [ ] **Step 3b: Move the disambiguator into `corpus.py`**

Move `_disambiguate` (`scripts/generate_sparsedata_user.py:329`), together with
`_author_first_name_matches`, `_author_affiliations_from_xml`, and
`INSTITUTION_STOPWORDS`, into `src/services/corpus.py` as:

```python
async def disambiguate_pmids(
    pmids: list[str], name: str, affiliations: list[str]
) -> list[str]:
    ...  # the seeder's _disambiguate body, unchanged, returning only the kept list
```

The move is mechanical: same efetch-author-affiliation check, same stopword handling,
same thresholds. The seeder imports `disambiguate_pmids` and `INSTITUTION_STOPWORDS`
from `src.services.corpus` and its local copies are deleted (Task 7 removes the last
call sites). Do not tighten the matcher (spec §6).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_sources.py -v`
Expected: PASS — 8 passed.

- [ ] **Step 5: Verify `_ncbi_get` and `EUTILS_BASE` are importable as used**

Run: `.venv-test/bin/python -c "from src.services.corpus import fetch_pubmed_by_orcid; print('ok')"`
Expected: `ok`. If `_ncbi_get`'s signature differs, adapt the call — do not change `pubmed.py`.

- [ ] **Step 6: Commit**

```bash
git add src/services/corpus.py scripts/generate_sparsedata_user.py tests/unit/test_corpus_sources.py
git commit -m "feat(corpus): OpenAlex + PubMed author-search sources; move disambiguator into the resolver module"
```

---

### Task 4: `resolve_corpus` — the union, with a coverage verdict

Closes D1/D2/D4 at the retrieval layer and implements P2.

**Files:**
- Modify: `src/services/corpus.py`
- Test: `tests/unit/test_corpus_resolution.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 2-3.
- Produces:
  - `@dataclass CorpusResult` with `pmids: list[str]`, `ceiling: int`, `retrieved: int`, `coverage_suspect: bool`, `source_counts: dict[str, int]`, `doi_unresolved: int`, `pmid_to_orcid_doi: dict[str, str]` (so callers don't re-fetch ORCID works just to rebuild the DOI map)
  - `assess_coverage(retrieved: int, ceiling: int, cap: int = 50) -> bool`
  - `async fetch_years(pmids: list[str]) -> dict[str, int]` — one esummary batch; dates the PMIDs that arrive year-less from S3/S4 **before** ranking (without this, any dated old work outranks an undated recent one and D3 is re-created for thin-ORCID PIs)
  - `async resolve_corpus(orcid, name, affiliations, cap=50, disambiguate=None) -> CorpusResult` — `disambiguate` is an **awaitable** hook that defaults to Task 3b's `disambiguate_pmids`; passing `None` means "use the default", **not** "skip filtering". S4 cannot run unfiltered.

- [ ] **Step 1: Write the failing test**

```python
"""Coverage verdict: 'ORCID returned 3 works' and 'this PI published 3 papers'
must stop being indistinguishable. WuBot was seeded with 3 of 118 papers and
nothing complained.
"""

from src.services.corpus import assess_coverage


def test_flags_the_wu_case():
    # Carl Wu: 3 retrieved, 118 indexed papers exist, cap 50 -> expected 50
    assert assess_coverage(retrieved=3, ceiling=118, cap=50) is True


def test_flags_a_zero_evidence_profile():
    # Jennifer Kavran: 0 retrieved, 33 exist
    assert assess_coverage(retrieved=0, ceiling=33, cap=50) is True


def test_does_not_flag_a_full_profile():
    assert assess_coverage(retrieved=50, ceiling=786, cap=50) is False


def test_does_not_flag_a_genuinely_small_corpus():
    # Utthara Nayar: 6 retrieved, ceiling 4 (most of her ORCID is preprints).
    # Correct as stored; flagging it would invite a damaging "fix".
    assert assess_coverage(retrieved=6, ceiling=4, cap=50) is False


def test_does_not_flag_small_corpora_below_the_floor():
    # Below 10 expected, the estimate is too noisy to accuse the pipeline.
    assert assess_coverage(retrieved=2, ceiling=9, cap=50) is False


def test_flags_exactly_at_the_half_threshold_boundary():
    assert assess_coverage(retrieved=10, ceiling=20, cap=50) is False
    assert assess_coverage(retrieved=9, ceiling=20, cap=50) is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_resolution.py -v`
Expected: FAIL — `ImportError: cannot import name 'assess_coverage'`

- [ ] **Step 3: Write the implementation**

Add to `src/services/corpus.py` (`from collections.abc import Awaitable, Callable` goes
at the **top of the file** — E402, see Global Constraints):

```python
COVERAGE_RATIO = 0.5
COVERAGE_FLOOR = 10


@dataclass
class CorpusResult:
    pmids: list[str]
    ceiling: int
    retrieved: int
    coverage_suspect: bool
    source_counts: dict[str, int]
    doi_unresolved: int
    pmid_to_orcid_doi: dict[str, str]


def assess_coverage(retrieved: int, ceiling: int, cap: int = DEFAULT_CAP) -> bool:
    """True when we retrieved far less than an independent source says exists.

    `ceiling` is the best estimate of the PI's PubMed-indexed corpus, so the
    most we should ever expect to hold is min(cap, ceiling). Below COVERAGE_FLOOR
    expected papers the estimate is too noisy to accuse the pipeline — a
    genuinely early-career PI must not be flagged.
    """
    expected = min(cap, ceiling)
    if expected < COVERAGE_FLOOR:
        return False
    return retrieved < COVERAGE_RATIO * expected


async def fetch_years(pmids: list[str]) -> dict[str, int]:
    """{pmid: year} via esummary, batched. Used to date S3/S4-only PMIDs
    BEFORE ranking — an undated candidate must not lose to a dated 1990 paper."""
    out: dict[str, int] = {}
    for i in range(0, len(pmids), 200):
        batch = pmids[i : i + 200]
        try:
            resp = await _ncbi_get(
                f"{EUTILS_BASE}/esummary.fcgi",
                {"db": "pubmed", "id": ",".join(batch), "retmode": "json"},
            )
            result = resp.json().get("result", {})
        except Exception as exc:
            logger.warning("esummary year batch failed (%s...): %s", batch[:3], exc)
            continue
        for pmid in result.get("uids", []):
            pubdate = (result.get(pmid) or {}).get("pubdate", "")
            m = re.match(r"(\d{4})", pubdate)
            if m:
                out[str(pmid)] = int(m.group(1))
    return out


async def resolve_corpus(
    orcid: str,
    name: str,
    affiliations: list[str],
    cap: int = DEFAULT_CAP,
    disambiguate: Callable[[list[str], str, list[str]], Awaitable[list[str]]] | None = None,
) -> CorpusResult:
    """Union every source, resolve DOIs, dedupe, date, rank, then cap.

    No source short-circuits another. The gate this replaces treated three
    ORCID works as proof that a PubMed search was unnecessary.

    S4 is ALWAYS disambiguated: `disambiguate=None` means "use the built-in
    disambiguate_pmids", never "skip filtering". An empty `orcid` skips S1-S3
    (the fetchers guard this) and resolves from S4 alone.
    """
    from src.services.orcid import fetch_orcid_works
    from src.services.pubmed import convert_dois_to_pmids

    candidates: list[CorpusCandidate] = []
    source_counts: dict[str, int] = {}
    pmid_to_orcid_doi: dict[str, str] = {}

    # S1 — ORCID works (PMIDs directly, DOIs resolved below)
    orcid_works: list[dict] = []
    if orcid:
        try:
            orcid_works = await fetch_orcid_works(orcid)
        except Exception as exc:
            logger.warning("ORCID works failed for %s: %s", orcid, exc)
    doi_only: list[dict] = []
    for w in orcid_works:
        if w.get("pmid"):
            candidates.append(
                CorpusCandidate(str(w["pmid"]), w.get("year"), w.get("title") or "", "orcid")
            )
            if w.get("doi"):
                pmid_to_orcid_doi[str(w["pmid"])] = w["doi"]
        elif w.get("doi"):
            doi_only.append(w)
    source_counts["orcid"] = sum(1 for c in candidates if c.source == "orcid")

    # S1b — DOI -> PMID. Misses are COUNTED, not swallowed (D4).
    doi_unresolved = 0
    if doi_only:
        dois = list({w["doi"] for w in doi_only})
        try:
            mapping = await convert_dois_to_pmids(dois)
        except Exception as exc:
            logger.warning("DOI->PMID failed for %s: %s", orcid, exc)
            mapping = {}
        doi_unresolved = len(dois) - len(mapping)
        if doi_unresolved:
            logger.warning(
                "DOI->PMID unresolved for %s: %d of %d", orcid, doi_unresolved, len(dois)
            )
        by_doi = {w["doi"]: w for w in doi_only}
        for doi, pmid in mapping.items():
            w = by_doi.get(doi, {})
            candidates.append(
                CorpusCandidate(str(pmid), w.get("year"), w.get("title") or "", "orcid_doi")
            )
            pmid_to_orcid_doi[str(pmid)] = doi
        source_counts["orcid_doi"] = len(mapping)

    # S2 — OpenAlex by ORCID (institution-agnostic; recovers prior-institution work)
    oa = await fetch_openalex_by_orcid(orcid)
    candidates.extend(oa)
    source_counts["openalex"] = len(oa)

    # S3 — PubMed's own ORCID tagging
    tagged = await fetch_pubmed_by_orcid(orcid)
    candidates.extend(CorpusCandidate(p, None, "", "pubmed_auid") for p in tagged)
    source_counts["pubmed_auid"] = len(tagged)

    # S4 — name + affiliation. The ONLY source needing disambiguation, and it
    # NEVER runs without it. fetch_pubmed_by_author returns [] when no
    # affiliation is known (name-only search is prohibited, spec §4.3).
    aff_hits = await fetch_pubmed_by_author(name, affiliations)
    if aff_hits:
        filt = disambiguate or disambiguate_pmids
        aff_hits = await filt(aff_hits, name, affiliations)
    candidates.extend(CorpusCandidate(p, None, "", "pubmed_aff") for p in aff_hits)
    source_counts["pubmed_aff"] = len(aff_hits)

    # DATE the undated (D3 would silently reappear without this: an undated
    # 2026 paper must not lose its cap slot to a dated 1990 one).
    dated_pmids = {c.pmid for c in candidates if c.year is not None}
    undated = sorted({c.pmid for c in candidates} - dated_pmids)
    years = await fetch_years(undated) if undated else {}
    if years:
        candidates = [
            CorpusCandidate(c.pmid, years.get(c.pmid), c.title, c.source)
            if c.year is None else c
            for c in candidates
        ]

    ceiling = max(
        source_counts.get("openalex", 0),
        source_counts.get("pubmed_auid", 0),
        source_counts.get("pubmed_aff", 0),
    )
    ranked = merge_and_rank(candidates, cap=cap)
    return CorpusResult(
        pmids=[c.pmid for c in ranked],
        ceiling=ceiling,
        retrieved=len(ranked),
        coverage_suspect=assess_coverage(len(ranked), ceiling, cap),
        source_counts=source_counts,
        doi_unresolved=doi_unresolved,
        pmid_to_orcid_doi=pmid_to_orcid_doi,
    )
```

Ranking note: S3/S4 PMIDs arrive year-less; `fetch_years` dates them before
`merge_and_rank`, so the cap operates on a fully dated corpus. The earlier draft ranked
them at `year=None` and relied on "run the backfill twice" to converge — that mitigation
was circular (a second run has identical inputs and produces an identical diff) and is
withdrawn.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_resolution.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/services/corpus.py tests/unit/test_corpus_resolution.py
git commit -m "feat(corpus): resolve_corpus unions all sources and reports a coverage verdict"
```

---

### Task 5: Route the new-PI pipeline through `resolve_corpus`

Closes D2 — the defect that would otherwise reproduce on every future PI. Also writes the evidence columns that Task 6 enforces.

**Files:**
- Modify: `src/services/profile_pipeline.py:101-175` (retrieval, including the dead
  `orcid_works_by_pmid` dict at `:175` — defined once, never used), and step 9's evidence
  assignment near `:380`
- Modify: `tests/characterization/test_profile_pipeline_gm.py` (`_install_fakes` gains a
  `resolve_corpus` fake — see step 5) and its snapshot (regenerated deliberately)
- Modify: `src/models/profile.py` (add `coverage_suspect: Mapped[bool | None]`, nullable,
  beside the migration-0023 provenance columns)
- Create: `alembic/versions/0026_coverage_suspect.py` — adds the nullable boolean column
  to `researcher_profiles`, no backfill (NULL = "never assessed", matching 0023's
  convention). Single head; `./scripts/ci.sh` round-trips it.
- Test: `tests/unit/test_profile_pipeline_corpus.py` (create)

**Interfaces:**
- Consumes: `resolve_corpus`, `CorpusResult` from Task 4.
- Produces: `run_profile_pipeline` unchanged in signature; `pmids` now comes from the union
  and is capped at 50; `ResearcherProfile.evidence_pmid_count` / `.evidence_pub_count` /
  `.coverage_suspect` written on every run.
- **Test seam:** `resolve_corpus` is imported at module level
  (`from src.services.corpus import DEFAULT_CAP, resolve_corpus`), so tests monkeypatch
  `profile_pipeline.resolve_corpus` exactly the way they already monkeypatch
  `profile_pipeline.fetch_orcid_works`. Do not import it inside the function — that would
  leave the characterization suite no way to intercept it, and the golden master would
  make live OpenAlex/NCBI calls.

- [ ] **Step 1: Write the failing test**

```python
"""The new-PI path must search PubMed, not just read ORCID.

profile_pipeline was purely ORCID-driven: a PI with a thin ORCID record got a
thin profile and a progress note asking THEM to fix ORCID. It also had no cap,
inconsistent with the seeder's deliberate 50.
"""

import inspect

import src.services.profile_pipeline as pp


def test_pipeline_imports_the_shared_resolver():
    src = inspect.getsource(pp)
    assert "resolve_corpus" in src, "pipeline must delegate retrieval to corpus.py"


def test_pipeline_no_longer_derives_pmids_from_orcid_alone():
    src = inspect.getsource(pp.run_profile_pipeline)
    assert 'pmids = [w["pmid"] for w in orcid_works if w.get("pmid")]' not in src


def test_pipeline_applies_the_cap():
    from src.services.corpus import DEFAULT_CAP

    assert DEFAULT_CAP == 50
    src = inspect.getsource(pp.run_profile_pipeline)
    assert "cap=" in src or "DEFAULT_CAP" in src
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_profile_pipeline_corpus.py -v`
Expected: FAIL on `test_pipeline_imports_the_shared_resolver`.

- [ ] **Step 3: Write the implementation**

In `src/services/profile_pipeline.py`, add the import beside the existing ORCID import:

```python
from src.services.corpus import DEFAULT_CAP, resolve_corpus
```

Replace the whole of step 3, the DOI-resolution block, and the dead
`orcid_works_by_pmid` comment+dict (lines 101-175 — after this replacement nothing in the
function references `orcid_works`, which no longer exists) with:

```python
    # Step 3: Resolve the publication corpus.
    #
    # ORCID alone is not enough and never was: it is PI-maintained, and the
    # 2026-08-13 audit found 30 PIs whose ORCID record held a small fraction of
    # their indexed output (one had 20 of 786). resolve_corpus unions ORCID,
    # OpenAlex, and two PubMed searches, then ranks by date and caps at 50.
    update_progress("step3", "Resolving publication corpus...")
    affiliations = [a for a in (user.institution, user.department) if a]
    corpus = await resolve_corpus(
        orcid=orcid_id,
        name=user.name,
        affiliations=affiliations,
        cap=DEFAULT_CAP,
    )
    pmids = corpus.pmids
    works_lookup_failed = corpus.retrieved == 0 and corpus.ceiling == 0

    if corpus.coverage_suspect:
        logger.warning(
            "[coverage] %s (%s): retrieved %d but external sources indicate ~%d "
            "— profile will be marked ungrounded",
            user.name, orcid_id, corpus.retrieved, corpus.ceiling,
        )
        update_progress(
            "coverage_suspect",
            f"Retrieved {corpus.retrieved} publications but ~{corpus.ceiling} "
            "appear to exist. Profile needs review before activation.",
        )
```

The `pmid_to_orcid_doi` map feeding `reconcile_pub_doi` is still required — take it from
the resolver instead of re-fetching ORCID (the earlier draft fetched the works a second
time, doubling ORCID load and risking disagreement between the two calls), leaving
`reconcile_pub_doi` itself untouched:

```python
    pmid_to_orcid_doi = corpus.pmid_to_orcid_doi
```

Then at step 9 (near line 380), replace the evidence assignment so it records the
resolver's verdict rather than the old ORCID-only count, and persist the coverage flag
(column added by Task 6's migration) next to the other provenance columns:

```python
    evidence_pmid_count = None if works_lookup_failed else corpus.retrieved
    ...
    profile.coverage_suspect = corpus.coverage_suspect   # beside evidence_pmid_count
```

(the model column and migration 0026 are part of this task's Files list — they land here
because this is the first writer; Task 6 only reads the column).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_profile_pipeline_corpus.py tests/unit/ -v`
Expected: PASS. Any existing test asserting ORCID-only retrieval is now wrong by design — update it and note why in the commit body.

- [ ] **Step 5: Update the characterization golden master — deliberately**

The pipeline GM (`tests/characterization/test_profile_pipeline_gm.py`) fakes the fetchers
in the pipeline's namespace; after this task the pipeline calls `resolve_corpus` instead,
so without a new fake the GM would hit live OpenAlex/NCBI. In `_install_fakes`, add:

```python
    async def fake_resolve_corpus(orcid, name, affiliations, cap=50, disambiguate=None):
        from src.services.corpus import CorpusResult

        return CorpusResult(
            pmids=["1001", "1002"],
            ceiling=2,
            retrieved=2,
            coverage_suspect=False,
            source_counts={"orcid": 2},
            doi_unresolved=0,
            pmid_to_orcid_doi={"1001": "10.1000/aaa", "1002": "10.1000/bbb"},
        )

    monkeypatch.setattr(profile_pipeline, "resolve_corpus", fake_resolve_corpus)
```

This task's observable behaviour change (progress step wording, evidence semantics,
`coverage_suspect`) makes a snapshot change **expected and correct**. Regenerate it scoped
to this one file, then review the diff hunk by hunk — the publications and profile fields
must be unchanged; only the new provenance/progress entries may differ:

```bash
.venv-test/bin/python -m pytest tests/characterization/test_profile_pipeline_gm.py --snapshot-update
git diff tests/characterization/__snapshots__/   # review before staging; test_agent_turn_gm.ambr must be untouched
.venv-test/bin/python -m pytest tests/ -v        # full suite, now green with no live calls
```

- [ ] **Step 6: Commit**

```bash
git add src/services/profile_pipeline.py src/models/profile.py alembic/versions/0026_coverage_suspect.py \
        tests/unit/test_profile_pipeline_corpus.py tests/characterization/
git commit -m "fix(pipeline): resolve the corpus from all sources, not ORCID alone"
```

---

### Task 6: Refuse to activate an ungrounded agent

Implements P3. `ResearcherProfile.evidence_state` already names this condition and has never been consulted.

**Files:**
- Modify: `src/services/admin_provisioning.py` (the gate function — `logger` already exists at `:27`)
- Modify: `src/routers/admin.py:1009-1015` (the call site — `admin_approve_agent` is the
  only *function* that writes `status='active'`, but it does so from **two branches**: the
  pending→active approval at `:1011` and the `agent_status` form-dropdown edit at
  `:1014-1015`. The gate must cover both — gating only `:1011` leaves a one-click
  re-activation bypass on the same page.)
- Modify: `templates/admin/agent_detail.html` (the `force` checkbox — without a form
  control the override is unreachable from the UI)
- Test: `tests/unit/test_activation_evidence_gate.py` (create)

**Interfaces:**
- Consumes: `ResearcherProfile.evidence_state` (`src/models/profile.py:84`) and
  `ResearcherProfile.coverage_suspect` (Task 5's migration 0026).
- Produces: `assert_activatable(profile: ResearcherProfile | None, *, override: bool = False) -> None`, raising `UngroundedProfileError` (new, defined in `admin_provisioning.py`).
- **Scope:** the gate applies to `role='pi_lab'` agents only. A `scout_hub` agent
  (BlackbirdBot) has no `user_id` and no profile *by design* (spec, Scope) and must stay
  approvable without an override.

- [ ] **Step 1: Write the failing test**

```python
"""A profile with no publications behind it must not go live.

Four bots reached production synthesized from a scraped faculty page and zero
papers. KavranBot's summary named the wrong protein families entirely.
"""

import pytest

from src.models import ResearcherProfile
from src.services.admin_provisioning import UngroundedProfileError, assert_activatable


def _profile(pub_count, pmid_count=0, coverage_suspect=None):
    p = ResearcherProfile()
    p.evidence_pub_count = pub_count
    p.evidence_pmid_count = pmid_count
    p.coverage_suspect = coverage_suspect
    return p


def test_grounded_profile_activates():
    assert assert_activatable(_profile(24, 30)) is None


def test_grounded_but_coverage_suspect_is_refused():
    # WuBot's future shape: grounded in 20 papers while ~118 exist. Grounded
    # alone is not enough — spec P3 gates on the coverage verdict too.
    with pytest.raises(UngroundedProfileError, match="coverage_suspect"):
        assert_activatable(_profile(20, 20, coverage_suspect=True))


def test_zero_evidence_profile_is_refused():
    with pytest.raises(UngroundedProfileError, match="no_evidence_available"):
        assert_activatable(_profile(0, 0))


def test_evidence_lost_profile_is_refused():
    with pytest.raises(UngroundedProfileError, match="evidence_lost"):
        assert_activatable(_profile(0, 30))


def test_legacy_unknown_profile_is_refused():
    with pytest.raises(UngroundedProfileError, match="unknown"):
        assert_activatable(_profile(None, None))


def test_missing_profile_is_refused():
    with pytest.raises(UngroundedProfileError):
        assert_activatable(None)


def test_explicit_override_is_allowed():
    assert assert_activatable(_profile(0, 0), override=True) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_activation_evidence_gate.py -v`
Expected: FAIL — `ImportError: cannot import name 'UngroundedProfileError'`

- [ ] **Step 3: Write the implementation**

In `src/services/admin_provisioning.py`:

```python
class UngroundedProfileError(RuntimeError):
    """Raised when activating an agent whose profile has no publications behind it."""


def assert_activatable(profile, *, override: bool = False) -> None:
    """Gate on ResearcherProfile.evidence_state.

    A faculty page may justify creating a PENDING agent. It may never justify
    an ACTIVE one — an active bot makes public factual claims about a lab.
    """
    if override:
        logger.warning("[evidence-gate] OVERRIDDEN by explicit admin action")
        return None
    if profile is None:
        raise UngroundedProfileError(
            "no researcher profile exists for this agent; refusing to activate"
        )
    state = profile.evidence_state
    if state != "grounded":
        raise UngroundedProfileError(
            f"profile evidence_state is {state!r}, not 'grounded'; refusing to "
            "activate. Backfill the corpus and regenerate, or pass override."
        )
    if profile.coverage_suspect:
        raise UngroundedProfileError(
            "profile is coverage_suspect: retrieval found far fewer publications "
            "than external sources indicate exist. Backfill and regenerate, or "
            "pass override."
        )
    return None
```

Then gate **both** transitions to `"active"` in `admin_approve_agent`
(`src/routers/admin.py:1009-1015`). The current code is:

```python
    if agent.status == "pending":
        agent.status = "active"
        ...
    elif agent_status in VALID_AGENT_STATUSES:
        agent.status = agent_status          # can also be "active" — must be gated too
```

Insert the check once, above the branch, firing whenever the outcome would be `active`:

```python
    becomes_active = agent.status == "pending" or agent_status == "active"
    if becomes_active and agent.role == "pi_lab":
        profile_row = await db.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
        )
        try:
            assert_activatable(profile_row.scalar_one_or_none(), override=force)
        except UngroundedProfileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if agent.status == "pending":
        agent.status = "active"
        agent.approved_at = datetime.now(UTC)
        agent.approved_by = current_user.id
    elif agent_status in VALID_AGENT_STATUSES:
        agent.status = agent_status
```

`force` comes from a new optional form field on the approve endpoint (`force: bool =
Form(False)`), defaulting to `False`, with a visible checkbox added to
`templates/admin/agent_detail.html` next to the status control — an override that cannot
be reached from the UI is not an override, it is a dead parameter. The override must be an
explicit admin action, never the default.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_activation_evidence_gate.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 5: Confirm the gate does not break existing provisioning or router tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_admin_provisioning.py tests/integration -v`
Expected: PASS. Existing fixtures that approve an agent now need a grounded profile; give them `evidence_pub_count=1`. A fixture that legitimately tests approval without a profile should pass `force=True` — if a test needs the override to pass, that is the gate working.

- [ ] **Step 6: Commit**

```bash
git add src/services/admin_provisioning.py src/routers/admin.py templates/admin/agent_detail.html \
        tests/unit/test_activation_evidence_gate.py
git commit -m "feat(provisioning): refuse to activate an agent whose profile has no evidence"
```

---

### Task 7: Retire the seeder's short-circuit

Closes D1 at its source and D8's grants half. After this there is one retrieval implementation, not two.

**Files:**
- Modify: `scripts/generate_sparsedata_user.py:690-740` and the persistence block near `:760`
- Test: `tests/unit/test_seeder_no_shortcircuit.py` (create)

**Interfaces:**
- Consumes: `resolve_corpus` (Task 4), `fetch_orcid_grants` (`src/services/orcid.py:76`).
- Produces: no new public interface; the seeder's `kept_pmids` now comes from `resolve_corpus`.

- [ ] **Step 1: Write the failing test**

```python
"""The >= EVIDENCE_FLOOR_PAPERS retrieval gate must be gone.

scripts/generate_sparsedata_user.py:724 skipped the PubMed name+affiliation
search whenever ORCID yielded 3+ PMIDs. That one line is why most of the Tier B
population is under-filled: Carl Wu curated 3 works and was stored with 3 of
118 papers. EVIDENCE_FLOOR_PAPERS keeps its legitimate job at the persistence
floor.
"""

import pathlib

_SRC = pathlib.Path("scripts/generate_sparsedata_user.py").read_text()


def test_retrieval_shortcircuit_is_gone():
    assert "if len(orcid_pmids) >= EVIDENCE_FLOOR_PAPERS:" not in _SRC


def test_seeder_delegates_to_the_shared_resolver():
    assert "resolve_corpus" in _SRC


def test_persistence_floor_is_retained():
    assert "if audit.n_papers_kept < EVIDENCE_FLOOR_PAPERS" in _SRC


def test_orcid_group_order_slice_is_gone():
    assert "list(dict.fromkeys(orcid_pmids))[:PUBMED_FETCH_CAP]" not in _SRC


def test_seeder_populates_grant_titles():
    assert "fetch_orcid_grants" in _SRC
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_seeder_no_shortcircuit.py -v`
Expected: FAIL — the short-circuit and the group-order slice are both still present.

- [ ] **Step 3: Write the implementation**

Replace the *body* of the discovery block (the ORCID fetch, staleness gate,
`[:PUBMED_FETCH_CAP]` slice, and the `if len(orcid_pmids) >= EVIDENCE_FLOOR_PAPERS`
branch, lines ~690-740) with a single resolver call. **Keep the surrounding structure**:
the `placeholder` flag still exists, and a placeholder (fake) ORCID must reach
`resolve_corpus` as `orcid=""` so S1-S3 are skipped and the corpus resolves from the
disambiguated S4 alone — `kept_pmids` must be assigned on *every* path through the block:

```python
    from src.services.corpus import resolve_corpus

    corpus = await resolve_corpus(
        orcid="" if placeholder else (row.orcid or ""),
        name=row.name,
        affiliations=row.affiliations,
        cap=PUBMED_FETCH_CAP,
        # no disambiguate= argument: the resolver applies Task 3b's
        # disambiguate_pmids to S4 by default. Passing None does NOT skip it.
    )
    kept_pmids = corpus.pmids
    audit.n_pubmed_hits = corpus.ceiling   # NOTE: semantics change — was raw ESearch hits,
                                           # is now the external ceiling estimate
    audit.n_papers_kept = len(kept_pmids)
    if corpus.coverage_suspect:
        audit.reasons.append(
            f"coverage_suspect: kept {corpus.retrieved} of ~{corpus.ceiling} expected"
        )
    if corpus.doi_unresolved:
        audit.reasons.append(f"doi_unresolved_{corpus.doi_unresolved}")
```

The `ORCID_STALE_AFTER_YEARS` gate is deleted with this block: it existed only to stop a
stale ORCID short-circuiting the PubMed search, and nothing short-circuits anything now.
Remove the constant at line 76 and its comment. The seeder's local `_disambiguate`,
`_author_first_name_matches`, `_author_affiliations_from_xml`, and `_esearch_pmids` become
dead once this block delegates to the resolver — delete them (Task 3b already moved the
logic into `corpus.py`; `_build_pubmed_query` is likewise superseded by
`build_author_query`).

Then, so `grant_titles` is populated (D8), add before the LLM synthesis call:

```python
    from src.services.orcid import fetch_orcid_grants

    grant_titles: list[str] = []
    if row.orcid:
        try:
            grant_titles = await fetch_orcid_grants(row.orcid)
        except Exception as exc:
            logger.warning("ORCID grants failed for %s: %s", row.name, exc)
```

and pass `grant_titles` through to the `ResearcherProfile` write.

Finally, write the evidence columns inside `_persist` (the seeder's persistence function)
so Task 6's gate can see them. The seeder's variable for the fetched records is
`pubmed_records` (there is no `pubs_for_synthesis` in this file — that name belongs to the
pipeline); count what actually grounds the synthesis, i.e. records that carried an
abstract:

```python
    profile.evidence_pmid_count = corpus.retrieved
    profile.evidence_pub_count = sum(1 for r in pubmed_records if r.get("abstract"))
    profile.coverage_suspect = corpus.coverage_suspect
```

(`corpus` and `pubmed_records` need to be passed into `_persist` — extend its parameters;
it already receives `pubmed_records` for the publication rows.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_seeder_no_shortcircuit.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Byte-compile the seeder (it has no import-time test coverage)**

Run: `.venv-test/bin/python -m py_compile scripts/generate_sparsedata_user.py && echo ok`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_sparsedata_user.py tests/unit/test_seeder_no_shortcircuit.py
git commit -m "fix(seeder): union all sources instead of short-circuiting on 3 ORCID works"
```

---

### Task 8: `backfill-corpus` CLI with a mandatory dry run

The remediation tool. Read-only by default so the ~909-publication ledger can be verified
before anything is written.

**Files:**
- Modify: `src/cli.py` (new `backfill-corpus` command; also add an `--agent` option to the
  existing `regenerate-profiles` — it currently takes **no arguments** and enqueues every
  user with an ORCID, which makes the tiered regeneration in Task 9 impossible)
- Create: `src/services/corpus_backfill.py`
- Test: `tests/unit/test_corpus_backfill.py` (create)

**Interfaces:**
- Consumes: `resolve_corpus` (Task 4), `fetch_pubmed_records` (`src/services/pubmed.py`).
- Produces:
  - `@dataclass BackfillPlan` with `agent_id: str`, `current: int`, `resolved: int`, `to_add: list[str]`, `to_remove: list[str]`, `coverage_suspect: bool`
  - `plan_backfill(existing_pmids: list[str], resolved_pmids: list[str]) -> BackfillPlan`-shaped diff via `diff_pmids(existing, resolved) -> tuple[list[str], list[str]]`
  - CLI: `python -m src.cli backfill-corpus [--agent ID] [--apply] [--limit N]`
  - CLI: `python -m src.cli regenerate-profiles [--agent ID]` (existing command, new filter)
- **Scope rules:** the all-agents mode covers `status='active'`, `role='pi_lab'` agents
  only; a `pending` agent (Tier A after its Task 9 demotion) is processed **only** when
  named explicitly with `--agent`. This is what makes the blanket apply safe to run after
  the demotion.

- [ ] **Step 1: Write the failing test**

```python
"""The backfill is a diff, not an append.

Tier D PIs (salzberg, janak, leung, norris, pekosz) are AT the cap holding the
wrong 50 — they need rows removed as well as added. leung holds 53 and must
come back down to 50.
"""

from src.services.corpus_backfill import diff_pmids


def test_adds_missing_pmids():
    add, remove = diff_pmids(existing=["1", "2"], resolved=["1", "2", "3"])
    assert add == ["3"]
    assert remove == []


def test_removes_pmids_outside_the_resolved_top_50():
    add, remove = diff_pmids(existing=["old1", "old2", "1"], resolved=["1", "2"])
    assert add == ["2"]
    assert sorted(remove) == ["old1", "old2"]


def test_no_change_when_already_correct():
    add, remove = diff_pmids(existing=["1", "2"], resolved=["2", "1"])
    assert add == []
    assert remove == []


def test_empty_resolution_removes_nothing():
    """A source outage must never empty a good profile."""
    add, remove = diff_pmids(existing=["1", "2"], resolved=[])
    assert add == []
    assert remove == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.corpus_backfill'`

- [ ] **Step 3: Write the implementation**

Create `src/services/corpus_backfill.py`:

```python
"""Diff a PI's stored publications against a freshly resolved corpus."""

import logging

logger = logging.getLogger(__name__)


def diff_pmids(existing: list[str], resolved: list[str]) -> tuple[list[str], list[str]]:
    """Return (to_add, to_remove).

    An empty resolution removes nothing. Every source is remote and a transient
    outage must never be able to empty a correct profile — Tier D needs
    deletions, but only against a resolution that actually succeeded.
    """
    if not resolved:
        logger.warning("Empty resolution — refusing to remove anything")
        return ([], [])
    have, want = set(existing), set(resolved)
    to_add = [p for p in resolved if p not in have]
    to_remove = [p for p in existing if p not in want]
    return (to_add, to_remove)
```

Add the CLI command to `src/cli.py`, following the existing `regenerate-profiles` command's
structure for session handling:

```python
@app.command(name="backfill-corpus")
def backfill_corpus(
    agent: str = typer.Option(None, help="Single agent_id; omit for all active PIs"),
    apply: bool = typer.Option(False, "--apply", help="Write changes (default: dry run)"),
    limit: int = typer.Option(0, help="Stop after N agents (0 = no limit)"),
):
    """Re-resolve each PI's corpus and report or apply the diff.

    Dry run by default: prints agent_id, current count, resolved count, +adds,
    -removes, and the coverage verdict. Review the report before --apply.
    """
    asyncio.run(_backfill_corpus(agent=agent, apply=apply, limit=limit))
```

The `_backfill_corpus` coroutine must, per agent: load the `User` + existing `Publication`
PMIDs, call `resolve_corpus(cap=50)`, call `diff_pmids`, and — only when `apply` is set —

1. `fetch_pubmed_records(to_add)` and insert the new `Publication` rows;
2. delete the `to_remove` rows;
3. **re-fetch the kept PMIDs too** (`set(existing) & set(resolved)`) and update
   `title` / `abstract` / `journal` / `year` in place where they differ — this, not the
   diff, is what repairs the ~112 truncated titles and truncated abstracts already
   sitting in rows a pure add/remove diff would never touch (Task 1's parser fix makes
   the refetched values correct);
4. write `evidence_pmid_count` / `evidence_pub_count` / `coverage_suspect`.

It must never write when `corpus.retrieved == 0`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_corpus_backfill.py -v`
Expected: PASS — 4 passed.

- [ ] **Step 5: Run the full gate**

(The in-container read-only verification that used to live here is now Task 9 step 2 — it
cannot run before Task 9 step 1 deploys: `blackbird-app` bakes `src/` into the image with
no bind mount, so until the rebuild the container simply does not have the
`backfill-corpus` command.)

Run: `./scripts/ci.sh`
Expected: PASS — single alembic head, ruff clean on `tests/`, coverage floor met.

- [ ] **Step 6: Commit**

```bash
git add src/services/corpus_backfill.py src/cli.py tests/unit/test_corpus_backfill.py
git commit -m "feat(cli): backfill-corpus, dry-run by default; --agent filter for regenerate-profiles"
```

---

### Task 9: Execute the backfill

Operational, not code. Run in tier order; the code from Tasks 1-8 must be deployed first.

**Files:**
- Modify: none. Database + `profiles/public/*.md` only.

**Interfaces:**
- Consumes: the `backfill-corpus` CLI (Task 8), `regenerate-profiles` (`src/cli.py:199`).
- Produces: publications and profiles matching the §3 ledger.

Tier ordering is load-bearing (spec §4.4, §4.6): **Tier A is demoted before anything is
applied anywhere**, because a blanket apply or an unfiltered regeneration reaching the
four hollow profiles pre-confirmation is exactly what the human gate exists to prevent.

- [ ] **Step 1: Deploy the code and migrate**

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC up -d --build blackbird-app worker
$DC --profile agent build agent          # src/ is BAKED IN — required
$DC exec -T blackbird-app alembic upgrade head
$DC exec -T blackbird-app alembic current
```

Expected: `alembic current` equals `alembic heads` and includes Task 5's `0026`.

- [ ] **Step 2: Verify the dry run is genuinely read-only**

(Moved here from Task 8 — before the step-1 rebuild the container does not have the
command.)

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A \
  -c "SELECT count(*) FROM publications;" > /tmp/before.txt
docker compose -f docker-compose.prod.yml exec -T blackbird-app \
  python -m src.cli backfill-corpus --agent wu
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A \
  -c "SELECT count(*) FROM publications;" > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt && echo "READ-ONLY CONFIRMED"
```

Expected: `READ-ONLY CONFIRMED`, and a report line showing `wu` at 3 → 50.

- [ ] **Step 3: Snapshot the database before any write**

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U copi -d copi -t publications -t researcher_profiles -t agents \
  > logs/pre_backfill_$(date +%s).sql
```

Expected: a non-empty dump (`agents` is included because step 4 changes it). Do not
proceed without it.

- [ ] **Step 4: Demote Tier A to `pending` — BEFORE any apply**

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -c "
UPDATE agents SET status='pending' WHERE agent_id IN
  ('kavran','pearce','rebecca','mukherjeeclavin');"
```

The roster sync picks this up within ~30s and the four bots stop posting; no restart. From
here on, Task 8's scope rule keeps every all-agents command away from them: a `pending`
agent is only touched via an explicit `--agent`.

- [ ] **Step 5: Full dry run; verify against the ledger**

```bash
docker compose -f docker-compose.prod.yml exec -T blackbird-app \
  python -m src.cli backfill-corpus | tee logs/backfill_dryrun.txt
```

Expected: ~771 net additions across the 33 active Tier B/C PIs (the §3 ledger's 909
pursued, minus Tier A's 138 which is now gated behind step 9). Spot-check rows against §3
— `wu` 3→50, `casadevall` 20→50, `nayar` 6→6 (unchanged). **If any Tier E PI shows a
delta larger than ±2, stop** — the resolver is over-matching and Task 4 needs revisiting
before anything is written.

- [ ] **Step 6: Apply Tier C, then Tier B + D**

```bash
for a in lee huganir mcmeniman; do
  docker compose -f docker-compose.prod.yml exec -T blackbird-app \
    python -m src.cli backfill-corpus --agent $a --apply
done
docker compose -f docker-compose.prod.yml exec -T blackbird-app \
  python -m src.cli backfill-corpus --apply | tee logs/backfill_apply.txt
```

Expected: `lee` 21→50, `huganir` 42→50, `mcmeniman` 24→32 first (lowest risk — ORCID
already had these works); then Tier D shows removals as well as additions (`salzberg`,
`janak`, `leung`, `norris`, `pekosz`), `leung` drops from 53 to 50, and kept rows report
refreshed titles/abstracts (the ~112 truncated titles go to zero — verify:
`SELECT count(*) FROM publications WHERE title LIKE '% ';`).

- [ ] **Step 7: Verify the cap and recency — scoped to the tiers just written**

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -c "
SELECT ag.agent_id, count(*) AS pubs, max(p.year) AS newest
FROM publications p JOIN agents ag ON ag.user_id = p.user_id
WHERE ag.status='active'
GROUP BY 1
HAVING count(*) > 50
    OR (max(p.year) < 2023 AND ag.agent_id IN
        ('lee','huganir','mcmeniman','salzberg','janak','leung','norris','pekosz',
         'wu','shastri','bailey','weeraratna','agre','zavala','carlton','coppens',
         'sinnis','markham','casadevall','wang','cai','chute','thompson','epearce',
         'davis','green','hart','camacho','culotta','mugnier','gordy','hardwick',
         'mueller','kevrekidis','klein','oneal','srinivasan','perrin'))
ORDER BY 2 DESC;"
```

Expected: **zero rows**. The cap check is global; the recency check is restricted to the
Tier B/C/D ledger rows — a genuinely small, older corpus elsewhere (`nayar`,
`mukherjeeclavin`) must not fail the runbook.

- [ ] **Step 8: Regenerate Tier B/C/D profiles from the new abstracts**

```bash
for a in lee huganir mcmeniman salzberg janak leung norris pekosz \
         wu shastri bailey weeraratna agre zavala carlton coppens sinnis markham \
         casadevall wang cai chute thompson epearce davis green hart camacho \
         culotta mugnier gordy hardwick mueller kevrekidis klein oneal srinivasan perrin; do
  docker compose -f docker-compose.prod.yml exec -T blackbird-app \
    python -m src.cli regenerate-profiles --agent $a
done
```

Per-agent on purpose: an unfiltered `regenerate-profiles` enqueues **every** user with an
ORCID, which would synthesize Tier A pre-confirmation and churn Tier E for nothing. The
pipeline export + the simulation's mtime polling propagate the new summaries to running
bots automatically; no restart for content.

Expected: `profile_version` increments; `evidence_pub_count` non-null and non-zero for
every Tier B/C/D PI. The stored `research_summary` was synthesized from the old thin set
and does not update itself — this step is not optional.

- [ ] **Step 9: Handle Tier A — human confirmation required**

For `kavran`, `pearce`, `rebecca`, `mukherjeeclavin` (already `pending` since step 4),
one PI at a time:

1. `backfill-corpus --agent <id>` (dry run); present the top 10 resolved titles to a
   human alongside the ORCID on the `users` row. A lab website or CV is the right
   tiebreak *here* — for confirming identity, never as evidence for synthesis.
2. Only after they confirm identity: `backfill-corpus --agent <id> --apply`, then
   `regenerate-profiles --agent <id>`.
3. Re-activate through the admin UI — Task 6's gate will now permit it (grounded, not
   coverage-suspect).

- [ ] **Step 10: Confirm the evidence gate now passes system-wide**

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -c "
SELECT count(*) FILTER (WHERE rp.evidence_pub_count IS NULL) AS unknown,
       count(*) FILTER (WHERE rp.evidence_pub_count = 0)    AS ungrounded,
       count(*) FILTER (WHERE rp.coverage_suspect)          AS suspect,
       count(*) AS total
FROM agents ag JOIN researcher_profiles rp ON rp.user_id = ag.user_id
WHERE ag.status='active' AND ag.role='pi_lab';"
```

Expected: `unknown = 0`, `ungrounded = 0`, `suspect = 0`, `total = 62`.

- [ ] **Step 11: Restart the simulation**

The agent image bakes `src/` and loads modules only at startup. **Ask the user before
restarting** — this interrupts a live run.

```bash
DC="docker compose -f docker-compose.prod.yml"
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
docker stop -t 30 blackbird-agent-run && docker rm blackbird-agent-run
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

Expected: startup banner reflects the new image. Confirm the container is this repo's:
`docker inspect blackbird-agent-run --format '{{index .Config.Labels "com.docker.compose.project"}}'`
must print `copi-blackbird`.

---

## Self-review

**Spec coverage:** D1→Tasks 4,7. D2→Task 5. D3→Task 2 (rank-before-cap) + Task 4
(`fetch_years` dates S3/S4 hits before ranking) + Task 9 step 6. D4→Task 4 (counted +
warned). D5→Task 6 + Task 9 steps 4/9. D6→Task 1 (parser) + Task 8 kept-row re-fetch
(existing rows). D7→Task 2. D8→Task 7 (grants) + Tasks 5,7 (evidence counts).
P1→Tasks 2-5,7 (one resolver, disambiguation included — not an optional hook).
P2→Tasks 4,5 (computed + **persisted**, migration 0026) + Task 6 (enforced). P3→Task 6
(both activation branches, `pi_lab`-scoped, `coverage_suspect` included). P4→Task 1.
P5→Task 2. P6→Task 7. §4.3 no-unfiltered-S4 / no-name-only-S4→Tasks 3,4. §4.5 Tier D
re-slice→Task 8 (`diff_pmids` removals) + Task 9 step 6. §4.4 Tier A human gate→Task 9
steps 4 (demotion **before** any apply) and 9. §4.6 kept-row repair→Task 8. No spec
section is unimplemented.

**Placeholder scan:** No TBDs. Every code step carries real code. The earlier draft's
"sync vs async `disambiguate` hook" decision is gone: the hook is awaitable, defaults to
`disambiguate_pmids`, and `None` means "use the default", never "skip".

**Type consistency:** `CorpusCandidate(pmid, year, title, source)` is constructed
identically in Tasks 3 and 4. `merge_and_rank(candidates, cap)` and
`assess_coverage(retrieved, ceiling, cap)` keep their Task 2/4 signatures at every call
site. `CorpusResult.retrieved` is used consistently in Tasks 5, 7, 8;
`CorpusResult.pmid_to_orcid_doi` feeds `reconcile_pub_doi` in Task 5 without a second
ORCID fetch. `resolve_corpus`'s `cap` is `DEFAULT_CAP` (50) in Task 5 and
`PUBMED_FETCH_CAP` (50) in Task 7 — the same value from the two modules' own constants.

**Known risks, stated rather than hidden:**

- `fetch_years` (Task 4) adds one esummary batch per resolution (~1 request per 200
  undated PMIDs, inside `_ncbi_get`'s rate limiter). The earlier draft ranked S3/S4 hits
  at `year=None` and claimed a second `--apply` pass would "converge on stored years" —
  that was circular (the resolver never reads the DB, so a second pass has identical
  inputs and an empty diff) and is withdrawn; dating before ranking is the fix.
- `audit.n_pubmed_hits` changes meaning in Task 7 (raw ESearch hits → ceiling estimate).
  Anything consuming old seeder audit JSON comparatively should know.
- After Task 5, every `monthly_refresh` job resolves through the full union
  (ORCID + OpenAlex + two PubMed searches + disambiguation efetches) instead of ORCID
  alone — roughly 3-5× the NCBI/OpenAlex traffic per profile, all rate-limited through
  `_ncbi_get`'s semaphore. Acceptable for 62 PIs on a monthly cadence; revisit before
  scaling the roster 10×.
