"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-20 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum types
    op.execute("CREATE TYPE author_position_enum AS ENUM ('first', 'last', 'middle')")
    op.execute("CREATE TYPE job_type_enum AS ENUM ('generate_profile', 'monthly_refresh')")
    op.execute(
        "CREATE TYPE job_status_enum AS ENUM ('pending', 'processing', 'completed', 'failed', 'dead')"
    )
    op.execute(
        "CREATE TYPE sim_run_status_enum AS ENUM ('running', 'completed', 'stopped')"
    )
    op.execute("CREATE TYPE agent_message_phase_enum AS ENUM ('decide', 'respond')")
    op.execute("CREATE TYPE channel_type_enum AS ENUM ('thematic', 'collaboration')")

    # Users table
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("institution", sa.String(255), nullable=True),
        sa.Column("department", sa.String(255), nullable=True),
        sa.Column("orcid", sa.String(50), unique=True, nullable=False),
        sa.Column("is_admin", sa.Boolean, default=False, nullable=False),
        sa.Column("email_notifications_enabled", sa.Boolean, default=True, nullable=False),
        sa.Column("onboarding_complete", sa.Boolean, default=False, nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Researcher profiles table
    op.create_table(
        "researcher_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("research_summary", sa.Text, nullable=True),
        sa.Column("techniques", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("experimental_models", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("disease_areas", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("key_targets", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("keywords", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("grant_titles", postgresql.ARRAY(sa.String), nullable=True),
        sa.Column("user_submitted_texts", postgresql.JSON, nullable=True),
        sa.Column("profile_version", sa.Integer, default=0, nullable=False),
        sa.Column("profile_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_abstracts_hash", sa.String(64), nullable=True),
        sa.Column("pending_profile", postgresql.JSON, nullable=True),
        sa.Column("pending_profile_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Publications table
    op.create_table(
        "publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pmid", sa.String(20), nullable=True),
        sa.Column("pmcid", sa.String(20), nullable=True),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("abstract", sa.Text, nullable=True),
        sa.Column("journal", sa.String(255), nullable=True),
        sa.Column("year", sa.Integer, nullable=True),
        sa.Column(
            "author_position",
            sa.Enum(
                "first", "last", "middle", name="author_position_enum", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("methods_text", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_publications_user_id", "publications", ["user_id"])
    op.create_index("ix_publications_pmid", "publications", ["pmid"])

    # Jobs table
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "type",
            sa.Enum(
                "generate_profile", "monthly_refresh", name="job_type_enum", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "completed",
                "failed",
                "dead",
                name="job_status_enum",
                create_type=False,
            ),
            default="pending",
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSON, nullable=False),
        sa.Column("attempts", sa.Integer, default=0, nullable=False),
        sa.Column("max_attempts", sa.Integer, default=3, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])

    # Simulation runs table
    op.create_table(
        "simulation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "completed",
                "stopped",
                name="sim_run_status_enum",
                create_type=False,
            ),
            default="running",
            nullable=False,
        ),
        sa.Column("total_messages", sa.Integer, default=0, nullable=False),
        sa.Column("total_api_calls", sa.Integer, default=0, nullable=False),
        sa.Column("config", postgresql.JSON, nullable=False),
    )

    # Agent messages table
    op.create_table(
        "agent_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("channel_id", sa.String(100), nullable=False),
        sa.Column("channel_name", sa.String(100), nullable=False),
        sa.Column("message_ts", sa.String(50), nullable=True),
        sa.Column("message_length", sa.Integer, default=0, nullable=False),
        sa.Column(
            "phase",
            sa.Enum(
                "decide", "respond", name="agent_message_phase_enum", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_agent_messages_run_id", "agent_messages", ["simulation_run_id"])

    # Agent channels table
    op.create_table(
        "agent_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(100), nullable=False),
        sa.Column("channel_name", sa.String(100), nullable=False),
        sa.Column(
            "channel_type",
            sa.Enum(
                "thematic", "collaboration", name="channel_type_enum", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("created_by_agent", sa.String(50), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_agent_channels_run_id", "agent_channels", ["simulation_run_id"])


def downgrade() -> None:
    op.drop_table("agent_channels")
    op.drop_table("agent_messages")
    op.drop_table("simulation_runs")
    op.drop_table("jobs")
    op.drop_table("publications")
    op.drop_table("researcher_profiles")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS channel_type_enum")
    op.execute("DROP TYPE IF EXISTS agent_message_phase_enum")
    op.execute("DROP TYPE IF EXISTS sim_run_status_enum")
    op.execute("DROP TYPE IF EXISTS job_status_enum")
    op.execute("DROP TYPE IF EXISTS job_type_enum")
    op.execute("DROP TYPE IF EXISTS author_position_enum")
