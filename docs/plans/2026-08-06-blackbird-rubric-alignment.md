# BlackbirdBot Rubric Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make BlackbirdBot actually screen opportunities against
`data/Blackbird_initial_priorities-criteria_v1.pdf` — fix the structurally-broken prior-art
search, drive the interview off the rubric instead of the PI-lab collaboration script, and
turn each assessment into a durable, machine-readable artifact staff can triage.

**Architecture:** Three independent layers. (1) `src/services/patents.py` gains a
progressive term-backoff so a long free-text query degrades to a broader title search
instead of a guaranteed zero-hit "clean" result. (2) Per-role prompt resolution is extended
to phases 2 and 4, and the hardcoded phase-4 guidance moves into a new
`src/agent/thread_guidance.py` with a `scout_hub` branch — the `pi_lab` branch stays
byte-identical so the syrupy snapshots stay green. (3) The Phase-5 assessment gains a
`<assessment_json>` sidecar that is stripped from the Slack body, parsed, scored
server-side, and persisted to a new `opportunity_assessments` table surfaced at
`/admin/assessments`.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, FastAPI +
Jinja2, httpx, pytest + pytest-asyncio + respx + syrupy, ruff.

## Global Constraints

- **`pi_lab` behaviour must stay byte-identical.** `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`
  pins the exact phase-4 guidance strings. Copy them verbatim; do not reflow, retype, or
  "improve" the wording. Never run `pytest --snapshot-update` to make a failure go away —
  a snapshot diff in this plan means you changed pi_lab by accident.
- **Gate:** `./scripts/ci.sh` must pass before every commit. Test-suite ruff findings must
  be **zero**; `src/` findings must stay at or below `SRC_LINT_MAX=260`. Never raise the
  ceiling to make a push succeed.
- **Alembic:** exactly one head. New revision is `0025`, `down_revision = "0024"`.
  Downgrades use `if_exists=True` per the 0022/0023/0024 convention.
- **Docker:** always pass `-f docker-compose.prod.yml`. **Never** pass `--remove-orphans`
  (it has killed the co-tenant org1 stack). This repo's containers are
  `blackbird-app` / `blackbird-agent-run`; the unprefixed `agent-run` belongs to org1 —
  verify with `docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project"}}'`
  (`copi-blackbird` = ours).
- **The agent image bakes `src/`.** `prompts/` and `profiles/` are bind-mounted (so
  prompt-only tasks reach production live), but any `src/` change needs
  `$DC --profile agent build agent` before it runs. Task 13 owns the deploy.
- **Rubric source of truth:** `data/Blackbird_initial_priorities-criteria_v1.pdf` Part C,
  transcribed in `profiles/private/blackbird.md`. Weights, exactly: differentiation 20,
  market_unmet_need 15, team 15, external_signals 15, ip_fto 10, platform 8,
  dev_regulatory_feasibility 7, workplan_capital_efficiency 5, exit_thesis 5. Bands:
  ≥4.0 advance, 3.0–3.9 conditional, <3.0 pass.
- **Recommendation vocabulary, exactly:** `advance | conditional | pass | route-to-incubation`.
- **Funnel stage vocabulary, exactly:** `incubation | pre-seed | seed | follow-on`.

---

## File Structure

**Create:**
- `src/agent/thread_guidance.py` — role-aware phase-4 `(thread_phase, phase_guidance, instructions)`. Pure functions, no I/O, no ORM.
- `src/services/blackbird_rubric.py` — the weight table, `weighted_score()`, `band()`. Pure arithmetic so the score is computed, never trusted from the LLM.
- `src/models/opportunity.py` — the `OpportunityAssessment` ORM model.
- `alembic/versions/0025_add_opportunity_assessments.py`
- `prompts/roles/scout_hub/phase4-thread-reply.md` — scouting-interview phase-4 template.
- `templates/admin/assessments.html`
- `tests/unit/test_thread_guidance.py`, `tests/unit/test_blackbird_rubric.py`, `tests/unit/test_assessment_sidecar.py`, `tests/integration/test_opportunity_assessment_persistence.py`

**Modify:**
- `src/services/patents.py` — term backoff + `PriorArtResult`.
- `src/agent/tools.py:109-113` (caveat), `:92-106` (tool description), `:277-308` (`_execute_search_prior_art`).
- `src/agent/agent.py:352` and `:424` (`_load_file` → `_load_prompt`), `:429-476` (delegate to `thread_guidance`).
- `src/agent/simulation.py:3074` (strip sidecar), `:2299-2301` (persist), plus a new module-level extractor near `_extract_slack_message` (`:4973`).
- `src/models/__init__.py` — register the model.
- `src/routers/admin.py` — `/admin/assessments` route.
- `templates/base.html:104` — nav link.
- `prompts/roles/scout_hub/agent-system.md`, `prompts/roles/scout_hub/phase5-new-post.md`.
- `tests/unit/test_patents.py:162-176` (the AND-query test), `:192-200`, `:213-225`.

---

### Task 1: Prior-art term backoff

Today `search_prior_art` ANDs *every* query token against the invention title only. All 12
production searches returned zero hits for this reason — measured live: `TFEB` → 10 hits,
`C9orf72 repeat` → 10 hits, but `TFEB inhibitor nuclear translocation melanoma BRAF resistance`
→ 0 and `MARK2 kinase inhibitor RAN translation C9orf72 repeat expansion ALS FTD` → 0. The
fix is to retry with progressively fewer, more specific terms and report which breadth
produced the answer.

**Files:**
- Modify: `src/services/patents.py`
- Test: `tests/unit/test_patents.py`

**Interfaces:**
- Produces: `PriorArtResult` (frozen dataclass, fields `hits: list[dict]`, `terms_used: list[str]`, `total_terms: int`, property `broadened: bool`); `search_prior_art(query: str, limit: int = 10) -> PriorArtResult | None`. `None` still means "could not search". Task 2 consumes this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_patents.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_backs_off_to_fewer_terms_when_full_phrase_misses(monkeypatch):
    # A 7-token free-text query ANDed on the title matches nothing. The search must
    # retry with the most specific terms rather than reporting a false clean.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    queries = []

    def _capture(request):
        import json
        q = json.loads(request.content)["q"]
        queries.append(q)
        # EXACT match, not substring: the full-phrase tier also contains both
        # tokens, so a substring test would match on the first attempt and the
        # backoff would never be exercised.
        if q == "applicationMetaData.inventionTitle:(TFEB AND BRAF)":
            return httpx.Response(200, json=_ODP_ONE_HIT)
        return httpx.Response(200, json={"patentFileWrapperDataBag": []})

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    result = await patents.search_prior_art(
        "TFEB inhibitor nuclear translocation melanoma BRAF resistance"
    )
    assert len(result.hits) == 1
    assert result.terms_used == ["TFEB", "BRAF"]
    assert result.total_terms == 7  # counts the raw query tokens, generics included
    assert result.broadened is True
    assert len(queries) == 3  # full phrase, top-3, top-2


@pytest.mark.asyncio
@respx.mock
async def test_no_backoff_needed_when_full_phrase_hits(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    route = respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_ONE_HIT)
    )
    result = await patents.search_prior_art("gene editing widget")
    assert result.broadened is False
    assert result.total_terms == 3
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_all_tiers_empty_reports_the_narrowest_breadth_tried(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"patentFileWrapperDataBag": []})
    )
    result = await patents.search_prior_art("alpha beta gamma delta epsilon")
    assert result.hits == []
    assert len(result.terms_used) == 2  # floored at two terms
    assert result.broadened is True


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_mid_backoff_returns_none_not_a_clean_result(monkeypatch):
    # A 429 on any tier must read as "could not search", never as novelty.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    calls = {"n": 0}

    def _capture(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"patentFileWrapperDataBag": []})
        return httpx.Response(429)

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    assert await patents.search_prior_art("alpha beta gamma delta") is None


def test_generic_terms_lose_to_specific_ones():
    ranked = patents._rank_terms(
        ["treatment", "C9orf72", "inhibitor", "MARK2", "disease"]
    )
    # Set comparison: which two survive is the invariant; their relative order is
    # an arbitrary tie-break not worth pinning.
    assert set(ranked[:2]) == {"C9orf72", "MARK2"}
    assert "treatment" not in ranked
    assert "inhibitor" not in ranked


def test_single_token_query_uses_one_tier():
    assert patents._tiers(["TFEB"]) == [["TFEB"]]


def test_first_tier_is_the_query_as_asked_in_original_order():
    # Only the backoff tiers reorder by salience. Tier 1 is the user's phrase.
    tiers = patents._tiers(["CRISPR", "base", "editing"])
    assert tiers[0] == ["CRISPR", "base", "editing"]
```

Replace the existing `test_query_ands_title_tokens` body (currently at
`tests/unit/test_patents.py:162`) so it asserts the *first* tier still ANDs every token —
the precision behaviour that backoff must not regress:

```python
@pytest.mark.asyncio
@respx.mock
async def test_first_tier_ands_all_title_tokens(monkeypatch):
    # The first attempt must still AND every token (OR returns tens of thousands of
    # junk matches on common words). Backoff only widens AFTER that misses.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    captured = []

    def _capture(request):
        import json
        captured.append(json.loads(request.content)["q"])
        return httpx.Response(200, json={"patentFileWrapperDataBag": []})

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    await patents.search_prior_art("CRISPR base editing")
    assert captured[0] == "applicationMetaData.inventionTitle:(CRISPR AND base AND editing)"
```

Every other existing test in this file that reads `hits[0][...]` or `hits == []` must be
updated to `result.hits[0][...]` / `result.hits == []`. Those are:
`test_search_returns_normalised_hits`, `test_enriches_abstract_and_first_claim_from_pgpub_xml`,
`test_fulltext_fetch_failure_leaves_hit_title_level`, `test_searched_but_empty_returns_empty_list`,
`test_404_no_matches_returns_empty_list`. The `None`-returning tests
(`test_missing_key_returns_none_and_does_not_call`, `test_http_error_returns_none`,
`test_rate_limited_returns_none`, `test_bad_json_returns_none`) are unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_patents.py -v`
Expected: FAIL — `AttributeError: module 'src.services.patents' has no attribute '_rank_terms'`
and `AttributeError: 'list' object has no attribute 'hits'`.

- [ ] **Step 3: Implement the backoff**

In `src/services/patents.py`, add `from dataclasses import dataclass` to the imports, then
insert after the `_Q_SANITISE` definition (currently line 32):

```python
@dataclass(frozen=True)
class PriorArtResult:
    """A completed title search. ``None`` from search_prior_art still means the
    search could not run at all — see that function's contract.

    ``terms_used`` is the breadth that produced ``hits``: when it is shorter than
    ``total_terms`` the query was broadened because the full phrase matched nothing,
    and the caller MUST say so rather than presenting the result as on-point.
    """

    hits: list[dict[str, Any]]
    terms_used: list[str]
    total_terms: int

    @property
    def broadened(self) -> bool:
        return len(self.terms_used) < self.total_terms


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

# Floor on breadth. One term is too broad to be informative for a multi-concept
# idea; two ANDed specific terms is the widest search worth reporting.
_MIN_TERMS = 2


def _salience(token: str) -> tuple[int, int, str]:
    """Rank key: gene/target symbols beat prose. Deterministic (ties break on the
    token itself) so the query sent to USPTO is reproducible across runs."""
    score = 0
    if any(ch.isdigit() for ch in token):
        score += 3  # C9orf72, MARK2, PE38, HER3
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
    """Breadths to try, widest first, at most three HTTP calls (the ODP
    rate-limits aggressively — a 429 costs us the whole search).

    Tier 1 is the query EXACTLY as asked, in the caller's own order: that is
    the precise search, and preserving it means the backoff only ever widens.
    Later tiers drop generic words and keep the most specific terms.
    """
    ranked = _rank_terms(tokens)
    tiers = [list(tokens)]
    for width in (3, _MIN_TERMS):
        if width < len(tokens) and width <= len(ranked):
            candidate = ranked[:width]
            if candidate not in tiers:
                tiers.append(candidate)
    return tiers
```

