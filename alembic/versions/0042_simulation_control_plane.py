"""simulation control plane: commands, process status, admin audit,
llm_call_logs.thread_ts

Revision ID: 0042
Revises: 0041
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "simulation_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("command", sa.Enum("start", "stop", name="sim_command_enum"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "done", "failed", "stale", name="sim_command_status_enum"),
            nullable=False, server_default="pending",
        ),
        sa.Column(
            "requested_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_simulation_commands_pending", "simulation_commands", ["status", "created_at"]
    )
    # At most ONE pending row per command kind — closes the two-admin
    # double-start race at the database (audit V7): the second concurrent
    # enqueue raises IntegrityError and the route renders a refusal.
    op.create_index(
        "uq_simulation_commands_one_pending",
        "simulation_commands", ["command"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "simulation_process_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="idle"),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "admin_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("llm_call_logs", sa.Column("thread_ts", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_call_logs", "thread_ts")
    op.drop_table("admin_audit_events")
    op.drop_table("simulation_process_status")
    op.drop_index("uq_simulation_commands_one_pending", table_name="simulation_commands")
    op.drop_index("ix_simulation_commands_pending", table_name="simulation_commands")
    op.drop_table("simulation_commands")
    sa.Enum(name="sim_command_status_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sim_command_enum").drop(op.get_bind(), checkfirst=True)
