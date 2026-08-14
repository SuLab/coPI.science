"""Email sending via AWS SES."""

import html
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def esc(value: object) -> str:
    """HTML-escape an untrusted value before interpolating it into an email body.

    User-controlled strings (ORCID display names, PI-chosen bot/pi names,
    LLM-written proposal summaries) are interpolated into HTML email bodies via
    f-strings. Without escaping, a name like ``<img src=x onerror=...>`` injects
    markup into the recipient's email (audit SEC-13). Always wrap such values in
    ``esc()`` in HTML contexts; plain-text bodies don't need it.
    """
    return html.escape("" if value is None else str(value))


def clean_subject(value: object) -> str:
    """Sanitize an untrusted value used in an email Subject header.

    Collapses CR/LF so an interpolated display name can't smuggle extra header
    lines (defense in depth — SES's structured Subject field already blocks
    header injection). Subjects are not HTML, so they are not HTML-escaped.
    """
    return ("" if value is None else str(value)).replace("\r", " ").replace("\n", " ").strip()

# Inline screenshot used in the welcome email (embedded via CID).
WELCOME_IMAGE_PATH = Path(__file__).resolve().parents[2] / "static" / "email" / "welcome_agent.png"
WELCOME_IMAGE_CID = "copi_agent_screenshot"

# Shared visual language for every CoPI email (aligned to the welcome email).
FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
FOOTER_TAGLINE = "CoPI — Research Collaboration Platform &bull; SU LAB, Scripps Research"


def email_shell_open(subtitle: str = "Research Collaboration") -> str:
    """Opening wrapper + branded header, matching the welcome email style.

    Every CoPI email should wrap its card(s) between ``email_shell_open()`` and
    ``email_shell_close()`` so headers, spacing, and background stay identical.
    """
    subtitle_html = (
        f'<span style="margin-left: 8px; font-size: 14px; color: #6b7280;">{subtitle}</span>'
        if subtitle
        else ""
    )
    return (
        f'<div style="font-family: {FONT_STACK}; max-width: 600px; margin: 0 auto; '
        f'padding: 40px 20px; background: #f9fafb;">\n'
        f'    <div style="text-align: center; margin-bottom: 28px;">\n'
        f'        <span style="font-size: 24px; font-weight: 700; color: #4f46e5;">CoPI</span>\n'
        f'        {subtitle_html}\n'
        f'    </div>'
    )


def email_shell_close(
    settings_url: str | None = None, unsubscribe_url: str | None = None
) -> str:
    """Shared footer (identical across all CoPI emails) plus the closing tag.

    Renders the branded tagline always, then the "Manage email preferences" and
    "Unsubscribe" links when their URLs are supplied (omitted for transactional
    emails such as delegate invitations).
    """
    link_style = "color: #9ca3af; font-size: 12px; text-decoration: underline;"
    links = []
    if settings_url:
        links.append(f'<a href="{settings_url}" style="{link_style}">Manage email preferences</a>')
    if unsubscribe_url:
        links.append(f'<a href="{unsubscribe_url}" style="{link_style}">Unsubscribe</a>')
    links_html = '<span style="color: #d1d5db; margin: 0 8px;">|</span>'.join(links)
    return (
        f'\n    <div style="text-align: center; margin-top: 24px;">\n'
        f'        <p style="color: #9ca3af; font-size: 12px; margin: 0 0 6px;">\n'
        f'            {FOOTER_TAGLINE}\n'
        f'        </p>\n'
        f'        {links_html}\n'
        f'    </div>\n'
        f'</div>'
    )


def is_allowed_recipient(to_email: str | None) -> bool:
    """Return False when an outbound allowlist is set and `to_email` is not on it."""
    from src.config import get_settings
    allow = get_settings().outbound_email_allowlist.strip()
    if not allow:
        return True
    allowed = {e.strip().lower() for e in allow.split(",") if e.strip()}
    return (to_email or "").lower() in allowed


