"""Control: the same stored requests on claude-opus-5-5 with NO fallbacks, non-beta client."""
import asyncio, json, uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from src.config import get_settings
from src.models import LlmCallLog
from src.services import llm
from src.agent.tools import tools_for_role
REFS = json.loads(open("/probe/refs.json").read())
async def main():
    s = get_settings()
    eng = create_async_engine(s.database_url, poolclass=NullPool, execution_options={"postgresql_readonly": True})
    c = llm._client_for_key(s.anthropic_api_key).with_options(max_retries=0)
    try:
        async with AsyncSession(eng) as db:
            for route, ref in REFS:
                row = (await db.execute(select(LlmCallLog).where(LlmCallLog.id == uuid.UUID(ref)))).scalar_one()
                msgs = row.messages_json
                kw = dict(model="claude-opus-5-5", max_tokens=8000, system=llm._cacheable_system(row.system_prompt),
                          thinking={"type": "adaptive"}, output_config={"effort": "low"})
                if route.startswith("thread_reply"):
                    first = []
                    for m in msgs:
                        if m.get("role") == "assistant": break
                        first.append(m)
                    kw.update(messages=first, tools=tools_for_role("pi_lab"), max_tokens=16000,
                              output_config={"effort": "medium"})
                else:
                    kw["messages"] = msgs
                r = await asyncio.to_thread(c.messages.create, **kw)
                sd = r.stop_details
                print(json.dumps({"route": route, "stop_reason": r.stop_reason,
                                  "category": getattr(sd, "category", None) if sd else None,
                                  "blocks": [b.type for b in r.content], "out": r.usage.output_tokens,
                                  "model": r.model}))
    finally:
        await eng.dispose()
asyncio.run(main())
