"""Append-only audit log.

Not required by the brief, included because in a disaster-response context
"who changed this incident's status to resolved, and when?" is an operational
question, not a nice-to-have.

Design notes:

* **Append-only.** No update or delete path exists in the repository layer. An
  audit trail you can edit is not an audit trail.
* **``actor_id`` has no foreign key.** A trail must outlive the rows it
  describes. An FK with RESTRICT would block user deletion forever; with CASCADE
  it would destroy exactly the evidence you kept it for.
* **``changes`` is JSONB.** The shape differs per entity and per action, and it
  is written far more often than it is queried. JSONB keeps it queryable
  (``changes->>'status'``) without pinning the schema.
"""

from __future__ import annotations

import uuid

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import AuditAction


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    action: Mapped[AuditAction] = mapped_column(
        SAEnum(AuditAction, name="audit_action", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # {"status": {"from": "reported", "to": "verified"}}
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # No updated_at: rows are never modified, so TimestampMixin would be a lie.
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # "Show me this incident's history" is the only read pattern, so index
        # exactly that: the entity, then time.
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_actor_id", "actor_id"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type}:{self.entity_id}>"
