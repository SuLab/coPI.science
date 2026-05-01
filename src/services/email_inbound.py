"""Inbound email processing for proposal review via email reply."""

import email
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import get_settings
from src.models import (
    AgentRegistry,
    EmailNotification,
    ProposalReview,
    ThreadDecision,
    User,
)
from src.services.email_notifications import mark_notification_responded, record_engagement

logger = logging.getLogger(__name__)

# Rate limit: max replies per token per hour
MAX_REPLIES_PER_TOKEN_PER_HOUR = 10


async def poll_inbound_emails(session_factory: async_sessionmaker) -> int:
    """Poll S3 for new inbound emails and process them.

    Returns the number of emails processed.
    """
    settings = get_settings()
    processed = 0

    try:
        import boto3

        s3 = boto3.client("s3", region_name=settings.aws_region)
        bucket = settings.ses_inbound_s3_bucket
        prefix = settings.ses_inbound_s3_prefix

        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=50)
        objects = response.get("Contents", [])

        for obj in objects:
            key = obj["Key"]
            if key == prefix:  # Skip the prefix itself
                continue

            try:
                email_obj = s3.get_object(Bucket=bucket, Key=key)
                raw_email = email_obj["Body"].read()

                async with session_factory() as db:
                    await process_inbound_email(raw_email, db)
                    await db.commit()

                # Delete processed email from S3
                s3.delete_object(Bucket=bucket, Key=key)
                processed += 1

            except Exception as exc:
                logger.error("Error processing inbound email %s: %s", key, exc, exc_info=True)

    except Exception as exc:
        logger.error("Error polling inbound emails: %s", exc, exc_info=True)

    if processed:
        logger.info("Processed %d inbound emails", processed)
    return processed


