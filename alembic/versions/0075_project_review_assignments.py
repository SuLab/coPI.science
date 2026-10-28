"""hub_project_review_assignments: staff assign a project to a reviewer

The projects pages' assignments (docs/specs/2026-10-27-projects-pages-redesign-design.md §7).
One row per (project, assignee), mirroring assessment_review_assignments: project_id CASCADEs
from hub_projects (so deleting a run removes it), assignee_user_id CASCADEs from users (an
assignment to a deleted user means nothing), and assigned_by_user_id is SET NULL with
assigned_by_name kept.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table.

NEW CODE ON THE OLD SCHEMA fails: the Projects list and a project's details page select the
table and raise UndefinedTable (a 500 on both pages), and so do the assign and unassign
routes. Migrate before the new web image serves. The engine and the worker never read the
table, so neither the agent nor the worker image needs a rebuild for it.

The foreign keys lock hub_projects and users briefly: run it only when no projects run is
live (owner decision D61) and with the worker idle; a 10 s lock_timeout rollback is
harmless, re-run.

No row is written and nothing is back-filled. Downgrade drops the table and every row.

Revision ID: 0075
Revises: 0074
Create Date: 2026-10-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hub_project_review_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hub_projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "assignee_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("assignee_name", sa.String(255), nullable=False),
        sa.Column(
            "assigned_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("assigned_by_name", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "assignee_user_id", name="uq_hub_project_review_assignment_once"
        ),
    )
    op.create_index(
        "ix_hub_project_review_assignments_project_id",
        "hub_project_review_assignments", ["project_id"],
    )
    op.create_index(
        "ix_hub_project_review_assignments_assignee",
        "hub_project_review_assignments", ["assignee_user_id"],
    )


def downgrade() -> None:
    op.drop_table("hub_project_review_assignments")
