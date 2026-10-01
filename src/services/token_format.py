"""Slack bot-token shape check with no project imports, so host-side scripts
(scripts/provision_slack_bots.py) apply the same rule as the app (SC-12)."""


def is_valid_token(token: str | None) -> bool:
    """True for a value that could plausibly be a Slack **bot** token.

    Every caller passes a bot token, and Slack bot tokens are always ``xoxb-``. The
    prefix check is not cosmetic. Whatever this accepts is what ``token_for_agent_row``
    and ``get_any_bot_token`` hand out, and what the engine's ``slack_enabled``
    auto-detect in ``src/agent/main.py`` counts: with ``SLACK_ENABLED`` unset, the mere
    *presence* of a valid-looking token switches the whole integration on. Before the
    prefix check, a user token (``xoxp-``), an app-config token (``xoxe.xoxp-`` — which
    lives in the same ``.env`` as the bot tokens), a stray ``"   "``, or an unfilled
    ``REPLACE_ME`` all counted as "usable", flipping Slack on and then failing every
    API call with ``invalid_auth`` or ``not_allowed_token_type``.

    ``xoxb-placeholder`` remains a recognised no-op value for seeded rows.
    """
    if not token:
        return False
    token = token.strip()
    return token.startswith("xoxb-") and not token.startswith("xoxb-placeholder")
