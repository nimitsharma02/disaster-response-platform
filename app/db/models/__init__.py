"""Model package.

Importing every model here is what makes ``Base.metadata`` complete. Alembic's
autogenerate compares the database against that metadata, so a model that is
never imported is a table Alembic will cheerfully offer to drop.
"""

from app.db.base import Base
from app.db.models.audit import AuditLog
from app.db.models.disaster import Disaster
from app.db.models.enums import (
    AuditAction,
    DisasterStatus,
    ReportPriority,
    ResourceType,
    UserRole,
)
from app.db.models.resource import Resource
from app.db.models.user import User

__all__ = [
    "AuditAction",
    "AuditLog",
    "Base",
    "Disaster",
    "DisasterStatus",
    "ReportPriority",
    "Resource",
    "ResourceType",
    "User",
    "UserRole",
]
