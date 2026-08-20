"""SQLAlchemy models package.

Import all models here so Alembic can detect them.
"""

from src.models.access import AccessAllowlist, WaitlistSignup
from src.models.agent_activity import (
    AgentChannel,
    AgentMessage,
    LlmCallLog,
    PiDmMessage,
    PrivateChannelMember,
    SimulationRun,
    ThreadDecision,
    VISIBILITY_COLLAB_PRIVATE,
    VISIBILITY_PUBLIC,
)
from src.models.agent_registry import AgentRegistry, ProposalReview
from src.models.cohort import (
    COHORT_ACTION_AGENT_ADDED,
    COHORT_ACTION_AGENT_REMOVED,
    COHORT_ACTION_CREATED,
    COHORT_ACTION_DELETED,
    COHORT_ACTION_TOPOLOGY_SNAPSHOT,
    COHORT_NAME_ALL,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
)
from src.models.delegate import AgentDelegate, DelegateInvitation
from src.models.email_notification import (
    EmailEngagementTracker,
    EmailNotification,
    EmailNotificationPreference,
)
from src.models.job import Job
from src.models.opportunity import AssessmentDrop, OpportunityAssessment
from src.models.profile_revision import ProfileRevision
from src.models.profile import ResearcherProfile
from src.models.proposal_vote import VOTE_DOWN, VOTE_UP, ProposalVote
from src.models.provisioning import AppSetting, SlackAppProvision
from src.models.publication import Publication
from src.models.specialist_consult import SpecialistConsult
from src.models.user import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    VALID_USER_ROLES,
    User,
)

__all__ = [
    "User",
    "ResearcherProfile",
    "Publication",
    "Job",
    "AssessmentDrop",
    "OpportunityAssessment",
    "SpecialistConsult",
    "SimulationRun",
    "AgentMessage",
    "AgentChannel",
    "LlmCallLog",
    "ThreadDecision",
    "PiDmMessage",
    "PrivateChannelMember",
    "VISIBILITY_PUBLIC",
    "VISIBILITY_COLLAB_PRIVATE",
    "AgentRegistry",
    "ProposalReview",
    "Cohort",
    "CohortAuditEvent",
    "CohortMembership",
    "COHORT_ACTION_CREATED",
    "COHORT_ACTION_DELETED",
    "COHORT_ACTION_AGENT_ADDED",
    "COHORT_ACTION_AGENT_REMOVED",
    "COHORT_ACTION_TOPOLOGY_SNAPSHOT",
    "COHORT_NAME_ALL",
    "ProposalVote",
    "VOTE_UP",
    "VOTE_DOWN",
    "DelegateInvitation",
    "AgentDelegate",
    "EmailNotification",
    "EmailEngagementTracker",
    "EmailNotificationPreference",
    "ProfileRevision",
    "AccessAllowlist",
    "WaitlistSignup",
    "AppSetting",
    "SlackAppProvision",
    "USER_ROLE_PI",
    "USER_ROLE_MANAGER",
    "USER_ROLE_ADMIN",
    "VALID_USER_ROLES",
]
