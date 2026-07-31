"""GrantBot's funding flow — live grants.gov, the real Anthropic API, real Postgres. (T10)

`tests/unit/test_funding_rules.py` and `tests/unit/test_grantbot_lead_time.py` cover the
pure functions. `tests/live_api/test_grants_live.py` covers the grants.gov client. What
neither can see is the flow: a real opportunity, as grants.gov returns it *today*,
travelling through the lead-time filter, the selection LLM, the draft LLM, the
`grantbot_posted_foas` claim and into a stored funding message. Before this file
`src/models/grantbot_posted.py` — the primitive that stops GrantBot posting the same FOA
twice — was referenced by no test at all.

**Slack is never touched.** Another agent owns the test workspace. Every test either
forces the Slack-off path or installs a recording double in place of `slack_sdk.WebClient`
(`_RecordingWebClient`), and the Slack-off tests install `_ExplodingWebClient`, which
fails the test if GrantBot so much as constructs a client.

**What is stubbed, and why.** The ceiling for this task is 25 Anthropic calls. Two tests
spend real tokens because only a real model can answer their question (`real_llm`):
whether a live FOA survives selection and comes back as a usable post, and whether the
`funding_rules` regexes — written against imagined phrasing — actually classify prose a
model writes. The dedup, lead-time and Slack-transport tests replace GrantBot's two LLM
stages with `_StageRecorder`, which is not a compromise but the sharper instrument: it
records *which opportunities reached each stage*, which is precisely the claim those
tests make, and it makes the assertion depend on the filter rather than on model
judgement. Everything else in those tests — grants.gov, the close dates, the database,
the claim — is real.

**Facts this file is built on** (established by the T3 agent against live grants.gov, not
re-derived here):

- `search2` never returns a `description`. `search_opportunities` therefore always yields
  `description=""` and `grantbot.py:306` feeds that empty string to the drafting LLM.
  Known, reported, deliberately unfixed — pinned by
  `test_the_draft_prompt_is_built_from_an_empty_description` so it cannot silently change
  in either direction.
- `fetchOpportunity`'s backend is currently returning an outage envelope (HTTP 200,
  `errorcode: 0`, `data.message` = backend unavailable), so `fetch_opportunity_detail`
  returns None. Tests that depend on detail data say "provider is down" explicitly rather
  than passing quietly.
- Live close dates are `MM/DD/YYYY`. An unparseable close date returns None, and
  `_has_sufficient_lead_time` treats None as "rolling" and PASSES. That asymmetry means a
  date-format change disables lead-time filtering entirely, silently — characterized by
  `test_an_unparseable_close_date_turns_the_lead_time_filter_off`.

Run:

    docker compose exec -T -e LIVE_API_TESTS=1 -e ANTHROPIC_API_KEY=sk-ant-... \\
      -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_b3 \\
      app python -m pytest tests/integration/test_grantbot_live.py -q -m live_api
"""

import asyncio
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.agent import grantbot
from src.agent.funding_rules import (
    is_acknowledgment_only_funding_reply,
    is_announcement_only_funding_reply,
    summarize_funding_thread,
)
from src.agent.message_log import LogEntry, MessageLog
from src.models import AgentMessage, GrantbotPostedFoa, SimulationRun
from src.services import grants

# The whole module is the live tier: every test reads today's grants.gov catalogue.
pytestmark = [pytest.mark.integration, pytest.mark.live_api]

needs_llm = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="no ANTHROPIC_API_KEY — real-API tests are opt-in and cost money",
)

# `list_posted_opportunities` pages at 250 internally and never sees the rate limiter;
# charge the budget for the pages it is about to request.
_PAGE_SIZE = 250

# The six channels grantbot's drafting prompt offers the model. A seventh would be
# posted to a channel that does not exist in the workspace.
ALLOWED_CHANNELS = {
    "drug-repurposing", "structural-biology", "aging-and-longevity",
    "single-cell-omics", "chemical-biology", "funding-opportunities",
}

# Mechanism + subject-matter filters used to choose live FOAs the selection prompt is
# meant to keep. Rule L2: these select *a* qualifying opportunity from today's catalogue,
# never a named one, so nothing here goes stale when an FOA closes.
_MECHANISM_RE = re.compile(r"\((R01|R21|R35|U01|U19|P01|R33|DP1|DP2)\b", re.IGNORECASE)
_BIOMEDICAL_RE = re.compile(
    r"\b(cancer|immun\w*|neuro\w*|virus|viral|infect\w*|protein\w*|structural|genom\w*|"
    r"proteom\w*|drug|therapeut\w*|molecul\w*|cell\w*|microbi\w*|aging|biolog\w*|"
    r"chemi\w*|discovery|metabol\w*)\b",
    re.IGNORECASE,
)
# The drafting prompt's own EXCLUDE list — feeding GrantBot one of these and then
# complaining that it was not selected would be testing the model's obedience to a rule
# we asked it to follow.
_EXCLUDED_RE = re.compile(
    r"\b(training|fellowship|T32|F31|F32|K\d\d|career|conference|supplement|scholar|"
    r"education|diversity|small business|SBIR|STTR)\b",
    re.IGNORECASE,
)

# Words that appear in every FOA title and therefore prove no grounding.
_BOILERPLATE = {
    "clinical", "trial", "optional", "required", "allowed", "research", "program",
    "grants", "grant", "award", "awards", "initiative", "opportunity", "limited",
    "competition", "national", "institute", "institutes", "notice", "funding",
}


# --------------------------------------------------------------------------- helpers


def content_words(text: str, min_len: int = 6) -> set[str]:
    """Distinctive lowercase words in `text` — boilerplate and short words removed."""
    words = re.findall(rf"[A-Za-z][A-Za-z\-]{{{min_len - 1},}}", text)
    return {w.lower() for w in words} - _BOILERPLATE


def days_out(opp: dict, now: datetime) -> int | None:
    """Days from `now` to the opportunity's close date, or None if unparseable/empty."""
    close = grantbot._parse_close_date(opp.get("close_date", ""))
    return None if close is None else (close - now).days


def expected_header(opp: dict) -> str:
    """The header `_run_grantbot_with_session` prepends to every funding post.

    Duplicated from grantbot.py on purpose: it is the part of the message that is NOT
    model output, and pinning it here is how a change to the FOA number, close date or
    grants.gov link that agents cite becomes a test failure instead of a silent edit.
    """
    return (
        ":moneybag: *Funding Opportunity*\n"
        f"*{opp.get('title', '')}*\n"
        f"{opp.get('number', 'unknown')} | Closes: {opp.get('close_date', 'Not specified')}\n"
        f"https://www.grants.gov/search-results-detail/{opp.get('id', '')}\n\n"
    )