Extract the single HTTP attempt. Add above `search_prior_art`:

```python
async def _search_titles(
    client: httpx.AsyncClient, terms: list[str], limit: int, key: str
) -> list[dict[str, Any]] | None:
    """One title search. ``None`` == rate limited (caller must treat as unavailable);
    ``[]`` == searched, matched nothing. Each hit carries ``_pgpub_uri`` for the
    optional full-text enrichment, which the caller pops before returning.
    """
    body = {
        "q": "applicationMetaData.inventionTitle:(%s)" % " AND ".join(terms),
        "pagination": {"offset": 0, "limit": max(1, min(limit, 50))},
        "sort": [{"field": "applicationMetaData.filingDate", "order": "desc"}],
    }
    resp = await client.post(SEARCH_URL, json=body, headers={"X-API-KEY": key})
    if resp.status_code == 429:
        logger.warning("[patents] rate limited (429) — treating as unavailable")
        return None
    # ODP answers 404 (not 200-with-empty) when a valid search matches nothing.
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()

    hits: list[dict[str, Any]] = []
    for entry in data.get("patentFileWrapperDataBag", []) or []:
        meta = entry.get("applicationMetaData", {}) or {}
        number = (
            meta.get("earliestPublicationNumber")
            or entry.get("applicationNumberText")
            or ""
        )
        hits.append({
            "patent_id": number,
            "title": meta.get("inventionTitle", ""),
            "date": meta.get("earliestPublicationDate") or meta.get("filingDate", ""),
            "applicant": meta.get("firstApplicantName", ""),
            "inventor": meta.get("firstInventorName", ""),
            "status": meta.get("applicationStatusDescriptionText", ""),
            "abstract": "",
            "claim": "",
            "_pgpub_uri": (entry.get("pgpubDocumentMetaData") or {}).get("fileLocationURI"),
        })
    return hits


async def _enrich(client: httpx.AsyncClient, hits: list[dict[str, Any]]) -> None:
    """Add abstract + first claim to the top few published hits, in place. Bounded
    and best-effort so a slow or missing XML never fails the search."""
    for i, hit in enumerate(hits):
        uri = hit.pop("_pgpub_uri", None)
        if uri and i < _FULLTEXT_MAX:
            hit["abstract"], hit["claim"] = await _fetch_fulltext(client, uri)
    for hit in hits:
        hit.pop("_pgpub_uri", None)
```

Replace the body of `search_prior_art` (keep the module docstring above it) with:

```python
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
      the query was widened and the hits may be adjacent rather than on point.
    """
    key = _api_key()
    if not key:
        logger.info("[patents] no USPTO API key configured — cannot search")
        return None
    tokens = _Q_SANITISE.sub(" ", query or "").split()
    if not tokens:
        return PriorArtResult(hits=[], terms_used=[], total_terms=0)

    tiers = _tiers(tokens)
    hits: list[dict[str, Any]] = []
    terms_used = tiers[-1]
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for terms in tiers:
                attempt = await _search_titles(client, terms, limit, key)
                if attempt is None:
                    return None
                if attempt:
                    hits, terms_used = attempt, terms
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
    return PriorArtResult(hits=hits, terms_used=list(terms_used), total_terms=len(tokens))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_patents.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add src/services/patents.py tests/unit/test_patents.py
git commit -m "fix(patents): back off to specific terms — a title AND of free text is a guaranteed zero-hit"
```

---

### Task 2: Report search breadth and correct the caveat

The bot has been telling PIs "no issued US patents found (caveat: US filings only via
PatentsView)". Two things are wrong: the source is the USPTO Open Data Portal, not
PatentsView (decommissioned 2026-03-20), and the caveat omits the limitation that actually
matters — it is a **title-only** search.

**Files:**
- Modify: `src/agent/tools.py`
- Test: `tests/unit/test_patents.py` (the tool-level tests at the bottom of the file)

**Interfaces:**
- Consumes: `PriorArtResult` from Task 1.
- Produces: `_scope_note(result: PriorArtResult) -> str`; `_execute_search_prior_art` output text.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_patents.py`, and update the three existing tool tests to build a
`PriorArtResult` instead of a bare list:

```python
from src.services.patents import PriorArtResult  # noqa: E402


@pytest.mark.asyncio
async def test_caveat_states_title_only_and_the_real_source(monkeypatch):
    from src.agent import tools as tools_mod
    monkeypatch.setattr(
        tools_mod, "search_prior_art",
        lambda q, limit=10: _fake(PriorArtResult([], ["widget"], 1)),
    )
    out = await _execute_search_prior_art("widget")
    assert "TITLE ONLY" in out
    assert "USPTO Open Data Portal" in out
    assert "PatentsView" not in out
    assert "freedom-to-operate" in out.lower()


@pytest.mark.asyncio
async def test_broadened_search_is_flagged_as_broader_than_asked(monkeypatch):
    from src.agent import tools as tools_mod
    result = PriorArtResult([], ["TFEB", "BRAF"], 7)
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake(result))
    out = await _execute_search_prior_art("TFEB inhibitor melanoma BRAF resistance x y")
    assert "BROADER" in out
    assert "2 most specific of your 7" in out


@pytest.mark.asyncio
async def test_unbroadened_search_reports_the_terms_plainly(monkeypatch):
    from src.agent import tools as tools_mod
    result = PriorArtResult([], ["gene", "editing"], 2)
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake(result))
    out = await _execute_search_prior_art("gene editing")
    assert "BROADER" not in out
    assert "gene AND editing" in out


def test_tool_description_demands_a_short_specific_query():
    spec = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_prior_art")
    text = spec["description"] + spec["input_schema"]["properties"]["query"]["description"]
    assert "2-4" in text
    assert "title" in text.lower()
```

Update the three existing tool tests — `test_searched_empty_carries_caveat_and_says_no_matches`,
`test_unavailable_when_search_could_not_run`, `test_output_carries_caveat_and_fields_on_has_hits_path`
— to wrap their fixtures: `_fake([])` becomes `_fake(PriorArtResult([], ["crispr", "delivery"], 2))`,
and `_fake([hit])` becomes `_fake(PriorArtResult([hit], ["widget"], 1))`. `_fake(None)` is unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_patents.py -v -k "caveat or broadened or description"`
Expected: FAIL — `assert "TITLE ONLY" in out` and `AttributeError: 'list' object has no attribute 'hits'`.

- [ ] **Step 3: Implement**

In `src/agent/tools.py`, replace `_PATENT_CAVEAT` (lines 109-113) with:

```python
_PATENT_CAVEAT = (
    "Source: USPTO Open Data Portal (api.uspto.gov), US filings only, matched on "
    "INVENTION TITLE ONLY — abstracts and claims are NOT searched. Absence of a hit "
    "is weak evidence at best: it does not cover EP/WO/JP filings, unpublished "
    "applications, non-patent prior art, or any patent whose title happens to use "
    "different words. NEVER report a clean title search as novelty or as "
    "freedom-to-operate; report it as what it is, a title search that found nothing.\n\n"
)
```

Replace the `search_prior_art` entry in `TOOL_DEFINITIONS` (lines 92-106) description
strings with:

```python
    {
        "name": "search_prior_art",
        "description": (
            "Search issued and published US patent filings (USPTO Open Data Portal) "
            "for prior art. Matches on INVENTION TITLE ONLY. Pass 2-4 highly specific "
            "terms — gene/target symbols, a compound name, a modality — NOT a sentence. "
            "A long descriptive query cannot match any real patent title and will come "
            "back empty no matter how crowded the field is. Good: 'TFEB melanoma'. "
            "Bad: 'TFEB inhibitor nuclear translocation melanoma BRAF resistance'. "
            "US filings only — absence of a hit is NOT proof of novelty or FTO."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "2-4 specific terms matched against the patent title (e.g. "
                        "'C9orf72 repeat', 'TFEB melanoma'). Not a description."
                    ),
                },
            },
            "required": ["query"],
        },
    },
```

Add `_scope_note` above `_execute_search_prior_art`, and rewrite that function:

```python
def _scope_note(result: "PriorArtResult") -> str:
    """Tell the model what breadth actually produced this answer. A broadened
    search must never be read as an on-point clean result."""
    if not result.terms_used:
        return ""
    terms = " AND ".join(result.terms_used)
    if result.broadened:
        return (
            f"SCOPE: your full phrase matched no title, so this searched the "
            f"{len(result.terms_used)} most specific of your {result.total_terms} "
            f"terms ({terms}). That is a BROADER search than you asked for — any hits "
            f"may be adjacent rather than on point, and an empty result at this "
            f"breadth is the strongest negative this tool can give you (still not FTO).\n\n"
        )
    return f"SCOPE: searched titles for {terms}.\n\n"


async def _execute_search_prior_art(query: str) -> str:
    """Search the USPTO ODP for prior art.

    Distinguishes three outcomes so the hub never mistakes an unreachable/unconfigured
    tool for a clean novelty result:
      * ``None``        → the search could not run → an explicit UNAVAILABLE notice;
      * empty ``hits``  → the search ran and matched nothing → caveat + scope + "no matches";
      * results         → caveat + scope + the filings.
    """
    result = await search_prior_art(query)
    if result is None:
        return _PATENT_UNAVAILABLE
    preamble = _PATENT_CAVEAT + _scope_note(result)
    if not result.hits:
        return preamble + "No US filings matched this query."
    lines = [preamble]
    for h in result.hits:
        applicant = h.get("applicant") or "Unknown applicant"
        inventor = h.get("inventor") or "Unknown inventor"
        status = h.get("status") or ""
        block = (
            f"{h.get('patent_id','')} ({h.get('date','')}) — {h.get('title','')}\n"
            f"  applicant: {applicant} | inventor: {inventor}"
            + (f" | status: {status}" if status else "")
        )
        if h.get("abstract"):
            block += f"\n  abstract: {h['abstract']}"
        if h.get("claim"):
            block += f"\n  claim 1: {h['claim']}"
        # Title/applicant/abstract/claim come from the USPTO API — untrusted
        # external text (SEC-14); fence it.
        lines.append(delimit(block, "patent"))
    return "\n\n".join(lines)
```

Add the import for the type annotation at the top of the file alongside the existing
`from src.services.patents import search_prior_art`:

```python
from src.services.patents import PriorArtResult, search_prior_art
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_patents.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools.py tests/unit/test_patents.py
git commit -m "fix(tools): a title-only search is not FTO — say so, and report the breadth used"
```

---

### Task 3: Correct the prior-art caveat in the scout_hub prompts

The prompts instruct the bot to attach a caveat naming "PatentsView/USPTO" and "US filings
only" — stale on the source and silent on the title-only limitation, which is the one that
made all 12 searches empty.

**Files:**
- Modify: `prompts/roles/scout_hub/agent-system.md`, `prompts/roles/scout_hub/phase5-new-post.md`
- Test: `tests/unit/test_roles.py`

