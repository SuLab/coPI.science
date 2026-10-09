"""researcher_profiles.full_career_fallback: thin JHU-era records use the full career

Design: docs/specs/2026-10-09-thin-profile-fallback-design.md §4.3 (Phase A).

Additive: ``researcher_profiles.full_career_fallback`` (boolean, NULL). True when the stored
profile text was generated from the PI's full stored career because the JHU-era record was
thin (a tenure year and at most 10 non-excluded papers dated in it); every persona export
then scopes by it (tenure_scope.scoped_publications_for_export). NULL means "generated
before this revision" and is read as False. Not backfilled.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads or writes the column. NEW CODE ON THE
OLD SCHEMA is not: ``ResearcherProfile`` maps the column, so every profile read on the web app
and the worker raises UndefinedColumn. Migrate BEFORE the new web and worker images serve,
with the worker idle (a running generation holds row locks on researcher_profiles and the
chain's 10 s lock_timeout trips). The engine imports src.models but never queries
researcher_profiles, so the agent image need not be rebuilt with it.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "researcher_profiles",
        sa.Column("full_career_fallback", sa.Boolean, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("researcher_profiles", "full_career_fallback")
