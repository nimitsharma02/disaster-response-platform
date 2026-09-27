"""Initial schema: extensions, enums, tables, spatial and lookup indexes."""

from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    user_role = postgresql.ENUM(
        "admin", "contributor", "responder", name="user_role", create_type=False
    )
    disaster_status = postgresql.ENUM(
        "reported", "verified", "in_progress", "resolved", "archived",
        name="disaster_status", create_type=False,
    )
    resource_type = postgresql.ENUM(
        "shelter", "hospital", "food", "water", "rescue",
        "medical_supply", "evacuation_point",
        name="resource_type", create_type=False,
    )
    audit_action = postgresql.ENUM(
        "create", "update", "delete", name="audit_action", create_type=False
    )
    for enum in (user_role, disaster_status, resource_type, audit_action):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", user_role, nullable=False, server_default="contributor"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_role", "users", ["role"])

    op.create_table(
        "disasters",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location_name", sa.String(300), nullable=True),
        sa.Column(
            "location",
            geoalchemy2.types.Geography(
                geometry_type="POINT", srid=4326, spatial_index=False, from_text="ST_GeogFromText"
            ),
            nullable=True,
        ),
        sa.Column("location_source", sa.String(48), nullable=True),
        sa.Column("location_confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("status", disaster_status, nullable=False, server_default="reported"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_disasters"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_disasters_created_by_users", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("char_length(btrim(title)) > 0", name="ck_disasters_title_not_blank"),
        sa.CheckConstraint(
            "location_confidence IS NULL OR (location_confidence >= 0 AND location_confidence <= 1)",
            name="ck_disasters_confidence_range",
        ),
        sa.CheckConstraint("array_length(tags, 1) IS NULL OR array_length(tags, 1) <= 20",
                           name="ck_disasters_tag_count"),
    )

    op.create_index("ix_disasters_location", "disasters", ["location"], postgresql_using="gist")
    op.create_index("ix_disasters_tags", "disasters", ["tags"], postgresql_using="gin")
    op.execute("CREATE INDEX ix_disasters_status_created_at ON disasters (status, created_at DESC)")
    op.execute("CREATE INDEX ix_disasters_created_at ON disasters (created_at DESC)")
    op.create_index("ix_disasters_created_by", "disasters", ["created_by"])
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_disasters_title_trgm ON disasters USING gin (title gin_trgm_ops)")

    op.create_table(
        "resources",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("type", resource_type, nullable=False),
        sa.Column(
            "location",
            geoalchemy2.types.Geography(
                geometry_type="POINT", srid=4326, spatial_index=False, from_text="ST_GeogFromText"
            ),
            nullable=False,
        ),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("contact", sa.String(120), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("disaster_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_resources"),
        sa.ForeignKeyConstraint(
            ["disaster_id"], ["disasters.id"],
            name="fk_resources_disaster_id_disasters", ondelete="SET NULL",
        ),
        sa.CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_resources_capacity_non_negative"),
    )
    op.create_index("ix_resources_location", "resources", ["location"], postgresql_using="gist")
    op.create_index("ix_resources_type", "resources", ["type"])
    op.create_index("ix_resources_disaster_id", "resources", ["disaster_id"])

    # audit_log
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", audit_action, nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_username", sa.String(64), nullable=True),
        sa.Column("changes", postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("resources")
    op.drop_table("disasters")
    op.drop_table("users")
    for name in ("audit_action", "resource_type", "disaster_status", "user_role"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