**Interfaces:**
- Consumes: nothing. Prompt-only; reaches production via the `prompts/` bind mount without a rebuild.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_roles.py`:

```python
def test_scout_hub_prompts_state_the_title_only_limitation():
    """The hub's whole novelty read rests on this tool. The prompt must not let it
    describe a title search as though it covered claims, and must not name the
    decommissioned PatentsView endpoint."""
    from pathlib import Path

    for name in ("agent-system.md", "phase5-new-post.md"):
        text = Path("prompts/roles/scout_hub") / name
        body = text.read_text(encoding="utf-8")
        assert "PatentsView" not in body, f"{name} still names the dead endpoint"
        assert "title" in body.lower(), f"{name} omits the title-only limitation"
    system = (Path("prompts/roles/scout_hub") / "agent-system.md").read_text(encoding="utf-8")
    assert "freedom-to-operate" in system.lower()
    assert "2-4" in system
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v -k title_only`
Expected: FAIL — `agent-system.md still names the dead endpoint`.

- [ ] **Step 3: Edit the prompts**

In `prompts/roles/scout_hub/agent-system.md`, replace principle 2 under "Core Principles"
(currently lines 48-52) with:

```markdown
2. **Honest novelty.** Ground your novelty read in what you actually checked. `search_prior_art`
   matches the **invention title only** on the USPTO Open Data Portal — not abstracts, not
   claims — and covers US filings only. So:
   - Query with **2-4 specific terms** (a gene/target symbol, a compound, a modality).
     A sentence-length query cannot match any real patent title and comes back empty no
     matter how crowded the field is. "TFEB melanoma", not "TFEB inhibitor nuclear
     translocation melanoma BRAF resistance".
   - If the tool reports it **broadened** your query, say so — those hits are adjacent,
     not necessarily on point.
   - An empty title search is **never** novelty and **never** freedom-to-operate. Report
     it as "a US title search on [terms] found nothing", with the limitation attached.
   - If you did not check prior art, say so plainly rather than implying a novelty read
     you haven't earned.
```

Replace the `search_prior_art` bullet in the "Tools" section (currently lines 158-161) with:

```markdown
- **`search_prior_art(query)`** — Search US patent filings (USPTO Open Data Portal) by
  **invention title only**. Use **2-4 specific terms**, never a sentence. Always report
  the limitation alongside any result: title-only, US-only, so no hit is not evidence of
  novelty or freedom-to-operate — the filing may be foreign or unpublished, the title may
  use different words, or it may simply be unfiled anywhere.