class _StageRecorder:
    """Stands in for GrantBot's two LLM stages and records what reached each.

    Only installed by tests whose claim is about the *filters*, not about model output:
    what a test of the lead-time cut or the dedup claim needs to observe is which
    opportunities arrived at the selection and drafting stages, and a real model's
    include/exclude judgement would only add noise to that.
    """

    def __init__(self, channel: str = "funding-opportunities"):
        self.channel = channel
        self.offered_to_select: list[str] = []
        self.drafted: list[str] = []
        self.select_calls = 0

    async def select(self, opportunities: dict, max_select: int = 30) -> list[str]:
        self.select_calls += 1
        self.offered_to_select.extend(opportunities)
        return list(opportunities)[:max_select]

    async def draft(self, opportunity: dict) -> dict:
        number = opportunity.get("number", "")
        self.drafted.append(number)
        return {
            "channel": self.channel,
            "post_text": f"Stubbed draft body for {number}. Scope, mechanism, eligibility.",
        }

    def install(self, monkeypatch):
        monkeypatch.setattr(grantbot, "_select_opportunities", self.select)
        monkeypatch.setattr(grantbot, "_draft_post", self.draft)
        return self


class _ExplodingWebClient:
    """Any construction is a test failure: T10 must never reach a Slack workspace."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "GrantBot constructed a real slack_sdk.WebClient during a Slack-OFF test — "
            "it would have posted into the shared copi-test workspace, which another "
            "agent owns. `slack_globally_enabled` was patched to False, so reaching here "
            "means the gate in _run_grantbot_with_session no longer consults it."
        )


class _RecordingWebClient:
    """A Slack transport double. Records posts; never opens a socket.

    `fail_post` makes `chat_postMessage` raise, which is how the claim-release path
    (a post that failed must not leave the FOA marked as posted) gets exercised.
    """

    instances: list["_RecordingWebClient"] = []

    def __init__(self, token: str = "", **kwargs):
        assert not token.startswith("xoxb-") or "fake" in token, (
            f"the Slack double was handed {token[:12]!r} — a test must never pass a "
            "real bot token anywhere near a transport, even a fake one"
        )
        self.token = token
        self.posts: list[dict] = []
        self.joined: list[str] = []
        self.fail_post = _RecordingWebClient.next_fail_post
        _RecordingWebClient.instances.append(self)

    next_fail_post = False

    def conversations_list(self, **kwargs):
        return {
            "channels": [{"name": n, "id": f"C{n[:8].upper()}"} for n in ALLOWED_CHANNELS],
            "response_metadata": {"next_cursor": ""},
        }

    def conversations_join(self, channel: str):
        self.joined.append(channel)
        return {"ok": True}

    def chat_postMessage(self, channel: str, text: str):
        if self.fail_post:
            raise RuntimeError("simulated Slack outage")
        self.posts.append({"channel": channel, "text": text})
        return {"ok": True, "ts": "1700000000.000100"}


class _SettingsWithFakeToken:
    """Real settings, with the two Slack bot tokens replaced by an obvious fake.

    Belt and braces: the transport is already a double, but this guarantees that even a
    regression that bypassed the double could not authenticate as a real bot.
    """

    def __init__(self, real, token: str = "xoxb-fake-t10-token"):
        self._real, self._token = real, token

    def __getattr__(self, name):
        if name in ("slack_bot_token_grantbot", "slack_bot_token_su"):
            return self._token
        return getattr(self._real, name)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolate_foa_cache(monkeypatch, tmp_path):
    """`cache_foa` writes into the repo's data/ directory. Redirect it at a tmp dir."""
    monkeypatch.setattr("src.agent.foa_cache.CACHE_DIR", tmp_path / "foa_cache")


@pytest.fixture(scope="session")
def now_utc() -> datetime:
    """One clock for the whole module, so a boundary FOA cannot flip mid-session."""
    return datetime.now(UTC)


@pytest.fixture(scope="session")
def live_catalogue(api_budget) -> list[dict]:
    """Today's posted NIH/NSF opportunities, fetched once and shared.

    Rule L3: an empty catalogue is grants.gov being down or its `data.oppHits` path
    moving, not a property of GrantBot — say so rather than letting every test below
    fail on an unrelated symptom.
    """
    for _ in range(4):  # the client pages internally at 250/request
        api_budget.wait("grants")
    opportunities = asyncio.run(grants.list_posted_opportunities())
    if not opportunities:
        pytest.fail(
            "PROVIDER: grants.gov returned zero posted "
            f"{grants.BIOMEDICAL_AGENCIES} opportunities. Every test in this module "
            "reads that catalogue, so nothing below can be concluded. This is not a "
            "GrantBot failure."
        )
    return opportunities


@pytest.fixture(scope="session")
def biomedical_candidates(live_catalogue, now_utc) -> list[dict]:
    """Live NIH opportunities the selection prompt is designed to keep.

    Comfortably past the lead-time cut (MIN_LEAD_DAYS + 30) so the selection test cannot
    fail for a lead-time reason, and sorted by FOA number so a rerun within the same day
    exercises the same opportunities.
    """
    out = [
        o for o in live_catalogue
        if o.get("agency") == "HHS-NIH11"
        and o.get("id")
        and (d := days_out(o, now_utc)) is not None
        and d >= grantbot.MIN_LEAD_DAYS + 30
        and _MECHANISM_RE.search(o.get("title", ""))
        and _BIOMEDICAL_RE.search(o.get("title", ""))
        and not _EXCLUDED_RE.search(o.get("title", ""))
    ]
    out.sort(key=lambda o: o["number"])
    return out


@pytest_asyncio.fixture
async def sim_run(db_session) -> SimulationRun:
    """A simulation run for GrantBot's DB post to land in, asserted to be the latest.

    `_post_funding_to_db` calls `get_latest_run_id`, which orders by `started_at`. If a
    stale run in this database were newer, the funding post would be filed against it and
    every assertion below would look for the message in the wrong run.
    """
    from src.services.pi_inbox import get_latest_run_id

    run = SimulationRun(
        id=uuid.uuid4(),
        started_at=datetime.now(UTC) + timedelta(seconds=5),
        status="running",
        config={"source": "tests/integration/test_grantbot_live.py"},
    )
    db_session.add(run)
    await db_session.flush()
    latest = await get_latest_run_id(db_session)
    assert latest == run.id, (
        f"get_latest_run_id returned {latest}, not the run this test just created "
        f"({run.id}) — a newer simulation_runs row exists in this database and GrantBot "
        "would post into it"
    )
    return run


@pytest.fixture
def slack_off(monkeypatch):
    """Force the DB-post path and make any real Slack client construction fatal."""
    async def _disabled(db):
        return False

    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _disabled)
    monkeypatch.setattr("slack_sdk.WebClient", _ExplodingWebClient)


@pytest.fixture
def slack_on(monkeypatch):
    """Take the Slack branch, but through `_RecordingWebClient` with a fake token."""
    async def _enabled(db):
        return True

    _RecordingWebClient.instances = []
    _RecordingWebClient.next_fail_post = False
    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _enabled)
    monkeypatch.setattr("slack_sdk.WebClient", _RecordingWebClient)
    real_settings = grantbot.get_settings()
    monkeypatch.setattr(grantbot, "get_settings", lambda: _SettingsWithFakeToken(real_settings))
    return _RecordingWebClient


