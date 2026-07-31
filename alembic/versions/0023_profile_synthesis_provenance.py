"""Add synthesis-provenance columns to researcher_profiles

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-31 00:00:00.000000

Two defects in src/services/profile_pipeline.py were invisible because the
pipeline wrote down nothing about *how* a profile was produced:

  1. Step 8 computed the validation result and step 9 stored on `if synthesized:`
     alone, so a profile that failed _validate_profile twice was persisted as if
     it had passed. `synthesis_validated` is the record of that decision.
  2. With PubMed unreachable, ORCID works never reach the synthesis prompt
     (_build_synthesis_context is fed only pubs_for_synthesis, which is derived
     solely from PubMed records), so the model invents a plausible profile from
     ~150 characters of name/department context and zero Publication rows are
     written. `evidence_pmid_count` / `evidence_pub_count` make that case
     self-identifying and separate it from a genuinely publication-less
     researcher (see ResearcherProfile.evidence_state).

All three are nullable and are deliberately NOT backfilled. NULL means "unknown
— this row predates the columns". Backfilling evidence_pub_count from
count(publications) would look like a free win and would be a lie: stored
publications accumulate across runs and include records with no abstract and
non-research article types, none of which reached any prompt. Inventing
provenance is exactly the failure these columns exist to expose.

Downgrades are idempotent (if_exists) so a rollback cannot wedge on a column a
partially-applied upgrade never created (see scripts/ci.sh and the 0022 note).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "researcher_profiles",
        sa.Column("synthesis_validated", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "researcher_profiles",
        sa.Column("evidence_pmid_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "researcher_profiles",
        sa.Column("evidence_pub_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("researcher_profiles", "evidence_pub_count", if_exists=True)
    op.drop_column("researcher_profiles", "evidence_pmid_count", if_exists=True)
    op.drop_column("researcher_profiles", "synthesis_validated", if_exists=True)