```

In `prompts/roles/scout_hub/phase5-new-post.md`, replace item 2 of the Option C section
(currently lines 125-129) with:

```markdown
2. **Novelty read.** What you found (or didn't) when you checked. If you ran
   `search_prior_art`, state the exact terms searched and the result, and **always attach
   the limitation: USPTO Open Data Portal, invention title only, US filings only** — no US
   title hit is not evidence the idea is unclaimed abroad, in the claims of a
   differently-titled patent, or in the non-patent literature. If the tool broadened your
   query, say so. If you did not check prior art, say so plainly.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v`
Expected: PASS — including the pre-existing `test_scout_hub_phase5_override_renders_in_both_modes`.

- [ ] **Step 5: Commit**

```bash
git add prompts/roles/scout_hub/agent-system.md prompts/roles/scout_hub/phase5-new-post.md tests/unit/test_roles.py
git commit -m "docs(scout_hub): the prior-art tool is a title search on ODP, not a PatentsView claims search"
```

---

### Task 4: Route phases 2 and 4 through role-aware prompt resolution

`agent.py:352` and `:424` call `_load_file(PROMPTS_DIR / ...)` directly, so a
`prompts/roles/<role>/phase2-scan-filter.md` or `phase4-thread-reply.md` override is
silently ignored. Task 6 needs this to work.

**Files:**
- Modify: `src/agent/agent.py:352-355`, `:424-427`
- Test: `tests/unit/test_agent_prompts.py`

**Interfaces:**
- Produces: role overrides for `phase2-scan-filter.md` and `phase4-thread-reply.md` now resolve. Task 6 consumes this.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_agent_prompts.py`:

```python
def test_phase2_and_phase4_honour_role_overrides(tmp_path, monkeypatch):
    """Every other phase resolves per-role; these two were hardcoded to the global
    file, so a role override was accepted into the repo and then ignored."""
    from src.agent import roles as roles_mod
    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    prompts = tmp_path / "prompts"
    (prompts / "roles" / "widget").mkdir(parents=True)
    (prompts / "phase2-scan-filter.md").write_text("GLOBAL SCAN {posts}", encoding="utf-8")
    (prompts / "phase4-thread-reply.md").write_text("GLOBAL REPLY", encoding="utf-8")
    (prompts / "roles" / "widget" / "phase2-scan-filter.md").write_text(
        "WIDGET SCAN {posts}", encoding="utf-8"
    )
    (prompts / "roles" / "widget" / "phase4-thread-reply.md").write_text(
        "WIDGET REPLY", encoding="utf-8"
    )
    monkeypatch.setattr(roles_mod, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(roles_mod, "ROLES_DIR", prompts / "roles")

    agent = Agent("w", "WBot", "W Lab", role="widget")
    _, scan_messages = agent.build_phase2_scan_prompt(
        [{"post_id": "p1", "channel": "general", "sender": "x", "content_snippet": "s"}]
    )
    assert "WIDGET SCAN" in scan_messages[0]["content"]

    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="o", message_count=1)
    _, reply_messages = agent.build_phase4_prompt(
        thread=thread,
        thread_history=[{"sender": "o", "content": "hello"}],
        other_agent_name="OBot",
        other_agent_lab="O Lab",
    )
    assert "WIDGET REPLY" in reply_messages[0]["content"]
```

The method is `build_phase4_prompt` (`src/agent/agent.py:400`). Its remaining parameters
(`is_funding_thread`, `your_prior_messages`, `thread_activity_summary`, `visibility`,
`channel_id`) all default, so the four above are sufficient. Do not change the signature.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_prompts.py -v -k role_overrides`
Expected: FAIL — `assert "WIDGET SCAN" in "GLOBAL SCAN ..."`.

- [ ] **Step 3: Implement**

`src/agent/agent.py` line 352, change:

```python
        phase2_template = self._load_file(
            PROMPTS_DIR / "phase2-scan-filter.md",
            "Evaluate posts and return JSON with selected_post_ids.",
        )
```

to:

```python
        phase2_template = self._load_prompt(
            "phase2-scan-filter.md",
            "Evaluate posts and return JSON with selected_post_ids.",
        )
```

Line 424, change:

```python
        phase4_template = self._load_file(
            PROMPTS_DIR / "phase4-thread-reply.md",
            "Compose a thread reply.",
        )
```

to:

```python
        phase4_template = self._load_prompt(
            "phase4-thread-reply.md",
            "Compose a thread reply.",
        )
```

No role ships an override for either file yet, so `pi_lab` **and** `scout_hub` both still
resolve to `prompts/<file>` — the snapshots must not move.

- [ ] **Step 4: Run the tests to verify they pass, and the snapshots to verify nothing moved**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_prompts.py tests/characterization -v`
Expected: PASS, zero snapshot diffs. If a snapshot fails here, you changed resolution for
`pi_lab` — fix the code, do not update the snapshot.

- [ ] **Step 5: Commit**

```bash
git add src/agent/agent.py tests/unit/test_agent_prompts.py
git commit -m "fix(agent): phases 2 and 4 must honour role prompt overrides like every other phase"
```

---

### Task 5: Role-aware phase-4 guidance

The strings that actually drive the interview are hardcoded in Python with no role branch.
BlackbirdBot — which has no lab — is told to "Share relevant specifics from your lab's
recent work", to judge "genuine complementarity", and at message 12 to "Post a `:memo:`
Summary with a collaboration proposal". Production evidence: the 11:08Z Wang thread closed
with the bot saying *"forcing a :memo: Summary would misrepresent what we discussed"*, and
the rubric-grade read it had written a minute earlier was stranded as a thread reply.

**Files:**
- Create: `src/agent/thread_guidance.py`
- Modify: `src/agent/agent.py:429-476` (inside `build_phase4_prompt`, which starts at `:400`)
- Test: `tests/unit/test_thread_guidance.py`

**Interfaces:**
- Produces: `phase4_guidance(role: str, message_count: int) -> tuple[str, str, str]` returning `(thread_phase, phase_guidance, instructions)`. `thread_phase` is one of `"EXPLORE" | "DECIDE" | "MUST CONCLUDE"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_thread_guidance.py`:

```python
import pytest

from src.agent.thread_guidance import phase4_guidance


@pytest.mark.parametrize("count,expected", [(1, "EXPLORE"), (4, "EXPLORE"),
                                           (5, "DECIDE"), (11, "DECIDE"),
                                           (12, "MUST CONCLUDE"), (99, "MUST CONCLUDE")])
def test_phase_boundaries_are_unchanged(count, expected):
    for role in ("pi_lab", "scout_hub"):
        assert phase4_guidance(role, count)[0] == expected


def test_pi_lab_strings_are_byte_identical_to_the_pinned_snapshot():
    # These exact strings are pinned in
    # tests/characterization/__snapshots__/test_agent_turn_gm.ambr. Any drift here
    # changes every PI bot's behaviour, which this refactor must not do.
    _, guidance, instructions = phase4_guidance("pi_lab", 5)
    assert guidance == (
        "You are in the DECIDE phase. Narrow the scope: is there genuine complementarity? "
        "Can you name a specific first experiment? If yes, build toward a :memo: Summary proposal. "
        "If no, start your reply with ⏸️ and explain graciously why there's no viable collaboration. "
        "It is OK to conclude with no proposal — not every conversation leads to one."
    )
    assert instructions == (
        "Write a reply that moves toward a conclusion. Either build toward a specific "
        ":memo: Summary proposal or acknowledge insufficient overlap."
    )


def test_unknown_role_falls_back_to_pi_lab():
    assert phase4_guidance("nonexistent", 5) == phase4_guidance("pi_lab", 5)


def test_scout_hub_never_asks_for_a_collaboration_proposal():
    for count in (1, 5, 12):
        _, guidance, instructions = phase4_guidance("scout_hub", count)
        blob = guidance + instructions
        assert ":memo:" not in blob
        assert "collaboration proposal" not in blob
        assert "your lab's recent work" not in blob
        assert "complementarity" not in blob


def test_scout_hub_decide_phase_works_the_gating_criteria():
    _, guidance, instructions = phase4_guidance("scout_hub", 5)
    blob = (guidance + instructions).lower()
    assert "baltimore" in blob
    assert "freedom-to-operate" in blob or "fto" in blob
    assert "differentiation" in blob
    # The measured failure: inferring the Baltimore gate from a JHU address.
    assert "jhu address" in blob or "institution is not" in blob
    # Part C.4 of the rubric — the target-level scientific checklist.
    assert "proof of mechanism" in blob


def test_scout_hub_conclusion_carries_the_verdict_and_names_the_artifact():
    phase, guidance, instructions = phase4_guidance("scout_hub", 12)
    assert phase == "MUST CONCLUDE"
    blob = guidance + instructions
    assert ":mag:" in blob
    assert "⏸️" in blob
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_thread_guidance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.agent.thread_guidance'`.

- [ ] **Step 3: Implement**

Create `src/agent/thread_guidance.py`:

```python
"""Per-role phase-4 thread guidance.

The EXPLORE/DECIDE/CONCLUDE strings used to be hardcoded in
src/agent/agent.py with no role branch, which meant the Blackbird scouting hub —
an agent with no lab and no collaborations to propose — was told to pitch its
lab's capabilities and to close every interview with a :memo: collaboration
proposal. See docs/plans/2026-08-06-blackbird-rubric-alignment.md (F3).

Dependency-free on purpose (no DB, no Agent import) so the branching is
unit-testable in isolation.

The ``pi_lab`` strings are BYTE-IDENTICAL to the pre-refactor literals and are
pinned by tests/characterization/__snapshots__/test_agent_turn_gm.ambr. Do not
reword them.
"""

from __future__ import annotations

EXPLORE = "EXPLORE"
DECIDE = "DECIDE"
CONCLUDE = "MUST CONCLUDE"

_PI_LAB = {
    EXPLORE: (
        "You are in the EXPLORE phase. Share relevant specifics from your lab's recent work. "
        "Ask clarifying questions about the other lab's capabilities. Use retrieve_profile and "
        "retrieve_abstract tools to learn more. Do NOT propose a full collaboration yet.",
        "Write a reply that shares specific details from your lab and asks a clarifying "
        "question. Use tools proactively to research the other lab.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Narrow the scope: is there genuine complementarity? "
        "Can you name a specific first experiment? If yes, build toward a :memo: Summary proposal. "
        "If no, start your reply with ⏸️ and explain graciously why there's no viable collaboration. "
        "It is OK to conclude with no proposal — not every conversation leads to one.",
        "Write a reply that moves toward a conclusion. Either build toward a specific "
        ":memo: Summary proposal or acknowledge insufficient overlap.",
    ),
    CONCLUDE: (
        "This is message 12 — you MUST conclude the thread now. Either post a :memo: Summary "
        "with a collaboration proposal, or close gracefully acknowledging insufficient overlap.",
        "This is the final message. You MUST either:\n"
        "1. Post a :memo: Summary with a specific collaboration proposal, OR\n"
        "2. If the other agent already posted a :memo: Summary you agree with AS-IS, reply with ✅ "
        "(no modifications — if you want changes, post your own revised :memo: Summary instead), OR\n"
        "3. Start your reply with ⏸️ and close gracefully explaining why there's no good proposal.\n\n"
        "Option 3 is perfectly acceptable — not every conversation should end in a proposal.",
    ),
}

_SCOUT_HUB = {
    EXPLORE: (
        "You are in the EXPLORE phase of a scouting interview. You have no lab and nothing "
        "to pitch — your job is to draw the PI out. Establish what the technology "
        "specifically IS (the compound, construct, dataset, assay, or method), and use "
        "retrieve_profile and retrieve_abstract to ground yourself in what this lab has "
        "actually published. Form a provisional read on where it sits on the Blackbird "
        "funnel (incubation / pre-seed / seed / follow-on), because that sets the evidence "
        "bar for everything after. Do NOT score it yet and do NOT offer an assessment.",
        "Write a reply that asks one specific question about the technology itself — what "
        "makes it different, what stage the evidence is at. Use tools proactively to ground "
        "yourself in this lab's publications before you ask.",
    ),
    DECIDE: (
        "You are in the DECIDE phase. Work the gating criteria explicitly — a 'no' on any "
        "of them blocks or heavily discounts the opportunity:\n"
        "- **Baltimore commitment.** ASK whether the PI would anchor a NewCo in Baltimore "
        "(ideally Blackbird BioHub) and keep forward activities there. A JHU address is NOT "
        "a Baltimore commitment — the institution is not the answer to this question, the "
        "founder is. Treat it as unconfirmed until the PI says it.\n"
        "- **Credible technology source** with a path to license the underlying IP.\n"
        "- **Freedom-to-operate** — any known encumbrance, co-ownership, or third-party "
        "blockade. Run search_prior_art with 2-4 specific terms (a gene/target symbol, a "
        "compound, a modality) — never a sentence — and read an empty title search as "
        "nothing more than an empty title search.\n"
        "Then probe the heaviest scoring dimensions: differentiation (first/best-in-class, "
        "not incremental), market size and actionable unmet need, team/founder quality, and "
        "external signals (VC interest, big-pharma interest or deal comps, a KOL who "
        "validates it). Ask about platform breadth versus single-asset risk. For a "
        "therapeutic or target proposal, work the target-level scientific checklist in "
        "your private instructions — clinical genetic evidence, animal-model rescue, "
        "in vitro functional data, available tool reagents and pharmacologic probes, "
        "whether selective modulation is achievable and by what modality, and whether "
        "proof of mechanism is established. If the idea clearly cannot clear the bar, "
        "start your reply with ⏸️ and say so specifically — an honest 'no' is more "
        "useful to Blackbird than an inflated maybe.",
        "Write a reply that closes the biggest gap in your screen. Ask about the gating "
        "criteria you still cannot answer — Baltimore commitment, licensable IP, FTO — or "
        "about differentiation, market, or external validation. One or two specific "
        "questions, not a questionnaire.",
    ),
    CONCLUDE: (
        "This is message 12 — you MUST conclude the interview now. Do NOT propose a "
        "collaboration; you are not a party to the science. Close with your verdict stated "
        "inline so nothing is lost: the funnel stage, which gating criteria are met versus "
        "unconfirmed, your recommendation (advance / conditional / pass / "
        "route-to-incubation), the red flags you saw, and a confidence label. If the idea "
        "warrants a standalone :mag: Opportunity Assessment, say that it will follow as its "
        "own post. If it does not, start your reply with ⏸️ and say specifically what would "
        "need to change.",
        "This is the final message. You MUST either:\n"
        "1. Close the interview with your inline verdict — funnel stage, gating status, "
        "recommendation (advance / conditional / pass / route-to-incubation), red flags, "
        "confidence label — noting that a standalone :mag: Opportunity Assessment will "
        "follow, OR\n"
        "2. Start your reply with ⏸️ and close gracefully, naming the specific missing "
        "piece that would make this assessable.\n\n"
        "Option 2 is perfectly acceptable — most interviews should end there. Never close "
        "by proposing that the two labs work together.",
    ),
}

_BY_ROLE = {"pi_lab": _PI_LAB, "scout_hub": _SCOUT_HUB}


def phase4_guidance(role: str, message_count: int) -> tuple[str, str, str]:
    """Return ``(thread_phase, phase_guidance, instructions)`` for ``role``.

    An unknown role degrades to ``pi_lab`` — the same "absence of overrides is
    pi_lab" rule src/agent/roles.py uses for prompt resolution.
    """
    if message_count <= 4:
        phase = EXPLORE
    elif message_count <= 11:
        phase = DECIDE
    else:
        phase = CONCLUDE
    guidance, instructions = _BY_ROLE.get(role, _PI_LAB)[phase]
    return phase, guidance, instructions
```

In `src/agent/agent.py`, add to the imports alongside the existing `roles` import:

```python
from src.agent.thread_guidance import phase4_guidance
```

Replace lines 429-476 — the `# Thread phase guidance` if/elif/else block **and** the
`# Build instructions based on phase` if/elif/else block — with:

```python
        # Thread phase guidance + instructions, per role. scout_hub scouts ideas
        # against Blackbird's screening rubric; it has no lab and never proposes a
        # collaboration. See src/agent/thread_guidance.py.
        thread_phase, phase_guidance, instructions = phase4_guidance(
            self.role, thread.message_count
        )
```

Leave everything after that untouched: the `history_text` construction, the
`cites_own_paper` append, the `pi_context` append, the funding-context block, and all the
`.replace()` calls still consume `thread_phase`, `phase_guidance` and `instructions`
exactly as before.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_thread_guidance.py tests/characterization -v`
Expected: PASS with zero snapshot diffs. A snapshot diff means a `pi_lab` string drifted —
diff it against the `.ambr` file and restore the original character-for-character.

- [ ] **Step 5: Commit**

```bash
git add src/agent/thread_guidance.py src/agent/agent.py tests/unit/test_thread_guidance.py
git commit -m "feat(scout_hub): drive the interview off the screening rubric, not the collaboration script"
```

---

### Task 6: scout_hub phase-4 template

The global template opens "You are continuing a conversation in a thread with another lab's
agent", drives toward a `:memo:` collaboration proposal ("What each lab brings", "Why this
collaboration beats either lab working alone"), and tells the agent `retrieve_foa` is
"**required** before replying to any :moneybag: funding post" — a tool `role.toml` withholds
and `tools_for_role` filters out. Measured: all 27 of BlackbirdBot's thread-reply calls
carried that instruction.

**Files:**
- Create: `prompts/roles/scout_hub/phase4-thread-reply.md`
- Test: `tests/unit/test_roles.py`

**Interfaces:**
- Consumes: role resolution for `phase4-thread-reply.md` (Task 4); `{phase_guidance}` / `{instructions}` (Task 5).
- Substitution tokens `build_phase4_thread_reply_prompt` replaces, all of which this file must contain: `{channel_name}`, `{other_agent_name}`, `{other_agent_lab}`, `{message_count}`, `{thread_phase}`, `{thread_history}`, `{funding_thread_context}`, `{phase_guidance}`, `{instructions}`, `{foa_number}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_roles.py`:

```python
def test_scout_hub_phase4_override_renders_and_drops_the_tool_it_lacks():
    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird Labs", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang", message_count=5
    )
    _, messages = agent.build_phase4_prompt(
        thread=thread,
        thread_history=[{"sender": "WangBot", "content": "our CRISPR screen hit DBT"}],
        other_agent_name="WangBot",
        other_agent_lab="Wang",
    )
    content = messages[0]["content"]

    # The override rendered, not a silent fallback to the pi_lab template.
    assert "scouting interview" in content.lower()
    # retrieve_foa is withheld from this role by role.toml — the prompt must not
    # tell the agent it is required, or available at all.
    assert "retrieve_foa" not in content
    # This role never brokers or proposes collaborations.
    assert ":memo:" not in content
    assert "beats either lab working alone" not in content
    # search_prior_art IS in this role's tool set and must be documented.
    assert "search_prior_art" in content
    # Every substitution token was consumed.
    for token in ("{channel_name}", "{other_agent_name}", "{other_agent_lab}",
                  "{message_count}", "{thread_phase}", "{thread_history}",
                  "{phase_guidance}", "{instructions}", "{foa_number}",
                  "{funding_thread_context}"):
        assert token not in content, f"leftover token {token!r}"
    # The Task 5 DECIDE guidance landed in the rendered prompt.
    assert "Baltimore commitment" in content
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v -k phase4_override`
Expected: FAIL — `assert "retrieve_foa" not in content` (the global template still renders).

- [ ] **Step 3: Create the template**

Create `prompts/roles/scout_hub/phase4-thread-reply.md`. The outer fence below is four
backticks because the file itself contains a three-backtick block — copy everything between
the four-backtick markers:

````markdown
# Phase 4: Scouting Interview Reply

You are continuing a **scouting interview** with one PI's lab agent. This is a
two-party conversation between you and exactly one lab. You have no lab of your own,
nothing to pitch, and you never broker introductions or propose collaborations —
your job is to draw the PI out and screen the idea against Blackbird's investment
priorities.

## Thread state

- **Channel:** #{channel_name}
- **Other agent:** {other_agent_name} ({other_agent_lab} lab)
- **Message count:** {message_count} of 12 max
- **Thread phase:** {thread_phase}
- **FOA Number:** {foa_number}

## Thread history

{thread_history}

{funding_thread_context}

## Phase guidance

{phase_guidance}

### If this thread is about a paper the other lab authored

That is the normal case — you are scouting their work. Cite it the way their public
profile does (DOI or PubMed link) and be specific about which result you are asking
about. Never characterise their work as more novel or more commercially advanced than
they have claimed.

### Funding threads

If the root post is a :moneybag: funding opportunity from GrantBot, or a
funding-originated collaboration between two labs, that thread exists so PI bots can
find co-applicants. **It is not a venue for scouting, and it is not yours to work.**
You have no FOA-fetching tool and you never fetch FOA text yourself — GrantBot posts
it, and what it has already surfaced in the thread is all you have to work with.
Reply only if you have a specific, grounded funding-fit observation about *one* PI's
idea and this FOA, reference the FOA number, and never tag a second lab. Otherwise
close your participation with ⏸️.

## Available tools

- `retrieve_profile(agent_id)` — the other agent's public profile
- `retrieve_abstract(pmid_or_doi)` — a paper abstract from PubMed
- `retrieve_full_text(pmid_or_doi)` — full text from PubMed Central (use sparingly)
- `search_prior_art(query)` — US patent filings (USPTO Open Data Portal), matched on
  **invention title only**. Pass **2-4 specific terms** — a gene/target symbol, a
  compound, a modality — never a sentence, which cannot match a real patent title.
  Always attach the limitation to any result you report: title-only, US-only, so an
  empty result is neither novelty nor freedom-to-operate.

Use tools proactively in the EXPLORE phase (messages 1–4). By the DECIDE phase (5+)
you should already have what you need.

## Instructions

{instructions}

## Output

Your final response MUST contain exactly one `<slack_message>` block. Everything
inside the block will be posted verbatim to Slack. Everything outside it is discarded.

```
<slack_message>
Your message here — written as it should appear in Slack.
</slack_message>
```

You may think/reason freely outside the block, but ONLY the content between
`<slack_message>` and `</slack_message>` tags will be posted.

Replies are 2-4 sentences unless you are concluding the interview. No
acknowledgment-only replies — "thanks", "sounds good", "noted" are forbidden. Every
reply must add a specific scouting question, a grounded novelty observation, or a
concrete screening judgement.

If you conclude the idea cannot clear Blackbird's bar, start your reply with ⏸️ and
say specifically why — which gating criterion fails, or what evidence is missing.
That closes the thread. If the other agent has already posted ⏸️, you may reply with
a brief ⏸️ acknowledgment, but no further replies after that.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py tests/characterization -v`
Expected: PASS. The characterization snapshots cover `pi_lab` only (verified: no
`scout_hub` fixture exists in `tests/characterization/`), so adding a `scout_hub`
override must not move them.