@pytest.fixture
def llm_calls(monkeypatch):
    """Record every real Anthropic call GrantBot makes, without changing its behaviour.

    The recording wrapper is how the drafting prompt gets inspected: the prompt is built
    inside `_draft_post` and is otherwise unobservable, and it is where the empty
    `description` ends up.
    """
    from src.services import llm as llm_service

    real = llm_service.generate_agent_response
    calls: list[dict] = []

    async def recording(system_prompt, messages, **kwargs):
        response = await real(system_prompt=system_prompt, messages=messages, **kwargs)
        calls.append({
            "phase": (kwargs.get("log_meta") or {}).get("phase"),
            "system": system_prompt,
            "user": messages[-1]["content"] if messages else "",
            "response": response,
        })
        return response

    monkeypatch.setattr(llm_service, "generate_agent_response", recording)
    return calls


@pytest.fixture
def fixed_catalogue(monkeypatch):
    """Serve GrantBot a chosen slice of the live catalogue.

    The opportunities are real and were fetched from grants.gov moments earlier; only
    *how many* of them GrantBot sees is controlled, because the unbounded pipeline drafts
    one LLM call per selected opportunity (up to 30) and this task has a 25-call ceiling.
    """
    def _install(opportunities: list[dict], budget=None):
        async def _listed(agencies=None):
            return [dict(o) for o in opportunities]

        monkeypatch.setattr(grantbot, "list_posted_opportunities", _listed)
        if budget is not None:
            for _ in opportunities:  # the pipeline fetches detail per selected opp
                budget.wait("grants")

    return _install


async def _messages_for_run(db_session, run_id) -> list[AgentMessage]:
    rows = await db_session.execute(
        select(AgentMessage)
        .where(AgentMessage.simulation_run_id == run_id)
        .order_by(AgentMessage.posted_at)
    )
    return list(rows.scalars().all())


async def _claimed_numbers(db_session) -> set[str]:
    rows = await db_session.execute(select(GrantbotPostedFoa.foa_number))
    return set(rows.scalars().all())


# --------------------------------------------------------------------------- the flow


@pytest.mark.real_llm
@needs_llm
async def test_a_live_opportunity_flows_through_to_a_drafted_funding_message(
    db_session, sim_run, biomedical_candidates, llm_calls, slack_off,
    fixed_catalogue, api_budget,
):
    """A real FOA from today's grants.gov reaches agent_messages as a usable post.

    Two live opportunities in, a real selection call, a real drafting call each, and a
    row in the database at the other end. Everything between is production code.

    Control (against the failure this test exists to catch): the drafted body must share
    a distinctive word with the live FOA title. A pipeline that lost its input and had
    the model write a plausible generic funding post would satisfy every structural
    assertion here and fail that one.

    Second control: the rerun at the end costs zero LLM calls, which proves the
    already-posted pre-filter runs *before* the model rather than after it.
    """
    candidates = biomedical_candidates[:2]
    assert len(candidates) == 2, (
        f"only {len(candidates)} live NIH opportunities matched "
        f"{_MECHANISM_RE.pattern} + biomedical wording with >= "
        f"{grantbot.MIN_LEAD_DAYS + 30} days of runway. grants.gov's catalogue is "
        "unusually thin today or the agency/mechanism filters no longer match its "
        "titles — this is about the catalogue, not about GrantBot"
    )
    fixed_catalogue(candidates, budget=api_budget)

    posted = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities",
        dry_run=False, max_posts=5, max_per_channel=5,
    )

    select_calls = [c for c in llm_calls if c["phase"] == "select"]
    draft_calls = [c for c in llm_calls if c["phase"] == "draft"]
    assert len(select_calls) == 1, (
        f"expected exactly one selection call, saw {len(select_calls)} "
        f"(phases seen: {[c['phase'] for c in llm_calls]})"
    )
    assert posted, (
        "the pipeline posted nothing. The selection LLM was offered "
        f"{[o['number'] + ': ' + o['title'][:60] for o in candidates]} and returned "
        f"{select_calls[0]['response'][:200]!r}; {len(draft_calls)} draft(s) followed. "
        "If selection returned an empty array the model rejected live NIH R-series "
        "biomedical FOAs, which is a prompt/model change, not a plumbing failure"
    )
    assert len(draft_calls) >= len(posted), (
        f"{len(posted)} opportunities were posted but only {len(draft_calls)} draft calls "
        "were made — a post went out with no model-written body"
    )

    by_number = {o["number"]: o for o in candidates}
    messages = await _messages_for_run(db_session, sim_run.id)
    assert len(messages) == len(posted), (
        f"the pipeline reported {len(posted)} post(s) {[p['number'] for p in posted]} but "
        f"{len(messages)} agent_messages row(s) exist for this run — the DB write and the "
        "return value disagree, so callers counting one are wrong about the other"
    )

    for record, message in zip(posted, messages, strict=True):
        opportunity = by_number[record["number"]]
        assert record["channel"] in ALLOWED_CHANNELS, (
            f"the drafting model chose channel {record['channel']!r}, which is not one of "
            f"the six offered in its prompt ({sorted(ALLOWED_CHANNELS)}) — GrantBot would "
            "post into a channel that does not exist"
        )
        assert message.agent_id == "grantbot" and message.is_bot, (
            f"funding post stored as agent_id={message.agent_id!r} is_bot={message.is_bot} "
            "— agents filter the log on both"
        )
        assert message.phase == "new_post" and message.visibility == "public", (
            f"funding post stored with phase={message.phase!r} "
            f"visibility={message.visibility!r}; funding threads are open to all and the "
            "Phase 2 scan only sees top-level public posts"
        )
        assert message.channel_name == record["channel"]
        assert message.channel_id == f"local:{record['channel']}"
        assert message.sender_name == "GrantBot"
        assert message.message_ts and float(message.posted_at) > 0

        header = expected_header(opportunity)
        assert message.content.startswith(header), (
            "the funding post's header is not the one grantbot.py builds. Agents and "
            "`_FOA_NUMBER_RE` in funding_rules.py read the number out of this header, and "
            "the grants.gov link is what a PI clicks.\nexpected prefix:\n"
            f"{header!r}\ngot:\n{message.content[:len(header) + 80]!r}"
        )
        body = message.content[len(header):].strip()
        assert len(body) > 120, (
            f"the model's post body for {record['number']} is {len(body)} chars "
            f"({body!r}) — the summary a PI is meant to triage on is essentially empty"
        )
        assert "**" not in body, (
            "the drafted body uses **double asterisks**, which Slack mrkdwn renders "
            f"literally; the prompt forbids them explicitly. Body: {body[:300]!r}"
        )
        assert not re.search(r"@\w+[Bb]ot\b", body), (
            "the drafted body tags a lab bot. The prompt forbids it ('lab agents will "
            f"decide relevance themselves') and a tag skews who replies. Body: {body[:300]!r}"
        )

        shared = content_words(opportunity["title"]) & content_words(body)
        assert shared, (
            f"the post drafted for {record['number']} shares no distinctive word with the "
            f"live FOA title.\n  title: {opportunity['title']!r}\n  body:  {body[:300]!r}\n"
            "Either the opportunity never reached the prompt (the pipeline lost its input) "
            "or the model wrote a generic funding post — both produce a post that "
            "misrepresents the FOA to every PI who reads it"
        )

    claimed = await _claimed_numbers(db_session)
    assert {p["number"] for p in posted} <= claimed, (
        f"posted {[p['number'] for p in posted]} but grantbot_posted_foas holds "
        f"{sorted(claimed)} — nothing recorded the post, so the next run reposts it"
    )
    row = (await db_session.execute(
        select(GrantbotPostedFoa).where(GrantbotPostedFoa.foa_number == posted[0]["number"])
    )).scalar_one()
    assert row.channel == posted[0]["channel"] and row.title == posted[0]["title"], (
        f"the claim row records channel={row.channel!r} title={row.title!r}, which does "
        "not match what was posted"
    )

    # Rerun over exactly what was posted: the cheap pre-filter must empty the set before
    # a single token is spent.
    calls_before = len(llm_calls)
    fixed_catalogue([by_number[p["number"]] for p in posted])
    again = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities",
        dry_run=False, max_posts=5, max_per_channel=5,
    )
    assert again == [], f"the same opportunities posted a second time: {again}"
    assert len(llm_calls) == calls_before, (
        f"the rerun spent {len(llm_calls) - calls_before} LLM call(s) on opportunities "
        "already in grantbot_posted_foas — _load_posted_numbers is no longer filtering "
        "before the model, which multiplies the cost of every daily run"
    )
    assert len(await _messages_for_run(db_session, sim_run.id)) == len(messages), (
        "the rerun added an agent_messages row for an FOA that was already posted"
    )


