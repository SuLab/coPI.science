"""Email notification scheduling, sending, and engagement tracking for proposal review."""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.config import get_settings
from src.models import (
    AgentDelegate,
    AgentRegistry,
    EmailEngagementTracker,
    EmailNotification,
    EmailNotificationPreference,
    ProposalReview,
    ThreadDecision,
    User,
)

logger = logging.getLogger(__name__)

# Frequency ladder for auto-downgrade (ordered from most to least frequent)
FREQUENCY_LADDER = ["daily", "twice_weekly", "weekly", "biweekly", "off"]

# How often each frequency should send (minimum interval in hours)
FREQUENCY_INTERVALS = {
    "daily": 24,
    "twice_weekly": 72,  # ~3 days; actual logic checks Mon/Thu
    "weekly": 168,  # 7 days
    "biweekly": 336,  # 14 days
    "monthly": 720,  # 30 days
}

# Days of week for twice_weekly (Monday=0, Thursday=3)
TWICE_WEEKLY_DAYS = {0, 3}

MISSED_THRESHOLD = 3  # emails without engagement before downgrade

# Defaults for table-backed categories (proposal_review is on User).
CATEGORY_DEFAULTS = {
    "status_overview": {"enabled": True, "frequency": "weekly"},
    "new_proposal": {"enabled": False, "frequency": "off"},
}

# Only consider proposals decided within this window for new-proposal alerts,
# so enabling the category doesn't blast the entire historical backlog.
NEW_PROPOSAL_LOOKBACK_DAYS = 7


async def get_or_create_pref(
    user_id, category: str, db: AsyncSession
) -> EmailNotificationPreference:
    """Fetch (or lazily create with category defaults) a notification preference row."""
    result = await db.execute(
        select(EmailNotificationPreference).where(
            EmailNotificationPreference.user_id == user_id,
            EmailNotificationPreference.category == category,
        )
    )
    pref = result.scalar_one_or_none()
    if pref is None:
        defaults = CATEGORY_DEFAULTS.get(category, {"enabled": True, "frequency": "weekly"})
        pref = EmailNotificationPreference(
            user_id=user_id,
            category=category,
            enabled=defaults["enabled"],
            frequency=defaults["frequency"],
        )
        db.add(pref)
        await db.flush()
    return pref


def _generate_unsubscribe_token(user_id: str) -> str:
    """Generate a signed unsubscribe token."""
    settings = get_settings()
    s = URLSafeTimedSerializer(settings.secret_key, salt="unsubscribe")
    return s.dumps(user_id)


def _verify_unsubscribe_token(token: str, max_age: int = 60 * 60 * 24 * 365) -> str | None:
    """Verify and decode an unsubscribe token. Returns user_id or None."""
    settings = get_settings()
    s = URLSafeTimedSerializer(settings.secret_key, salt="unsubscribe")
    try:
        return s.loads(token, max_age=max_age)
    except Exception:
        return None


def _is_time_to_send(frequency: str, last_sent_at: datetime | None) -> bool:
    """Check if it's time to send a notification based on frequency and last send time."""
    now = datetime.now(timezone.utc)

    if frequency == "off":
        return False

    # If never sent before, send now
    if last_sent_at is None:
        if frequency == "twice_weekly":
            return now.weekday() in TWICE_WEEKLY_DAYS
        return True

    if frequency == "daily":
        return (now - last_sent_at) >= timedelta(hours=20)  # ~daily with some slack

    if frequency == "twice_weekly":
        # Send on Mon/Thu, but not if we sent within 48h
        return now.weekday() in TWICE_WEEKLY_DAYS and (now - last_sent_at) >= timedelta(hours=48)

    if frequency == "weekly":
        return (now - last_sent_at) >= timedelta(days=6)

    if frequency == "biweekly":
        return (now - last_sent_at) >= timedelta(days=13)

    if frequency == "monthly":
        return (now - last_sent_at) >= timedelta(days=29)

    return False


