"""The single definition of "which agents belong on a live roster".

Two consumers — engine startup (src/agent/main.py) and the ~30s live sync
(SimulationEngine._sync_roster_from_db) — must agree, or an agent evicted by
one is resurrected by the other. pi_lab rows with user_id IS NULL are
excluded: that is the activation gate's invariant
(src/services/agent_activation.py — "no profile to stand behind this lab"),
enforced here for rows orphaned by a user deletion that predates the teardown
service (docs/audits/2026-08-25-pi-deletion, F1/D7). Hub and specialist roles
carry no user by design and are exempt.
"""
from sqlalchemy import or_, select
from sqlalchemy.sql import Select

from src.models import AgentRegistry


def active_roster_select() -> Select:
    return (
        select(
            AgentRegistry.agent_id,
            AgentRegistry.bot_name,
            AgentRegistry.pi_name,
            AgentRegistry.slack_bot_token,
            AgentRegistry.role,
        )
        .where(
            AgentRegistry.status == "active",
            or_(
                AgentRegistry.role != "pi_lab",
                AgentRegistry.user_id.isnot(None),
            ),
        )
        .order_by(AgentRegistry.agent_id)
    )
