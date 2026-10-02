"""Live evaluation of COI founder extraction against the stored case set (spec §8, O14).

Runs `coi_llm.extract_founder_claims` once per case in
`tests/fixtures/company_discovery/coi_eval_cases.json`, sequentially and paced, and
prints one line per case:

  PASS            every returned (company, role) is expected and none is missing
  FALSE_POSITIVE  a returned (company, role) is not in the case's expected list
  MISS            no false positive, but an expected claim was not returned
  SKIPPED         the gate made no call (PI not located, collision, no "found")
  UNAVAILABLE     refusal, API error or malformed reply (reported, not gated)

Companies compare by `pi_companies.normalize_company_name` ("DELFI Diagnostics, Inc."
equals "DELFI Diagnostics"); roles compare exactly. Exit status:

  1  any case is a false positive (the O14 acceptance bar is zero)
  2  no API key; or no case with a non-empty expected list passed, or more than 5% of
     the cases sent to the API came back unavailable, so the zero-false-positive bar
     cannot pass trivially (a run that returns no claims at all passes every case whose
     expected list is empty)
  0  otherwise; misses are reported, not gated

It spends real API calls (one per gated-in case, about 300) on the key in the
environment, so it is operator-run only, never from CI. `--record PATH` writes every
raw reply (stop reason, parsed payload, text) keyed by case id, plus a `_meta` entry
(the prompt file's sha256, the model setting, the date), for
tests/unit/test_coi_llm.py's replay test (`coi_eval_recorded.json`), which fails once
the prompt or the model changes without a new recording.

Run from a checkout on the host (the images carry no tests/ tree; `--cases` points
elsewhere). `--cases` and `--record` are relative to the directory the script is run
from. Settings, including ANTHROPIC_API_KEY, load from the repository's `.env`:

  .venv-test/bin/python scripts/eval_coi_extraction.py \\
      --record tests/fixtures/company_discovery/coi_eval_recorded.json
  .venv-test/bin/python scripts/eval_coi_extraction.py --only r3-fp-pending-cofounder
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.services import llm  # noqa: E402
from src.services.company_sources import pi_name  # noqa: E402
from src.services.company_sources.coi_llm import (  # noqa: E402
    COI_PROMPT_PATH,
    extract_founder_claims,
)
from src.services.pi_companies import normalize_company_name  # noqa: E402

CASES_PATH = ROOT / "tests/fixtures/company_discovery/coi_eval_cases.json"
#: Seconds between calls: one request at a time, well under any rate limit.
PACE_SECONDS = 1.0
#: Above this share of unavailable replies among the cases sent, the run proves nothing.
MAX_UNAVAILABLE_SHARE = 0.05


class _Recorder:
    """Wraps the real client so the raw reply of each call can be kept: `abeta_create`
    reaches `client.beta.messages.create`, which lands in `create` here."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.last: Any = None
        self.beta = self
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.last = self._client.beta.messages.create(**kwargs)
        return self.last


def _raw(message: Any) -> dict:
    text = "".join(
        getattr(b, "text", "") or "" for b in (getattr(message, "content", None) or [])
        if getattr(b, "type", None) == "text"
    )
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    details = getattr(message, "stop_details", None)
    usage = getattr(message, "usage", None)
    return {
        # Token counts, so the operator can price the run (spec O14: the run is paid for).
        "usage": {
            k: getattr(usage, k, None)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens")
        },
        "stop_reason": getattr(message, "stop_reason", None),
        "stop_category": getattr(details, "category", None),
        "model": getattr(message, "model", None),
        "payload": payload,
        "text": text,
    }


def _record_of(case: dict) -> dict:
    return {
        "pmid": case.get("pmid") or case["id"],
        "year": case.get("year"),
        "authors": case["authors"],
        "coi_statement": case["statement"],
    }


def _verdict(returned: set[tuple[str, str]], expected: set[tuple[str, str]]) -> str:
    if returned - expected:
        return "FALSE_POSITIVE"
    return "MISS" if expected - returned else "PASS"