async def _get_unreviewed_proposals_for_user(
    user: User, db: AsyncSession
) -> list[tuple[ThreadDecision, AgentRegistry]]:
    """Get all unreviewed proposals for agents this user has access to (as PI or delegate)."""
    # Get agent IDs this user has access to
    agent_ids = []

    # As PI
    if user.agent:
        agent_ids.append(user.agent.id)

    # As delegate
    delegate_result = await db.execute(
        select(AgentDelegate.agent_registry_id).where(
            AgentDelegate.user_id == user.id,
            AgentDelegate.notify_proposals.is_(True),
        )
    )
    agent_ids.extend(row[0] for row in delegate_result.all())

    if not agent_ids:
        return []

    # Get agents
    agents_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id.in_(agent_ids))
    )
    agents = {a.id: a for a in agents_result.scalars().all()}

    # Get all proposals where these agents are involved
    proposals = []
    for agent_db_id, agent in agents.items():
        td_result = await db.execute(
            select(ThreadDecision).where(
                ThreadDecision.outcome == "proposal",
                (ThreadDecision.agent_a == agent.agent_id)
                | (ThreadDecision.agent_b == agent.agent_id),
            )
        )
        for td in td_result.scalars().all():
            # Check if reviewed by this agent
            review_result = await db.execute(
                select(ProposalReview).where(
                    ProposalReview.thread_decision_id == td.id,
                    ProposalReview.agent_id == agent.agent_id,
                )
            )
            if not review_result.scalar_one_or_none():
                proposals.append((td, agent))

    # Sort by oldest first
    proposals.sort(key=lambda x: x[0].decided_at or x[0].id)
    return proposals


async def check_and_send_notifications(session_factory: async_sessionmaker) -> int:
    """Check all users and send proposal notification emails as needed.

    Returns the number of emails sent.
    """
    sent_count = 0
    async with session_factory() as db:
        # Get all users with email notifications enabled
        result = await db.execute(
            select(User)
            .options(selectinload(User.agent))
            .where(
                User.email_notification_frequency != "off",
                User.email_notifications_paused_by_system.is_(False),
                User.email.isnot(None),
            )
        )
        users = result.scalars().all()

        for user in users:
            try:
                sent = await _process_user_notifications(user, db)
                if sent:
                    sent_count += 1
            except Exception as exc:
                logger.error(
                    "Error processing notifications for user %s: %s",
                    user.id,
                    exc,
                    exc_info=True,
                )

        await db.commit()

    return sent_count


async def _process_user_notifications(user: User, db: AsyncSession) -> bool:
    """Process notifications for a single user. Returns True if an email was sent."""
    # Get or create engagement tracker
    tracker_result = await db.execute(
        select(EmailEngagementTracker).where(
            EmailEngagementTracker.user_id == user.id
        )
    )
    tracker = tracker_result.scalar_one_or_none()
    if not tracker:
        tracker = EmailEngagementTracker(user_id=user.id)
        db.add(tracker)
        await db.flush()

    # Check if it's time to send based on frequency
    if not _is_time_to_send(user.email_notification_frequency, tracker.last_notification_sent_at):
        return False

    # Check for outstanding (unanswered) proposal-review notification
    outstanding = await db.execute(
        select(EmailNotification).where(
            EmailNotification.user_id == user.id,
            EmailNotification.category == "proposal_review",
            EmailNotification.status == "sent",
        )
    )
    if outstanding.scalar_one_or_none():
        # There's already an unanswered email — check engagement and maybe downgrade
        await _check_engagement_and_downgrade(user, tracker, db)
        return False

    # Get unreviewed proposals
    proposals = await _get_unreviewed_proposals_for_user(user, db)
    if not proposals:
        return False

    # Send one email for the oldest unreviewed proposal
    td, agent = proposals[0]
    total_unreviewed = len(proposals)

    # Determine the other agent in the proposal
    other_agent_id = td.agent_b if td.agent_a == agent.agent_id else td.agent_a
    other_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == other_agent_id)
    )
    other_agent = other_result.scalar_one_or_none()
    other_bot_name = other_agent.bot_name if other_agent else other_agent_id

    success = await send_proposal_notification(
        user=user,
        thread_decision=td,
        agent=agent,
        other_bot_name=other_bot_name,
        total_unreviewed=total_unreviewed,
        db=db,
    )

    if success:
        tracker.last_notification_sent_at = datetime.now(timezone.utc)
        tracker.consecutive_missed += 1  # Will be reset if they engage

    return success