- [ ] **Step 5: Commit**

```bash
git add prompts/roles/scout_hub/phase4-thread-reply.md tests/unit/test_roles.py
git commit -m "feat(scout_hub): a scouting-interview phase-4 template without retrieve_foa or :memo:"
```

---

### Task 7: Rewrite the Phase-5 assessment to the C.6 rubric

The operative artifact template asks for five prose sections (idea, novelty, funding fit,
commercialization path, next step) — none of which are the PDF's rubric. Measured across 41
production messages: 0 weighted scores, 0 red-flag sections, 0 structured verdicts. The
funding-fit section pushes an NIH-mechanism frame where the PDF's dimension 8 is about
non-dilutive Maryland leverage (TEDCO MII, MSCRF, BIITC/QOF).

**Files:**
- Modify: `prompts/roles/scout_hub/phase5-new-post.md`, `prompts/roles/scout_hub/agent-system.md`
- Test: `tests/unit/test_roles.py`

**Interfaces:**
- Produces: an `<assessment_json>` block, outside `<slack_message>`, containing **bare JSON with no ``` fence**. Task 8 parses it. The fence matters: `_parse_phase5_response` takes the LAST ```json``` block as the action, so fencing the sidecar would hijack the action data.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_roles.py`:

```python
def test_scout_hub_assessment_follows_the_blackbird_rubric():
    from pathlib import Path

    body = (Path("prompts/roles/scout_hub") / "phase5-new-post.md").read_text(
        encoding="utf-8"
    )
    # C.1 gating, C.2 funnel, C.3 scores, C.5 red flags, C.6 verdict.
    for required in (
        "Funnel stage", "Gating criteria", "Red flags", "Recommendation",
        "route-to-incubation", "<assessment_json>", "weighted_score",
        "suggested_derisking_milestones",
    ):
        assert required in body, f"assessment template omits {required!r}"
    # The Baltimore gate is asked, never inferred from the institution.
    assert "JHU address is not" in body
    # Maryland non-dilutive leverage, not a generic NIH-mechanism frame.
    assert "TEDCO" in body and "BIITC" in body
    # The sidecar must NOT be fenced — _parse_phase5_response takes the last
    # ```json``` block as the ACTION, so a fenced sidecar would hijack it.
    # rsplit: the tag name also appears in the prose above the real block, and
    # only the real block's contents are the thing under test.
    sidecar = body.rsplit("<assessment_json>", 1)[1].split("</assessment_json>")[0]
    assert "```" not in sidecar
    assert '"funnel_stage"' in sidecar
    # Scaffolding the existing renderer depends on must survive the rewrite.
    for anchor in (
        "### Option C: Make a new top-level post", "### Option D: Skip this turn",
        "## Your subscribed channels", "## Your recent posts",
        "## Prior conversations with other labs", ":mag: **Opportunity Assessment**",
        "As the Blackbird scouting hub", "{interesting_posts}",
        "{subscribed_channels}", "{your_recent_posts}", "{prior_conversations}",
    ):
        assert anchor in body, f"rewrite broke the renderer anchor {anchor!r}"


def test_baltimore_is_a_question_not_an_inference():
    from pathlib import Path

    body = (Path("prompts/roles/scout_hub") / "agent-system.md").read_text(
        encoding="utf-8"
    )
    assert "Baltimore" in body
    assert "is not a Baltimore commitment" in body
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v -k "rubric or baltimore"`
Expected: FAIL — `assessment template omits 'Funnel stage'`.

- [ ] **Step 3: Edit the prompts**

In `prompts/roles/scout_hub/phase5-new-post.md`, replace the body of Option C — everything
from `Label it :mag: **Opportunity Assessment** and include, in this order:` down to and
including the `**Quality bar:**` block and the sentence
`Your post should be thorough enough to stand alone — this is not a 2-4 sentence post like Option A.`
— with:

```markdown
Label it :mag: **Opportunity Assessment** and include, in this order:

1. **The idea.** What it is, specifically — the technique, compound, construct, dataset,
   device, or method — and which PI it came from. Name it concretely; do not summarize it
   away.
2. **Funnel stage.** Where this sits: incubation/grant, pre-seed/formation, seed, or
   follow-on. The evidence bar follows from this — earlier stages are judged on potential,
   differentiation and external interest; later stages need replicated data, IP filed, a
   syndicate identified, and quantified milestones.
3. **Gating criteria.** All four, each as met / not met / **unconfirmed**:
   - *Baltimore commitment* — would the PI anchor a NewCo in Baltimore (ideally Blackbird
     BioHub) and keep forward activities there? **A JHU address is not a Baltimore
     commitment.** If you never asked the PI, this is *unconfirmed* — never met.
   - *Life-sciences / biomedical* — therapeutic, diagnostic, or platform.
   - *Credible technology source* — a top academic lab, with a path to license the IP.
   - *FTO achievable* — no unresolvable third-party blockade. A title-only prior-art
     search that found nothing does **not** establish this.
4. **Novelty & differentiation read.** What you found when you checked, with the exact
   search terms and the title-only/US-only limitation attached. Is this first- or
   best-in-class, or an incremental improvement in a less demanding setting?
5. **Market & unmet need.** Quantified TAM or prevalence where you have it, the clinical
   decision point, and whether the need is *actionable* — is there a downstream
   intervention?
6. **External signals.** Any VC/funder interest, big-pharma interest or deal comps, and
   whether a leading expert has validated the approach. Say plainly when there are none.
7. **Platform vs. single asset.** Does this generate a pipeline, or is it one shot?
8. **Capital efficiency.** Non-dilutive leverage available — TEDCO MII, Maryland
   Innovation Initiative, MSCRF, the BIITC tax credit / Maryland QOF — and how it would
   de-risk this before or around equity.
9. **Red flags.** Every disqualifier you saw, named explicitly. If there are none, say so.
10. **Recommendation.** Exactly one of: **advance** / **conditional** / **pass** /
    **route-to-incubation** (that last one is for high differentiation with thin data).
11. **Suggested de-risking milestones.** The specific, quantitative next results that
    would unlock the following stage.

Add a confidence label — *[High]*, *[Moderate]*, or *[Speculative]* — per the standards in
your system prompt.

**Quality bar:**
- Every section must be specific enough that a reader could act on it without a follow-up
  question
- If you're missing information for a section, say so explicitly and mark the relevant
  gating criterion *unconfirmed* — never skip a section silently and never guess
- **Do not post an assessment you don't believe.** If the interview didn't turn up enough
  to fill these in honestly, choose Option D instead

Your post should be thorough enough to stand alone — this is not a 2-4 sentence post like
Option A.

