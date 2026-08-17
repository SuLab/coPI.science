"""User model."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

# Account types. One user has exactly one; they are mutually exclusive (D7).
# Named user_role, not role: AgentRegistry.role and PrivateChannelMember.role
# already exist and mean other things (F3).
USER_ROLE_PI = "pi"
USER_ROLE_MANAGER = "manager"
USER_ROLE_ADMIN = "admin"
VALID_USER_ROLES = (USER_ROLE_PI, USER_ROLE_MANAGER, USER_ROLE_ADMIN)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    orcid: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    user_role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=USER_ROLE_PI, server_default=USER_ROLE_PI
    )
    email_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    email_notification_frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, default="weekly"
    )  # daily, twice_weekly, weekly, biweekly, off
    email_notifications_paused_by_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    access_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # allowed, pending, denied
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    profile: Mapped["ResearcherProfile | None"] = relationship(
        "ResearcherProfile", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    publications: Mapped[list["Publication"]] = relationship(
        "Publication", back_populates="user", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        "Job", back_populates="user", cascade="all, delete-orphan"
    )
    agent: Mapped["AgentRegistry | None"] = relationship(
        "AgentRegistry", back_populates="user", uselist=False, foreign_keys="AgentRegistry.user_id"
    )
    delegated_agents: Mapped[list["AgentDelegate"]] = relationship(
        "AgentDelegate", back_populates="user", cascade="all, delete-orphan"
    )

    # is_admin stays readable as a hybrid rather than a plain @property because
    # src/main.py:52 runs `select(User.is_admin)` — SQL, which a @property
    # cannot satisfy (F13). The hybrid compiles to `users.user_role = 'admin'`,
    # so main.py, templates/base.html:69, base.html:93,
    # templates/admin/user_detail.html:38 and tests/integration/test_cli.py:383
    # all keep working with no edit. It is READ-ONLY on purpose: `is_admin =
    # False` on a manager would have no correct answer.
    @hybrid_property
    def is_admin(self) -> bool:
        return self.user_role == USER_ROLE_ADMIN

    @is_admin.inplace.expression
    @classmethod
    def _is_admin_expr(cls):
        return cls.user_role == USER_ROLE_ADMIN

    @hybrid_property
    def is_manager(self) -> bool:
        return self.user_role == USER_ROLE_MANAGER

    @is_manager.inplace.expression
    @classmethod
    def _is_manager_expr(cls):
        return cls.user_role == USER_ROLE_MANAGER

    # The "may see the manager views" predicate. Everything that means
    # "admin OR manager" must name THIS, never a widened is_admin (F7).
    @hybrid_property
    def is_staff(self) -> bool:
        return self.user_role in (USER_ROLE_MANAGER, USER_ROLE_ADMIN)

    @is_staff.inplace.expression
    @classmethod
    def _is_staff_expr(cls):
        return cls.user_role.in_((USER_ROLE_MANAGER, USER_ROLE_ADMIN))

    def __repr__(self) -> str:
        return f"<User id={self.id} orcid={self.orcid} name={self.name!r}>"