async def send_proposal_notification(
    user: User,
    thread_decision: ThreadDecision,
    agent: AgentRegistry,
    other_bot_name: str,
    total_unreviewed: int,
    db: AsyncSession,
) -> bool:
    """Compose and send a proposal notification email. Returns True on success."""
    settings = get_settings()

    from src.services.email import is_allowed_recipient
    if not is_allowed_recipient(user.email):
        logger.info(
            "Proposal notification to %s suppressed by outbound allowlist (proposal %s)",
            user.email,
            thread_decision.id,
        )
        return False

    reply_token = secrets.token_urlsafe(48)  # 64-char base64

    # Create notification record
    notification = EmailNotification(
        user_id=user.id,
        thread_decision_id=thread_decision.id,
        agent_registry_id=agent.id,
        reply_token=reply_token,
        category="proposal_review",
        status="sent",
    )
    db.add(notification)
    await db.flush()

    # Build email
    reply_to = f"review+{reply_token}@{settings.ses_reply_domain}"
    dashboard_url = f"{settings.base_url}/agent/{agent.agent_id}/dashboard"
    unsubscribe_token = _generate_unsubscribe_token(str(user.id))
    unsubscribe_url = f"{settings.base_url}/unsubscribe/{unsubscribe_token}"
    settings_url = f"{settings.base_url}/settings"

    summary = thread_decision.summary_text or "(No summary available)"
    channel = thread_decision.channel or "unknown"

    subject = f"{agent.bot_name} has a new collaboration proposal to review"

    # Backlog notice
    backlog_text = ""
    backlog_html = ""
    if total_unreviewed > 1:
        remaining = total_unreviewed - 1
        backlog_text = (
            f"\n---\nThere {'is' if remaining == 1 else 'are'} {remaining} additional "
            f"proposal{'s' if remaining > 1 else ''} waiting for review. Your agent is "
            f"blocked from starting new collaborations until proposals are reviewed. "
            f"You can review all proposals at {dashboard_url}\n"
        )
        backlog_html = (
            f'<div style="background: #fef3c7; border: 1px solid #f59e0b; border-radius: 8px; '
            f'padding: 12px 16px; margin-top: 20px;">'
            f'<p style="color: #92400e; font-size: 13px; margin: 0;">'
            f'There {"is" if remaining == 1 else "are"} <strong>{remaining}</strong> additional '
            f'proposal{"s" if remaining > 1 else ""} waiting for review. Your agent is blocked '
            f'from starting new collaborations until proposals are reviewed. '
            f'<a href="{dashboard_url}" style="color: #92400e; text-decoration: underline;">'
            f"Review all proposals</a>.</p></div>"
        )

    text_body = (
        f"{agent.bot_name} and {other_bot_name} developed a collaboration proposal in #{channel}:\n\n"
        f"---\n{summary}\n---\n\n"
        f"To review this proposal, you can:\n\n"
        f"1. Reply to this email with a rating (1-4) and any comments:\n"
        f"   1 = Not a good idea (not interesting, or multiple major weaknesses)\n"
        f"   2 = Good idea (medium interest, or one major weakness)\n"
        f"   3 = Great idea (high interest, minor weaknesses only)\n"
        f"   4 = Excellent idea (high interest, no notable weaknesses)\n\n"
        f"2. Reply with instructions for your agent (e.g., \"focus on the\n"
        f'   mitochondrial angle instead") and it will re-engage to refine\n'
        f"   the proposal.\n\n"
        f"3. Review on the web: {dashboard_url}\n"
        f"{backlog_text}\n"
        f"---\n"
        f"Unsubscribe: {unsubscribe_url}\n"
        f"Manage preferences: {settings_url}\n"
    )

    html_body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">
    <div style="text-align: center; margin-bottom: 32px;">
        <span style="font-size: 24px; font-weight: 700; color: #4f46e5;">CoPI</span>
        <span style="margin-left: 8px; font-size: 14px; color: #6b7280;">Research Collaboration</span>
    </div>
    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="margin: 0 0 8px; font-size: 18px; color: #111827;">New collaboration proposal</h2>
        <p style="color: #6b7280; font-size: 14px; margin: 0 0 20px;">
            {agent.bot_name} and {other_bot_name} in #{channel}
        </p>
        <div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 24px;">
            <p style="color: #374151; line-height: 1.6; margin: 0; font-size: 14px; white-space: pre-wrap;">{summary}</p>
        </div>

        <p style="color: #374151; font-size: 14px; font-weight: 600; margin: 0 0 8px;">Reply to this email to review:</p>
        <ul style="color: #374151; line-height: 1.8; margin: 0 0 8px; padding-left: 20px; font-size: 14px;">
            <li><strong>Rate it</strong> with a number 1-4 and any comments</li>
            <li><strong>Give instructions</strong> to refine the proposal</li>
        </ul>
        <p style="color: #9ca3af; font-size: 12px; margin: 0 0 20px;">
            1 = Not a good idea &bull; 2 = Good idea &bull; 3 = Great idea &bull; 4 = Excellent idea
        </p>

        <div style="text-align: center; margin: 24px 0;">
            <a href="{dashboard_url}"
               style="display: inline-block; padding: 12px 32px; background: #4f46e5; color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px;">
                Review on Web
            </a>
        </div>

        {backlog_html}
    </div>
    <div style="text-align: center; margin-top: 24px;">
        <a href="{unsubscribe_url}" style="color: #9ca3af; font-size: 12px; text-decoration: underline;">Unsubscribe</a>
        <span style="color: #d1d5db; margin: 0 8px;">|</span>
        <a href="{settings_url}" style="color: #9ca3af; font-size: 12px; text-decoration: underline;">Manage preferences</a>
    </div>
