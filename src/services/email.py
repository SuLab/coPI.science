"""Email sending via AWS SES."""

import logging

logger = logging.getLogger(__name__)


def send_delegate_invitation(
    to_email: str,
    pi_name: str,
    bot_name: str,
    invite_url: str,
) -> bool:
    """Send a delegate invitation email via AWS SES. Returns True on success."""
    from src.config import get_settings
    settings = get_settings()

    subject = f"{pi_name} invited you to join their lab on CoPI"

    text_body = (
        f"{pi_name} has invited you as a delegate for {bot_name} on CoPI.\n\n"
        f"As a delegate, you can view and manage the lab's AI agent, "
        f"review collaboration proposals, and provide guidance.\n\n"
        f"Accept the invitation: {invite_url}\n\n"
        f"This invitation expires in 30 days.\n"
        f"You'll sign in with your ORCID account.\n"
    )

    html_body = f"""<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 560px; margin: 0 auto; padding: 40px 20px;">
    <div style="text-align: center; margin-bottom: 32px;">
        <span style="font-size: 24px; font-weight: 700; color: #4f46e5;">CoPI</span>
        <span style="margin-left: 8px; font-size: 14px; color: #6b7280;">Research Collaboration</span>
    </div>
    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="margin: 0 0 16px; font-size: 18px; color: #111827;">You've been invited</h2>
        <p style="color: #374151; line-height: 1.6; margin: 0 0 16px;">
            <strong>{pi_name}</strong> has invited you as a delegate for
            <strong>{bot_name}</strong> on CoPI. As a delegate, you can:
        </p>
        <ul style="color: #374151; line-height: 1.8; margin: 0 0 24px; padding-left: 20px;">
            <li>View and manage the lab's AI agent</li>
            <li>Review collaboration proposals</li>
            <li>Edit agent instructions and provide guidance</li>
        </ul>
        <div style="text-align: center; margin: 24px 0;">
            <a href="{invite_url}"
               style="display: inline-block; padding: 12px 32px; background: #4f46e5; color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px;">
                Accept Invitation
            </a>
        </div>
        <p style="color: #9ca3af; font-size: 13px; margin: 24px 0 0; text-align: center;">
            This invitation expires in 30 days. You'll sign in with your ORCID account.
        </p>
    </div>
</div>"""

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
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
        logger.info("Invitation email sent to %s for %s", to_email, bot_name)
        return True
    except Exception as exc:
        logger.error("Failed to send invitation email to %s: %s", to_email, exc)
        return False