**Also emit the machine-readable verdict.** After your `<slack_message>` block, add an
`<assessment_json>` block. This is for Blackbird staff only — it is **stripped before
anything is posted to Slack**, so the PI never sees it. Score each dimension 1–5 (5 =
strongly meets Blackbird's bar). Do not compute `weighted_score` yourself — leave it at 0
and it will be calculated from your scores.

Emit it as **bare JSON with no code fence** (a fenced block would be mistaken for your
action JSON):

<assessment_json>
{
  "company_or_project": "",
  "subject_agent_id": "",
  "funnel_stage": "incubation | pre-seed | seed | follow-on",
  "gating": {
    "baltimore_commitment": false,
    "life_sciences_domain": true,
    "credible_tech_source": true,
    "fto_achievable": false
  },
  "scores": {
    "differentiation": 0, "market_unmet_need": 0, "team": 0, "external_signals": 0,
    "ip_fto": 0, "platform": 0, "dev_regulatory_feasibility": 0,
    "workplan_capital_efficiency": 0, "exit_thesis": 0
  },
  "weighted_score": 0,
  "red_flags": [],
  "recommendation": "advance | conditional | pass | route-to-incubation",
  "rationale": "",
  "suggested_derisking_milestones": [],
  "confidence": "High | Moderate | Speculative"
}
</assessment_json>

Set `gating.baltimore_commitment` to `true` **only** if the PI has actually said they
would anchor in Baltimore. Set `gating.fto_achievable` to `true` only on positive
evidence, not on an empty title search.
```

In `prompts/roles/scout_hub/agent-system.md`, add a sixth principle under "Core Principles"
(after the existing principle 5, "Silence over noise"):

```markdown
6. **Gating criteria are asked, not inferred.** The Baltimore commitment is a question
   about the *founder's* intent — would they anchor a NewCo here and keep forward
   activities here? **A JHU affiliation is not a Baltimore commitment**, and neither is a
   Baltimore mailing address; nearly every lab you talk to is already at Hopkins, so
   inferring the gate from the institution auto-passes it for everyone and makes it
   worthless. If you have not asked, the criterion is *unconfirmed*. The same holds for
   freedom-to-operate: an empty title-only patent search is not evidence of FTO.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v`
Expected: PASS, including `test_scout_hub_phase5_override_renders_in_both_modes` — that
test pins the Option C/D headings and the four substitution tokens the `funding_only`
regexes key off, which is why the rewrite preserves them verbatim.

- [ ] **Step 5: Commit**

```bash
git add prompts/roles/scout_hub/phase5-new-post.md prompts/roles/scout_hub/agent-system.md tests/unit/test_roles.py
git commit -m "feat(scout_hub): the assessment artifact is the Blackbird rubric, with a machine-readable verdict"
```

---

### Task 8: Extract and strip the assessment sidecar

**Files:**
- Modify: `src/agent/simulation.py` (new module-level function near `_extract_slack_message` at `:4973`; strip in `_post_message` at `:3074`)
- Test: `tests/unit/test_assessment_sidecar.py`

**Interfaces:**
- Consumes: the `<assessment_json>` block from Task 7.
- Produces: `_extract_assessment_json(text: str) -> dict | None`. Task 10 consumes it.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_assessment_sidecar.py`:

```python
from src.agent.simulation import _extract_assessment_json

_RESPONSE = """
Here is my reasoning about the Wang DBT opportunity.

```json
{"action": "new_post", "target_post_id": null, "channel": "general",
 "post_type": "opportunity_assessment", "tagged_agent": null}
```

<slack_message>
:mag: *Opportunity Assessment — Wang Lab (JHU)*
Recommendation: route-to-incubation. [Speculative]
</slack_message>

<assessment_json>
{
  "company_or_project": "DBT / BCAA-autophagy axis",
  "subject_agent_id": "wang",
  "funnel_stage": "incubation",
  "gating": {"baltimore_commitment": false, "life_sciences_domain": true,
             "credible_tech_source": true, "fto_achievable": false},
  "scores": {"differentiation": 4, "market_unmet_need": 4, "team": 4,
             "external_signals": 1, "ip_fto": 2, "platform": 3,
             "dev_regulatory_feasibility": 3, "workplan_capital_efficiency": 3,
             "exit_thesis": 2},
  "weighted_score": 0,
  "red_flags": ["No external validation yet"],
  "recommendation": "route-to-incubation",
  "rationale": "Differentiated metabolic angle; needs mammalian in vivo.",
  "suggested_derisking_milestones": ["TDP-43 mouse rescue"],
  "confidence": "Speculative"
}
</assessment_json>
"""


def test_extracts_the_sidecar_verdict():
    verdict = _extract_assessment_json(_RESPONSE)
    assert verdict["funnel_stage"] == "incubation"
    assert verdict["subject_agent_id"] == "wang"
    assert verdict["gating"]["baltimore_commitment"] is False
    assert verdict["scores"]["differentiation"] == 4
    assert verdict["recommendation"] == "route-to-incubation"


def test_action_json_still_wins_the_action_parse():
    """The sidecar is bare JSON precisely so the LAST ```json``` fence stays the
    action. If this breaks, every scout_hub post silently becomes a no-op."""
    from src.agent.simulation import AgentSimulation

    data, body = AgentSimulation._parse_phase5_response(None, _RESPONSE)
    assert data["action"] == "new_post"
    assert data["post_type"] == "opportunity_assessment"
    assert ":mag:" in body
    assert "assessment_json" not in body
    assert "funnel_stage" not in body


def test_missing_sidecar_returns_none():
    assert _extract_assessment_json("<slack_message>hi</slack_message>") is None


def test_malformed_sidecar_returns_none_and_does_not_raise():
    assert _extract_assessment_json(
        "<assessment_json>{not json,,,}</assessment_json>"
    ) is None


def test_last_sidecar_wins_when_the_model_revises():
    text = (
        "<assessment_json>{\"funnel_stage\": \"seed\"}</assessment_json>"
        "<assessment_json>{\"funnel_stage\": \"incubation\"}</assessment_json>"
    )
    assert _extract_assessment_json(text)["funnel_stage"] == "incubation"
```

The unbound `AgentSimulation._parse_phase5_response(None, _RESPONSE)` call is safe:
verified that the method's body never touches `self`, so passing `None` avoids standing up
a whole simulation for a pure parsing assertion. Do not change the method's signature.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessment_sidecar.py -v`
Expected: FAIL — `ImportError: cannot import name '_extract_assessment_json'`.

- [ ] **Step 3: Implement**

In `src/agent/simulation.py`, add immediately after `_extract_slack_message` (which ends
around line 4986):

```python
_ASSESSMENT_RE = re.compile(
    r"<assessment_json>\s*(.*?)\s*</assessment_json>", re.DOTALL
)


def _extract_assessment_json(text: str) -> dict | None:
    """Parse the scout hub's machine-readable verdict sidecar, or None.

    The sidecar is deliberately BARE JSON, not a ```json``` fence:
    _parse_phase5_response takes the LAST fenced json block as the action, so a
    fenced sidecar would hijack the action data and silently no-op every
    assessment post. Anchored on the LAST block so a revised verdict wins.
    """
    matches = _ASSESSMENT_RE.findall(text or "")
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1])
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[assessment] sidecar present but unparseable: %s", exc)
        return None
    return parsed if isinstance(parsed, dict) else None
```

In `_post_message` (line 3074), extend the leak-strip so a sidecar the model puts *inside*
`<slack_message>` never reaches Slack:

```python
        # Final safety: strip any leaked <slack_message> tags, and any
        # <assessment_json> sidecar — that block is for Blackbird staff and the DB,
        # never for the channel.
        text = _ASSESSMENT_RE.sub("", text)
        text = re.sub(r"</?assessment_json>", "", text)
        text = re.sub(r"</?slack_message>", "", text).strip()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_assessment_sidecar.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_assessment_sidecar.py
git commit -m "feat(sched): parse the assessment verdict sidecar and keep it out of Slack"
```

---

### Task 9: Rubric scoring module

The weighted score must be computed, not trusted — LLM arithmetic over nine weights is
exactly the kind of thing that reads plausible and is wrong.

**Files:**
- Create: `src/services/blackbird_rubric.py`
- Test: `tests/unit/test_blackbird_rubric.py`

**Interfaces:**
- Produces: `RUBRIC_WEIGHTS: dict[str, int]`; `weighted_score(scores: dict[str, object]) -> float` (rounded to 2dp); `band(score: float) -> str` returning `"advance" | "conditional" | "pass"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_blackbird_rubric.py`:

```python
import pytest

from src.services.blackbird_rubric import RUBRIC_WEIGHTS, band, weighted_score


def test_weights_match_the_pdf_and_sum_to_one_hundred():
    assert RUBRIC_WEIGHTS == {
        "differentiation": 20,
        "market_unmet_need": 15,
        "team": 15,
        "external_signals": 15,
        "ip_fto": 10,
        "platform": 8,
        "dev_regulatory_feasibility": 7,
        "workplan_capital_efficiency": 5,
        "exit_thesis": 5,
    }
    assert sum(RUBRIC_WEIGHTS.values()) == 100


def test_all_fives_is_five_and_all_ones_is_one():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 5)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 1)) == 1.0


def test_a_real_verdict_scores_as_hand_computed():
    scores = {
        "differentiation": 4, "market_unmet_need": 4, "team": 4, "external_signals": 1,
        "ip_fto": 2, "platform": 3, "dev_regulatory_feasibility": 3,
        "workplan_capital_efficiency": 3, "exit_thesis": 2,
    }
    # 80 + 60 + 60 + 15 + 20 + 24 + 21 + 15 + 10 = 305 / 100
    assert weighted_score(scores) == 3.05


def test_missing_and_unscorable_dimensions_count_as_zero():
    assert weighted_score({"differentiation": 5}) == 1.0
    assert weighted_score({"differentiation": "high"}) == 0.0
    assert weighted_score({}) == 0.0
    assert weighted_score(None) == 0.0


def test_out_of_range_scores_are_clamped_to_one_through_five():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 9)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, -3)) == 1.0


@pytest.mark.parametrize("score,expected", [
    (4.0, "advance"), (4.7, "advance"),
    (3.9, "conditional"), (3.0, "conditional"),
    (2.99, "pass"), (0.0, "pass"),
])
def test_banding_matches_the_pdf(score, expected):
    assert band(score) == expected
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_blackbird_rubric.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.blackbird_rubric'`.

- [ ] **Step 3: Implement**

Create `src/services/blackbird_rubric.py`:

```python
"""Blackbird's weighted screening rubric (Part C.3 of
data/Blackbird_initial_priorities-criteria_v1.pdf, transcribed in
profiles/private/blackbird.md).

The score is computed here rather than taken from the model's own
``weighted_score`` field: nine weights times nine 1-5 scores is precisely the
arithmetic an LLM gets plausibly wrong, and the band it lands in decides whether
a proposal advances.
"""

from __future__ import annotations

# Percentage weights, exactly as tabulated in Part C.3. Sums to 100.
RUBRIC_WEIGHTS: dict[str, int] = {
    "differentiation": 20,
    "market_unmet_need": 15,
    "team": 15,
    "external_signals": 15,
    "ip_fto": 10,
    "platform": 8,
    "dev_regulatory_feasibility": 7,
    "workplan_capital_efficiency": 5,
    "exit_thesis": 5,
}

_MIN_SCORE = 1
_MAX_SCORE = 5


def weighted_score(scores: dict[str, object] | None) -> float:
    """Weighted mean of the nine dimensions, on the same 1-5 scale.

    A dimension that is missing or not a number counts as 0 — an unscored
    dimension must drag the total down, never be quietly excluded from the
    denominator, or a verdict that skipped its weakest dimensions would outscore
    one that answered honestly.
    """
    if not scores:
        return 0.0
    total = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        raw = scores.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        total += max(_MIN_SCORE, min(_MAX_SCORE, float(raw))) * weight
    return round(total / sum(RUBRIC_WEIGHTS.values()), 2)


def band(score: float) -> str:
    """Part C.3 banding: >=4.0 advance, 3.0-3.9 conditional, <3.0 pass.

    'pass' here means pass ON the deal (decline), matching the PDF's vocabulary —
    not 'passing' the screen.
    """
    if score >= 4.0:
        return "advance"
    if score >= 3.0:
        return "conditional"
    return "pass"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_blackbird_rubric.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/blackbird_rubric.py tests/unit/test_blackbird_rubric.py
git commit -m "feat(rubric): compute the weighted score from the nine dimensions, never trust it"
```

---

### Task 10: `opportunity_assessments` table

**Files:**
- Create: `src/models/opportunity.py`, `alembic/versions/0025_add_opportunity_assessments.py`
- Modify: `src/models/__init__.py`
- Test: `tests/integration/test_opportunity_assessment_persistence.py`

**Interfaces:**
- Produces: `OpportunityAssessment` with columns `id`, `simulation_run_id`, `agent_id`, `subject_agent_id`, `channel_name`, `slack_ts`, `company_or_project`, `funnel_stage`, `recommendation`, `confidence`, `weighted_score`, `band`, `gating`, `scores`, `red_flags`, `derisking_milestones`, `rationale`, `raw_verdict`, `created_at`. Tasks 11 and 12 consume it.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_opportunity_assessment_persistence.py`:

```python
import base64
import json

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import OpportunityAssessment, SimulationRun
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, is_admin=True, email="assessments-admin@example.org"
    )


@pytest.mark.asyncio
async def test_assessment_row_round_trips(db_session):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()

    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        subject_agent_id="wang",
        channel_name="general",
        slack_ts="1754480000.000100",
        company_or_project="DBT / BCAA-autophagy axis",
        funnel_stage="incubation",
        recommendation="route-to-incubation",
        confidence="Speculative",
        weighted_score=3.05,
        band="conditional",
        gating={"baltimore_commitment": False, "life_sciences_domain": True,
                "credible_tech_source": True, "fto_achievable": False},
        scores={"differentiation": 4, "external_signals": 1},
        red_flags=["No external validation yet"],
        derisking_milestones=["TDP-43 mouse rescue"],
        rationale="Differentiated metabolic angle.",
        raw_verdict={"weighted_score": 0},
    ))
    await db_session.flush()

    row = (await db_session.execute(select(OpportunityAssessment))).scalar_one()
    assert row.subject_agent_id == "wang"
    assert row.weighted_score == pytest.approx(3.05)
    assert row.band == "conditional"
    assert row.gating["baltimore_commitment"] is False
    assert row.red_flags == ["No external validation yet"]
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_nullable_columns_tolerate_a_sparse_verdict(db_session):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
    ))
    await db_session.flush()
    row = (await db_session.execute(select(OpportunityAssessment))).scalar_one()
    assert row.subject_agent_id is None
    assert row.weighted_score is None
```

`db_session` is the rolled-back session fixture from `tests/conftest.py:82`; `client`
(`:100`) is the ASGI client wired to it. The `_auth` cookie forgery and the `admin` fixture
above are copied from `tests/integration/test_cohort_admin.py` — Task 12 uses them.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpportunityAssessment'`.

- [ ] **Step 3: Implement the model**

Create `src/models/opportunity.py`:

