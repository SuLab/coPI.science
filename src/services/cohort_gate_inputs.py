"""The inputs to compute_gates for the WEB previews (RA-04): the admin cohort
preview and the PI conversations feed. The roster is `active_roster_select()` — the
query the engine's roster sync uses — so an orphaned pi_lab row is absent here as
it is from the engine's DB roster. The engine itself keeps gating on its running
agent set (`list(self.agents)`, FA-3): mid-run activations without a client and
`--all-agents` runs make that set differ from the DB roster, and switching it would
change cohort gates."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.roster_query import active_roster_select
from src.models import Cohort, CohortMembership


@dataclass(frozen=True)
class GateInputs:
    membership_rows: list[tuple]
    agent_ids: list[str]
    cohort_count: int
    bot_names: dict[str, str]


async def load_gate_inputs(db: AsyncSession, *, extra_agent_ids: Iterable[str] = ()) -> GateInputs:
    """Load roster, memberships and cohort count; `extra_agent_ids` widens the roster
    (the conversations feed adds the viewing agent, which may be inactive)."""
    rows = (await db.execute(active_roster_select())).all()
    bot_names = {r.agent_id: r.bot_name for r in rows}
    roster = set(bot_names) | set(extra_agent_ids)
    memberships = [(r[0], r[1]) for r in (await db.execute(
        select(CohortMembership.cohort_id, CohortMembership.agent_id))).all()]
    cohort_count = (await db.execute(select(func.count()).select_from(Cohort))).scalar() or 0
    return GateInputs(memberships, sorted(roster), cohort_count, bot_names)
