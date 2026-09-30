"""The engine's patch seam.

Every engine unit calls these through module-attribute access
(``deps.generate_with_tools(...)``, ``deps.time.time()``), so one test patch on
``src.agent.engine.deps.<name>`` reaches every unit and the orchestrator, exactly
what one patch on ``src.agent.simulation.<name>`` did while the engine was one
module. Nothing else belongs here.
"""

import time
from datetime import datetime

# The engine's role reads go through the start-time snapshot (spec §8.6); the name
# stays `load_role` so `deps.load_role` patches keep working.
from src.agent.prompt_snapshot import role_spec as load_role
from src.config import get_settings
from src.services.build_info import get_build_info
from src.services.llm import generate_agent_response, generate_with_tools

__all__ = [
    "datetime", "generate_agent_response", "generate_with_tools",
    "get_build_info", "get_settings", "load_role", "time",
]
