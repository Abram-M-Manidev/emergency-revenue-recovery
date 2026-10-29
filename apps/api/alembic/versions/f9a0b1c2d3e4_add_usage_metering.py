"""add usage metering: per-call usage records and per-conversation LLM tokens

- `call_usage_records` — one row per Vapi call, written from the
  end-of-call report, including calls that never had a conversation and
  calls no voice line could be matched to (`organization_id` NULL). Holds
  the reported facts, the billing decision made from them under
  `rules_version`, and Vapi's own cost figures exactly as reported. See
  `app/domain/usage.py`.
- `conversation_llm_usage` — provider-reported token counts per
  conversation and model, added to on every turn. See
  `app/domain/ai/usage.py`.

Additive only: two new tables, no existing row changed. `conversation_id`
is SET NULL on both so usage survives transcript deletion. Downgrade drops
both tables, and the usage history with them.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-10-29 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'call_usage_records',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=True),
        sa.Column('conversation_id', sa.UUID(), nullable=True),
        sa.Column('vapi_call_id', sa.String(length=128), nullable=False),
        # Non-native enums: store the member NAME (e.g. 'VOICE_CALL'), like
        # every other enum column in this schema.
        sa.Column('attribution', sa.Enum(
            'VOICE_CALL', 'VOICE_LINE', 'UNATTRIBUTED',
            name='usage_attribution', native_enum=False, length=20), nullable=False),
        sa.Column('vapi_assistant_id', sa.String(length=128), nullable=True),
        sa.Column('vapi_phone_number_id', sa.String(length=128), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reported_duration_seconds', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('ended_reason', sa.String(length=128), nullable=True),
        sa.Column('billing_period', sa.Date(), nullable=False),
        sa.Column('period_basis', sa.Enum(
            'STARTED_AT', 'ENDED_AT', 'RECEIVED_AT',
            name='usage_period_basis', native_enum=False, length=20), nullable=False),
        sa.Column('duration_source', sa.Enum(
            'REPORTED', 'TIMESTAMPS', 'MISSING',
            name='usage_duration_source', native_enum=False, length=20), nullable=False),
        sa.Column('billable_seconds', sa.Integer(), nullable=False),
        sa.Column('billable_minutes', sa.Integer(), nullable=False),
        sa.Column('is_billable', sa.Boolean(), nullable=False),
        sa.Column('exclusion_reason', sa.Enum(
            'DURATION_UNKNOWN', 'UNDER_MINIMUM', 'FAULT_ENDED_REASON',
            name='usage_exclusion_reason', native_enum=False, length=30), nullable=True),
        sa.Column('rules_version', sa.String(length=32), nullable=False),
        sa.Column('vapi_reported_cost', sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column('vapi_reported_cost_breakdown', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column('report_count', sa.Integer(), nullable=False),
        sa.Column('first_reported_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_reported_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vapi_call_id'),
    )
    op.create_index('ix_call_usage_records_organization_period', 'call_usage_records',
                    ['organization_id', 'billing_period'], unique=False)

    op.create_table(
        'conversation_llm_usage',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('organization_id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=True),
        sa.Column('channel', sa.String(length=16), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('requests', sa.Integer(), nullable=False),
        sa.Column('requests_without_usage', sa.Integer(), nullable=False),
        sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
        sa.Column('cached_prompt_tokens', sa.BigInteger(), nullable=False),
        sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('conversation_id', 'model', name='uq_conversation_llm_usage_model'),
    )
    op.create_index('ix_conversation_llm_usage_organization_created', 'conversation_llm_usage',
                    ['organization_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_conversation_llm_usage_organization_created',
                  table_name='conversation_llm_usage')
    op.drop_table('conversation_llm_usage')
    op.drop_index('ix_call_usage_records_organization_period', table_name='call_usage_records')
    op.drop_table('call_usage_records')