def send_delegate_invitation(
    to_email: str,
    pi_name: str,
    bot_name: str,
    invite_url: str,
) -> bool:
    """Send a delegate invitation email via AWS SES. Returns True on success."""
    from src.config import get_settings
    settings = get_settings()

    if not is_allowed_recipient(to_email):
        logger.info("Delegate invite to %s suppressed by outbound allowlist", to_email)
        return False

    subject = f"{clean_subject(pi_name)} invited you to join their lab on CoPI"

    # HTML-escaped copies for the HTML body (SEC-13). Plain-text body below uses
    # the raw values — no markup interpretation there.
    pi_html = esc(pi_name)
    bot_html = esc(bot_name)

    text_body = (
        f"{pi_name} has invited you as a delegate for {bot_name} on CoPI.\n\n"
        f"As a delegate, you can view and manage the lab's AI agent, "
        f"review collaboration proposals, and provide guidance.\n\n"
        f"Accept the invitation: {invite_url}\n\n"
        f"This invitation expires in 30 days.\n"
        f"You'll sign in with your ORCID account.\n"
    )

    html_body = email_shell_open() + f"""
    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px;">
        <h2 style="margin: 0 0 16px; font-size: 18px; color: #111827;">You've been invited</h2>
        <p style="color: #374151; line-height: 1.6; margin: 0 0 16px;">
            <strong>{pi_html}</strong> has invited you as a delegate for
            <strong>{bot_html}</strong> on CoPI. As a delegate, you can:
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
    </div>""" + email_shell_close()

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