async def process_inbound_email(raw_email: bytes, db: AsyncSession) -> None:
    """Parse and process a single inbound email."""
    msg = email.message_from_bytes(raw_email)

    # Extract reply token from To header
    to_addr = msg.get("To", "")
    token = _extract_reply_token(to_addr)
    if not token:
        logger.warning("No reply token found in To address: %s", to_addr)
        return

    # Look up notification by token
    result = await db.execute(
        select(EmailNotification).where(EmailNotification.reply_token == token)
    )
    notification = result.scalar_one_or_none()
    if not notification:
        logger.warning("No notification found for token: %s...", token[:8])
        return

    if notification.status != "sent":
        logger.info("Notification %s already %s, ignoring reply", notification.id, notification.status)
        return

    # Verify sender
    from_addr = _extract_email_address(msg.get("From", ""))
    user_result = await db.execute(
        select(User).where(User.id == notification.user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        logger.error("User %s not found for notification %s", notification.user_id, notification.id)
        return

    if user.email and from_addr and from_addr.lower() != user.email.lower():
        logger.warning(
            "Sender email mismatch: expected %s, got %s (notification %s)",
            user.email,
            from_addr,
            notification.id,
        )
        return

    # Extract reply body
    body = _extract_reply_body(msg)
    if not body or not body.strip():
        logger.info("Empty reply body for notification %s", notification.id)
        return

    # Get proposal context
    td_result = await db.execute(
        select(ThreadDecision).where(ThreadDecision.id == notification.thread_decision_id)
    )
    td = td_result.scalar_one_or_none()
    if not td:
        logger.error("ThreadDecision %s not found", notification.thread_decision_id)
        return

    # Classify reply via LLM
    classification = await classify_reply(body, td.summary_text or "")

    category = classification.get("category", "unparseable")

    if category == "review":
        rating = classification.get("rating")
        comment = classification.get("comment", "")
        if not rating or rating < 1 or rating > 4:
            category = "unparseable"
        else:
            await _handle_review(
                user=user,
                notification=notification,
                td=td,
                rating=rating,
                comment=comment,
                db=db,
            )
            await record_engagement(user.id, db)
            await mark_notification_responded(user.id, td.id, "review", db)
            await _send_review_confirmation(user, notification, td, rating, db)
            return

    if category == "instruction":
        instruction = classification.get("instruction", body)
        await _handle_instruction(
            user=user,
            notification=notification,
            td=td,
            instruction=instruction,
            db=db,
        )
        await record_engagement(user.id, db)
        await mark_notification_responded(user.id, td.id, "instruction", db)
        await _send_instruction_confirmation(user, notification, td, db)
        return

    # Unparseable
    await _send_help_email(user, notification)
    logger.info("Unparseable reply for notification %s from %s", notification.id, from_addr)


def _extract_reply_token(to_address: str) -> str | None:
    """Extract reply token from an address like review+TOKEN@reply.copi.science."""
    match = re.search(r"review\+([A-Za-z0-9_-]+)@", to_address)
    return match.group(1) if match else None


def _extract_email_address(from_header: str) -> str | None:
    """Extract bare email from a From header like 'Name <email@example.com>'."""
    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1)
    # Maybe it's just a bare email
    if "@" in from_header:
        return from_header.strip()
    return None


def _extract_reply_body(msg: email.message.Message) -> str:
    """Extract the reply body, stripping quoted content and signatures."""
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                body = part.get_payload(decode=True).decode(charset, errors="replace")
                break
    else:
        charset = msg.get_content_charset() or "utf-8"
        body = msg.get_payload(decode=True).decode(charset, errors="replace")

    # Strip quoted content (lines starting with >)
    lines = body.split("\n")
    cleaned = []
    for line in lines:
        # Stop at signature delimiter
        if line.strip() == "--":
            break
        # Skip quoted lines
        if line.startswith(">"):
            continue
        # Stop at common "On ... wrote:" patterns
        if re.match(r"^On .+ wrote:$", line.strip()):
            break
        cleaned.append(line)

    return "\n".join(cleaned).strip()


async def classify_reply(body: str, proposal_summary: str) -> dict:
    """Classify an email reply using Sonnet LLM.

    Returns dict with keys: category, rating, comment, instruction
    """
    from src.services.llm import get_anthropic_client

    system_prompt = "You classify email replies to collaboration proposal notifications. Respond with only valid JSON."

    user_message = f"""You are classifying an email reply to a collaboration proposal notification.

The proposal summary the user was asked to review:
---
{proposal_summary}
---

The user's reply:
---
{body}
---

Classify this reply into one of three categories:

1. "review" — The reply contains a rating (1-4) of the proposal, and optionally a comment.
   Extract the rating as an integer 1-4 and any additional text as the comment.

2. "instruction" — The reply contains instructions for the AI agent about how to refine,
   adjust, or continue working on the proposal. The user is NOT rating it but wants changes.
   Extract the full instruction text.

3. "unparseable" — You cannot determine whether this is a review or an instruction.

Respond with a JSON object:
{{"category": "review|instruction|unparseable", "rating": null or 1-4, "comment": "extracted comment or empty string", "instruction": "extracted instruction or empty string"}}

Respond with only the JSON object, no other text."""

    try:
        settings = get_settings()
        client = get_anthropic_client()
        message = client.messages.create(
            model=settings.llm_agent_model_sonnet,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        response_text = message.content[0].text.strip()

        # Handle potential markdown code blocks
        if response_text.startswith("```"):
            response_text = re.sub(r"^```(?:json)?\n?", "", response_text)
            response_text = re.sub(r"\n?```$", "", response_text)

        return json.loads(response_text)
    except Exception as exc:
        logger.error("LLM classification failed: %s", exc)
        return {"category": "unparseable", "rating": None, "comment": "", "instruction": ""}


async def _handle_review(
    user: User,
    notification: EmailNotification,
    td: ThreadDecision,
    rating: int,
    comment: str,
    db: AsyncSession,
) -> None:
    """Create a ProposalReview from an email reply."""
    # Get the agent
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == notification.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    # Check if already reviewed
    existing = await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == td.id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )
    if existing.scalar_one_or_none():
        logger.info("Proposal %s already reviewed for agent %s", td.id, agent.agent_id)
        return

    # Determine if this is the PI or a delegate
    is_owner = agent.user_id == user.id

    review = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,  # Always the PI
        delegate_user_id=user.id if not is_owner else None,
        reviewed_by_user_id=user.id,
        rating=rating,
        comment=comment.strip() or None,
        submitted_via="email",
    )
    db.add(review)
    await db.flush()
    logger.info(
        "Email review created: user=%s agent=%s rating=%d proposal=%s",
        user.id,
        agent.agent_id,
        rating,
        td.id,
    )


