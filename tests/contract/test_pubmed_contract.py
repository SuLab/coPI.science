"""Contract tests for src/services/pubmed.py against NCBI E-utilities shapes.

Pins the efetch-XML parse path, the esummary/idconv JSON paths, and the
swallow-and-continue error behavior. respx intercepts the internal httpx client;
_ncbi_get retries a transient failure and paces every call — success or
failure — at a rate keyed on whether NCBI_API_KEY is set; these tests zero both
the retry backoff and the NCBI pacing gate to stay fast (except the two tests
below that need real values).
"""

import asyncio
import itertools
import math
import time

import httpx
import pytest
import respx

from src.config import get_settings
from src.services import http_retry, pubmed

pytestmark = pytest.mark.contract

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles"

_UNPACED_TESTS = {
    "test_semaphores_are_sized_by_api_key_presence",
    # Reads the shipped pacing constants; the fixture below would otherwise hand it zeroes.
    "test_ncbi_pacing_lands_at_the_policy_ceiling_not_below_it",
    "test_ncbi_pacing_spaces_concurrent_starts",
    "test_a_429_retry_still_respects_the_ncbi_pacing_gate",
    # Both read the shipped pacing constants: the point is the burst NCBI would actually
    # count, so a zeroed interval would make them vacuous.
    "test_a_concurrent_burst_never_exceeds_the_ncbi_arrival_ceiling",
    "test_the_keyed_burst_bound_is_one_under_the_policy_ceiling",
}


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch, request):
    """Zero the retry loop's backoff so a mocked 5xx doesn't add ~3.5s of real
    sleep per test, and — except for the two tests below that need real values — also zero the
    NCBI pacing gate and reset its shared clock, so the other ~10 tests that reach
    _ncbi_get don't each pay the pacing interval too.

    A per-test Semaphore rebind is unnecessary here: `pubmed._ncbi_semaphore` keys the
    semaphores on the RUNNING loop, so a fresh `asyncio.Semaphore` binding to a loop at its
    first *contended* acquire is a non-issue across the separate event loop each test runs —
    `test_the_ncbi_semaphore_is_not_shared_across_event_loops` below pins that directly."""
    monkeypatch.setattr(pubmed, "_RETRY_BACKOFF", 0)
    if request.node.name not in _UNPACED_TESTS:
        monkeypatch.setattr(pubmed, "_NCBI_PACING_SECONDS", {True: 0.0, False: 0.0})
        monkeypatch.setattr(pubmed, "_ncbi_next_start", 0.0)


def test_semaphores_are_sized_by_api_key_presence():
    """A keyless deployment must use the smaller concurrency bound; a keyed one the
    larger. Both must exist regardless of the current settings' key."""
    assert pubmed._NCBI_SEMAPHORE_SIZES[False] == 2
    assert pubmed._NCBI_SEMAPHORE_SIZES[True] == 8


def test_ncbi_pacing_lands_at_the_policy_ceiling_not_below_it():
    """The resized semaphore must not leave the keyed path paced at
    `1/0.12 = 8.33` req/s against NCBI's **10** req/s keyed ceiling — 83.3%, i.e. we throttled
    ourselves 17% below the limit the policy actually grants, on the path the profile pipeline
    spends all its time in. The keyless path was already at 98.0% of its 3 req/s ceiling.

    `_pace_ncbi` only ever DELAYS a start, never advances one, so the achieved rate is at most
    `1/interval`: the upper bounds below are the policy ceilings themselves, and going over them
    risks a real NCBI IP block. This is an arithmetic pin, deliberately not a timed measurement —
    a "we hit 9.5 req/s in a second of wall clock" assertion would flake the moment another agent's
    pytest run loads the machine.
    """
    keyed_rate = 1.0 / pubmed._NCBI_PACING_SECONDS[True]
    keyless_rate = 1.0 / pubmed._NCBI_PACING_SECONDS[False]
    # A 9.5 lower bound would force the interval to 0.105 -- and at 0.105 the worst-case
    # BURST is floor(1/0.105)+1 = 10, i.e. exactly NCBI's ceiling with no tolerance for
    # jitter, and pinning the average alone would let a 17-in-one-second regression
    # through undetected. The bound is 8.9 (0.112 s, 89.3% of the granted ceiling) and
    # the burst is pinned separately by
    # test_the_keyed_burst_bound_is_one_under_the_policy_ceiling.
    assert 8.9 <= keyed_rate <= 10.0, keyed_rate      # NCBI keyed ceiling: 10 req/s
    assert 2.9 <= keyless_rate <= 3.0, keyless_rate   # NCBI keyless ceiling: 3 req/s


