"""The draft a regeneration stages when a person edited the profile since its last
generation (spec 2026-10-05 §6.3, D19). Written by profile_pipeline step 9, accepted or
discarded on the manager PI page (src/services/profile_review.py)."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.models import ResearcherProfile

#: The profile text fields a draft carries, in persona order.
DRAFT_FIELDS = (
    "research_summary", "techniques", "experimental_models",
    "disease_areas", "key_targets", "keywords",
)


@dataclass(frozen=True)
class Draft:
    fields: dict[str, Any]
    synthesis_validated: bool
    evidence_pmid_count: int
    evidence_pub_count: int
    evidence_flagged_count: int
    base_profile_version: int
    job_id: str | None
    created_at: datetime | None


def edited_since_generation(profile: ResearcherProfile) -> bool:
    """human_edited_at is set and later than profile_generated_at (or nothing was ever
    generated)."""
    edited = profile.human_edited_at
    if edited is None:
        return False
    generated = profile.profile_generated_at
    return generated is None or edited > generated


def build_draft_payload(
    *, fields: Mapping[str, Any], synthesis_validated: bool, evidence_pmid_count: int,
    evidence_pub_count: int, evidence_flagged_count: int, base_profile_version: int,
    job_id: uuid.UUID | None,
) -> dict[str, Any]:
    """The JSON stored in researcher_profiles.pending_profile (DRAFT_FIELDS only)."""
    return {
        "fields": {name: fields.get(name) for name in DRAFT_FIELDS},
        "synthesis_validated": bool(synthesis_validated),
        "evidence_pmid_count": int(evidence_pmid_count),
        "evidence_pub_count": int(evidence_pub_count),
        "evidence_flagged_count": int(evidence_flagged_count),
        "base_profile_version": int(base_profile_version),
        "job_id": str(job_id) if job_id is not None else None,
    }


def _int(value: Any) -> int:
    # bool is an int subclass; a payload this module wrote never stores one here.
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_draft(profile: ResearcherProfile) -> Draft | None:
    """The staged draft, or None when there is none or the payload is not one this module
    wrote (a malformed payload reads as None, never raises)."""
    payload = profile.pending_profile
    if not isinstance(payload, dict):
        return None
    fields = payload.get("fields")
    base = payload.get("base_profile_version")
    if not isinstance(fields, dict) or not isinstance(base, int) or isinstance(base, bool):
        return None
    job_id = payload.get("job_id")
    return Draft(
        fields={name: fields.get(name) for name in DRAFT_FIELDS},
        synthesis_validated=payload.get("synthesis_validated") is True,
        evidence_pmid_count=_int(payload.get("evidence_pmid_count")),
        evidence_pub_count=_int(payload.get("evidence_pub_count")),
        evidence_flagged_count=_int(payload.get("evidence_flagged_count")),
        base_profile_version=base,
        job_id=job_id if isinstance(job_id, str) else None,
        created_at=profile.pending_profile_created_at,
    )


def draft_is_stale(profile: ResearcherProfile, draft: Draft) -> bool:
    """The profile changed since the draft's run read it (profile_version !=
    base_profile_version): only Discard or Regenerate is offered."""
    return (profile.profile_version or 0) != draft.base_profile_version