async def _handle_instruction(
    user: User,
    notification: EmailNotification,
    td: ThreadDecision,
    instruction: str,
    db: AsyncSession,
) -> None:
    """Post PI guidance to the Slack thread, same as the web reopen_proposal flow."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == notification.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    try:
        from slack_sdk import WebClient

        settings = get_settings()
        env_tokens = settings.get_slack_tokens()
        bot_token = env_tokens.get(agent.agent_id, "")

        if not bot_token or bot_token.startswith("xoxb-placeholder"):
            logger.error("No bot token for agent %s", agent.agent_id)
            return

        client = WebClient(token=bot_token)

        # Find channel ID
        channels_result = client.conversations_list(
            types="public_channel,private_channel", limit=200
        )
        channel_id = None
        for ch in channels_result.get("channels", []):
            if ch["name"] == td.channel:
                channel_id = ch["id"]
                break

        if not channel_id:
            logger.error("Channel #%s not found for instruction posting", td.channel)
            return

        message = f"*PI guidance from {user.name} (via email):*\n\n{instruction}"
        client.chat_postMessage(
            channel=channel_id,
            text=message,
            thread_ts=td.thread_id,
        )
        logger.info(
            "PI %s posted email guidance in proposal thread %s via %s",
            user.name,
            td.thread_id,
            agent.agent_id,
        )

        # Create a review record with rating=0 (reopened) like the web flow
        existing = await db.execute(
            select(ProposalReview).where(
                ProposalReview.thread_decision_id == td.id,
                ProposalReview.agent_id == agent.agent_id,
            )
        )
        if not existing.scalar_one_or_none():
            is_owner = agent.user_id == user.id
            review = ProposalReview(
                thread_decision_id=td.id,
                agent_id=agent.agent_id,
                user_id=agent.user_id,
                delegate_user_id=user.id if not is_owner else None,
                reviewed_by_user_id=user.id,
                rating=0,  # 0 = reopened with guidance
                comment=f"[Reopened via email] {instruction[:500]}",
                submitted_via="email",
            )
            db.add(review)

    except Exception as exc:
        logger.error("Failed to post PI guidance to Slack: %s", exc)


async def _send_review_confirmation(
    user: User,
    notification: EmailNotification,
    td: ThreadDecision,
    rating: int,
    db: AsyncSession,
) -> None:
    """Send confirmation email after a review is processed."""
    settings = get_settings()

    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == notification.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    other_agent_id = td.agent_b if td.agent_a == agent.agent_id else td.agent_a
    other_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == other_agent_id)
    )
    other_agent = other_result.scalar_one_or_none()
    other_name = other_agent.bot_name if other_agent else other_agent_id

    subject = f"Review received - {other_name} proposal rated {rating}"
    text_body = (
        f"Got it - you rated the {other_name} collaboration proposal a {rating}. "
        f"{agent.bot_name} is unblocked and can start new conversations."
    )

    _send_simple_email(user.email, subject, text_body)


async def _send_instruction_confirmation(
    user: User,
    notification: EmailNotification,
    td: ThreadDecision,
    db: AsyncSession,
) -> None:
    """Send confirmation email after an instruction is processed."""
    settings = get_settings()

    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == notification.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    other_agent_id = td.agent_b if td.agent_a == agent.agent_id else td.agent_a
    other_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == other_agent_id)
    )
    other_agent = other_result.scalar_one_or_none()
    other_name = other_agent.bot_name if other_agent else other_agent_id

    subject = f"Instructions received - {agent.bot_name} will refine proposal"
    text_body = (
        f"Got it - I've passed your feedback to {agent.bot_name}. "
        f"It will re-engage with {other_name} to refine the proposal. "
        f"You'll get another email when the revised proposal is ready."
    )

    _send_simple_email(user.email, subject, text_body)


async def _send_help_email(user: User, notification: EmailNotification) -> None:
    """Send help email when a reply can't be parsed."""
    subject = "CoPI - Could not process your reply"
    text_body = (
        "I couldn't tell if you wanted to rate this proposal or give your agent instructions.\n\n"
        "To rate: reply with a number 1-4 and any comments.\n"
        "  1 = Not interesting\n"
        "  2 = Weak - unlikely to pursue\n"
        "  3 = Promising - worth exploring further\n"
        "  4 = Strong - let's pursue this\n\n"
        "To direct your agent: describe what you'd like changed (e.g., "
        '"focus on the mitochondrial angle instead").\n'
    )

    _send_simple_email(user.email, subject, text_body)


def _send_simple_email(to_email: str, subject: str, text_body: str) -> bool:
    """Send a simple text email via SES."""
    settings = get_settings()
    try:
        import boto3

        client = boto3.client("ses", region_name=settings.aws_region)
        client.send_email(
            Source=settings.ses_sender_email,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False