# More than the largest semaphore (8 keyed, 2 keyless), so at least one acquire below MUST wait —
# and it is the waiting branch of `Semaphore.acquire`, the only one that calls `self._get_loop()`,
# that binds a semaphore to an event loop. A literal rather than a read of
# `pubmed._NCBI_SEMAPHORE_SIZES` so this test body is identical against the pre-fix module, which
# has no such attribute.
_CONTENDING_CALLS = 12


@respx.mock
def test_the_ncbi_semaphore_is_not_shared_across_event_loops():
    """`_NCBI_SEMAPHORES` must not be a module-level singleton, or the HAZARD this
    guards against in `src/services/pubmed.py` is only documented, not fixed.

    An `asyncio.Semaphore` does not bind to a loop at construction; it binds at its first
    *contended* `acquire()` (checked against CPython 3.11's and 3.12's `asyncio/locks.py`: the
    fast path returns before `self._get_loop()`). So a process-wide semaphore survives any number
    of `asyncio.run()` calls until one of them contends it, and then raises
    `RuntimeError: ... is bound to a different event loop` in every loop after that — permanently,
    since nothing rebuilds it.

    Two real `asyncio.run()` calls, the same shape as
    `test_pace_ncbi_survives_two_separate_event_loops` above. The mocked handler yields once
    while the slot is held, so the concurrent callers genuinely contend rather than each finishing
    before the next starts.
    """
    async def _slow(request):
        await asyncio.sleep(0)  # yield while holding the slot, so the rest really do contend
        return httpx.Response(200, text=EFETCH_XML)

    respx.get(f"{EUTILS}/efetch.fcgi").mock(side_effect=_slow)

    async def _contend():
        await asyncio.gather(*(
            pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {}) for _ in range(_CONTENDING_CALLS)
        ))

    asyncio.run(_contend())  # first event loop — this is where a singleton would bind
    asyncio.run(_contend())  # second, separate loop — a singleton raises here


def _hand_driven_clock(monkeypatch):
    """Give `http_retry` a monotonic clock (and an `asyncio.sleep`) this test advances by hand.

    Rebinding the module-global names means nothing outside `http_retry` sees a doctored clock, and
    an attempt can "cost" 60 s without the test sleeping for 60 s — so no assertion below is a
    wall-clock threshold, which matters because other agents run pytest concurrently here.
    `raising=False` on purpose: `http_retry` grew its `import time` as part of the very change this
    test covers, so without it the test would be red against the pre-fix module for a missing
    module global instead of for the unbounded retry loop it is actually about.
    """
    class _Clock:
        t = 0.0

        def monotonic(self):
            return self.t

        async def sleep(self, seconds):
            self.t += seconds

    clock = _Clock()
    monkeypatch.setattr(http_retry, "time", clock, raising=False)
    monkeypatch.setattr(http_retry.asyncio, "sleep", clock.sleep)
    return clock