</div>"""

    try:
        import boto3

        client = boto3.client("ses", region_name=settings.aws_region)
        # Use send_raw_email to include List-Unsubscribe headers (RFC 8058)
        import email.mime.multipart
        import email.mime.text

        raw_msg = email.mime.multipart.MIMEMultipart("alternative")
        raw_msg["From"] = settings.ses_sender_email
        raw_msg["To"] = user.email
        raw_msg["Subject"] = subject
        raw_msg["Reply-To"] = reply_to
        raw_msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        raw_msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        raw_msg.attach(email.mime.text.MIMEText(text_body, "plain", "utf-8"))
        raw_msg.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))

        client.send_raw_email(
            Source=settings.ses_sender_email,
            Destinations=[user.email],
            RawMessage={"Data": raw_msg.as_string()},
        )
        logger.info(
            "Proposal notification sent to %s for %s (proposal %s)",
            user.email,
            agent.bot_name,
            thread_decision.id,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send proposal notification to %s: %s", user.email, exc)
        return False


async def _check_engagement_and_downgrade(
    user: User, tracker: EmailEngagementTracker, db: AsyncSession
) -> None:
    """Check if user has missed enough emails to warrant a frequency downgrade."""
    if tracker.consecutive_missed < MISSED_THRESHOLD:
        return

    current_idx = FREQUENCY_LADDER.index(user.email_notification_frequency)
    if current_idx >= len(FREQUENCY_LADDER) - 1:
        return  # Already at 'off'

    # Downgrade one notch
    new_frequency = FREQUENCY_LADDER[current_idx + 1]
    now = datetime.now(timezone.utc)

    if new_frequency == "off":
        # Send final "paused" email
        user.email_notification_frequency = "off"
        user.email_notifications_paused_by_system = True
        tracker.consecutive_missed = 0
        tracker.last_downgrade_at = now
        await _send_paused_email(user)
        logger.info("Auto-paused email notifications for user %s", user.id)
    else:
        user.email_notification_frequency = new_frequency
        tracker.consecutive_missed = 0
        tracker.last_downgrade_at = now
        logger.info(
            "Auto-downgraded email frequency for user %s to %s",
            user.id,
            new_frequency,
        )


async def _send_paused_email(user: User) -> None:
    """Send the 'notifications paused' email."""
    settings = get_settings()
    dashboard_url = f"{settings.base_url}/agent"
    settings_url = f"{settings.base_url}/settings"

    subject = "CoPI proposal notifications paused"

    text_body = (
        "We've paused your proposal notification emails since you haven't reviewed "
        "recently.\n\n"
        f"To turn them back on, log into CoPI and review your pending proposals: {dashboard_url}\n\n"
        f"You can adjust your notification frequency anytime in settings: {settings_url}\n"
    )

    html_body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 20px;">
    <div style="text-align: center; margin-bottom: 32px;">
        <span style="font-size: 24px; font-weight: 700; color: #4f46e5;">CoPI</span>
    </div>
    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="margin: 0 0 16px; font-size: 18px; color: #111827;">Notifications paused</h2>
        <p style="color: #374151; line-height: 1.6; margin: 0 0 16px;">
            We've paused your proposal notification emails since you haven't reviewed recently.
        </p>
        <p style="color: #374151; line-height: 1.6; margin: 0 0 24px;">
            To turn them back on, log into CoPI and review your pending proposals. You can
            adjust your notification frequency anytime in settings.
        </p>
        <div style="text-align: center; margin: 24px 0;">
            <a href="{dashboard_url}"
               style="display: inline-block; padding: 12px 32px; background: #4f46e5; color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px;">
                Review Proposals
            </a>
        </div>
        <div style="text-align: center;">
            <a href="{settings_url}" style="color: #6b7280; font-size: 13px; text-decoration: underline;">
                Manage notification preferences
            </a>
        </div>
    </div>
</div>"""

    try:
        import boto3

        client = boto3.client("ses", region_name=settings.aws_region)
        client.send_email(
            Source=settings.ses_sender_email,
            Destination={"ToAddresses": [user.email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
    except Exception as exc:
        logger.error("Failed to send paused notification to %s: %s", user.email, exc)


async def record_engagement(user_id, db: AsyncSession) -> None:
    """Record user engagement (review via web or email). Resets missed counter."""
    result = await db.execute(
        select(EmailEngagementTracker).where(
            EmailEngagementTracker.user_id == user_id
        )
    )
    tracker = result.scalar_one_or_none()
    if tracker:
        tracker.consecutive_missed = 0
        tracker.last_engagement_at = datetime.now(timezone.utc)


async def mark_notification_responded(
    user_id, thread_decision_id, response_type: str, db: AsyncSession
) -> None:
    """Mark outstanding email notifications for this user+proposal as responded.

    Clears any category (proposal_review reminder and/or new_proposal alert) so a
    single web review retires every outstanding email about that proposal.
    """
    result = await db.execute(
        select(EmailNotification).where(
            EmailNotification.user_id == user_id,
            EmailNotification.thread_decision_id == thread_decision_id,
            EmailNotification.status == "sent",
        )
    )
    for notification in result.scalars().all():
        notification.status = "responded"
        notification.response_type = response_type
        notification.responded_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Shared sending + agent-scope helpers
# ---------------------------------------------------------------------------


def _send_html_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    reply_to: str | None = None,
    unsubscribe_url: str | None = None,
) -> bool:
    """Send a multipart text+HTML email via SES. Honors the outbound allowlist."""
    settings = get_settings()
    from src.services.email import is_allowed_recipient

    if not is_allowed_recipient(to_email):
        logger.info(
            "Email to %s suppressed by outbound allowlist (subject=%r)", to_email, subject
        )
        return False
    try:
        import boto3
        import email.mime.multipart
        import email.mime.text

        client = boto3.client("ses", region_name=settings.aws_region)
        raw_msg = email.mime.multipart.MIMEMultipart("alternative")
        raw_msg["From"] = settings.ses_sender_email
        raw_msg["To"] = to_email
        raw_msg["Subject"] = subject
        if reply_to:
            raw_msg["Reply-To"] = reply_to
        if unsubscribe_url:
            raw_msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            raw_msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        raw_msg.attach(email.mime.text.MIMEText(text_body, "plain", "utf-8"))
        raw_msg.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
        client.send_raw_email(
            Source=settings.ses_sender_email,
            Destinations=[to_email],
            RawMessage={"Data": raw_msg.as_string()},
        )
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


async def _get_user_agent_records(user: User, db: AsyncSession) -> list[AgentRegistry]:
    """All agents this user has access to (own + delegated with notify_proposals)."""
    agent_ids = []
    if user.agent:
        agent_ids.append(user.agent.id)
    delegate_result = await db.execute(
        select(AgentDelegate.agent_registry_id).where(
            AgentDelegate.user_id == user.id,
            AgentDelegate.notify_proposals.is_(True),
        )
    )
    agent_ids.extend(row[0] for row in delegate_result.all())
    if not agent_ids:
        return []
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id.in_(agent_ids))
    )
    return list(result.scalars().all())