# --------------------------------------------------------------------------- dedup


async def test_claim_foa_is_the_dedup_primitive(db_session):
    """`models/grantbot_posted.py`, which no test referenced before this one.

    Absence and control interleaved: the second claim on the same number must fail, a
    claim on a *different* number must succeed, and after `_release_foa` the first number
    must be claimable again. A `_claim_foa` that always returned False would satisfy the
    absence assertion on its own; it cannot satisfy the other two.
    """
    number = f"TEST-T10-{uuid.uuid4().hex[:8].upper()}"
    other = f"TEST-T10-{uuid.uuid4().hex[:8].upper()}"

    assert await grantbot._claim_foa(db_session, number, "funding-opportunities", "First"), (
        "the first claim on an unseen FOA number failed — INSERT ... ON CONFLICT DO "
        "NOTHING reported rowcount 0 for a row that cannot have conflicted"
    )
    assert not await grantbot._claim_foa(db_session, number, "chemical-biology", "Second"), (
        "the same FOA number was claimed twice. The foa_number primary key plus ON "
        "CONFLICT DO NOTHING is the only thing stopping two GrantBot instances posting "
        "the same opportunity, and it is not holding"
    )
    assert await grantbot._claim_foa(db_session, other, "funding-opportunities", "Other"), (
        "CONTROL FAILED: a different, unseen FOA number was also refused — the claim is "
        "rejecting everything, so the refusal above proves nothing about deduplication"
    )

    rows = (await db_session.execute(
        select(GrantbotPostedFoa).where(GrantbotPostedFoa.foa_number == number)
    )).scalars().all()
    assert len(rows) == 1, f"{len(rows)} rows for one FOA number — the PK is not unique"
    assert rows[0].channel == "funding-opportunities" and rows[0].title == "First", (
        "the losing claim overwrote the winner's channel/title; ON CONFLICT DO NOTHING "
        "must not update"
    )
    assert rows[0].posted_at is not None, "posted_at server_default did not fire"

    await grantbot._release_foa(db_session, number)
    assert number not in await _claimed_numbers(db_session)
    assert await grantbot._claim_foa(db_session, number, "aging-and-longevity", "Retry"), (
        "after _release_foa the number could not be re-claimed — a failed Slack post "
        "would permanently retire the FOA instead of letting the next run retry it"
    )


async def test_the_claim_not_the_prefilter_is_what_stops_a_repost(
    db_session, sim_run, biomedical_candidates, slack_off, fixed_catalogue,
    monkeypatch, api_budget,
):
    """Dedup holds even when the cheap pre-filter is defeated — and a new FOA still posts.

    Three runs over live opportunities with the LLM stages recorded rather than called:

    1. FOA A posts.
    2. FOA A again, with `_load_posted_numbers` forced to return an empty set so the
       pre-filter cannot hide the claim. The drafting stage must run (proving the
       pre-filter really was bypassed) and nothing must be posted.
    3. CONTROL — FOA B, never seen, must post. Without it a `_claim_foa` that refused
       everything, or a pipeline that had stopped posting entirely, would pass step 2.
    """
    assert len(biomedical_candidates) >= 2, (
        f"need two live NIH opportunities, found {len(biomedical_candidates)}"
    )
    first, second = biomedical_candidates[0], biomedical_candidates[1]

    # --- 1. first post
    recorder = _StageRecorder().install(monkeypatch)
    fixed_catalogue([first], budget=api_budget)
    run_one = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )
    assert [p["number"] for p in run_one] == [first["number"]], (
        f"expected the live FOA {first['number']} to post, got {run_one}"
    )
    assert await _claimed_numbers(db_session) == {first["number"]}
    assert len(await _messages_for_run(db_session, sim_run.id)) == 1

    # --- 2. same FOA, pre-filter defeated
    async def _no_prefilter(session):
        return set()

    monkeypatch.setattr(grantbot, "_load_posted_numbers", _no_prefilter)
    recorder_two = _StageRecorder().install(monkeypatch)
    fixed_catalogue([first], budget=api_budget)
    run_two = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )
    assert recorder_two.drafted == [first["number"]], (
        "the drafting stage did not see the already-posted FOA, so the pre-filter was "
        f"still in play and this run never reached the claim (drafted: "
        f"{recorder_two.drafted}). The assertion below would prove nothing"
    )
    assert run_two == [], (
        f"the FOA already in grantbot_posted_foas was posted again: {run_two}. With the "
        "pre-filter bypassed, `_claim_foa` is the last line of defence and it did not hold"
    )
    messages = await _messages_for_run(db_session, sim_run.id)
    assert len(messages) == 1, (
        f"{len(messages)} funding messages exist for one FOA — the duplicate reached "
        "agent_messages even though the claim was refused"
    )
    claim_rows = (await db_session.execute(
        select(GrantbotPostedFoa).where(GrantbotPostedFoa.foa_number == first["number"])
    )).scalars().all()
    assert len(claim_rows) == 1

    # --- 3. control: an unseen FOA still posts (pre-filter still bypassed)
    recorder_three = _StageRecorder().install(monkeypatch)
    fixed_catalogue([second], budget=api_budget)
    run_three = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )
    assert [p["number"] for p in run_three] == [second["number"]], (
        f"CONTROL FAILED: the unseen live FOA {second['number']} did not post "
        f"({run_three}). A dedup that blocks everything would have passed step 2 — until "
        "this passes, step 2 means nothing"
    )
    assert await _claimed_numbers(db_session) == {first["number"], second["number"]}
    assert len(await _messages_for_run(db_session, sim_run.id)) == 2
    assert recorder.select_calls == recorder_three.select_calls == 1


