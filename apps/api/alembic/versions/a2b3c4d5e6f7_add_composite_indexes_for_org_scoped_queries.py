"""add composite indexes for org-scoped queries

Revision ID: a2b3c4d5e6f7
Revises: e5f6a7b8c9d0
Create Date: 2026-07-27 09:00:00.000000

Milestone 10 (Production Polish) database audit. Every table here already
has a single-column index on `organization_id`, but the actual query
patterns (read directly from `emergency_ticket_repository_impl.py`,
`appointment_repository_impl.py`, `conversation_repository_impl.py`)
always filter on `organization_id` *plus* a second column (`status`,
`created_at`, or `closed_at`/`started_at`) — a single-column index can't
serve those as efficiently as a matching composite one. Purely additive:
no column/table changes, no data backfill, no new permissions.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_emergency_tickets_org_status", "emergency_tickets", ["organization_id", "status"]
    )
    op.create_index(
        "ix_emergency_tickets_org_created_at",
        "emergency_tickets",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_emergency_tickets_org_closed_at", "emergency_tickets", ["organization_id", "closed_at"]
    )
    op.create_index(
        "ix_appointments_org_status", "appointments", ["organization_id", "status"]
    )
    op.create_index(
        "ix_appointments_org_created_at", "appointments", ["organization_id", "created_at"]
    )
    op.create_index(
        "ix_appointments_org_closed_at", "appointments", ["organization_id", "closed_at"]
    )
    op.create_index(
        "ix_conversations_org_started_at", "conversations", ["organization_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_org_started_at", table_name="conversations")
    op.drop_index("ix_appointments_org_closed_at", table_name="appointments")
    op.drop_index("ix_appointments_org_created_at", table_name="appointments")
    op.drop_index("ix_appointments_org_status", table_name="appointments")
    op.drop_index("ix_emergency_tickets_org_closed_at", table_name="emergency_tickets")
    op.drop_index("ix_emergency_tickets_org_created_at", table_name="emergency_tickets")
    op.drop_index("ix_emergency_tickets_org_status", table_name="emergency_tickets")