def _one_liner(summary_text: str | None, limit: int = 140) -> str:
    """Condense a proposal summary to a single short line."""
    if not summary_text:
        return "(no summary)"
    line = " ".join(summary_text.split())
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


# ---------------------------------------------------------------------------
# Category: status_overview (periodic digest)
# ---------------------------------------------------------------------------


async def check_and_send_status_overviews(session_factory: async_sessionmaker) -> int:
    """Send periodic activity-digest emails. Returns the number sent."""
    sent_count = 0
    async with session_factory() as db:
        result = await db.execute(
            select(User).options(selectinload(User.agent)).where(User.email.isnot(None))
        )
        users = result.scalars().all()
        for user in users:
            try:
                pref = await get_or_create_pref(user.id, "status_overview", db)
                if not pref.enabled:
                    continue
                if not _is_time_to_send(pref.frequency, pref.last_sent_at):
                    continue
                if await _send_status_overview(user, pref, db):
                    sent_count += 1
            except Exception as exc:
                logger.error(
                    "Error sending status overview for user %s: %s",
                    user.id, exc, exc_info=True,
                )
        await db.commit()
    return sent_count


async def _send_status_overview(
    user: User, pref: EmailNotificationPreference, db: AsyncSession
) -> bool:
    """Build and send one activity-digest email for the user's window."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    start = pref.last_sent_at or (now - timedelta(days=7))

    agents = await _get_user_agent_records(user, db)
    if not agents:
        return False
    my_ids = {a.agent_id for a in agents}

    td_result = await db.execute(
        select(ThreadDecision).where(
            ThreadDecision.decided_at >= start,
            (ThreadDecision.agent_a.in_(my_ids)) | (ThreadDecision.agent_b.in_(my_ids)),
        )
    )
    tds = list(td_result.scalars().all())
    if not tds:
        # Nothing happened this window — skip (and reset the clock so the next
        # window starts here rather than accumulating empty history).
        pref.last_sent_at = now
        return False

    proposals = [td for td in tds if td.outcome == "proposal"]
    no_proposal_count = sum(1 for td in tds if td.outcome == "no_proposal")

    # Ratings for proposals (max rating per proposal decides successful vs no-go)
    ratings_by_td: dict = {}
    if proposals:
        rev_result = await db.execute(
            select(ProposalReview.thread_decision_id, ProposalReview.rating).where(
                ProposalReview.thread_decision_id.in_([td.id for td in proposals])
            )
        )
        for td_id, rating in rev_result.all():
            ratings_by_td.setdefault(td_id, []).append(rating)

    successful = sum(
        1 for td in proposals if max(ratings_by_td.get(td.id, [0])) >= 3
    )
    no_go = no_proposal_count + sum(
        1 for td in proposals if 1 <= max(ratings_by_td.get(td.id, [0])) <= 2
    )

    # Counterpart agents (the bot that isn't ours), resolved to names
    counterpart_ids = set()
    for td in tds:
        counterpart_ids.add(td.agent_b if td.agent_a in my_ids else td.agent_a)
    name_result = await db.execute(
        select(AgentRegistry).where(
            AgentRegistry.agent_id.in_(my_ids | counterpart_ids)
        )
    )
    bot_names = {a.agent_id: a.bot_name for a in name_result.scalars().all()}

    def _bot(aid: str) -> str:
        return bot_names.get(aid, aid)

    collaborators = sorted({_bot(c) for c in counterpart_ids})

    # One-liners for proposals (cap to keep the email digestible)
    def _status_label(td) -> str:
        ratings = ratings_by_td.get(td.id)
        if not ratings:
            return "awaiting your review"
        top = max(ratings)
        return {4: "rated Excellent idea", 3: "rated Great idea", 2: "rated Good idea", 1: "rated Not a good idea"}.get(top, "reviewed")

    idea_lines = []
    for td in proposals[:8]:
        mine = td.agent_a if td.agent_a in my_ids else td.agent_b
        other = td.agent_b if mine == td.agent_a else td.agent_a
        idea_lines.append(
            (f"{_bot(mine)} × {_bot(other)}", _one_liner(td.summary_text), _status_label(td))
        )

    period = f"{start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    dashboard_url = f"{settings.base_url}/agent"
    unsubscribe_token = _generate_unsubscribe_token(str(user.id))
    unsubscribe_url = f"{settings.base_url}/unsubscribe/{unsubscribe_token}"
    settings_url = f"{settings.base_url}/settings"

    subject = (
        f"Your CoPI activity: {len(proposals)} "
        f"idea{'s' if len(proposals) != 1 else ''}, "
        f"{len(collaborators)} collaborator{'s' if len(collaborators) != 1 else ''}"
    )

    # Plain text
    text_lines = [
        f"Your CoPI activity — {period}",
        "",
        f"{len(proposals)} ideas proposed · {len(collaborators)} collaborators "
        f"· {successful} promising · {no_go} no-go · {len(tds)} conversations",
        "",
        "Ideas discussed:",
    ]
    for pair, summary, status in idea_lines:
        text_lines.append(f"  • [{pair}] {summary} — {status}")
    if not idea_lines:
        text_lines.append("  (no new proposals this period)")
    text_lines += [
        "",
        f"Collaborators: {', '.join(collaborators) if collaborators else '(none)'}",
        "",
        f"Review pending proposals: {dashboard_url}",
        f"Manage notifications: {settings_url}",
        f"Unsubscribe: {unsubscribe_url}",
    ]
    text_body = "\n".join(text_lines)

    idea_html = "".join(
        f'<li style="margin-bottom: 8px;"><span style="color:#4f46e5;font-weight:600;">'
        f'{pair}</span><br><span style="color:#374151;">{summary}</span> '
        f'<span style="color:#9ca3af;font-size:12px;">— {status}</span></li>'
        for pair, summary, status in idea_lines
    ) or '<li style="color:#9ca3af;">No new proposals this period.</li>'

    html_body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">
    <div style="text-align: center; margin-bottom: 24px;">
        <span style="font-size: 24px; font-weight: 700; color: #4f46e5;">CoPI</span>
        <span style="margin-left: 8px; font-size: 14px; color: #6b7280;">Activity overview</span>
    </div>
    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="margin: 0 0 4px; font-size: 18px; color: #111827;">Your CoPI activity</h2>
        <p style="color: #6b7280; font-size: 13px; margin: 0 0 20px;">{period}</p>
        <div style="display:flex; flex-wrap:wrap; gap:8px; margin-bottom:24px;">
            <span style="background:#eef2ff;color:#3730a3;border-radius:6px;padding:6px 10px;font-size:13px;">{len(proposals)} ideas proposed</span>
            <span style="background:#eef2ff;color:#3730a3;border-radius:6px;padding:6px 10px;font-size:13px;">{len(collaborators)} collaborators</span>
            <span style="background:#ecfdf5;color:#065f46;border-radius:6px;padding:6px 10px;font-size:13px;">{successful} promising</span>
            <span style="background:#fef2f2;color:#991b1b;border-radius:6px;padding:6px 10px;font-size:13px;">{no_go} no-go</span>
            <span style="background:#f9fafb;color:#374151;border-radius:6px;padding:6px 10px;font-size:13px;">{len(tds)} conversations</span>
        </div>
        <p style="color:#374151;font-size:14px;font-weight:600;margin:0 0 8px;">Ideas discussed</p>
        <ul style="margin:0 0 20px;padding-left:18px;font-size:14px;line-height:1.5;">{idea_html}</ul>
        <p style="color:#6b7280;font-size:13px;margin:0 0 20px;">
            <strong>Collaborators:</strong> {', '.join(collaborators) if collaborators else '(none)'}
        </p>
        <div style="text-align:center;margin:24px 0;">
            <a href="{dashboard_url}" style="display:inline-block;padding:12px 32px;background:#4f46e5;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;font-size:14px;">Review pending proposals</a>
        </div>
    </div>
    <div style="text-align:center;margin-top:24px;">
        <a href="{unsubscribe_url}" style="color:#9ca3af;font-size:12px;text-decoration:underline;">Unsubscribe</a>
        <span style="color:#d1d5db;margin:0 8px;">|</span>
        <a href="{settings_url}" style="color:#9ca3af;font-size:12px;text-decoration:underline;">Manage preferences</a>
    </div>
</div>"""

    sent = _send_html_email(
        user.email, subject, text_body, html_body, unsubscribe_url=unsubscribe_url
    )
    if sent:
        pref.last_sent_at = now
        logger.info(
            "Status overview sent to %s (%d proposals, %d collaborators)",
            user.email, len(proposals), len(collaborators),
        )
    return sent


