"""add emergency paging: on-call settings, pages, and page notifications

Getting a named on-call person to ACKNOWLEDGE an emergency, escalating to a
backup when they do not. Three tables:

- `organization_paging_settings` — per tenant: enabled, primary and backup
  numbers (E.164), SMS / voice channels, acknowledgement timeout. Its own
  table rather than columns on `business_profiles`, because that profile is
  assembled into the LLM prompt and these are personal mobiles.
- `emergency_pages` — one per emergency ticket (unique, the idempotency
  key): the escalation state machine paging_primary -> paging_backup ->
  acknowledged | unresolved, its deadline, and who acknowledged how.
- `emergency_page_notifications` — one per (page, role, channel): the send
  outbox, with attempts, a lease for in-flight sends, and the provider's
  verdict (sent is NOT acknowledged).

Additive only: three new tables, no change to any existing table, no data
migration. Downgrade drops them; emergency tickets and the webhook alert
outbox are untouched either way.

Enums are VARCHAR (`native_enum=False`) storing the member NAMES, like every
enum in this schema.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-30 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
    ]


def _role(nullable: bool) -> sa.Column:
    return sa.Column(
        'role' if not nullable else 'acknowledged_by_role',
        sa.Enum('PRIMARY', 'BACKUP', name='paging_recipient_role', native_enum=False, length=20),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        'organization_paging_settings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('primary_number', sa.String(length=32), nullable=True),
        sa.Column('backup_number', sa.String(length=32), nullable=True),
        sa.Column('sms_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('voice_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('ack_timeout_seconds', sa.Integer(), nullable=False, server_default='300'),
        *_timestamps(),
        sa.CheckConstraint('ack_timeout_seconds BETWEEN 60 AND 3600',
                           name='ck_organization_paging_settings_ack_timeout'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_organization_paging_settings_organization_id'),
        'organization_paging_settings', ['organization_id'], unique=True,
    )

    op.create_table(
        'emergency_pages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('emergency_ticket_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.Enum(
            'PAGING_PRIMARY', 'PAGING_BACKUP', 'ACKNOWLEDGED', 'UNRESOLVED',
            name='emergency_page_status', native_enum=False, length=20), nullable=False),
        sa.Column('ack_timeout_seconds', sa.Integer(), nullable=False),
        sa.Column('escalate_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
        _role(nullable=True),
        sa.Column('acknowledged_via', sa.Enum(
            'LINK', 'DASHBOARD', name='paging_ack_method', native_enum=False, length=20),
            nullable=True),
        sa.Column('acknowledged_by_user_id', sa.UUID(), nullable=True),
        sa.Column('unresolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('unresolved_reason', sa.String(length=50), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['emergency_ticket_id'], ['emergency_tickets.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['acknowledged_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('emergency_ticket_id'),
    )
    op.create_index(op.f('ix_emergency_pages_organization_id'), 'emergency_pages',
                    ['organization_id'], unique=False)
    op.create_index('ix_emergency_pages_escalate_at', 'emergency_pages', ['escalate_at'],
                    unique=False, postgresql_where=sa.text('escalate_at IS NOT NULL'))

    op.create_table(
        'emergency_page_notifications',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('page_id', sa.UUID(), nullable=False),
        _role(nullable=False),
        sa.Column('channel', sa.Enum(
            'SMS', 'VOICE', name='paging_channel', native_enum=False, length=20), nullable=False),
        sa.Column('destination', sa.String(length=32), nullable=False),
        sa.Column('status', sa.Enum(
            'QUEUED', 'SENDING', 'SENT', 'RETRYING', 'FAILED', 'CANCELED',
            name='emergency_page_notification_status', native_enum=False, length=20),
            nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('provider', sa.String(length=50), nullable=True),
        sa.Column('provider_message_id', sa.String(length=64), nullable=True),
        sa.Column('error_code', sa.String(length=50), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['page_id'], ['emergency_pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('page_id', 'role', 'channel',
                            name='uq_emergency_page_notifications_page_role_channel'),
    )
    op.create_index(op.f('ix_emergency_page_notifications_organization_id'),
                    'emergency_page_notifications', ['organization_id'], unique=False)
    op.create_index(op.f('ix_emergency_page_notifications_page_id'),
                    'emergency_page_notifications', ['page_id'], unique=False)
    op.create_index('ix_emergency_page_notifications_due', 'emergency_page_notifications',
                    ['next_attempt_at'], unique=False,
                    postgresql_where=sa.text('next_attempt_at IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('ix_emergency_page_notifications_due',
                  table_name='emergency_page_notifications')
    op.drop_index(op.f('ix_emergency_page_notifications_page_id'),
                  table_name='emergency_page_notifications')
    op.drop_index(op.f('ix_emergency_page_notifications_organization_id'),
                  table_name='emergency_page_notifications')
    op.drop_table('emergency_page_notifications')
    op.drop_index('ix_emergency_pages_escalate_at', table_name='emergency_pages')
    op.drop_index(op.f('ix_emergency_pages_organization_id'), table_name='emergency_pages')
    op.drop_table('emergency_pages')
    op.drop_index(op.f('ix_organization_paging_settings_organization_id'),
                  table_name='organization_paging_settings')
    op.drop_table('organization_paging_settings')
