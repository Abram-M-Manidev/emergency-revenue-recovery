"""add caller disclosure: per-tenant notice policy, and what each call was told

- `organization_disclosure_settings` — one row per tenant that has chosen
  its caller notices (AI disclosure, recording notice). No row means the
  default: both on.
- `voice_calls.disclosure_sent_at` / `disclosed_ai` / `disclosed_recording`
  — the notice ERRS gave on the call, recorded once. NULL means unknown
  (every call before this migration), deliberately distinct from "no notice".

Additive only: a new table and three nullable columns, no data migration, no
existing row changed. Downgrade drops them; existing recordings and calls are
untouched either way.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-10-01 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'organization_disclosure_settings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('ai_disclosure_enabled', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('recording_notice_enabled', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_organization_disclosure_settings_organization_id'),
        'organization_disclosure_settings', ['organization_id'], unique=True,
    )
    op.add_column('voice_calls',
                  sa.Column('disclosure_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('voice_calls', sa.Column('disclosed_ai', sa.Boolean(), nullable=True))
    op.add_column('voice_calls', sa.Column('disclosed_recording', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('voice_calls', 'disclosed_recording')
    op.drop_column('voice_calls', 'disclosed_ai')
    op.drop_column('voice_calls', 'disclosure_sent_at')
    op.drop_index(op.f('ix_organization_disclosure_settings_organization_id'),
                  table_name='organization_disclosure_settings')
    op.drop_table('organization_disclosure_settings')
