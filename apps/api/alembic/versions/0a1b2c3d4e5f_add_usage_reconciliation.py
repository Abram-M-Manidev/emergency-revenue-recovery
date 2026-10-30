"""add usage reconciliation: Vapi's view of each call beside our usage record

- `call_usage_records.source` — WEBHOOK (the end-of-call report) or
  RECONCILIATION (no report ever arrived; recorded from Vapi's call API).
  Every existing row came from a report, so the server default backfills
  them as WEBHOOK.
- `usage_reconciliation_runs` — one row per reconciliation run.
- `vapi_call_reconciliations` — the latest reconciliation of each Vapi
  call: outcome, discrepancies, the fields it filled, and Vapi's own
  figures. See `app/domain/usage_reconciliation.py`.

Additive only: one defaulted column and two new tables. Downgrade drops the
tables and the column, and the reconciliation history with them; usage
records created by reconciliation stay, indistinguishable from reports.

Revision ID: 0a1b2c3d4e5f
Revises: f9a0b1c2d3e4
Create Date: 2026-10-30 12:00:00.000000

"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0a1b2c3d4e5f'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Non-native enums store the member NAME, like every other enum column.
    op.add_column('call_usage_records', sa.Column('source', sa.Enum(
        'WEBHOOK', 'RECONCILIATION', name='usage_source', native_enum=False, length=20),
        server_default='WEBHOOK', nullable=False))

    op.create_table(
        'usage_reconciliation_runs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.Enum(
            'RUNNING', 'SUCCEEDED', 'FAILED',
            name='usage_reconciliation_run_status', native_enum=False, length=20),
            nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'vapi_call_reconciliations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('vapi_call_id', sa.String(length=128), nullable=False),
        sa.Column('call_usage_record_id', sa.UUID(), nullable=True),
        sa.Column('last_run_id', sa.UUID(), nullable=True),
        sa.Column('outcome', sa.Enum(
            'MATCHED', 'FILLED', 'DISCREPANT', 'MISSING_LOCALLY', 'MISSING_AT_VAPI', 'NOT_ENDED',
            name='usage_reconciliation_outcome', native_enum=False, length=20), nullable=False),
        sa.Column('discrepancies', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('filled_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('check_count', sa.Integer(), nullable=False),
        sa.Column('vapi_status', sa.String(length=32), nullable=True),
        sa.Column('vapi_assistant_id', sa.String(length=128), nullable=True),
        sa.Column('vapi_phone_number_id', sa.String(length=128), nullable=True),
        sa.Column('vapi_created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vapi_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vapi_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vapi_ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vapi_ended_reason', sa.String(length=128), nullable=True),
        sa.Column('vapi_cost', sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column('vapi_cost_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('vapi_minutes_normal', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('vapi_minutes_overage', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('transport_minutes', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('vapi_usage_incomplete', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.ForeignKeyConstraint(['call_usage_record_id'], ['call_usage_records.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['last_run_id'], ['usage_reconciliation_runs.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vapi_call_id'),
    )
    op.create_index('ix_vapi_call_reconciliations_outcome', 'vapi_call_reconciliations',
                    ['outcome'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_vapi_call_reconciliations_outcome',
                  table_name='vapi_call_reconciliations')
    op.drop_table('vapi_call_reconciliations')
    op.drop_table('usage_reconciliation_runs')
    op.drop_column('call_usage_records', 'source')