@respx.mock
async def test_a_hung_ncbi_stops_retrying_once_its_call_budget_is_spent(monkeypatch):
    """over-impl R3, through pubmed's own wiring: `_ncbi_get` shipped four attempts and no total
    ceiling of any kind. With the 60 s timeout on pubmed's own `httpx.AsyncClient`, one logical
    call could burn 4x60 s of request plus its backoff (420 s once a `Retry-After: 60` storm
    replaces the backoff) — and `convert_dois_to_pmids` issues one such call per unresolved DOI,
    sequentially, while `worker/main.py` awaits one job at a time, so a hung NCBI multiplied that
    by the DOI count and head-of-line-blocked every other queued profile job.

    Two cases, both counted rather than timed:
      * a hung NCBI, every attempt burning the full 60 s timeout — the 120 s budget admits attempts
        at t=0 and t=60.5 and stops the third, which would have started at t=121.5;
      * an NCBI that fails FAST, spending no budget — all four attempts still run, i.e. the
        deadline does not quietly shorten ordinary retrying.
    """
    clock = _hand_driven_clock(monkeypatch)
    # The shipped backoff; the autouse fixture above zeroes it for speed, and speed is free here.
    monkeypatch.setattr(pubmed, "_RETRY_BACKOFF", 0.5)
    cost = {"seconds": 60.0}
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        clock.t += cost["seconds"]
        return httpx.Response(503, text="down")

    respx.get(f"{EUTILS}/efetch.fcgi").mock(side_effect=_handler)

    with pytest.raises(httpx.HTTPStatusError):
        await pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {})
    assert calls["n"] == 2

    cost["seconds"] = 0.0
    calls["n"] = 0
    clock.t = 0.0
    with pytest.raises(httpx.HTTPStatusError):
        await pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {})
    assert calls["n"] == 4


@respx.mock
async def test_ncbi_pacing_spaces_concurrent_starts(monkeypatch):
    """The rate ceiling must hold under concurrency, via a gate that
    (unlike an `asyncio.Lock`) is never bound to whichever event loop happens to be running when
    it is first used — see the module-level comment above `_pace_ncbi`. With the old per-slot
    `finally: sleep(interval)`, N semaphore slots each sleeping `interval` allow N/interval
    requests per second — on this pre-gate code, 2 keyless slots and instant mocked responses let
    two pairs of the four concurrent starts land close together (measured over 50 runs of a
    faithful reproduction of the pre-gate code: worst observed min-gap 0.026s, worst total span
    0.154s), nowhere near a real `interval` apart. The lock-free reservation cursor below must
    instead space every call's START at least `interval` seconds apart, process-wide, regardless
    of how many callers are in flight.

    Thresholds are looser than `interval` itself because the recorded timestamp is the mocked
    HTTP call, one `await` past the gate release (through client construction and
    ``get_with_retry``) — real, if small, per-call scheduling overhead that can reorder which
    call's dispatch lands first without violating the gate. Measured over 300 runs at this
    interval, the worst observed adjacent gap was 78% of `interval` and the worst total span 96%
    of `3 * interval`; the 50%/85% thresholds below leave ample margin above that noise floor
    while still failing every one of the 50 pre-gate-reproduction runs above — a ~1.9x margin on
    the gap check and ~1.6x (environment-dependent; ≥1.5x) on the span check, not the "~50x" this
    docstring previously (and wrongly) claimed.
    """
    interval = 0.1
    monkeypatch.setattr(pubmed, "_NCBI_PACING_SECONDS", {True: interval, False: interval})
    monkeypatch.setattr(pubmed, "_ncbi_next_start", 0.0)
    starts: list[float] = []

    def _record_start(request):
        starts.append(time.monotonic())
        return httpx.Response(200, text=EFETCH_XML)

    respx.get(f"{EUTILS}/efetch.fcgi").mock(side_effect=_record_start)
    await asyncio.gather(
        *(pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {}) for _ in range(4))
    )

    starts.sort()
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(gap >= interval * 0.5 for gap in gaps), gaps
    assert starts[-1] - starts[0] >= 3 * interval * 0.85


def test_the_keyed_burst_bound_is_one_under_the_policy_ceiling():
    """Audit D5, the arithmetic half. `test_ncbi_pacing_lands_at_the_policy_ceiling_not_below_it`
    above pins the AVERAGE rate, and that is the wrong invariant on its own -- it is exactly why
    a defect that put 17 requests into a one-second window passed it.

    NCBI counts ARRIVALS per second. `_pace_ncbi` spaces STARTS at least `interval` apart, so the
    worst-case number of starts inside a 1 s window is `floor(1/interval) + 1` -- ten starts at
    0.105 s span 0.945 s and all fall in the same second. At 0.105 that bound is exactly 10,
    which is the ceiling itself, leaving zero tolerance for any jitter between our start and
    NCBI's arrival. The interval must leave one request of margin.
    """
    # The two paths are held to DIFFERENT bounds, deliberately. Keyed is the production
    # path the profile pipeline spends its time in, and one request of margin costs
    # 9.52 -> 8.93 req/s there. Buying the same margin on the keyless fallback needs
    # interval > 0.5 s -- a third of its throughput -- so that path is held AT its ceiling.
    for has_key, ceiling, max_burst in ((True, 10, 9), (False, 3, 3)):
        interval = pubmed._NCBI_PACING_SECONDS[has_key]
        burst = math.floor(1.0 / interval) + 1
        assert burst <= max_burst, (
            f"has_key={has_key}: interval {interval}s admits {burst} starts in a 1 s window, "
            f"over the {max_burst} allowed against NCBI's {ceiling} req/s ceiling"
        )


