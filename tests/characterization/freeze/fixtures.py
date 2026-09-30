"""Deterministic inputs for the prompt-freeze golden suite (spec §11).

The suite pins what TODAY's code sends. These helpers remove the inputs that would
make a capture depend on the machine instead of the code: the host's `.env` (it
sets model names and limits in production) and the wall clock (message
timestamps). Everything here is FROZEN together with the snapshot — editing a
constant invalidates what the snapshot proves (spec §11 freeze rules).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from src.agent.agent import Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, AssessmentReview, OpportunityAssessment, SpecialistConsult
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from tests import factories
from tests.assessment_chat_support import (
    CONSULT_CONCERN,
    CONSULT_ESTABLISHED,
    CONSULT_QTA,
    CONSULT_QUESTION,
    HUB_QUESTION_TEXT,
    PITCH_TEXT,
    STAFF_ONLY_TEXT,
    VERDICT_TEXT,
)
from tests.fakes import FakeSlackClient

HUB_ID = "blackbird"
LAB_ID = "wang"
OTHER_ID = "gordy"
INTERVIEW_CHANNEL = "single-cell-omics"
CHANNEL_IDS = {
    "general": "C_general",
    INTERVIEW_CHANNEL: "C_interview",
    ASSESSMENTS_SUMMARY_CHANNEL: "C_summary",
}
#: Below FakeSlackClient's own ts counter (1_700_000_000), so a message the engine
#: posts during a capture sorts AFTER every seeded message.
SEED_BASE = 1_600_000_000

#: Every setting a captured request or text reads. Environment variables beat
#: `.env` in pydantic-settings, so this overrides the host's production file.
#: The two rate-limit settings are deliberately NOT pinned (CLAUDE.md).
FROZEN_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": "golden-not-a-real-key",
    "LLM_PROFILE_MODEL": "golden-profile-model",
    "LLM_AGENT_MODEL": "golden-agent-model",
    "LLM_AGENT_MODEL_OPUS": "golden-opus-model",
    "LLM_REVIEW_MODEL": "golden-review-model",
    "LLM_ASSESSMENT_CHAT_MODEL": "claude-opus-5-5",
    "ASSESSMENT_CHAT_EFFORT": "medium",
    "ASSESSMENT_CHAT_MAX_TURNS": "50",
    "ASSESSMENT_CHAT_MAX_QUESTION_CHARS": "4000",
    "ASSESSMENT_CHAT_DAILY_QUESTION_LIMIT": "100",
    "ASSESSMENT_CHAT_DAILY_USER_USD_LIMIT": "20.0",
    "ASSESSMENT_CHAT_DAILY_TOTAL_USD_LIMIT": "100.0",
    "MAX_THREAD_MESSAGES": "12",
    "ACTIVE_THREAD_THRESHOLD": "3",
    "MAX_ABSTRACTS_OTHER_PER_THREAD": "10",
    "MAX_FULL_TEXT_PER_THREAD": "2",
    "PANEL_NOTES_IN_THREAD": "true",
    "COHORT_ISOLATION_ENABLED": "false",
    "PHASE5_SKIP_PROBABILITY": "0.0",
    "LAB_DAILY_POST_CAP": "1",
    "REPLY_LANE_MAX_IN_FLIGHT": "4",
    "NCBI_API_KEY": "",
    "NCBI_CONTACT_EMAIL": "golden@example.org",
    "USPTO_API_KEY": "golden-uspto-key",
    "PATENTSVIEW_API_KEY": "",
    "COHORT_DEFAULT_POLICY": "open",
    "ENVIRONMENT": "development",
    "SLACK_ENABLED": "true",
}

LAB_PROFILE = """# Jane Wang Lab — Public Profile

**PI:** Jane Wang
**Institution:** Johns Hopkins University

## Research Summary

We build isogenic single-cell organoid panels for colorectal cancer and screen small molecule drug combinations against them.

## Key Methods and Technologies

- single-cell RNA sequencing
- CRISPR base editing

## Recent Publications

- An isogenic organoid panel for colorectal targets. *Cell Reports* (2024). https://doi.org/10.1016/j.celrep.2024.000001
- Base editing in patient-derived organoids. *Nature Methods* (2023). https://doi.org/10.1038/s41592-023-000002

## Active Grants

- R01 Organoid screening platform
"""

OTHER_LAB_PROFILE = """# Sam Gordy Lab — Public Profile

