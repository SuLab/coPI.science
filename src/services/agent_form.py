"""A version token for the admin agent form (RA-02).

`agents` has no updated_at column and 0056 adds none, so the form carries a
fingerprint of every field the form or a concurrent writer can change; a mismatch
at POST time means the row changed after the page was rendered. The fingerprint
is an HMAC keyed with the app secret, so the page never carries a hash an
attacker could use to confirm a guess at the Slack bot token it covers."""
import hashlib
import hmac

from src.config import get_settings
from src.models import AgentRegistry


def agent_form_version(agent: AgentRegistry) -> str:
    parts = (agent.agent_id, agent.bot_name, agent.status, agent.role, agent.user_id,
             agent.slack_bot_token, agent.muted_at)
    message = "|".join("" if p is None else str(p) for p in parts).encode()
    key = get_settings().secret_key.encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]