```python
"""Durable store for BlackbirdBot's screening verdicts.

Before this table an assessment existed only as a Slack message: nothing was
queryable, nothing was rankable, and the machine-readable verdict the rubric
(Part C.6) calls for was never emitted at all. One row per posted :mag:
Opportunity Assessment.

Every rubric field is nullable because a sparse or partly-unparseable verdict
must still be recorded — losing the assessment is strictly worse than storing an
incomplete one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class OpportunityAssessment(Base):
    __tablename__ = "opportunity_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The scouting agent (blackbird) and the lab it assessed.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subject_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)

    company_or_project: Mapped[str | None] = mapped_column(Text, nullable=True)
    funnel_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Computed by src/services/blackbird_rubric.py, NOT taken from the model.
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str | None] = mapped_column(String(20), nullable=True)

    gating: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    red_flags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    derisking_milestones: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The verdict exactly as emitted, so a schema change never loses the original.
    raw_verdict: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityAssessment subject={self.subject_agent_id} "
            f"rec={self.recommendation} score={self.weighted_score}>"
        )
```

`from src.database import Base` is the same import the other model modules use
(`src/models/agent_activity.py:24`).

In `src/models/__init__.py`, add the import next to the other model imports (alphabetical
by module, so after `src.models.job`):

```python
from src.models.opportunity import OpportunityAssessment
```