@respx.mock
async def test_a_concurrent_burst_never_exceeds_the_ncbi_arrival_ceiling(monkeypatch):
    """Audit D5, the behavioural half: 17 requests left in one second against a 10 req/s ceiling.

    `_pace_ncbi` can only delay a start relative to a loop that is RUNNING. `962aa6c` moved
    `httpx.AsyncClient(...)` construction out of the concurrency slot, and building one does ~32 ms
    of synchronous, loop-blocking work (SSL context + CA bundle). `asyncio.gather` puts every
    coroutine in the ready queue, each runs to its first real await only after building its client,
    so the loop stalls for N x 32 ms and every reservation that came due during the stall departs
    in the same tick. At N=30: 17 starts in one second, minimum gap 0.15 ms.

    Counted in a sliding window, never compared against a wall-clock threshold, so a loaded
    machine cannot flake it: the assertion is "how many starts share any one second", which is
    precisely what NCBI meters.
    """
    starts: list[float] = []

    def _record_start(request):
        starts.append(time.monotonic())
        return httpx.Response(200, text=EFETCH_XML)

    respx.get(f"{EUTILS}/efetch.fcgi").mock(side_effect=_record_start)
    n = 30
    await asyncio.gather(
        *(pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {}) for _ in range(n))
    )
    assert len(starts) == n, f"only {len(starts)} of {n} requests were issued"

    starts.sort()
    has_key = bool(get_settings().ncbi_api_key)
    # Same asymmetry as the arithmetic pin above: keyed carries a request of margin,
    # keyless is held at its ceiling.
    ceiling, allowed = (10, 9) if has_key else (3, 3)
    worst = max(
        sum(1 for t in starts if s <= t < s + 1.0) for s in starts
    )
    assert worst <= allowed, (
        f"{worst} requests left inside one second, over the {allowed} allowed against "
        f"NCBI's {ceiling} req/s ceiling "
        f"(interval={pubmed._NCBI_PACING_SECONDS[has_key]}s, n={n}); "
        f"min gap {min(b - a for a, b in itertools.pairwise(starts)) * 1000:.2f} ms"
    )


@respx.mock
async def test_a_429_retry_still_respects_the_ncbi_pacing_gate(monkeypatch):
    """I1: `get_with_retry`'s retry attempts issued no request to `_pace_ncbi`, so a burst of
    429s let every retry re-fire immediately once its (small) exponential backoff elapsed —
    restoring exactly the over-rate `_pace_ncbi` exists to prevent. `_RETRY_BACKOFF` is zeroed by
    the autouse fixture above so any spacing observed here can only come from the pacing gate
    being re-entered on each attempt, not from the retry loop's own backoff."""
    interval = 0.1
    monkeypatch.setattr(pubmed, "_NCBI_PACING_SECONDS", {True: interval, False: interval})
    monkeypatch.setattr(pubmed, "_ncbi_next_start", 0.0)
    starts: list[float] = []
    calls = {"n": 0}

    def _handler(request):
        starts.append(time.monotonic())
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text=EFETCH_XML)

    respx.get(f"{EUTILS}/efetch.fcgi").mock(side_effect=_handler)
    await pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {})

    assert calls["n"] == 3
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(gap >= interval * 0.5 for gap in gaps), gaps


