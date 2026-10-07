"""Safe chrome identities; neither carries ORM relationships or account secrets."""

from dataclasses import dataclass
from uuid import UUID

from src.services.web_permissions import WebCapabilities, capabilities_for


@dataclass(frozen=True)
class UserIdentity:
    id: UUID
    name: str
    user_role: str
    orcid: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.user_role == "admin"

    @property
    def is_manager(self) -> bool:
        return self.user_role == "manager"

    @property
    def is_reviewer(self) -> bool:
        return self.user_role == "reviewer"

    @property
    def is_staff(self) -> bool:
        return self.user_role in ("admin", "manager")

    @property
    def may_use_pi_surfaces(self) -> bool:
        return self.user_role in ("admin", "pi")


@dataclass(frozen=True)
class WebIdentity:
    actor: UserIdentity
    effective: UserIdentity
    is_impersonating: bool
    permissions: WebCapabilities


def identity_for(user) -> WebIdentity:
    impersonating = bool(getattr(user, "_is_impersonated", False))
    actor = getattr(user, "_real_admin", None) if impersonating else user
    actor = actor or user
    return WebIdentity(
        actor=UserIdentity(actor.id, actor.name, actor.user_role, getattr(actor, "orcid", None)),
        effective=UserIdentity(user.id, user.name, user.user_role, getattr(user, "orcid", None)),
        is_impersonating=impersonating,
        permissions=capabilities_for(user),
    )