# --------------------------------------------------------------------------- lead time


async def test_lead_time_filtering_against_live_close_dates(
    db_session, sim_run, live_catalogue, now_utc, slack_off, fixed_catalogue,
    monkeypatch, api_budget,
):
    """Both halves of the lead-time cut, against close dates grants.gov is serving today.

    The unit tests use hand-written dates. This one partitions the live catalogue and
    pushes one FOA from each side through the real pipeline, so it fails if grants.gov
    changes its date format, if MIN_LEAD_DAYS stops being applied, or if the filter is
    applied to the wrong side.

    Rule L3: if grants.gov happens to have nothing closing inside the window today, the
    reject half is unverifiable and this SKIPS with that reason rather than passing. The
    two guards below exist because a *skip* is how this test would otherwise hide the two
    changes it most needs to catch: the partition is drawn relative to MIN_LEAD_DAYS and
    from parsed dates, so lowering the constant to zero or breaking the parser empties the
    imminent side and turns a failure into a silent skip. Both were survivors in the
    mutation run until these guards were added.
    """
    assert grantbot.MIN_LEAD_DAYS >= 7, (
        f"MIN_LEAD_DAYS is {grantbot.MIN_LEAD_DAYS}. Below about a week the filter no "
        "longer does the job it was added for — a lab cannot prepare a credible response "
        "— and the imminent side of the partition below collapses, so this test would "
        "SKIP rather than fail. If the constant was lowered deliberately, lower this "
        "guard with it and say why"
    )
    parseable = [o for o in live_catalogue if days_out(o, now_utc) is not None]
    assert len(parseable) >= len(live_catalogue) * 0.5, (
        f"only {len(parseable)} of {len(live_catalogue)} live close_dates parse with "
        "_parse_close_date. grants.gov changed its date format or the parser broke; every "
        "FOA is now treated as rolling and the lead-time filter is off. Without this "
        "assertion the empty partition below would SKIP and hide it"
    )
    imminent = sorted(
        (o for o in live_catalogue
         if (d := days_out(o, now_utc)) is not None and 0 <= d <= grantbot.MIN_LEAD_DAYS - 3),
        key=lambda o: (days_out(o, now_utc), o["number"]),
    )
    roomy = [o for o in live_catalogue
             if (d := days_out(o, now_utc)) is not None and d >= grantbot.MIN_LEAD_DAYS + 3]
    if not imminent:
        pytest.skip(
            "CATALOGUE, not a failure: no posted grants.gov opportunity closes within "
            f"{grantbot.MIN_LEAD_DAYS - 3} days today, so the reject half of the "
            "lead-time filter cannot be exercised against a live date"
        )
    assert roomy, (
        f"no posted opportunity closes more than {grantbot.MIN_LEAD_DAYS + 3} days out — "
        "with no accept half, a filter that rejected everything would pass"
    )
    short, long = imminent[0], sorted(roomy, key=lambda o: o["number"])[0]

    # The pure function, on live date strings rather than invented ones.
    assert not grantbot._has_sufficient_lead_time(
        short["close_date"], now_utc, grantbot.MIN_LEAD_DAYS
    ), (
        f"{short['number']} closes {short['close_date']} "
        f"({days_out(short, now_utc)} days out) and passed a "
        f"{grantbot.MIN_LEAD_DAYS}-day lead-time filter"
    )
    assert grantbot._has_sufficient_lead_time(
        long["close_date"], now_utc, grantbot.MIN_LEAD_DAYS
    ), (
        f"CONTROL FAILED: {long['number']} closes {long['close_date']} "
        f"({days_out(long, now_utc)} days out) and was still rejected — the filter is "
        "dropping everything, so the rejection above says nothing about lead time"
    )

    # The pipeline: the recorder shows exactly which FOA reached the model.
    recorder = _StageRecorder().install(monkeypatch)
    fixed_catalogue([short, long], budget=api_budget)
    posted = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )

    assert short["number"] not in recorder.offered_to_select, (
        f"{short['number']} (closes {short['close_date']}, "
        f"{days_out(short, now_utc)} days out) reached the selection stage. Step 2b of "
        "_run_grantbot_with_session is meant to drop it — labs cannot prepare a credible "
        "response in that time, and the money is spent scoring an FOA that cannot be used"
    )
    assert long["number"] in recorder.offered_to_select, (
        f"CONTROL FAILED: {long['number']} (closes {long['close_date']}) did not reach "
        "the selection stage either. The filter dropped both, so the exclusion above is "
        "not evidence of lead-time filtering"
    )
    assert [p["number"] for p in posted] == [long["number"]], (
        f"expected only {long['number']} to post, got {[p['number'] for p in posted]}"
    )
    assert await _claimed_numbers(db_session) == {long["number"]}, (
        f"grantbot_posted_foas holds {sorted(await _claimed_numbers(db_session))} — an "
        "FOA that was filtered out must not be claimed"
    )


