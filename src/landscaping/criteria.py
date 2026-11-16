"""The landscaping criteria file, ``prompts/landscaping/criteria.toml`` (spec §8.1, L25).

Loaded and validated once per process; an invalid file raises ``CriteriaError``, which
stops the worker the way the rubric loader does.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

CRITERIA_PATH = Path("prompts/landscaping/criteria.toml")

_DEAL_BREAKER_TESTS = ("data", "llm")

# Tables that are not judging inputs: editing them must not open a new epoch (D61).
_NON_JUDGING_TABLES = ("meta", "reason_tags")


class CriteriaError(RuntimeError):
    """The criteria file is unreadable, not TOML, or fails validation."""


@dataclass(frozen=True)
class DealBreaker:
    """One deal-breaker flag (spec §7.4 step 5, §7.5). Flags never exclude (L24).

    ``test`` is ``"data"`` (computed from a database record) or ``"llm"`` (judged by
    the model at triage); ``description`` states that test.
    """

    key: str
    test: str
    description: str


@dataclass(frozen=True)
class Criteria:
    """The parsed criteria file.

    ``content_hash`` is the hex sha256 of the file's bytes. ``judging_hash`` is the hex
    sha256 of the parsed file without its ``[meta]`` and ``[reason_tags]`` tables, as
    canonical JSON, so editing the version, changelog or reason tags changes no epoch
    (D61). ``evidence_grades`` is in ladder order, weakest first.
    ``ambiguous_gene_words`` are upper-cased at load; compare with ``name.upper()``.
    """

    version: str
    content_hash: str
    judging_hash: str
    scope_text: str
    idea_types: tuple[str, ...]
    directions: tuple[str, ...]
    stages: tuple[str, ...]
    evidence_grades: tuple[str, ...]
    agent_kinds: tuple[str, ...]
    deal_breakers: tuple[DealBreaker, ...]
    ambiguous_gene_words: frozenset[str]
    reason_tags: tuple[str, ...]
    reason_tags_approved: bool
    dimension_share: float


def _table(data: dict, name: str) -> dict:
    table = data.get(name)
    if not isinstance(table, dict):
        raise CriteriaError(f"criteria file: missing [{name}] table")
    return table


def _str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CriteriaError(f"criteria file: {where} must be a non-empty string")
    return value


def _str_list(value: object, where: str) -> tuple[str, ...]:
    """A non-empty list of distinct non-empty strings, in file order."""
    if not isinstance(value, list) or not value:
        raise CriteriaError(f"criteria file: {where} must be a non-empty list")
    items = tuple(_str(v, f"{where}[{i}]") for i, v in enumerate(value))
    if len(set(items)) != len(items):
        raise CriteriaError(f"criteria file: {where} has duplicate entries")
    return items


def _deal_breakers(value: object) -> tuple[DealBreaker, ...]:
    if not isinstance(value, list) or not value:
        raise CriteriaError("criteria file: [[deal_breakers]] must hold at least one entry")
    out: list[DealBreaker] = []
    for i, entry in enumerate(value):
        where = f"[[deal_breakers]][{i}]"
        if not isinstance(entry, dict):
            raise CriteriaError(f"criteria file: {where} must be a table")
        test = _str(entry.get("test"), f"{where}.test")
        if test not in _DEAL_BREAKER_TESTS:
            raise CriteriaError(f"criteria file: {where}.test must be one of {_DEAL_BREAKER_TESTS}")
        out.append(DealBreaker(
            key=_str(entry.get("key"), f"{where}.key"),
            test=test,
            description=_str(entry.get("description"), f"{where}.description"),
        ))
    if len({d.key for d in out}) != len(out):
        raise CriteriaError("criteria file: [[deal_breakers]] keys must be distinct")
    return tuple(out)


def _dimension_share(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        raise CriteriaError("criteria file: [triage].dimension_share must be a number in (0, 1]")
    return float(value)


def _judging_hash(data: dict) -> str:
    judging = {k: v for k, v in data.items() if k not in _NON_JUDGING_TABLES}
    canonical = json.dumps(judging, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_criteria(path: Path = CRITERIA_PATH) -> Criteria:
    """Read and validate the criteria file at ``path``; raises ``CriteriaError``."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CriteriaError(f"criteria file unreadable at {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CriteriaError(f"criteria file is not valid TOML ({path}): {exc}") from exc
    return _parse(raw, text, str(path))


def parse_criteria(text: str, source: str = "criteria.toml") -> Criteria:
    """Validate criteria-file ``text`` (an epoch's stored copy, say); ``source`` names it in
    errors. ``content_hash`` is taken over the text's UTF-8 bytes. Raises ``CriteriaError``."""
    return _parse(text.encode("utf-8"), text, source)


def _parse(raw: bytes, text: str, source: str) -> Criteria:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CriteriaError(f"criteria file is not valid TOML ({source}): {exc}") from exc
    try:
        return _validated(raw, data)
    except CriteriaError as exc:
        # The validators name the field; this names which copy (the file or a snapshot).
        raise CriteriaError(f"{exc} ({source})") from None


def _validated(raw: bytes, data: dict) -> Criteria:
    meta = _table(data, "meta")
    _str_list(meta.get("changelog"), "[meta].changelog")
    vocab = _table(data, "vocab")
    reason_tags = _table(data, "reason_tags")
    approved = reason_tags.get("approved")
    if not isinstance(approved, bool):
        raise CriteriaError("criteria file: [reason_tags].approved must be true or false")
    gene_words = _str_list(
        _table(data, "normalization").get("ambiguous_gene_words"),
        "[normalization].ambiguous_gene_words",
    )
    return Criteria(
        version=_str(meta.get("version"), "[meta].version"),
        content_hash=hashlib.sha256(raw).hexdigest(),
        judging_hash=_judging_hash(data),
        scope_text=_str(_table(data, "scope").get("text"), "[scope].text").strip(),
        idea_types=_str_list(vocab.get("idea_types"), "[vocab].idea_types"),
        directions=_str_list(vocab.get("directions"), "[vocab].directions"),
        stages=_str_list(vocab.get("stages"), "[vocab].stages"),
        evidence_grades=_str_list(vocab.get("evidence_grades"), "[vocab].evidence_grades"),
        agent_kinds=_str_list(vocab.get("agent_kinds"), "[vocab].agent_kinds"),
        deal_breakers=_deal_breakers(data.get("deal_breakers")),
        ambiguous_gene_words=frozenset(w.upper() for w in gene_words),
        reason_tags=_str_list(reason_tags.get("tags"), "[reason_tags].tags"),
        reason_tags_approved=approved,
        dimension_share=_dimension_share(_table(data, "triage").get("dimension_share")),
    )
