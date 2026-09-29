import asyncio, json, importlib.util, sys
from decimal import Decimal
spec = importlib.util.spec_from_file_location("opus55_replay", "/app/scripts/dev/opus55_replay.py")
R = importlib.util.module_from_spec(spec); sys.modules["opus55_replay"] = R; spec.loader.exec_module(R)
import anthropic
async def main():
    budget = R.Budget(Decimal("1"), 6)
    c = R._client()
    out = {"sdk": anthropic.__version__, "shape_probe": await R.shape_probe(c, budget),
           "rate_limits": await R.rate_limit_probe(c, budget), "spent_usd": float(budget.spent)}
    print(json.dumps(out, indent=2, default=str))
asyncio.run(main())
