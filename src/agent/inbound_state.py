"""``agent_messages.pi_inbound_state`` values.

Pulled out of ``src.agent.simulation``
into a dependency-free module. ``src.services.pi_inbox`` (the web/worker request
path that writes a PI message row) previously imported ``PI_INBOUND_PENDING``
from ``src.agent.simulation`` inside its own function body — a request-path
import that drags the whole simulation engine module into the web/worker
process just to read one string constant. These four values have no
dependency on the engine itself, so they live here instead; ``src.agent.
simulation`` re-exports them (``from src.agent.inbound_state import ...``) so
every existing ``from src.agent.simulation import PI_INBOUND_*`` continues to
work unchanged.
"""

# The DB inbound poller's durable handled-marker, and the only two values
# anything writes for that purpose. It exists because
# SimulationEngine._poll_inbound_from_db appends a PI row to the MessageLog
# BEFORE it runs the handler (the append is what records the PI's text
# durably), so the log entry's presence can no longer be the dedup key:
# MessageLog.append is not idempotent, so keying on it would either skip the
# retry or duplicate the PI's message. A THIRD state is carried by NULL — "no
# inbound poller has claimed this row" — which every reader must treat as the
# pre-0029 behaviour, i.e. dedup on log presence. That covers every legacy row
# and, crucially, every row _poll_channels appended itself: that poller
# re-reads those with no origin predicate, so a two-valued marker would re-run
# handle_channel_tag on every tagged Slack message.
PI_INBOUND_INGESTED = "ingested"
PI_INBOUND_HANDLED = "handled"

# A fourth state, written at INSERT time by record_pi_message:
# "this row needs a DB inbound poller's attention no matter how far
# behind the cursor it is". Without it, a PI message written while agent-run
# was down could age past PI_INBOX_LOOKBACK_S (src.agent.simulation) before
# the process came back — _seed_pi_inbox_cursor jumps the cursor to
# max(created_at) at startup, so the lookback window never reaches a row
# older than that. 'pending' rows are fetched by _poll_inbound_from_db
# regardless of the cursor and are never skipped by the dedup predicate (it
# only special-cases HANDLED and the NULL-fallback), so they always reach the
# normal ingest→handle→HANDLED path once a poller is running again.
PI_INBOUND_PENDING = "pending"

# A handler that raises deterministically (not a
# transient ConnectionError) would otherwise be re-run forever — the row stays
# 'ingested' and the cursor-independent disjunct re-selects it every tick
# regardless of the lookback window, which only bounds the cursor-based path.
# This caps in-process retries per message_ts; on the Nth failure the row is
# stamped HANDLED (terminal) so it stops being re-selected. See
# src.agent.simulation._poll_inbound_from_db.
PI_INBOUND_MAX_ATTEMPTS = 3

# PiOwnershipLookupFailed (a transient DB failure
# resolving which agents a PI's Slack/user id owns) used to count toward the
# same PI_INBOUND_MAX_ATTEMPTS cap as a deterministically-failing handler --
# a 30s DB blip during a run of unlucky polls could exhaust the cap and get a
# genuine PI directive stamped HANDLED (terminal) after as little as
# PI_INBOUND_MAX_ATTEMPTS attempts, permanently dropping it. A lookup failure
# is retried on its own, much larger budget instead, so it takes a sustained
# outage (not a blip) to give up on the row. See
# src.agent.simulation._poll_inbound_from_db.
PI_INBOUND_MAX_LOOKUP_FAILURES = 30