# ---------------------------------------------------------------------------
# Category: new_proposal (event-driven)
# ---------------------------------------------------------------------------


async def check_and_send_new_proposal_emails(session_factory: async_sessionmaker) -> int:
    """Send a one-off email when an agent generates a new proposal. Returns count sent."""
    sent_count = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEW_PROPOSAL_LOOKBACK_DAYS)
    async with session_factory() as db:
        td_result = await db.execute(
            select(ThreadDecision).where(
                ThreadDecision.outcome == "proposal",
                ThreadDecision.decided_at >= cutoff,
            )
        )
        proposals = list(td_result.scalars().all())
        for td in proposals:
            for agent_id_str in (td.agent_a, td.agent_b):
                try:
                    if await _maybe_send_new_proposal(td, agent_id_str, db):
                        sent_count += 1
                except Exception as exc:
                    logger.error(
                        "Error sending new-proposal email (proposal %s, agent %s): %s",
                        td.id, agent_id_str, exc, exc_info=True,
                    )
        await db.commit()
    return sent_count


async def _maybe_send_new_proposal(
    td: ThreadDecision, agent_id_str: str, db: AsyncSession
) -> bool:
    """Send a new-proposal email to each enabled recipient for one side of the proposal."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id_str)
    )
    agent = agent_result.scalar_one_or_none()
    if agent is None:
        return False

    # Recipients: owning PI + delegates opted into proposal notifications.
    recipients: list[User] = []
    if agent.user_id:
        pi = await db.get(User, agent.user_id)
        if pi and pi.email:
            recipients.append(pi)
    deleg_result = await db.execute(
        select(User)
        .join(AgentDelegate, AgentDelegate.user_id == User.id)
        .where(
            AgentDelegate.agent_registry_id == agent.id,
            AgentDelegate.notify_proposals.is_(True),
            User.email.isnot(None),
        )
    )
    recipients.extend(deleg_result.scalars().all())

    other_agent_id = td.agent_b if td.agent_a == agent.agent_id else td.agent_a
    other_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == other_agent_id)
    )
    other_agent = other_result.scalar_one_or_none()
    other_bot_name = other_agent.bot_name if other_agent else other_agent_id

    any_sent = False
    seen: set = set()
    for user in recipients:
        if user.id in seen:
            continue
        seen.add(user.id)

        pref = await get_or_create_pref(user.id, "new_proposal", db)
        if not pref.enabled:
            continue

        # Dedup: one new_proposal email per user per proposal.
        existing = await db.execute(
            select(EmailNotification).where(
                EmailNotification.user_id == user.id,
                EmailNotification.thread_decision_id == td.id,
                EmailNotification.category == "new_proposal",
            )
        )
        if existing.scalar_one_or_none():
            continue

        if await _send_new_proposal_email(user, td, agent, other_bot_name, db):
            any_sent = True

    return any_sent


async def _send_new_proposal_email(
    user: User,
    td: ThreadDecision,
    agent: AgentRegistry,
    other_bot_name: str,
    db: AsyncSession,
) -> bool:
    """Compose and send a single new-proposal email; logs an EmailNotification."""
    settings = get_settings()
    reply_token = secrets.token_urlsafe(48)

    notification = EmailNotification(
        user_id=user.id,
        thread_decision_id=td.id,
        agent_registry_id=agent.id,
        reply_token=reply_token,
        category="new_proposal",
        status="sent",
    )
    db.add(notification)
    await db.flush()

    summary = td.summary_text or "(No summary available)"
    channel = td.channel or "unknown"
    reply_to = f"review+{reply_token}@{settings.ses_reply_domain}"
    dashboard_url = f"{settings.base_url}/agent/{agent.agent_id}/dashboard"
    unsubscribe_token = _generate_unsubscribe_token(str(user.id))
    unsubscribe_url = f"{settings.base_url}/unsubscribe/{unsubscribe_token}"
    settings_url = f"{settings.base_url}/settings"

    subject = f"{agent.bot_name} proposed a collaboration with {other_bot_name}"

    text_body = (
        f"{agent.bot_name} just proposed a collaboration with {other_bot_name} in #{channel}:\n\n"
        f"---\n{summary}\n---\n\n"
        f"Reply to this email to rate it (1-4) or give your agent instructions, "
        f"or review on the web: {dashboard_url}\n\n"
        f"---\n"
        f"Unsubscribe: {unsubscribe_url}\n"
        f"Manage preferences: {settings_url}\n"
    )

    html_body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">
    <div style="text-align: center; margin-bottom: 24px;">
        <span style="font-size: 24px; font-weight: 700; color: #4f46e5;">CoPI</span>
        <span style="margin-left: 8px; font-size: 14px; color: #6b7280;">New proposal</span>
    </div>
    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="margin: 0 0 4px; font-size: 18px; color: #111827;">\U0001f9ea {agent.bot_name} × {other_bot_name}</h2>
        <p style="color: #6b7280; font-size: 13px; margin: 0 0 20px;">#{channel}</p>
        <div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 24px;">
            <p style="color: #374151; line-height: 1.6; margin: 0; font-size: 14px; white-space: pre-wrap;">{summary}</p>
        </div>
        <p style="color:#374151;font-size:14px;margin:0 0 8px;">
            Reply to this email to <strong>rate it (1–4)</strong> or <strong>give instructions</strong>.
        </p>
        <div style="text-align:center;margin:24px 0;">
            <a href="{dashboard_url}" style="display:inline-block;padding:12px 32px;background:#4f46e5;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;font-size:14px;">Review this proposal</a>
        </div>
    </div>
    <div style="text-align:center;margin-top:24px;">
        <a href="{unsubscribe_url}" style="color:#9ca3af;font-size:12px;text-decoration:underline;">Unsubscribe</a>
        <span style="color:#d1d5db;margin:0 8px;">|</span>
        <a href="{settings_url}" style="color:#9ca3af;font-size:12px;text-decoration:underline;">Manage preferences</a>
    </div>
</div>"""

    sent = _send_html_email(
        user.email, subject, text_body, html_body,
        reply_to=reply_to, unsubscribe_url=unsubscribe_url,
    )
    if sent:
        logger.info(
            "New-proposal email sent to %s for %s (proposal %s)",
            user.email, agent.bot_name, td.id,
        )
    return sent