async def test_an_unparseable_close_date_turns_the_lead_time_filter_off(
    db_session, sim_run, live_catalogue, now_utc, slack_off, fixed_catalogue,
    monkeypatch, api_budget,
):
    """CHARACTERIZATION of a known asymmetry — this test asserts current behaviour, not
    desired behaviour, and must not be "fixed" into passing differently.

    `_parse_close_date` returns None for anything outside %m/%d/%Y, %Y-%m-%d and
    %Y/%m/%d, and `_has_sufficient_lead_time` reads None as "rolling submission, keep it".
    Live dates are %m/%d/%Y. So the day grants.gov switches to an ISO timestamp or a
    written-out month — a change no contract test can see, because those fixtures are
    hand-written — every FOA becomes rolling, the lead-time filter stops filtering, and
    nothing anywhere reports it.

    Same live opportunity, same real deadline, three renderings. The MM/DD/YYYY control
    proves the filter works on the real feed; the other two show it switched off.

    The two guards repeat those in the test above for the same mutation-run reason: this
    test selects its victim through `_parse_close_date` and `MIN_LEAD_DAYS`, so breaking
    either would empty the selection and skip instead of failing.
    """
    assert grantbot.MIN_LEAD_DAYS >= 7, (
        f"MIN_LEAD_DAYS is {grantbot.MIN_LEAD_DAYS} — too low to select an imminent FOA "
        "with, so this test would SKIP rather than report that the filter was weakened"
    )
    parseable = [o for o in live_catalogue if days_out(o, now_utc) is not None]
    assert len(parseable) >= len(live_catalogue) * 0.5, (
        f"only {len(parseable)} of {len(live_catalogue)} live close_dates parse — the "
        "format change this test *predicts* has happened; the lead-time filter is already "
        "disabled in production and this test must not skip past it"
    )
    imminent = sorted(
        (o for o in live_catalogue
         if (d := days_out(o, now_utc)) is not None and 0 <= d <= grantbot.MIN_LEAD_DAYS - 3),
        key=lambda o: (days_out(o, now_utc), o["number"]),
    )
    if not imminent:
        pytest.skip(
            "CATALOGUE, not a failure: nothing closes inside the lead-time window today, "
            "so there is no imminent FOA to smuggle past the filter"
        )
    victim = imminent[0]
    real_close = grantbot._parse_close_date(victim["close_date"])
    assert real_close is not None, (
        f"{victim['close_date']!r} is no longer parseable — grants.gov has ALREADY "
        "changed its date format and the lead-time filter is already disabled in "
        "production. That is the failure this test predicts"
    )

    # Control: as grants.gov actually serves it, the filter rejects.
    assert not grantbot._has_sufficient_lead_time(
        victim["close_date"], now_utc, grantbot.MIN_LEAD_DAYS
    ), f"CONTROL FAILED: {victim['number']} closing {victim['close_date']} was not rejected"

    plausible_reformats = {
        "ISO 8601 with time": real_close.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "written-out month": real_close.strftime("%d %b %Y"),
        "US long form": real_close.strftime("%B %d, %Y"),
    }
    for label, rendered in plausible_reformats.items():
        assert grantbot._parse_close_date(rendered) is None, (
            f"{label} ({rendered!r}) is parseable after all — update this test's list of "
            "formats grants.gov could plausibly move to"
        )
        assert grantbot._has_sufficient_lead_time(
            rendered, now_utc, grantbot.MIN_LEAD_DAYS
        ), (
            f"{label} no longer passes the lead-time filter. If _has_sufficient_lead_time "
            "has been changed to reject unparseable dates, rolling/standing FOAs (which "
            "legitimately have no deadline) are now being dropped — check that before "
            "editing this test"
        )

    # And at the pipeline level: an FOA closing in `days` days walks straight through.
    reformatted = dict(victim, close_date=real_close.strftime("%Y-%m-%dT%H:%M:%SZ"))
    recorder = _StageRecorder().install(monkeypatch)
    fixed_catalogue([reformatted], budget=api_budget)
    posted = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )
    assert recorder.offered_to_select == [victim["number"]], (
        "the reformatted date did NOT reach selection — behaviour has changed and the "
        "asymmetry this test characterizes may be fixed. Re-read _has_sufficient_lead_time"
    )
    assert [p["number"] for p in posted] == [victim["number"]], (
        "an FOA closing in "
        f"{days_out(victim, now_utc)} days was not posted despite passing the filter"
    )
    assert days_out(victim, now_utc) < grantbot.MIN_LEAD_DAYS, (
        "the opportunity chosen for this test is not actually imminent"
    )


# --------------------------------------------------------------------------- Slack leg


async def test_the_slack_leg_posts_through_a_double_and_releases_a_failed_claim(
    db_session, sim_run, biomedical_candidates, slack_on, fixed_catalogue,
    monkeypatch, api_budget,
):
    """The Slack branch, exercised without a Slack workspace.

    Both outcomes, because they are the two halves of one invariant — a claim exists iff
    the post landed:

    - the post succeeds: `chat_postMessage` is called with the full text and the claim stays;
    - the post raises: `_release_foa` removes the claim so the next run can retry.

    Also pins a real asymmetry: on the Slack branch GrantBot writes NOTHING to
    agent_messages. CLAUDE.md states the DB, not Slack, is the durable store, and every
    other writer in the system persists first.
    """
    assert biomedical_candidates, "no live NIH opportunity available"
    opportunity = biomedical_candidates[0]

    recorder = _StageRecorder(channel="chemical-biology").install(monkeypatch)
    fixed_catalogue([opportunity], budget=api_budget)
    posted = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )

    assert len(slack_on.instances) == 1, (
        f"{len(slack_on.instances)} Slack clients were constructed for one run"
    )
    client = slack_on.instances[0]
    assert [p["number"] for p in posted] == [opportunity["number"]]
    assert len(client.posts) == 1, (
        f"the Slack branch made {len(client.posts)} chat_postMessage call(s) for one "
        f"opportunity: {client.posts}"
    )
    sent = client.posts[0]
    assert sent["channel"] == "#chemical-biology", (
        f"posted to {sent['channel']!r}; the drafted channel must be sent with a leading "
        "'#', which is how the WebClient resolves a name rather than an id"
    )
    assert sent["text"].startswith(expected_header(opportunity)), (
        f"the Slack text does not start with the funding header:\n{sent['text'][:250]!r}"
    )
    assert opportunity["number"] in sent["text"] and recorder.drafted == [opportunity["number"]]
    assert client.joined, (
        "the bot never called conversations_join — GrantBot cannot post to a public "
        "channel it has not joined, so the first run in a fresh workspace would fail"
    )
    assert await _claimed_numbers(db_session) == {opportunity["number"]}, (
        "a successful Slack post left no grantbot_posted_foas row — the next run reposts it"
    )
    assert await _messages_for_run(db_session, sim_run.id) == [], (
        "GrantBot wrote a funding post to agent_messages on the Slack branch. That is not "
        "current behaviour (see _run_grantbot_with_session step 6, which returns straight "
        "after chat_postMessage); if it has changed, this pin should change with it"
    )

    # --- the failure half: a raising transport must release the claim
    _RecordingWebClient.next_fail_post = True
    other = biomedical_candidates[1]
    _StageRecorder(channel="chemical-biology").install(monkeypatch)
    fixed_catalogue([other], budget=api_budget)
    failed = await grantbot._run_grantbot_with_session(
        db_session, channel="funding-opportunities", dry_run=False,
        max_posts=5, max_per_channel=5,
    )
    assert failed == [], f"a failed Slack post was reported as posted: {failed}"
    assert other["number"] not in await _claimed_numbers(db_session), (
        f"{other['number']} is still claimed after chat_postMessage raised — the FOA is "
        "permanently retired: never posted, never retried. _release_foa did not run"
    )
    assert await _claimed_numbers(db_session) == {opportunity["number"]}, (
        "CONTROL FAILED: the successful claim was released too, so the release above is "
        "not evidence that failures specifically are rolled back"
    )


# --------------------------------------------------------------- the description bug


