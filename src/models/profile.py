"""ResearcherProfile model."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class ResearcherProfile(Base):
    __tablename__ = "researcher_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    research_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    techniques: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    experimental_models: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    disease_areas: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    key_targets: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    grant_titles: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # [{label: str, content: str, submitted_at: str}]  — deprecated, use private_profile_md
    user_submitted_texts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # retired 2026-08-12 removal cycle; columns kept, no writers
    private_profile_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLM-generated draft staged for user review during onboarding
    private_profile_seed: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profile_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_abstracts_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # --- provenance of the stored synthesis (migration 0023) -------------------
    # Did the stored fields pass profile_pipeline._validate_profile?
    #   True  = passed (first attempt or the stricter retry)
    #   False = failed twice and was stored anyway as an editable draft
    #   None  = no synthesized profile has ever been stored here, or the row
    #           predates this column (legacy rows are NOT backfilled: guessing
    #           would fabricate the very provenance these columns exist to pin)
    synthesis_validated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # How much evidence the STORED profile is grounded in. Written together with
    # the synthesized fields, so they always describe the same synthesis (unlike
    # raw_abstracts_hash, which records this run's input even when nothing was
    # stored). Both None means "no synthesis stored / pre-0023 row".
    #   evidence_pmid_count — distinct PMIDs resolved from the ORCID works list,
    #                         i.e. what the pipeline should have been able to fetch
    #   evidence_pub_count  — PubMed records that were research-type AND carried an
    #                         abstract, i.e. the set offered to the synthesis prompt
    #                         (len(pubs_for_synthesis) at profile_pipeline.py step 9).
    #                         Read it as a lower bound on grounding, not as a count of
    #                         what the model saw: _build_synthesis_context sorts by year
    #                         and keeps sorted_pubs[:30], so for a PI with more than 30
    #                         abstract-bearing papers this exceeds what reached the
    #                         prompt. It is exact where it matters — the 0 / non-zero
    #                         boundary this column exists to draw is the same either way,
    #                         because 30 is a cap and never a floor.
    evidence_pmid_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_pub_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Nullable JSON: stores candidate profile awaiting user review
    pending_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_profile_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="profile")

    @property
    def evidence_state(self) -> str:
        """How well founded the stored profile is — the four cases, named once.

        A profile synthesized while PubMed was unreachable is textually
        indistinguishable from a real one: the model invents a plausible
        narrative from the researcher's name and department, it passes
        _validate_profile, and profile_version is bumped as usual. The
        difference is that no publication abstract ever reached the prompt.

          grounded              at least one abstract reached the prompt
          evidence_lost         identifiers were resolved but no abstract
                                survived (a PubMed/NCBI outage or rate-limit), or
                                the ORCID works lookup itself failed so the
                                identifier count is unknown. Ungrounded and worth
                                regenerating.
          no_evidence_available nothing to fetch in the first place (a genuinely
                                publication-less or non-PubMed-indexed
                                researcher). Ungrounded, but nothing was lost and
                                regenerating will not change it.
          unknown               pre-0023 row, or no synthesis was ever stored

        Limit of what two counts can tell you: they describe what the synthesis
        HAD, not every reason it had that. One case is still understated —
        `convert_dois_to_pmids` failing for a researcher whose ORCID lists only
        DOIs leaves zero identifiers in hand and reads as no_evidence_available.
        The count is a measured lower bound, deliberately, because a partial count
        is more useful than NULL; the NCBI failure itself is logged by step 3/4.
        """
        if self.evidence_pub_count is None:
            return "unknown"
        if self.evidence_pub_count > 0:
            return "grounded"
        if self.evidence_pmid_count is None or self.evidence_pmid_count > 0:
            return "evidence_lost"
        return "no_evidence_available"

    def __repr__(self) -> str:
        return f"<ResearcherProfile id={self.id} user_id={self.user_id} version={self.profile_version}>"
