from dataclasses import dataclass, field


@dataclass
class EvidenceItem:
    source: str
    kind: str
    external_id: str
    company_name: str | None
    company_external_id: str | None
    company_class: str
    year: int | None
    pi_role: str | None
    in_tenure: bool
    evidence: dict = field(default_factory=dict)


JHU_OPENALEX_IDS = frozenset({
    "I145311948", "I2799853436", "I4210150714", "I2802946424", "I2802697821",
    "I4210098865", "I4210129832", "I4210092215", "I4389425327", "I4210114877",
})


@dataclass(frozen=True)
class Paged:
    """One paged upstream search (spec 2026-10-05 §6.2, D12): every item fetched, and
    whether paging stopped at its cap with results left."""

    items: list[dict]
    truncated: bool = False