async def test_the_draft_prompt_is_built_from_an_empty_description(
    biomedical_candidates, monkeypatch, api_budget,
):
    """PINNED BUG, reported and deliberately unfixed — do not "fix" this test green.

    grants.gov `search2` returns no `description` field, so `search_opportunities` maps it
    to `""` and grantbot.py:306 interpolates that empty string into the drafting prompt.
    The prompt then asks the model to "summarize the scientific scope and goals" of an FOA
    it has been told nothing about beyond the title.

    Three halves, so a green run means "verified", not "could not look":

    1. live: every search2 hit has an empty `description`, while `title` is non-empty —
       the control that proves the response itself is not empty;
    2. the real `_draft_post` prompt, captured, carries `Description:` with nothing after it;
    3. the `Synopsis:` line, whose content depends on `fetch_opportunity_detail`. With the
       detail backend down (T3's finding, re-checked live here) the model is left with a
       title and nothing else; when it recovers, the assertion flips to requiring content.
    """
    opportunity = biomedical_candidates[0]

    api_budget.wait("grants")
    hits = await grants.search_opportunities("cancer", agencies=["HHS-NIH11"], rows=10)
    assert hits, (
        "search2 returned nothing for 'cancer' at HHS-NIH11 — grants.gov is down or the "
        "oppHits path moved; the description claim is unchecked either way"
    )
    assert all(h["title"].strip() for h in hits), (
        "CONTROL FAILED: live hits came back with empty titles too, so an empty "
        "description would just mean the whole response is empty"
    )
    with_description = [h["number"] for h in hits if h["description"]]
    assert not with_description, (
        f"search2 now returns a description for {with_description} — the bug at "
        "grantbot.py:306 (an empty description fed to the drafting LLM) may be gone. "
        "Verify and update the reported issue rather than deleting this assertion"
    )

    # Capture the prompt the real _draft_post builds, without spending a token on it.
    captured: dict[str, str] = {}

    async def _capture(system_prompt, messages, **kwargs):
        captured["system"] = system_prompt
        captured["user"] = messages[-1]["content"]
        return json.dumps({"channel": "funding-opportunities", "post_text": "captured"})

    monkeypatch.setattr("src.services.llm.generate_agent_response", _capture)

    api_budget.wait("grants")
    detail = await grants.fetch_opportunity_detail(str(opportunity["id"]))
    drafted = await grantbot._draft_post(detail or opportunity)
    assert drafted is not None and captured, "_draft_post never reached the LLM stage"

    # Slice the prompt on grantbot.py's own literal line prefixes rather than parsing
    # it: a description can contain newlines, and a line-wise parse would silently read
    # only its first line.
    prompt = captured["user"]
    for prefix in ("Title: ", "\nNumber: ", "\nAgency: ", "\nClose Date: ",
                   "\nDescription: ", "\nSynopsis: "):
        assert prefix in prompt, (
            f"the drafting prompt no longer contains a {prefix.strip()!r} line — "
            f"grantbot._draft_post's opp_text was restructured:\n{prompt[:400]!r}"
        )
    description = prompt[
        prompt.index("\nDescription: ") + len("\nDescription: "):prompt.index("\nSynopsis: ")
    ]
    synopsis = prompt[prompt.index("\nSynopsis: ") + len("\nSynopsis: "):]
    assert prompt.splitlines()[0].removeprefix("Title: ").strip(), (
        f"the drafting prompt has no Title either: {prompt[:300]!r}"
    )

    if detail is None:
        assert description == "", (
            "the drafting prompt now carries a Description even though "
            "fetch_opportunity_detail returned None and search2 supplies none. "
            f"Prompt:\n{prompt[:400]!r}"
        )
        assert synopsis == "", (
            f"unexpected Synopsis with no detail available: {prompt[:400]!r}"
        )
        assert len(captured["user"]) < 400, (
            "PROVIDER DOWN + the description bug together: grants.gov's fetchOpportunity "
            "backend is unavailable (T3's finding, still true) and search2 supplies no "
            "description, so the entire user prompt behind every funding post GrantBot "
            f"writes today is {len(captured['user'])} characters of title, number, agency "
            f"and close date:\n{captured['user']!r}\nIf this assertion fails the prompt "
            "grew — check whether the detail endpoint recovered"
        )
    else:
        assert (description + synopsis).strip(), (
            f"fetchOpportunity recovered and returned {sorted(detail)}, but the drafting "
            "prompt still has neither a Description nor a Synopsis — the mapping in "
            f"services/grants.py is dropping both. Prompt:\n{prompt[:400]!r}"
        )


# --------------------------------------------------------------- funding-rules validators


def test_the_announcement_detector_only_matches_first_person_openers():
    """CHARACTERIZATION of a real gap, found by the live test below. NOT a fix.

    `_ANNOUNCEMENT_PHRASES` in funding_rules.py is anchored on an explicit first-person
    subject — `I'll <verb>`, `I will <verb>`, `I'm going to <verb>`. Slack prose drops
    the subject, and every one of the three replies below (verbatim
    `claude-sonnet-4-6` output from the live test, 2026-07-30) announces a spin-off and
    is NOT flagged. The consequence is the incident the rule was written for: an agent
    replies "will spin up a thread", never does, and the funding thread dies with an
    announcement instead of a contribution.

    This test needs no network — it is here rather than in tests/unit/ so that the
    finding sits next to the live measurement that produced it, and so the paired
    controls can be read together. It is a pin, not an aspiration: if someone widens the
    phrase list, this test SHOULD fail, and the right response is to delete it.
    """
    pairs = [
        # (an equivalent the detector DOES catch, the real reply it MISSES)
        ("I'll spin up a dedicated thread for our group on this one later this week.",
         "will spin up a dedicated thread for our group on this one later this week"),
        ("I'm going to start a separate thread for coordinating our response to this.",
         "going to start a separate thread for coordinating our response to this — stay tuned"),
        ("I'll post a dedicated thread for this later today once I've reviewed it.",
         "will post a dedicated thread for this later today once i've had a chance to review"),
    ]
    for covered, subject_dropped in pairs:
        assert is_announcement_only_funding_reply(covered) is True, (
            "CONTROL FAILED: the first-person form is no longer caught either, so the "
            "miss below is not about the dropped subject — the detector is simply off. "
            f"Reply: {covered!r}"
        )
        assert is_announcement_only_funding_reply(subject_dropped) is False, (
            "the subject-dropped announcement is now caught. The gap this test pins has "
            "been closed (good) — delete this test and tighten the live one. "
            f"Reply: {subject_dropped!r}"
        )