async def _run_case(case: dict, recorder: _Recorder) -> tuple[str, str, dict | None]:
    """(verdict, detail line, raw reply or None when no call was made)."""
    name = pi_name(case["pi"])
    if name is None:
        return "SKIPPED", "unusable PI name", None
    recorder.last = None
    outcome = await extract_founder_claims(_record_of(case), name, client=recorder)
    raw = _raw(recorder.last) if recorder.last is not None else None
    if outcome.status == "skipped":
        return "SKIPPED", outcome.reason or "", raw
    if outcome.status == "unavailable":
        return "UNAVAILABLE", outcome.reason or "", raw
    returned = {(normalize_company_name(c.company_name), c.pi_role) for c in outcome.claims}
    expected = {(normalize_company_name(c), r) for c, r in case["expected"]}
    detail = f"returned={sorted(returned)} expected={sorted(expected)}"
    if outcome.dropped:
        detail += f" dropped={outcome.dropped}"
    return _verdict(returned, expected), detail, raw


def _meta() -> dict:
    """The recording's `_meta` entry: what the replies were produced with."""
    prompt = (ROOT / COI_PROMPT_PATH).read_bytes()
    return {
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "model": get_settings().llm_coi_model,
        "recorded_at": date.today().isoformat(),
    }


def _exit_status(totals: dict[str, int], positive_passes: int) -> int:
    """1 on any false positive; 2 when no case with a non-empty expected list passed
    (`positive_passes`) or too many sent cases were unavailable (the bar would pass
    trivially); else 0."""
    if totals.get("FALSE_POSITIVE"):
        return 1
    sent = sum(n for verdict, n in totals.items() if verdict != "SKIPPED")
    unavailable = totals.get("UNAVAILABLE", 0)
    if not positive_passes:
        print("no case with an expected claim passed: the run does not show the "
              "zero-false-positive bar", file=sys.stderr)
        return 2
    if unavailable > MAX_UNAVAILABLE_SHARE * sent:
        print(f"{unavailable} of {sent} sent cases unavailable (over {MAX_UNAVAILABLE_SHARE:.0%})",
              file=sys.stderr)
        return 2
    return 0


async def _main() -> int:
    p = argparse.ArgumentParser(description="Live COI founder-extraction evaluation (spends API calls).")
    p.add_argument("--record", type=Path, help="write every raw reply, keyed by case id, to this JSON file")
    p.add_argument("--only", action="append", help="run only this case id (repeatable)")
    p.add_argument("--cases", type=Path, default=CASES_PATH, help="case set (default: %(default)s)")
    a = p.parse_args()
    # Paths given on the command line are the caller's; resolve them before moving.
    a.cases = a.cases.resolve()
    a.record = a.record.resolve() if a.record else None
    os.chdir(ROOT)  # COI_PROMPT_PATH is relative to the repository root
    if not get_settings().anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set; this script makes live calls and needs one.", file=sys.stderr)
        return 2
    cases = json.loads(a.cases.read_text(encoding="utf-8"))
    if a.only:
        cases = [c for c in cases if c["id"] in set(a.only)]
        if not cases:
            print(f"no case matches {a.only}", file=sys.stderr)
            return 2
    recorder = _Recorder(llm.get_anthropic_client())
    totals: dict[str, int] = {}
    positive_passes = 0  # PASS verdicts on cases that expect at least one claim
    recorded: dict[str, dict] = {"_meta": _meta()}
    for n, case in enumerate(cases, 1):
        verdict, detail, raw = await _run_case(case, recorder)
        totals[verdict] = totals.get(verdict, 0) + 1
        positive_passes += verdict == "PASS" and bool(case["expected"])
        print(f"[{n}/{len(cases)}] {verdict:<14} {case['id']}  {detail}", flush=True)
        if raw is not None:
            recorded[case["id"]] = raw
            await asyncio.sleep(PACE_SECONDS)
    if a.record:
        a.record.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"recorded {len(recorded) - 1} replies to {a.record}")
    print("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    return _exit_status(totals, positive_passes)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
