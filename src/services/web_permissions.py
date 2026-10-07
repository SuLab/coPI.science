"""Human web permissions, calculated from the live effective account.

These predicates are intentionally HTTP-free: chat needs the same role rule
with its own ordered JSON refusals. Domain ownership/state checks remain in
the existing services and route dependencies.
"""

from dataclasses import dataclass

from src.models.user import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER

ROLE_DESCRIPTIONS = {
    USER_ROLE_PI: "Own research profile and lab agent.",
    USER_ROLE_MANAGER: "Manage PI research and lab operations; review assessments globally.",
    USER_ROLE_ADMIN: "All manager features plus account, agent and system administration.",
    USER_ROLE_REVIEWER: "View PI research and review assessments globally; assignments are workflow metadata.",
}


def can_review(user) -> bool:
    return user.user_role in (USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER)


def is_staff(user) -> bool:
    return user.user_role in (USER_ROLE_ADMIN, USER_ROLE_MANAGER)


@dataclass(frozen=True)
class WebCapabilities:
    research: bool
    staff: bool
    admin: bool
    pi_surfaces: bool
    verify_pi_email: bool
    assign_reviews: bool
    manage_suggestions: bool
    chat: bool


def capabilities_for(user) -> WebCapabilities:
    impersonating = bool(getattr(user, "_is_impersonated", False))
    staff = is_staff(user)
    review = can_review(user)
    return WebCapabilities(
        research=review,
        staff=staff,
        admin=user.user_role == USER_ROLE_ADMIN,
        pi_surfaces=user.user_role in (USER_ROLE_PI, USER_ROLE_ADMIN),
        verify_pi_email=staff and not impersonating,
        assign_reviews=staff and not impersonating,
        manage_suggestions=staff and not impersonating,
        chat=review and not impersonating,
    )