**PI:** Sam Gordy

## Research Summary

Structural biology of covalent chemical probes, mass spectrometry chemoproteomics.

## Recent Publications

- Covalent probes for kinase pockets. *Cell* (2022). https://doi.org/10.1016/j.cell.2022.000003
"""


def reset_settings_cache() -> None:
    from src.config import get_settings

    get_settings.cache_clear()


def apply_frozen_settings(monkeypatch):
    """Pin FROZEN_ENV for this test and return the rebuilt Settings."""
    from src.config import get_settings

    for key, value in FROZEN_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def install_profiles(tmp_path: Path, monkeypatch) -> Path:
    """A private profiles/ tree with the two lab profiles; every reader repointed."""
    root = tmp_path / "profiles"
    (root / "public").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / "public" / f"{LAB_ID}.md").write_text(LAB_PROFILE, encoding="utf-8")
    (root / "public" / f"{OTHER_ID}.md").write_text(OTHER_LAB_PROFILE, encoding="utf-8")
    for target in (
        "src.agent.agent.PROFILES_DIR",
        "src.agent.tools.PROFILES_DIR",
        "src.agent.engine.constants.PROFILES_DIR",
    ):
        monkeypatch.setattr(target, root)
    return root


def build_engine() -> tuple[SimulationEngine, dict[str, Agent], dict[str, FakeSlackClient]]:
    """A DB-less engine: one hub, two labs, a connected FakeSlackClient each."""
    agents = {
        "hub": Agent(HUB_ID, "BlackbirdBot", "Blackbird", role="scout_hub"),
        "lab": Agent(LAB_ID, "WangBot", "Jane Wang", role="pi_lab"),
        "other": Agent(OTHER_ID, "GordyBot", "Sam Gordy", role="pi_lab"),
    }
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in agents.values()}
    sim = SimulationEngine(agents=list(agents.values()), slack_clients=clients)
    sim._channel_id_map.update(CHANNEL_IDS)
    sim._assessments_summary_channel_id = CHANNEL_IDS[ASSESSMENTS_SUMMARY_CHANNEL]
    for client in clients.values():
        client.cache_channel_ids(dict(CHANNEL_IDS))
    # A LIVE engine: `_reply_to_thread` passes `should_continue=lambda:
    # self._running` to generate_with_tools, and a never-started engine (False)
    # would end every tool loop after round 0 — the stop path, not the normal one.
    sim._running = True
    return sim, agents, clients


def seed_thread(
    sim: SimulationEngine,
    *,
    replier: Agent,
    partner: Agent,
    thread_id: str,
    prior: int,
    channel: str = INTERVIEW_CHANNEL,
    root_by_replier: bool = False,
) -> ThreadState:
    """Seed ``prior`` messages (root by ``partner`` unless ``root_by_replier``,
    then alternating) into the engine's real MessageLog and register the
    replier's active ThreadState.

    Seeded into the log rather than only onto ``message_count``:
    `_reply_to_thread` recomputes the count from the log (src/agent/CLAUDE.md).
    """
    base = int(float(thread_id))
    starter, answerer = (replier, partner) if root_by_replier else (partner, replier)
    for i in range(prior):
        sender = starter if i % 2 == 0 else answerer
        ts = thread_id if i == 0 else f"{base + i}.000000"
        sim.message_log.append(LogEntry(
            ts=ts,
            channel=channel,
            sender_agent_id=sender.agent_id,
            sender_name=sender.bot_name,
            content=f"{sender.bot_name} message {i} in thread {thread_id}",
            thread_ts=None if i == 0 else thread_id,
            posted_at=float(ts),
            is_bot=True,
            slack_ts=ts,
            slack_channel_id=CHANNEL_IDS[channel],
        ))
    thread = ThreadState(
        thread_id=thread_id,
        channel=channel,
        other_agent_id=partner.agent_id,
        message_count=prior,
        has_pending_reply=True,
    )
    replier.state.active_threads[thread_id] = thread
    return thread


CHAT_RUN_ID = uuid.UUID("00000000-0000-0000-0000-00000000c100")
CHAT_ASSESSMENT_ID = uuid.UUID("00000000-0000-0000-0000-00000000c101")
CHAT_REVIEW_ID = uuid.UUID("00000000-0000-0000-0000-00000000c102")
CHAT_CHANNEL = "chat-golden-channel"
PROVISIONAL_TEXT = "CHAT-GOLDEN-PROVISIONAL-VERDICT: decline until n is larger."


async def seed_frozen_interview(db_session) -> uuid.UUID:
    """`seed_interview` (tests/assessment_chat_support.py) on a FIXED clock with
    FIXED ids, in today's post-supersession shape: an earlier provisional hub
    verdict reply whose row the engine retired (its verdict survives only on a
    duplicate_thread_verdict drop), the concluding reply carrying the stored
    verdict (`slack_ts`), one consult, and a review re-pointed onto the row.
    """
    run = await factories.make_simulation_run(
        db_session, id=CHAT_RUN_ID, config={},
        started_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
    )
    base = float(SEED_BASE + 800)
    root_ts = f"{base:.6f}"
    verdict_ts = f"{base + 120:.6f}"
    for agent_id, ts, thread_ts, phase, content, at in (
        (LAB_ID, root_ts, None, "new_post", PITCH_TEXT, base),
        (HUB_ID, f"{base + 60:.6f}", root_ts, "thread_reply", HUB_QUESTION_TEXT, base + 60),
        (HUB_ID, f"{base + 100:.6f}", root_ts, "thread_reply", PROVISIONAL_TEXT, base + 100),
        (HUB_ID, verdict_ts, root_ts, "thread_reply", VERDICT_TEXT, base + 120),
    ):
        await factories.make_agent_message(
            db_session, run=run, agent_id=agent_id, channel_name=CHAT_CHANNEL,
            message_ts=ts, thread_ts=thread_ts, phase=phase, content=content, posted_at=at,
        )
    db_session.add(SpecialistConsult(
        simulation_run_id=run.id, agent_id=HUB_ID, subject_agent_id=LAB_ID,
        thread_id=root_ts, channel_name=CHAT_CHANNEL, domain="clinical",
        question=CONSULT_QUESTION, verdict_signal="adequate", confidence="high",
        concerns=[CONSULT_CONCERN], questions_to_ask=[CONSULT_QTA],
        raw_opinion="CHAT-GOLDEN-RAW-OPINION", established=[CONSULT_ESTABLISHED],
        read_state="parsed", created_at=datetime.fromtimestamp(base + 90, UTC),
    ))
    db_session.add(AssessmentDrop(
        simulation_run_id=run.id, agent_id=HUB_ID, subject_agent_id=LAB_ID,
        thread_id=root_ts, reason="duplicate_thread_verdict",
        detail="verdict from message ordinal 3 superseded by the interview's "
               "concluding verdict at ordinal 4",
        raw_verdict={"recommendation": "decline"},
        created_at=datetime.fromtimestamp(base + 121, UTC),
    ))
    db_session.add(OpportunityAssessment(
        id=CHAT_ASSESSMENT_ID, simulation_run_id=run.id, agent_id=HUB_ID,
        subject_agent_id=LAB_ID, channel_name=CHAT_CHANNEL, slack_ts=verdict_ts,
        thread_id=root_ts, company_or_project="Chat Golden Co",
        headline="CHAT-GOLDEN-HEADLINE an isogenic panel for colorectal targets",
        recommendation="conditional", confidence="Moderate", weighted_score=3.2,
        band="conditional",
        gating={"life_sciences_domain": "met", "translational_potential": "unconfirmed"},
        scores={"scientific_credibility": 4}, red_flags=["CHAT-GOLDEN-RED-FLAG"],
        rationale="CHAT-GOLDEN-RATIONALE", strengths=[STAFF_ONLY_TEXT], panel_owed=True,
        rubric_version=RUBRIC_VERSION, rubric_content_hash=RUBRIC_CONTENT_HASH,
        raw_verdict={"sentinel": "CHAT-GOLDEN-RAW-VERDICT"},
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    ))
    await db_session.flush()
    db_session.add(AssessmentReview(
        id=CHAT_REVIEW_ID, assessment_id=CHAT_ASSESSMENT_ID, reviewer_user_id=None,
        reviewer_name="GOLDEN-CHAT-REVIEWER", score=3, feedback_mode="learn",
        comment="Re-pointed from the retired provisional row.",
        created_at=datetime(2026, 9, 2, 9, 30, tzinfo=UTC),
    ))
    await db_session.flush()
    return CHAT_ASSESSMENT_ID
