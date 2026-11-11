"""Add users.session_epoch and the user_microsoft_identities table

Revision ID: 0031
Revises: 0030

``users.session_epoch`` is a nullable INTEGER with no default and no backfill; NULL
reads as 0. Bumping it ends every session of that account at once. ``session_epoch``
is not a revocation record by itself: a re-upgrade after a downgrade brings it back as
NULL, which makes every previously revoked session valid again.

``user_microsoft_identities`` links one user to one Microsoft (Entra ID) account,
keyed by (tenant_id, object_id) -- Microsoft's immutable ``tid`` and ``oid`` -- and
never by email. ``sign_in_name`` is display-only. One link per user and one user per
Microsoft account, both enforced by named unique constraints. ``ON DELETE CASCADE``:
deleting the user removes their link.

Old code on the new schema is safe (nothing old reads either object). New code on the
old schema is not (every User load selects session_epoch), so migrate before the new
web image serves; scripts/redeploy.sh does.

Sizing: the ADD COLUMN is catalogue-only (no rewrite); the new table is empty, and its
foreign key takes SHARE ROW EXCLUSIVE on users briefly.

Downgrade is idempotent (if_exists) per the 0022+ convention, and deletes every link.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("session_epoch", sa.Integer(), nullable=True))
    op.create_table(
        "user_microsoft_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sign_in_name", sa.String(255), nullable=True),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="user_microsoft_identities_user_id_fkey",
        ),
        sa.UniqueConstraint("user_id", name="uq_user_microsoft_identities_user_id"),
        sa.UniqueConstraint(
            "tenant_id", "object_id", name="uq_user_microsoft_identities_tenant_object"
        ),
    )


def downgrade() -> None:
    op.drop_table("user_microsoft_identities", if_exists=True)
    op.drop_column("users", "session_epoch", if_exists=True)