def test_pace_ncbi_survives_two_separate_event_loops():
    """Important 1 (review round 2): pin that `_pace_ncbi`'s lock-free cursor survives being
    called from two separate event loops in one process — the exact scenario the pre-fix
    `asyncio.Lock` body (commit 914330b) could not survive.

    `asyncio.Lock` (and `asyncio.Semaphore`) do not bind to a loop at construction; they bind at
    their first *contended* `acquire()` (see the module-level comment above `_ncbi_next_start` in
    pubmed.py). Against `git show 914330b:src/services/pubmed.py`, `_pace_ncbi` lazily
    constructed a module-level singleton `asyncio.Lock` on first call and held it for the
    process's life. Gathering 3 concurrent `_pace_ncbi` calls guarantees at least one contended
    `acquire()`, which binds that Lock to whichever loop is running. A second, separate
    `asyncio.run()` call — a fresh event loop — then contends the same Lock again and raises
    `RuntimeError: <Lock ...> is bound to a different event loop`. This RED claim is reasoned
    from the 914330b source and verified in an untracked scratch reproduction, not reproduced by
    reverting the tracked module: the preamble bans reverting tracked files for RED evidence, and
    the pre-fix Lock body no longer exists in this tree.

    The shipped cursor holds no loop-bound state at all — `_ncbi_next_start` is a plain float
    advanced by a non-`await` read-modify-write — so this must pass GREEN against the current
    code: two separate `asyncio.run()` calls, each gathering 3 concurrent `_pace_ncbi(0.01)`
    calls (guaranteeing contention-shaped concurrency within each run), both complete without
    error. This is a sync test (not async) precisely so it can drive two independent
    `asyncio.run()` event loops itself; the autouse fixture above already resets
    `_ncbi_next_start` to 0.0 before this test runs (this test's name is not in
    `_UNPACED_TESTS`), and that reset doesn't need to interfere since each call passes its
    interval directly rather than reading `_NCBI_PACING_SECONDS`.
    """

    async def _gather_three():
        await asyncio.gather(*(pubmed._pace_ncbi(0.01) for _ in range(3)))

    asyncio.run(_gather_three())  # first event loop
    asyncio.run(_gather_three())  # second, separate event loop — 914330b's Lock couldn't survive this


EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>31000000</PMID>
      <Article>
        <Journal>
          <Title>Nature</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>A Great Paper</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Some background.</AbstractText>
          <AbstractText>Plain conclusion.</AbstractText>
        </Abstract>
        <ELocationID EIdType="doi">10.1038/ignored-because-idlist-wins</ELocationID>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
        <AuthorList>
          <Author><LastName>Smith</LastName></Author>
          <Author><LastName>Jones</LastName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">31000000</ArticleId>
        <ArticleId IdType="doi">10.1038/xyz</ArticleId>
        <ArticleId IdType="pmc">PMC7000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@respx.mock
async def test_fetch_pubmed_records_parses_article_scoped_fields():
    respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text=EFETCH_XML))
    recs = await pubmed.fetch_pubmed_records(["31000000"])
    assert len(recs) == 1
    r = recs[0]
    assert r["pmid"] == "31000000"
    assert r["doi"] == "10.1038/xyz"  # from ArticleIdList, not the ELocationID
    assert r["pmcid"] == "PMC7000000"
    assert r["title"] == "A Great Paper"
    assert r["abstract"] == "BACKGROUND: Some background. Plain conclusion."
    assert r["journal"] == "Nature"
    assert r["year"] == 2020
    assert r["pub_types"] == ["Journal Article"]
    assert r["author_count"] == 2
    assert r["authors"] == ["Smith", "Jones"]


EFETCH_XML_AUTHOR_EDGE_CASES = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>32000000</PMID>
      <Article>
        <Journal>
          <Title>Cell</Title>
          <JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Another Great Paper</ArticleTitle>
        <Abstract>
          <AbstractText>Some abstract.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Wu</LastName><Initials>C</Initials></Author>
          <Author><CollectiveName>The Consortium Group</CollectiveName></Author>
          <Author><ForeName>NoLastName</ForeName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">32000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@respx.mock
