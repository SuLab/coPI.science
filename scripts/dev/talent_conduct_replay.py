"""Replay stored talent consults whose opinion rested on the PI's conduct.

Selects ``llm_call_logs`` rows (phase ``consult_talent``, ``created_at >= --since``) whose
stored question matches ``CONDUCT_RE`` (candour, volunteering, conceding and similar),
re-issues each question and context through ``_execute_consult_specialist`` against the
persona currently on disk, and flags any replayed opinion that still reasons from those
terms. PASS means every replay produced a parsed opinion and none carries a conduct term;
REVIEW means read the flagged or failed rows in the output JSON.

Selection is read-only (the transaction is READ ONLY at the wire level). The replay calls
``_execute_consult_specialist`` with both persistence callbacks None, as
scripts/panel_calibration_ladder.py does: it writes no ``specialist_consults`` row and
credits no specialist floor. No prompt snapshot is installed, so the persona is read from disk.

Modes:
  (default)  list the matching rows and exit; no API call
  --apply    spends money — one Opus consult per matching row (about 15 in production);
             writes ``[{log_id, question, signal, read_state, raw, conduct_terms}]`` to --out

Run it inside the agent container so .env and the baked src/ are both present. The output
quotes the hub's questions and the specialist's full replies, which can cite a PI's
unpublished results, so write it OUTSIDE the checkout (``origin`` is public):

  mkdir -p ~/backups-blackbird/talent-replay
  docker compose -f docker-compose.prod.yml --profile agent run --rm --no-deps \\
    -v "$PWD/scripts:/app/scripts:ro" -v ~/backups-blackbird/talent-replay:/out agent \\
    python scripts/dev/talent_conduct_replay.py [--since 2026-09-20] [--apply --out /out/talent_conduct_replay.json]

Exit codes: 0 for a listing or a PASS; 1 for REVIEW (an opinion still carries a conduct
term, or a replay failed or did not parse); 2 when --apply matched no rows, so nothing was
checked. A release step can gate on 0.

The pure helpers are module-level and import nothing heavy, so
tests/unit/test_talent_conduct_replay.py can load this file by path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONDUCT_RE = re.compile(
    r"(cando(u)?r|unprompted|volunteer|conce(?:d|ssion)|forthright|transparen|self-critic"
    r"|intellectual(ly)? honest)",
    re.I,
)
# The consult's user message, as built in src/agent/tools.py::_execute_consult_specialist.
_QUESTION_HEADER = "## Question from the hub\n\n"
_CONTEXT_HEADER = "\n\n## What the PI has said\n\n"
_PHASE = "consult_talent"
_TRAILER = re.compile(r"—\s*signal:\s*([a-z_]+)\s*\(read:\s*([a-z_]+)\)", re.I)


def split_consult_message(content: str) -> tuple[str, str] | None:
    """``(question, context)`` from a stored consult user message, or None if it lacks the
    consult header format."""
    if not isinstance(content, str) or not content.startswith(_QUESTION_HEADER):
        return None
    question, sep, context = content[len(_QUESTION_HEADER):].partition(_CONTEXT_HEADER)
    if not sep:
        return None
    return question, context


def flag_conduct(opinion_text: str) -> list[str]:
    """The ``CONDUCT_RE`` terms found in ``opinion_text``: lower-cased, in order of first
    appearance, de-duplicated."""
    seen: list[str] = []
    for m in CONDUCT_RE.finditer(opinion_text or ""):
        term = m.group(0).lower()
        if term not in seen:
            seen.append(term)
    return seen


def _first_user_text(messages_json: Any) -> str | None:
    """Text of the first user message in a stored conversation (string or text blocks)."""
    if not isinstance(messages_json, list):
        return None
    for message in messages_json:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        return None
    return None


async def select_rows(since: datetime) -> list[dict[str, Any]]:
    """Matching rows as ``{log_id, created_at, question, context}``, oldest first."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from src.config import get_settings
    from src.models import LlmCallLog

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool,
                                 execution_options={"postgresql_readonly": True})
    out: list[dict[str, Any]] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            result = await db.execute(
                select(LlmCallLog.id, LlmCallLog.created_at, LlmCallLog.messages_json)
                .where(LlmCallLog.phase == _PHASE, LlmCallLog.created_at >= since)
                .order_by(LlmCallLog.created_at)
            )
            for log_id, created_at, messages in result.all():
                parts = split_consult_message(_first_user_text(messages) or "")
                if parts is None or not CONDUCT_RE.search(parts[0]):
                    continue
                out.append({"log_id": str(log_id), "created_at": created_at,
                            "question": parts[0], "context": parts[1]})
    finally:
        await engine.dispose()
    return out


async def replay(row: dict[str, Any]) -> dict[str, Any]:
    from src.agent.tools import _execute_consult_specialist

    try:
        raw = await _execute_consult_specialist(
            "talent", row["question"], row["context"],
            agent_id="blackbird", channel=None,
            on_consult=None, on_consult_record=None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"log_id": row["log_id"], "question": row["question"],
                "signal": f"ERROR:{type(exc).__name__}", "read_state": None,
                "raw": str(exc)[:300], "conduct_terms": []}
    matches = list(_TRAILER.finditer(raw or ""))
    m = matches[-1] if matches else None
    return {
        "log_id": row["log_id"], "question": row["question"],
        "signal": m.group(1).lower() if m else "UNPARSED",
        "read_state": m.group(2).lower() if m else None,
        "raw": raw, "conduct_terms": flag_conduct(raw or ""),
    }


async def main(*, since: datetime, apply: bool, out: Path) -> int:
    rows = await select_rows(since)
    print(f"{len(rows)} consult_talent rows since {since.date()} with a conduct term in the question")
    if not apply:
        for r in rows:
            q = r["question"].replace("\n", " ")
            print(f"{r['log_id']}  {r['created_at']:%Y-%m-%d %H:%M}  {q[:100]}")
        return 0
    results = []
    for r in rows:
        res = await replay(r)
        results.append(res)
        print(f"{res['log_id']}  signal={res['signal']}  conduct_terms={res['conduct_terms']}",
              flush=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"full results -> {out}")
    if not results:
        print("NO ROWS: nothing matched, so nothing was checked")
        return 2
    # A defaulted or unparsed signal is not an opinion the persona gave, so it cannot pass.
    failed = [r for r in results if r["read_state"] != "parsed"]
    flagged = [r for r in results if r["conduct_terms"]]
    if not failed and not flagged:
        print("PASS")
        return 0
    print(f"REVIEW ({len(flagged)} with conduct terms, {len(failed)} not parsed)")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-09-20", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--apply", action="store_true", help="replay on the API (spends money)")
    ap.add_argument("--out", default="talent_conduct_replay.json")
    args = ap.parse_args()
    since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
    sys.exit(asyncio.run(main(since=since_dt, apply=args.apply, out=Path(args.out))))
