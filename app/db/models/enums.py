"""Domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """Roles, ordered loosely from most to least privileged."""

    ADMIN = "admin"              # full control, including delete
    CONTRIBUTOR = "contributor"  # create; update only records they own
    RESPONDER = "responder"      # read-only field user


class DisasterStatus(StrEnum):
    """Lifecycle of an incident."""

    REPORTED = "reported"        # newly filed, unverified
    VERIFIED = "verified"        # confirmed by an authority
    IN_PROGRESS = "in_progress"  # response under way
    RESOLVED = "resolved"        # response complete
    ARCHIVED = "archived"        # closed, retained for the record


class ResourceType(StrEnum):
    """Kinds of response resource."""

    SHELTER = "shelter"
    HOSPITAL = "hospital"
    FOOD = "food"
    WATER = "water"
    RESCUE = "rescue"
    MEDICAL_SUPPLY = "medical_supply"
    EVACUATION_POINT = "evacuation_point"


class ReportPriority(StrEnum):
    """Triage bucket assigned by the classifier (see services/classification.py)."""

    CRITICAL = "critical"  # life-threatening, act now
    HIGH = "high"          # urgent need
    MEDIUM = "medium"      # actionable need
    LOW = "low"            # informational


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
