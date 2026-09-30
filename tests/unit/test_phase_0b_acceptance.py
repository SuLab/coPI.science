"""No removed symbol, route or setting is referenced in src/, templates/, static/ or
scripts/."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
RETIRED = re.compile(
    r"review_proposal|reopen_proposal|_extract_proposal_title|_is_lost_review_race"
    r"|_refuse_integrity_error|_REVIEW_UNIQUE_CONSTRAINT|AgentBadgeMiddleware"
    r"|agent_badge_count|pi_inbox|record_pi_message|get_latest_run_id"
    r"|_poll_inbound_from_db|_seed_pi_inbox_cursor|_pi_inbox_cursor|PI_INBOX_LOOKBACK"
    r"|EPOCH_UTC|pending_proposals|ProposalRef|proposal_thread:|reviews_by_decision"
    r"|proposal_counts|email_notifications\.py|services\.email_notifications"
    r"|email_inbound|/settings/save|/settings/unsubscribe|unsubscribe\.html"
    r"|_generate_unsubscribe_token|_verify_unsubscribe_token|CATEGORY_DEFAULTS"
    r"|notification_check_interval|llm_agent_model_sonnet|enable_inbound_email"
    r"|inbound_poll_interval|ses_inbound_s3|ses_reply_domain|classify_reply"
    r"|poll_inbound_emails|_sync_private_channels_from_db"
    r"|_rewind_cursors_for_private_channels|_private_channel_members\b"
    r"|_finalized_private_channels|_PRIVATE_CHANNEL_ACTIVE_WINDOW_S|visibility_lookup"
    r"|_is_private_channel|BotNotInvitedToPrivateChannel|proposal-vote|ProposalVoteIn"
    r"|ProposalVoteDetailsIn|_vote_limiter|services\.rate_limit|SlidingWindowRateLimiter"
    r"|ORIGINLESS_POST_PREFIXES"
)
SUFFIXES = {".py", ".sh", ".html", ".js", ".css", ".md", ".toml", ".json"}


def test_no_retired_symbol_is_referenced():
    hits = []
    for top in ("src", "templates", "static", "scripts"):
        for path in (ROOT / top).rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES or "__pycache__" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if RETIRED.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert hits == [], "\n".join(hits)