async def test_fetch_pubmed_records_authors_collective_name_and_skip():
    """Exercises the three branches the hand-built contract fixture above
    (LastName-only) doesn't reach: LastName+Initials concatenation, the
    CollectiveName (group-authorship) branch, and the skip case for an
    <Author> with neither LastName nor CollectiveName.
    """
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=EFETCH_XML_AUTHOR_EDGE_CASES)
    )
    recs = await pubmed.fetch_pubmed_records(["32000000"])
    assert len(recs) == 1
    r = recs[0]
    # All three <Author> elements count toward author_count, including the
    # one that gets skipped from the names list below.
    assert r["author_count"] == 3
    # The third <Author> (ForeName only, no LastName/CollectiveName) is
    # absent — skipped, not rendered as an empty/garbage entry.
    assert r["authors"] == ["Wu C", "The Consortium Group"]


EFETCH_XML_INLINE_MARKUP = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>33000000</PMID>
      <Article>
        <Journal>
          <Title>Genes</Title>
          <JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Role of <i>TP53</i> in cancer</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">We studied <i>TP53</i> signaling.</AbstractText>
          <AbstractText>H<sub>2</sub>O is required.</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">33000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@respx.mock
async def test_fetch_pubmed_records_keeps_inline_markup_text_in_title_and_abstract():
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=EFETCH_XML_INLINE_MARKUP)
    )
    recs = await pubmed.fetch_pubmed_records(["33000000"])
    r = recs[0]
    assert r["title"] == "Role of TP53 in cancer"
    assert r["abstract"] == "BACKGROUND: We studied TP53 signaling. H2O is required."


async def test_fetch_pubmed_records_empty_input_no_http():
    assert await pubmed.fetch_pubmed_records([]) == []


@respx.mock
async def test_fetch_abstract_surfaces_the_doi():
    """fetch_abstract must pass the parsed DOI through to its return dict —
    the retrieve tools cite it so the emit gate's DOI requirement is
    satisfiable for a legit first-person share."""
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=EFETCH_XML)
    )
    out = await pubmed.fetch_abstract("31000000")
    assert out["doi"] == "10.1038/xyz"
    assert out["authors"] == ["Smith", "Jones"]


@respx.mock
async def test_fetch_pubmed_records_swallows_non_200_returns_empty():
    route = respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(500, text="err"))
    assert await pubmed.fetch_pubmed_records(["31000000"]) == []
    assert route.called  # fail if the mocked URL drifts — the swallowed error would otherwise hide it


@respx.mock
async def test_fetch_pubmed_records_malformed_xml_returns_empty():
    route = respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text="<not-xml"))
    assert await pubmed.fetch_pubmed_records(["31000000"]) == []
    assert route.called


@respx.mock
async def test_fetch_authoritative_dois_from_esummary():
    data = {
        "result": {
            "uids": ["31000000"],
            "31000000": {
                "articleids": [
                    {"idtype": "pubmed", "value": "31000000"},
                    {"idtype": "doi", "value": "10.1038/XYZ"},
                ]
            },
        }
    }
    respx.get(f"{EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(200, json=data))
    out = await pubmed.fetch_authoritative_dois(["31000000"])
    assert out == {"31000000": "10.1038/XYZ"}


async def test_fetch_authoritative_dois_empty_input_no_http():
    assert await pubmed.fetch_authoritative_dois([]) == {}


@respx.mock
async def test_fetch_authoritative_dois_swallows_non_200_returns_empty():
    route = respx.get(f"{EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(502))
    assert await pubmed.fetch_authoritative_dois(["31000000"]) == {}
    assert route.called


@respx.mock
async def test_convert_dois_to_pmids_via_idconv():
    data = {"records": [{"doi": "10.1038/xyz", "pmid": "31000000"}]}
    respx.get(IDCONV).mock(return_value=httpx.Response(200, json=data))
    out = await pubmed.convert_dois_to_pmids(["10.1038/xyz"])
    assert out == {"10.1038/xyz": "31000000"}


@respx.mock
async def test_convert_dois_to_pmids_skips_error_records():
    data = {"records": [{"doi": "10.1/missing", "status": "error"}]}
    respx.get(IDCONV).mock(return_value=httpx.Response(200, json=data))
    # unresolved DOI falls through to esearch phase
    respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["42"]}})
    )
    out = await pubmed.convert_dois_to_pmids(["10.1/missing"])
    assert out == {"10.1/missing": "42"}


async def test_convert_dois_to_pmids_empty_input_no_http():
    assert await pubmed.convert_dois_to_pmids([]) == {}