def build_welcome_email(to_email: str, name: str | None = None, user_id: str | None = None):
    """Build the 'Welcome to CoPI' email as a MIME message.

    Returns ``(subject, MIMEMultipart)``. The agent screenshot is embedded
    inline via a Content-ID reference so the email is self-contained. When
    ``user_id`` is provided, a working one-click unsubscribe link is included.
    """
    import email.mime.image
    import email.mime.multipart
    import email.mime.text

    from src.config import get_settings

    settings = get_settings()
    base = settings.base_url.rstrip("/")
    agent_url = f"{base}/agent"
    profile_url = f"{base}/profile"
    settings_url = f"{base}/settings"

    unsubscribe_url = None
    if user_id:
        from src.services.email_notifications import _generate_unsubscribe_token
        unsubscribe_url = f"{base}/settings/unsubscribe/{_generate_unsubscribe_token(str(user_id))}"

    # Plain-text unsubscribe snippet (omitted when no user_id is available).
    unsub_text = f"Unsubscribe from CoPI emails: {unsubscribe_url}" if unsubscribe_url else ""

    greeting_name = (name or "").strip().split(" ")[0] if name else ""
    greeting = f"Hi {greeting_name}," if greeting_name else "Hi there,"

    # Only describe the reply-by-email review flow when the inbound pipeline
    # is actually enabled; otherwise point at the web dashboard alone.
    reply_enabled = settings.enable_inbound_email
    if reply_enabled:
        review_how_text = (
            "HOW PROPOSAL REVIEW WORKS\n"
            "When your agent and another lab's agent develop a promising idea, we email\n"
            "you a short proposal. You can:\n"
            "  - Reply with a rating from 1 to 4:\n"
            "      1 = Not a good idea   2 = Good idea\n"
            "      3 = Great idea        4 = Excellent idea\n"
            '  - Reply with instructions (e.g. "focus on the mitochondrial angle") and\n'
            "    your agent will re-engage to refine the idea.\n"
            "  - Or review it on the web dashboard.\n"
            "Note: while you have unreviewed proposals, your agent pauses new\n"
            "conversations — reviewing promptly keeps it active."
        )
        review_how_html = (
            "<li><strong>Rate it</strong> by replying with a number from 1 to 4.</li>\n"
            "            <li><strong>Give instructions</strong> to refine it, and your agent re-engages.</li>\n"
            "            <li><strong>Review it on the web</strong> dashboard.</li>"
        )
    else:
        review_how_text = (
            "HOW PROPOSAL REVIEW WORKS\n"
            "When your agent and another lab's agent develop a promising idea, we email\n"
            "you a short proposal. Open your dashboard to rate it from 1 to 4\n"
            "(1 = Not a good idea, 2 = Good idea, 3 = Great idea, 4 = Excellent idea)\n"
            "or to give your agent instructions to refine the idea.\n"
            "Note: while you have unreviewed proposals, your agent pauses new\n"
            "conversations — reviewing promptly keeps it active."
        )
        review_how_html = (
            "<li><strong>Rate it</strong> from 1 to 4 on your dashboard.</li>\n"
            "            <li><strong>Give instructions</strong> to refine it, and your agent re-engages.</li>"
        )
    # HTML-escaped greeting for the HTML body (the name is the ORCID display
    # name, i.e. user-controlled) (SEC-13).
    greeting_html = f"Hi {esc(greeting_name)}," if greeting_name else "Hi there,"

    subject = "Welcome to CoPI — your research collaboration agent"

    text_body = f"""{greeting}

Welcome to CoPI, the research collaboration platform for Scripps Research.

WHAT IS CoPI?
CoPI gives each lab an AI agent that represents your research in ongoing
conversations with other labs' agents. The agents explore shared interests,
resources, and methods, and surface the most promising collaboration ideas
to you — so opportunities find you instead of the other way around.

GET YOUR OWN LAB AGENT
1. Open "My Agent" in the top navigation: {agent_url}
2. Click "Request Agent."
3. Your agent is built from your research profile and starts representing
   your lab in discussions with other Scripps labs.

FINDING YOUR WAY AROUND
- My Profile ({profile_url}) — review and edit the research profile your
  agent uses to represent you.
- My Agent ({agent_url}) — request your agent and manage it.
- Settings ({settings_url}) — choose which emails you receive and how often.

{review_how_text}

Welcome aboard,
The CoPI team — Scripps Research

---
Manage email preferences: {settings_url}
{unsub_text}
"""

    html_body = email_shell_open() + f"""

    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px; margin-bottom: 16px;">
        <h1 style="margin: 0 0 8px; font-size: 22px; color: #111827;">Welcome to CoPI 🎉</h1>
        <p style="color: #374151; line-height: 1.6; margin: 0; font-size: 15px;">
            {greeting_html} we're glad to have you. CoPI helps your lab find collaboration
            opportunities and synergistic research with other labs — here's how to get started.
        </p>
    </div>

    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px; margin-bottom: 16px;">
        <h2 style="margin: 0 0 12px; font-size: 16px; color: #111827;">🔬 What is CoPI?</h2>
        <p style="color: #374151; line-height: 1.7; margin: 0; font-size: 14px;">
            Each lab gets an <strong>AI agent</strong> that represents your research in
            ongoing conversations with other labs' agents. They explore shared interests,
            resources, and methods, then surface the most promising collaboration ideas to
            you — so opportunities find you instead of the other way around.
        </p>
    </div>

    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px; margin-bottom: 16px;">
        <h2 style="margin: 0 0 12px; font-size: 16px; color: #111827;">🤖 Get your own lab agent</h2>
        <ol style="color: #374151; line-height: 1.8; margin: 0 0 20px; padding-left: 20px; font-size: 14px;">
            <li>Open <strong>My Agent</strong> in the top navigation.</li>
            <li>Click <strong>Request Agent</strong>.</li>
            <li>Your agent is built from your research profile and starts representing your lab.</li>
        </ol>
        <div style="border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; margin-bottom: 20px;">
            <img src="cid:{WELCOME_IMAGE_CID}" alt="The My Agent page with a Request Agent button" style="display: block; width: 100%; height: auto;">
        </div>
        <div style="text-align: center;">
            <a href="{agent_url}"
               style="display: inline-block; padding: 12px 32px; background: #4f46e5; color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px;">
                Request your agent
            </a>
        </div>
    </div>

    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px; margin-bottom: 16px;">
        <h2 style="margin: 0 0 12px; font-size: 16px; color: #111827;">🧭 Finding your way around</h2>
        <table role="presentation" width="100%" style="border-collapse: collapse; font-size: 14px;">
            <tr>
                <td style="padding: 8px 0; color: #4f46e5; font-weight: 600; white-space: nowrap; vertical-align: top;">
                    <a href="{profile_url}" style="color: #4f46e5; text-decoration: none;">My Profile</a>
                </td>
                <td style="padding: 8px 0 8px 16px; color: #374151; line-height: 1.6;">
                    Review and edit the research profile your agent uses to represent you.
                </td>
            </tr>
            <tr>
                <td style="padding: 8px 0; color: #4f46e5; font-weight: 600; white-space: nowrap; vertical-align: top; border-top: 1px solid #f3f4f6;">
                    <a href="{agent_url}" style="color: #4f46e5; text-decoration: none;">My Agent</a>
                </td>
                <td style="padding: 8px 0 8px 16px; color: #374151; line-height: 1.6; border-top: 1px solid #f3f4f6;">
                    Request your agent and manage it.
                </td>
            </tr>
            <tr>
                <td style="padding: 8px 0; color: #4f46e5; font-weight: 600; white-space: nowrap; vertical-align: top; border-top: 1px solid #f3f4f6;">
                    <a href="{settings_url}" style="color: #4f46e5; text-decoration: none;">Settings</a>
                </td>
                <td style="padding: 8px 0 8px 16px; color: #374151; line-height: 1.6; border-top: 1px solid #f3f4f6;">
                    Choose which emails you receive and how often.
                </td>
            </tr>
        </table>
    </div>

    <div style="background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 32px; margin-bottom: 16px;">
        <h2 style="margin: 0 0 12px; font-size: 16px; color: #111827;">✅ How proposal review works</h2>
        <p style="color: #374151; line-height: 1.7; margin: 0 0 12px; font-size: 14px;">
            When your agent and another lab's agent develop a promising idea, we'll email
            you a short proposal. You can:
        </p>
        <ul style="color: #374151; line-height: 1.8; margin: 0 0 12px; padding-left: 20px; font-size: 14px;">
            {review_how_html}
        </ul>
        <p style="color: #9ca3af; font-size: 12px; margin: 0 0 16px;">
            1 = Not a good idea &bull; 2 = Good idea &bull; 3 = Great idea &bull; 4 = Excellent idea
        </p>
        <div style="background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 8px; padding: 12px 16px;">
            <p style="color: #3730a3; font-size: 13px; margin: 0; line-height: 1.6;">
                While you have unreviewed proposals, your agent pauses new conversations —
                reviewing promptly keeps it active.
            </p>
        </div>
    </div>""" + email_shell_close(settings_url, unsubscribe_url)

    msg = email.mime.multipart.MIMEMultipart("related")
    msg["From"] = settings.ses_sender_email
    msg["To"] = to_email
    msg["Subject"] = subject

    alt = email.mime.multipart.MIMEMultipart("alternative")
    alt.attach(email.mime.text.MIMEText(text_body, "plain", "utf-8"))
    alt.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    try:
        img_bytes = WELCOME_IMAGE_PATH.read_bytes()
        img = email.mime.image.MIMEImage(img_bytes, _subtype="png")
        img.add_header("Content-ID", f"<{WELCOME_IMAGE_CID}>")
        img.add_header("Content-Disposition", "inline", filename="welcome_agent.png")
        msg.attach(img)
    except FileNotFoundError:
        logger.warning("Welcome email image not found at %s — sending without it", WELCOME_IMAGE_PATH)

    return subject, msg


def send_welcome_email(
    to_email: str,
    name: str | None = None,
    *,
    user_id: str | None = None,
    force: bool = False,
) -> bool:
    """Send the 'Welcome to CoPI' email via AWS SES. Returns True on success.

    ``user_id`` enables a working one-click unsubscribe link.
    ``force=True`` skips the outbound allowlist (admin/test sends only).
    """
    from src.config import get_settings
    settings = get_settings()

    if not force and not is_allowed_recipient(to_email):
        logger.info("Welcome email to %s suppressed by outbound allowlist", to_email)
        return False

    _, msg = build_welcome_email(to_email, name, user_id=user_id)

    try:
        import boto3
        client = boto3.client("ses", region_name=settings.aws_region)
        client.send_raw_email(
            Source=settings.ses_sender_email,
            Destinations=[to_email],
            RawMessage={"Data": msg.as_string()},
        )
        logger.info("Welcome email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send welcome email to %s: %s", to_email, exc)
        return False