@pytest.mark.real_llm
@needs_llm
async def test_funding_rules_validators_against_real_model_output(
    biomedical_candidates, api_budget,
):
    """The `funding_rules` validators, judged on prose a real model wrote.

    Every existing test of these regexes feeds them strings their author wrote while
    writing the regexes, which cannot show whether they match how a model actually
    phrases things. Two real calls: one asking for the non-compliant replies the rules
    exist to stop (announcement-only spin-off notices and social acknowledgments), one
    asking for compliant substantive replies.

    The two directions carry different weight and are asserted differently:

    - false POSITIVES (a real scientific reply silenced as an announcement or an "ack")
      are the damaging direction and the bar is zero;
    - false NEGATIVES are a real leak, but the *rate* is model output and would make this
      test flap. So the bar here is that each detector catches something (it is alive
      against prose it did not author) and that no miss was caused by
      `_SUBSTANTIVE_MARKERS_RE` firing on a reply with no science in it — that override
      exists to protect contributions, and an override that fires on empty replies is a
      worse bug than a phrase list that is merely incomplete. The measured miss rate is
      characterized deterministically in
      `test_the_announcement_detector_only_matches_first_person_openers`.

    Neither number means anything without the other: a detector returning True for
    everything scores perfectly on violations, and one returning False for everything
    scores perfectly on compliant replies.
    """
    from src.agent.funding_rules import _SUBSTANTIVE_MARKERS_RE
    from src.services.llm import generate_agent_response

    settings = grantbot.get_settings()
    foa = next(
        (o for o in biomedical_candidates
         if re.match(r"^(PAR?|RFA)-", o["number"], re.IGNORECASE)),
        biomedical_candidates[0],
    )
    context = (
        f"A GrantBot funding post in Slack:\n\n:moneybag: *Funding Opportunity*\n"
        f"*{foa['title']}*\n{foa['number']} | Closes: {foa['close_date']}\n"
    )

    violations_raw = await generate_agent_response(
        system_prompt=(
            "You are simulating replies that lab PI agents post in a Slack funding "
            "thread. Produce examples of two kinds of reply that the funding-thread "
            "rules forbid.\n\n"
            "\"announcement_only\": 5 replies that merely ANNOUNCE that the PI will "
            "create a dedicated spin-off thread later, instead of contributing. They "
            "must contain no scientific content at all — no aims, reagents, models, "
            "assays, techniques, targets or mechanisms.\n\n"
            "\"acknowledgment_only\": 5 purely social one-liners (thanks, agreement, "
            "confirmation). Under 100 characters, no question mark, no scientific "
            "content, and do NOT quote the FOA number.\n\n"
            "Write the way a terse scientist types in Slack. Respond with ONLY JSON: "
            '{"announcement_only": [...], "acknowledgment_only": [...]}'
        ),
        messages=[{"role": "user", "content": context}],
        model=settings.llm_agent_model_sonnet,
        max_tokens=900,
        log_meta={"agent_id": "grantbot", "phase": "t10-violations"},
    )
    compliant_raw = await generate_agent_response(
        system_prompt=(
            "You are simulating replies that lab PI agents post in a Slack funding "
            "thread. Produce 5 GOOD replies: each states a concrete scientific "
            "contribution to a joint application — a specific aim, a reagent, a model "
            "system, an assay or a platform the lab owns — in 2 to 3 sentences. Each "
            "reply must tag exactly one collaborator, chosen from @WisemanBot, "
            "@CravattBot and @PetrascheckBot, written exactly like that. Respond with "
            'ONLY JSON: {"substantive": [...]}'
        ),
        messages=[{"role": "user", "content": context}],
        model=settings.llm_agent_model_sonnet,
        # Comfortably above what five 2-3 sentence replies need: a stop_reason of
        # max_tokens makes generate_agent_response retry, which is a second billed call.
        max_tokens=1500,
        log_meta={"agent_id": "grantbot", "phase": "t10-compliant"},
    )

    def _parse(raw: str, key: str) -> list[str]:
        text = raw.strip()
        start = text.find("{")
        assert start >= 0, (
            f"the model did not return JSON for {key!r}; this test cannot proceed. "
            f"Raw:\n{raw[:600]!r}"
        )
        # raw_decode stops at the end of the first complete object. A stray trailing
        # brace — which this model has produced here — breaks a find/rfind slice.
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"the model's {key!r} response is not parseable JSON ({exc}). This is a "
                f"harness problem, not a funding_rules result. Raw:\n{raw[:600]!r}"
            )
        items = payload.get(key) or []
        assert len(items) >= 4, (
            f"the model returned {len(items)} {key!r} examples, too few to measure "
            f"against: {items}"
        )
        return [str(i) for i in items]

    announcements = _parse(violations_raw, "announcement_only")
    acks = _parse(violations_raw, "acknowledgment_only")
    substantive = _parse(compliant_raw, "substantive")

    caught_ann = [t for t in announcements if is_announcement_only_funding_reply(t)]
    missed_ann = [t for t in announcements if t not in caught_ann]
    assert caught_ann, (
        f"is_announcement_only_funding_reply caught NONE of {len(announcements)} "
        "announcement-only replies a real model wrote. Its phrase list no longer "
        "overlaps how a model phrases a spin-off announcement at all, and the atomic "
        "spin-off rule is unenforced. Replies:\n  "
        + "\n  ".join(repr(t) for t in announcements)
    )
    caught_ack = [t for t in acks if is_acknowledgment_only_funding_reply(t)]
    missed_ack = [t for t in acks if t not in caught_ack]
    assert caught_ack, (
        f"is_acknowledgment_only_funding_reply caught NONE of {len(acks)} "
        "acknowledgment-only replies. Replies:\n  "
        + "\n  ".join(repr(t) for t in acks)
    )
    for label, missed in (("announcement", missed_ann), ("acknowledgment", missed_ack)):
        for text in missed:
            marker = _SUBSTANTIVE_MARKERS_RE.search(text)
            assert marker is None, (
                f"an {label}-only reply with no scientific content was let through "
                f"because _SUBSTANTIVE_MARKERS_RE matched {marker.group(0)!r}. That "
                "override exists to stop the filters suppressing real contributions; "
                "firing on an empty reply means it is too broad and every violation "
                f"containing that word is now invisible. Reply: {text!r}"
            )

    false_ann = [t for t in substantive if is_announcement_only_funding_reply(t)]
    assert not false_ann, (
        "a substantive reply was classified as announcement-only and would have been "
        "suppressed — the damaging direction, because it silences a real scientific "
        "contribution:\n  " + "\n  ".join(repr(t) for t in false_ann)
    )
    false_ack = [t for t in substantive if is_acknowledgment_only_funding_reply(t)]
    assert not false_ack, (
        "a substantive reply was classified as acknowledgment-only:\n  "
        + "\n  ".join(repr(t) for t in false_ack)
    )

    # The summarizer, over the same real replies.
    log = MessageLog()
    root_ts = "1700000000.000001"
    log.append(LogEntry(
        ts=root_ts, channel="funding-opportunities", sender_agent_id=None,
        sender_name="GrantBot", content=context, thread_ts=None,
        posted_at=float(root_ts), is_bot=True,
    ))
    for index, text in enumerate(substantive, start=2):
        ts = f"1700000000.{index:06d}"
        log.append(LogEntry(
            ts=ts, channel="funding-opportunities", sender_agent_id="wiseman",
            sender_name="WisemanBot", content=text, thread_ts=root_ts,
            posted_at=float(ts), is_bot=True,
        ))

    summary = summarize_funding_thread(log, root_ts)
    assert len(summary.alignments) == len(substantive), (
        f"summarize_funding_thread recorded {len(summary.alignments)} alignments for "
        f"{len(substantive)} replies — a late joiner would be shown an incomplete thread"
    )
    # All replies share one sender, so the summarizer dedups pairings to one per tagged
    # bot. Compare against the tags actually present rather than against the three the
    # prompt offered — the model chooses which to use.
    tagged_bots = {
        m.group(1).lower() for t in substantive for m in re.finditer(r"@(\w+[Bb]ot)\b", t)
    }
    assert tagged_bots, (
        "the model tagged no collaborator in any of its replies, so the pairing half of "
        f"summarize_funding_thread is untested this run. Replies: {substantive}"
    )
    assert {b.lower() for _, b in summary.pairings_proposed} == tagged_bots, (
        f"the replies tag {sorted(tagged_bots)} but summarize_funding_thread reports "
        f"{sorted(b.lower() for _, b in summary.pairings_proposed)} — proposed "
        "collaborations are being lost from the summary a late joiner is shown"
    )
