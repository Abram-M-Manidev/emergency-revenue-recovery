"""System-wide permission catalogue and the default role templates every
new organization is seeded with.

Business-feature permissions (calls, tickets, appointments, ...) are added
in the milestones that introduce those features — this file only covers
what Milestone 1 needs to prove RBAC works: managing the org itself and
its users.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    code: str
    description: str


class Permissions:
    ORGANIZATION_MANAGE = "organization:manage"
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    BUSINESS_KNOWLEDGE_READ = "business_knowledge:read"
    BUSINESS_KNOWLEDGE_MANAGE = "business_knowledge:manage"
    AI_CONVERSATIONS_READ = "ai_conversations:read"
    AI_CONVERSATIONS_SIMULATE = "ai_conversations:simulate"


PERMISSION_CATALOGUE: tuple[PermissionDefinition, ...] = (
    PermissionDefinition(Permissions.ORGANIZATION_MANAGE, "Manage organization settings"),
    PermissionDefinition(Permissions.USERS_READ, "View users within the organization"),
    PermissionDefinition(Permissions.USERS_MANAGE, "Invite, edit, and deactivate users"),
    PermissionDefinition(
        Permissions.BUSINESS_KNOWLEDGE_READ, "View the organization's business knowledge"
    ),
    PermissionDefinition(
        Permissions.BUSINESS_KNOWLEDGE_MANAGE,
        "Manage the organization's business knowledge (profile, hours, services, FAQs, ...)",
    ),
    PermissionDefinition(
        Permissions.AI_CONVERSATIONS_READ, "View AI Brain conversation logs and outcomes"
    ),
    PermissionDefinition(
        Permissions.AI_CONVERSATIONS_SIMULATE,
        "Start and send messages in test AI Brain conversations",
    ),
)

DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "Owner": (
        Permissions.ORGANIZATION_MANAGE,
        Permissions.USERS_READ,
        Permissions.USERS_MANAGE,
        Permissions.BUSINESS_KNOWLEDGE_READ,
        Permissions.BUSINESS_KNOWLEDGE_MANAGE,
        Permissions.AI_CONVERSATIONS_READ,
        Permissions.AI_CONVERSATIONS_SIMULATE,
    ),
    "Admin": (
        Permissions.USERS_READ,
        Permissions.USERS_MANAGE,
        Permissions.BUSINESS_KNOWLEDGE_READ,
        Permissions.BUSINESS_KNOWLEDGE_MANAGE,
        Permissions.AI_CONVERSATIONS_READ,
        Permissions.AI_CONVERSATIONS_SIMULATE,
    ),
    "Member": (
        Permissions.USERS_READ,
        Permissions.BUSINESS_KNOWLEDGE_READ,
        Permissions.AI_CONVERSATIONS_READ,
    ),
}

OWNER_ROLE_NAME = "Owner"
