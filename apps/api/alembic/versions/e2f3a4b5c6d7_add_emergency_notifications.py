"""add emergency notification settings and delivery tracking

Backs the product promise ESSR could not previously keep. The assistant told
emergency callers "a dispatcher has been alerted" while the system had no
outbound notification mechanism of any kind, so the sentence was false on
every call that produced one.

Two tables, deliberately separate:

- `organization_notification_settings` — where one tenant's alerts go. Its
  own table rather than columns on `business_profiles`, because that profile
  is assembled into the LLM system prompt on every turn and `destination` is
  a credential (a Slack or Teams incoming-webhook URL is the whole secret).
- `emergency_notification_deliveries` — what happened when we tried. The
  unique index on `emergency_ticket_id` is the idempotency mechanism, not
  merely a constraint: `claim()` inserts against it with ON CONFLICT DO
  NOTHING so exactly one caller, across all four uvicorn workers, wins the
  right to send. Without it a ticket created by the tool loop and again by
  the webhook's outcome sync would page the on-call engineer twice for one
  gas leak.

Additive only: two new tables, no change to any existing table, no data
migration. Dropping them restores the previous behaviour, in which the
backend simply has no evidence either way and the assistant is not permitted
to claim an alert.

Enums are stored as VARCHAR + CHECK (`native_enum=False`), matching every
other enum column in this schema — this project has twice been bitten by
PostgreSQL ENUM type/label mismatches, and this avoids that class entirely.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-10 05:30:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHANNELS = ('webhook',)
_STATUSES = ('pending', 'delivered', 'failed', 'not_configured')


def upgrade() -> None:
    op.create_table(
        'organization_notification_settings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column(
            'channel',
            sa.Enum(*_CHANNELS, name='notification_channel', native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column('destination', sa.Text(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # One configuration per tenant, as a unique INDEX rather than a separate
    # UNIQUE constraint plus a plain index: the model declares
    # `unique=True, index=True` on this column, which SQLAlchemy renders as
    # exactly one unique index. Splitting it in the migration made
    # `alembic check` report permanent drift.
    op.create_index(
        op.f('ix_organization_notification_settings_organization_id'),
        'organization_notification_settings',
        ['organization_id'],
        unique=True,
    )

    op.create_table(
        'emergency_notification_deliveries',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('emergency_ticket_id', sa.UUID(), nullable=False),
        # Nullable: a delivery row exists even when nothing is configured.
        # "We tried and there was nowhere to send" is the fact an operator
        # needs in order to discover the gap.
        sa.Column(
            'channel',
            sa.Enum(*_CHANNELS, name='notification_channel', native_enum=False, length=20),
            nullable=True,
        ),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                *_STATUSES, name='notification_delivery_status', native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_code', sa.String(length=50), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['emergency_ticket_id'], ['emergency_tickets.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        # The idempotency mechanism. See the module docstring.
        sa.UniqueConstraint('emergency_ticket_id', name='uq_notification_delivery_ticket'),
    )
    op.create_index(
        op.f('ix_emergency_notification_deliveries_organization_id'),
        'emergency_notification_deliveries',
        ['organization_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_emergency_notification_deliveries_organization_id'),
        table_name='emergency_notification_deliveries',
    )
    op.drop_table('emergency_notification_deliveries')
    op.drop_index(
        op.f('ix_organization_notification_settings_organization_id'),
        table_name='organization_notification_settings',
    )
    op.drop_table('organization_notification_settings')