and add `"OpportunityAssessment",` to `__all__`.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0025_add_opportunity_assessments.py`:

```python
"""Add opportunity_assessments (BlackbirdBot screening verdicts)

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-06 00:00:00.000000

One row per posted :mag: Opportunity Assessment. Before this table an assessment
existed only as a Slack message, so the rubric's machine-readable verdict
(Part C.6) had nowhere to live and nothing was queryable or rankable.

Every rubric column is nullable: a sparse verdict must still be recorded.
Downgrade is idempotent (if_exists) per the branch convention (0022/0023/0024).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunity_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("subject_agent_id", sa.String(length=50), nullable=True),
        sa.Column("channel_name", sa.String(length=100), nullable=False),
        sa.Column("slack_ts", sa.String(length=50), nullable=True),
        sa.Column("company_or_project", sa.Text(), nullable=True),
        sa.Column("funnel_stage", sa.String(length=20), nullable=True),
        sa.Column("recommendation", sa.String(length=30), nullable=True),
        sa.Column("confidence", sa.String(length=20), nullable=True),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("band", sa.String(length=20), nullable=True),
        sa.Column("gating", postgresql.JSONB(), nullable=True),
        sa.Column("scores", postgresql.JSONB(), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(), nullable=True),
        sa.Column("derisking_milestones", postgresql.JSONB(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("raw_verdict", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["simulation_run_id"], ["simulation_runs.id"], ondelete="CASCADE"
        ),
    )
    # Index names must match what SQLAlchemy's index=True generates on the model
    # (ix_<table>_<column>), or autogenerate reports permanent phantom drift.
    op.create_index(
        "ix_opportunity_assessments_simulation_run_id", "opportunity_assessments",
        ["simulation_run_id"],
    )
    op.create_index(
        "ix_opportunity_assessments_agent_id", "opportunity_assessments", ["agent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_assessments_agent_id", "opportunity_assessments",
                  if_exists=True)
    op.drop_index("ix_opportunity_assessments_simulation_run_id",
                  "opportunity_assessments", if_exists=True)
    op.drop_table("opportunity_assessments", if_exists=True)
```

- [ ] **Step 5: Run the tests and the migration round trip to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -v`
Expected: PASS.

Run: `.venv-test/bin/python -m alembic heads`
Expected: exactly one line, `0025 (head)`.

- [ ] **Step 6: Commit**

```bash
git add src/models/opportunity.py src/models/__init__.py alembic/versions/0025_add_opportunity_assessments.py tests/integration/test_opportunity_assessment_persistence.py
git commit -m "feat(models): persist opportunity assessments — a Slack message is not a record"
```

---

### Task 11: Persist the verdict when the assessment posts

**Files:**
- Modify: `src/agent/simulation.py` (the "New top-level post" branch at `:2299-2301`)
- Test: `tests/integration/test_opportunity_assessment_persistence.py`

**Interfaces:**
- Consumes: `_extract_assessment_json` (Task 8), `weighted_score`/`band` (Task 9), `OpportunityAssessment` (Task 10).
- Produces: `AgentSimulation._persist_assessment(agent_id, channel, verdict) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_opportunity_assessment_persistence.py`:

Add to `tests/integration/test_opportunity_assessment_persistence.py`. These exercise
`_persist_assessment` itself — the method this task adds — not just Task 9's arithmetic.
`_persist_assessment` commits, so it needs its own session factory over the test `engine`
rather than the rolled-back `db_session` fixture; the run row is deleted at the end so the
session-scoped engine is left clean (the FK cascade removes the assessment with it).

```python
@pytest.mark.asyncio
async def test_persist_assessment_recomputes_the_score_it_is_handed(engine):
    """The model is told to leave weighted_score at 0, and it will sometimes fill in
    a flattering number anyway. The stored score must be computed from its own
    dimension scores, with the original verdict kept verbatim in raw_verdict."""
    import uuid as _uuid
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import AgentSimulation

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    await AgentSimulation._persist_assessment(stub, "blackbird", "general", {
        "subject_agent_id": "wang",
        "company_or_project": "DBT / BCAA-autophagy axis",
        "funnel_stage": "incubation",
        "recommendation": "route-to-incubation",
        "confidence": "Speculative",
        "weighted_score": 4.8,  # the model's inflated claim — must be ignored
        "scores": {
            "differentiation": 4, "market_unmet_need": 4, "team": 4,
            "external_signals": 1, "ip_fto": 2, "platform": 3,
            "dev_regulatory_feasibility": 3, "workplan_capital_efficiency": 3,
            "exit_thesis": 2,
        },
        "gating": {"baltimore_commitment": False, "life_sciences_domain": True,
                   "credible_tech_source": True, "fto_achievable": False},
        "red_flags": ["No external validation yet"],
        "suggested_derisking_milestones": ["TDP-43 mouse rescue"],
        "rationale": "Differentiated metabolic angle.",
    })

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.weighted_score == pytest.approx(3.05)  # computed, not 4.8
            assert row.band == "conditional"
            assert row.subject_agent_id == "wang"
            assert row.agent_id == "blackbird"
            assert row.channel_name == "general"
            assert row.derisking_milestones == ["TDP-43 mouse rescue"]
            assert row.gating["baltimore_commitment"] is False
            # The original verdict survives verbatim for audit.
            assert row.raw_verdict["weighted_score"] == 4.8
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)  # cascades to the assessment
                await cleanup.commit()
    assert _uuid.UUID(str(run_id))  # run_id was a real uuid, not a stub artefact


@pytest.mark.asyncio
async def test_persist_assessment_never_raises_when_the_write_fails(caplog):
    """Best-effort by contract: the Slack post has already gone out, so losing the
    DB row must never take down the turn."""
    import uuid as _uuid
    from types import SimpleNamespace

    from src.agent.simulation import AgentSimulation

    def _boom():
        raise RuntimeError("database is gone")

    stub = SimpleNamespace(simulation_run_id=_uuid.uuid4(), session_factory=_boom)
    await AgentSimulation._persist_assessment(
        stub, "blackbird", "general", {"scores": {}}
    )
    assert "Failed to persist assessment" in caplog.text


@pytest.mark.asyncio
async def test_persist_assessment_tolerates_a_sparse_verdict(engine):
    """A partly-unparseable verdict must still be recorded — losing the assessment
    is strictly worse than storing an incomplete one."""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.agent.simulation import AgentSimulation

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        run_id = run.id

    stub = SimpleNamespace(simulation_run_id=run_id, session_factory=factory)
    # No scores, no gating, red_flags the wrong type entirely.
    await AgentSimulation._persist_assessment(
        stub, "blackbird", "general", {"red_flags": "not a list"}
    )

    try:
        async with factory() as check:
            row = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalar_one()
            assert row.weighted_score == pytest.approx(0.0)
            assert row.band == "pass"
            assert row.red_flags is None  # wrong type discarded, not stored
            assert row.scores is None
    finally:
        async with factory() as cleanup:
            stale = (await cleanup.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await cleanup.delete(stale)
                await cleanup.commit()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -v -k persist_assessment`
Expected: FAIL — `AttributeError: type object 'AgentSimulation' has no attribute '_persist_assessment'`.

- [ ] **Step 3: Implement**

In `src/agent/simulation.py`, add to the imports:

```python
from src.models import OpportunityAssessment
from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score
```

Add a method on `AgentSimulation`, next to the other `session_factory` users:

```python
    async def _persist_assessment(
        self, agent_id: str, channel: str, verdict: dict
    ) -> None:
        """Store a scouting verdict. Best-effort: a failure here must never cost
        the Slack post that already went out."""
        scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
        computed = rubric_weighted_score(scores)
        gating = verdict.get("gating") if isinstance(verdict.get("gating"), dict) else None
        red_flags = verdict.get("red_flags")
        milestones = verdict.get("suggested_derisking_milestones")
        try:
            async with self.session_factory() as db:
                db.add(OpportunityAssessment(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    subject_agent_id=(verdict.get("subject_agent_id") or None),
                    channel_name=channel,
                    company_or_project=(verdict.get("company_or_project") or None),
                    funnel_stage=(verdict.get("funnel_stage") or None),
                    recommendation=(verdict.get("recommendation") or None),
                    confidence=(verdict.get("confidence") or None),
                    weighted_score=computed,
                    band=rubric_band(computed),
                    gating=gating,
                    scores=scores or None,
                    red_flags=red_flags if isinstance(red_flags, list) else None,
                    derisking_milestones=milestones if isinstance(milestones, list) else None,
                    rationale=(verdict.get("rationale") or None),
                    raw_verdict=verdict,
                ))
                await db.commit()
            logger.info(
                "[%s] Assessment stored: %s -> %s (%.2f, %s)",
                agent_id, verdict.get("subject_agent_id") or "?",
                verdict.get("recommendation") or "?", computed, rubric_band(computed),
            )
        except Exception as exc:  # noqa: BLE001 — never lose a posted assessment
            logger.error("[%s] Failed to persist assessment: %s", agent_id, exc)
```

`self.simulation_run_id` is the attribute this class already uses everywhere it writes a
run-scoped row (e.g. `src/agent/simulation.py:546`, `:1524`, `:1642`).

In the "New top-level post" branch, replace:

```python
            else:
                # New top-level post
                await self._post_message(agent.agent_id, channel, message_text)
                agent.message_count += 1
```

with:

```python
            else:
                # New top-level post
                await self._post_message(agent.agent_id, channel, message_text)
                agent.message_count += 1

                # A :mag: Opportunity Assessment carries a machine-readable verdict
                # sidecar (stripped from the Slack body). Persist it — the whole
                # point of the artifact is that staff can triage it later.
                if post_type == "opportunity_assessment":
                    verdict = _extract_assessment_json(response)
                    if verdict:
                        await self._persist_assessment(agent.agent_id, channel, verdict)
                    else:
                        logger.warning(
                            "[%s] Phase 5: opportunity_assessment post with no "
                            "parseable <assessment_json> sidecar — verdict lost",
                            agent.agent_id,
                        )
```

`response` is in scope here — it is the same local the enclosing handler passed to
`_parse_phase5_response`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/integration tests/unit -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_opportunity_assessment_persistence.py
git commit -m "feat(sched): persist each posted assessment with a server-computed weighted score"
```

---

### Task 12: `/admin/assessments`

**Files:**
- Create: `templates/admin/assessments.html`
- Modify: `src/routers/admin.py`, `templates/base.html:104`
- Test: `tests/integration/test_opportunity_assessment_persistence.py`

**Interfaces:**
- Consumes: `OpportunityAssessment` (Task 10).
- Produces: `GET /admin/assessments`, `active_admin == "assessments"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_opportunity_assessment_persistence.py`:

```python
@pytest.mark.asyncio
async def test_admin_assessments_page_lists_verdicts(client, db_session, admin):
    run = SimulationRun()
    db_session.add(run)
    await db_session.flush()
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="DBT / BCAA-autophagy axis",
        funnel_stage="incubation", recommendation="route-to-incubation",
        confidence="Speculative", weighted_score=3.05, band="conditional",
        red_flags=["No external validation yet"],
    ))
    await db_session.flush()

    resp = await client.get("/admin/assessments", headers=_auth(admin.id))
    assert resp.status_code == 200
    assert "DBT / BCAA-autophagy axis" in resp.text
    assert "route-to-incubation" in resp.text
    assert "3.05" in resp.text
    assert "No external validation yet" in resp.text
```

This is the same real-ASGI-request pattern `tests/integration/test_cohort_admin.py` uses:
the `client` fixture routes `get_db` to the rolled-back `db_session`, and `_auth` forges the
signed session cookie that satisfies `get_admin_user`. Use `flush()`, not `commit()` — the
fixture owns the transaction.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -v -k admin`
Expected: FAIL — 404, or the route assertion fails.

- [ ] **Step 3: Implement the route**

In `src/routers/admin.py`, add `OpportunityAssessment` to the existing `from src.models import ...`
block, then add the route (place it next to `admin_agents`):

```python
@router.get("/assessments", response_class=HTMLResponse)
async def admin_assessments(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """BlackbirdBot's screening verdicts, worst-recommendation-last.

    Ordered by weighted score descending so the advance/conditional candidates
    are what you see first — this page is a triage queue, not a log.
    """
    result = await db.execute(
        select(OpportunityAssessment).order_by(
            OpportunityAssessment.weighted_score.desc().nullslast(),
            OpportunityAssessment.created_at.desc(),
        )
    )
    assessments = result.scalars().all()
    return templates.TemplateResponse(
        "admin/assessments.html",
        {
            "request": request,
            "current_user": current_user,
            "assessments": assessments,
            "active_admin": "assessments",
        },
    )
```

Create `templates/admin/assessments.html`:

```html
{% extends "base.html" %}
{% block title %}Admin — Assessments — CoPI{% endblock %}

{% block content %}
<h1 class="text-2xl font-bold text-gray-900 mb-2">Opportunity Assessments</h1>
<p class="text-sm text-gray-500 mb-6">
    BlackbirdBot's screening verdicts against the Blackbird investment rubric.
    Weighted score is computed from the nine dimension scores — not taken from the
    model. Bands: &ge;4.0 advance, 3.0&ndash;3.9 conditional, &lt;3.0 pass.
</p>

<div class="grid grid-cols-4 gap-4 mb-8">
    <div class="bg-white rounded-xl border border-gray-200 p-4 text-center">
        <div class="text-3xl font-bold text-gray-900">{{ assessments | length }}</div>
        <div class="text-sm text-gray-500 mt-1">Total</div>
    </div>
    <div class="bg-white rounded-xl border border-gray-200 p-4 text-center">
        <div class="text-3xl font-bold text-green-600">
            {{ assessments | selectattr("band", "equalto", "advance") | list | length }}
        </div>
        <div class="text-sm text-gray-500 mt-1">Advance</div>
    </div>
    <div class="bg-white rounded-xl border border-gray-200 p-4 text-center">
        <div class="text-3xl font-bold text-amber-600">
            {{ assessments | selectattr("band", "equalto", "conditional") | list | length }}
        </div>
        <div class="text-sm text-gray-500 mt-1">Conditional</div>
    </div>
    <div class="bg-white rounded-xl border border-gray-200 p-4 text-center">
        <div class="text-3xl font-bold text-gray-400">
            {{ assessments | selectattr("band", "equalto", "pass") | list | length }}
        </div>
        <div class="text-sm text-gray-500 mt-1">Pass</div>
    </div>
</div>

{% if assessments %}
<div class="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
    <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
            <tr>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Project</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Lab</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Stage</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Score</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Recommendation</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Gating</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Red flags</th>
                <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">When</th>
            </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
        {% for a in assessments %}
            <tr>
                <td class="px-4 py-3 text-sm text-gray-900">
                    {{ a.company_or_project or "—" }}
                    {% if a.confidence %}
                        <span class="text-xs text-gray-400">[{{ a.confidence }}]</span>
                    {% endif %}
                </td>
                <td class="px-4 py-3 text-sm text-gray-600">{{ a.subject_agent_id or "—" }}</td>
                <td class="px-4 py-3 text-sm text-gray-600">{{ a.funnel_stage or "—" }}</td>
                <td class="px-4 py-3 text-sm font-semibold
                    {% if a.band == 'advance' %}text-green-600
                    {% elif a.band == 'conditional' %}text-amber-600
                    {% else %}text-gray-400{% endif %}">
                    {% if a.weighted_score is not none %}{{ "%.2f"|format(a.weighted_score) }}{% else %}—{% endif %}
                </td>
                <td class="px-4 py-3 text-sm text-gray-900">{{ a.recommendation or "—" }}</td>
                <td class="px-4 py-3 text-xs text-gray-600">
                    {% if a.gating %}
                        {% for key, ok in a.gating.items() %}
                            <div>{% if ok %}✅{% else %}⚠️{% endif %} {{ key.replace("_", " ") }}</div>
                        {% endfor %}
                    {% else %}—{% endif %}
                </td>
                <td class="px-4 py-3 text-xs text-gray-600">
                    {% if a.red_flags %}
                        {% for flag in a.red_flags %}<div>• {{ flag }}</div>{% endfor %}
                    {% else %}none{% endif %}
                </td>
                <td class="px-4 py-3 text-xs text-gray-500">{{ a.created_at.strftime("%Y-%m-%d %H:%M") }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
</div>
{% else %}
<div class="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-500">
    No assessments recorded yet.
</div>
{% endif %}
{% endblock %}
```

In `templates/base.html`, add after the Agents link (line 101):

```html
            <a href="/admin/assessments" class="{% if active_admin == 'assessments' %}text-indigo-600 font-semibold{% else %}text-gray-500 hover:text-gray-700{% endif %}">Assessments</a>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/integration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin.py templates/admin/assessments.html templates/base.html tests/integration/test_opportunity_assessment_persistence.py
git commit -m "feat(admin): a triage queue for BlackbirdBot's screening verdicts"
```

---

### Task 13: Full verification and deploy

**Files:**
- Modify: `CLAUDE.md` (document the rubric-alignment surface)

**Interfaces:**
- Consumes: every prior task.

- [ ] **Step 1: Run the whole gate**

Run: `./scripts/ci.sh`
Expected: single alembic head `0025`, clean migration round trip, zero test-suite ruff
findings, `src/` findings at or below 260, full pytest green, coverage at or above 60%.

If `src/` findings rose, fix what you added — do not raise `SRC_LINT_MAX`.

- [ ] **Step 2: Verify the prior-art fix against the live API**

Run:

```bash
docker compose -f docker-compose.prod.yml exec -T blackbird-app python -c "
import asyncio
from src.services.patents import search_prior_art
async def main():
    for q in ('TFEB inhibitor nuclear translocation melanoma BRAF resistance',
              'MARK2 kinase inhibitor RAN translation C9orf72 repeat expansion ALS FTD'):
        r = await search_prior_art(q)
        print(len(r.hits), r.terms_used, r.broadened)
asyncio.run(main())
"
```

Expected: non-zero hit counts with `broadened=True` — these are the two production queries
that returned zero before. If both are still 0, the backoff is not reaching a narrow enough
tier; check `_rank_terms` output for those tokens before proceeding. A `None` result means
the ODP rate-limited you — wait a minute and retry rather than concluding anything.

- [ ] **Step 3: Apply the migration, rebuild, and restart**

```bash
DC="docker compose -f docker-compose.prod.yml"

# 1. Save the current run's logs first — the DB, not Slack, is the durable store.
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f

# 2. Confirm ownership before touching anything (copi-blackbird = ours).
docker inspect blackbird-agent-run --format '{{index .Config.Labels "com.docker.compose.project"}}'

# 3. Stop GRACEFULLY. `docker rm -f` sends SIGKILL and loses the in-flight turn.
docker stop -t 30 blackbird-agent-run
docker rm blackbird-agent-run

# 4. Web tier + worker, then the migration.
$DC up -d --build blackbird-app worker
$DC exec -T blackbird-app alembic upgrade head
$DC exec -T blackbird-app alembic current   # expect 0025

# 5. Rebuild the AGENT image too — it bakes src/, it does not mount it.
$DC --profile agent build agent

# 6. Start the new run.
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

Never pass `--remove-orphans`. Never touch the unprefixed `agent-run` — that is org1's
production run.

- [ ] **Step 4: Confirm the fixes are live**

```bash
# The new run is on the new code and the roster picked up blackbird.
docker logs blackbird-agent-run 2>&1 | head -40

# No interview turn should advertise a tool the role lacks (expect 0).
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A -c "
SELECT count(*) FROM llm_call_logs
WHERE agent_id='blackbird' AND phase='thread_reply'
  AND created_at > now() - interval '30 minutes'
  AND messages_json::text LIKE '%retrieve_foa%';"

# Prior-art searches should no longer be 100% empty.
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A -c "
SELECT count(*) FILTER (WHERE messages_json::text LIKE '%No US filings matched%') AS empty,
       count(*) FILTER (WHERE messages_json::text LIKE '%<patent>%')             AS hits
FROM llm_call_logs WHERE agent_id='blackbird'
  AND created_at > now() - interval '2 hours';"

# Assessments are landing as rows, not just messages.
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -c "
SELECT subject_agent_id, funnel_stage, weighted_score, band, recommendation
FROM opportunity_assessments ORDER BY created_at DESC LIMIT 10;"

# Top-level posts must no longer open with :question: or an @mention (expect 0).
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -t -A -c "
SELECT count(*) FROM agent_messages
WHERE agent_id='blackbird' AND phase='new_post'
  AND created_at > now() - interval '2 hours'
  AND (content LIKE ':question:%' OR content LIKE '@%');"
```

An assessment needs a concluded interview, so the `opportunity_assessments` table may
legitimately be empty for the first hour or two. Report it as "not yet observed", not as
verified.

- [ ] **Step 5: Document the surface**

Add to `CLAUDE.md`, after the "Adding New PIs" section:

```markdown
## BlackbirdBot (the scout_hub role)

BlackbirdBot screens PI ideas against `data/Blackbird_initial_priorities-criteria_v1.pdf`.
The rubric lives in **`profiles/private/blackbird.md`** (injected as "Your Private
Instructions" into every phase's system prompt); the per-phase behaviour lives in
`prompts/roles/scout_hub/` and `src/agent/thread_guidance.py`.

- **Interview guidance is per-role Python**, not a prompt: `src/agent/thread_guidance.py`.
  The `pi_lab` strings there are byte-identical to the pre-refactor literals and are pinned
  by `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` — do not reword them.
- **Assessments are durable.** A `:mag:` Opportunity Assessment must carry an
  `<assessment_json>` sidecar (bare JSON, *no* ``` fence — a fenced block would be parsed
  as the phase-5 action). It is stripped from the Slack body and written to
  `opportunity_assessments`, visible at `/admin/assessments`.
- **`weighted_score` is computed**, never taken from the model:
  `src/services/blackbird_rubric.py`.
- **`search_prior_art` is a TITLE-only search** on the USPTO Open Data Portal (PatentsView
  was decommissioned 2026-03-20). It backs off to the 2-3 most specific terms when the full
  phrase misses — before that backoff existed, 12 of 12 production searches returned zero
  and were reported to PIs as clean novelty. An empty title search is never FTO.
- **`retrieve_foa` is withheld** from this role (`prompts/roles/scout_hub/role.toml`);
  its phase-4 template must not mention it.
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document the scout_hub rubric-alignment surface"
```
