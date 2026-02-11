"""Add database indexes to frequently queried columns

Revision ID: cee392a8acc3
Revises: 10fda7ae4bf3
Create Date: 2026-02-10 22:35:52.479596

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cee392a8acc3'
down_revision: Union[str, Sequence[str], None] = '10fda7ae4bf3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add indexes to frequently queried columns for pagination performance."""
    with op.batch_alter_table('activitylog', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_activitylog_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_activitylog_source_account_id'), ['source_account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_activitylog_source_expert_id'), ['source_expert_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_activitylog_type'), ['type'], unique=False)

    with op.batch_alter_table('expertrecommendation', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_expertrecommendation_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_expertrecommendation_instance_id'), ['instance_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_expertrecommendation_market_analysis_id'), ['market_analysis_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_expertrecommendation_symbol'), ['symbol'], unique=False)

    with op.batch_alter_table('marketanalysis', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_marketanalysis_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_marketanalysis_expert_instance_id'), ['expert_instance_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_marketanalysis_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_marketanalysis_symbol'), ['symbol'], unique=False)

    with op.batch_alter_table('trade_action_result', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trade_action_result_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_trade_action_result_expert_recommendation_id'), ['expert_recommendation_id'], unique=False)

    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tradingorder_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_tradingorder_expert_recommendation_id'), ['expert_recommendation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tradingorder_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_tradingorder_symbol'), ['symbol'], unique=False)
        batch_op.create_index(batch_op.f('ix_tradingorder_transaction_id'), ['transaction_id'], unique=False)

    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_transaction_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_transaction_expert_id'), ['expert_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transaction_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_transaction_symbol'), ['symbol'], unique=False)


def downgrade() -> None:
    """Remove indexes."""
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_transaction_symbol'))
        batch_op.drop_index(batch_op.f('ix_transaction_status'))
        batch_op.drop_index(batch_op.f('ix_transaction_expert_id'))
        batch_op.drop_index(batch_op.f('ix_transaction_created_at'))

    with op.batch_alter_table('tradingorder', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tradingorder_transaction_id'))
        batch_op.drop_index(batch_op.f('ix_tradingorder_symbol'))
        batch_op.drop_index(batch_op.f('ix_tradingorder_status'))
        batch_op.drop_index(batch_op.f('ix_tradingorder_expert_recommendation_id'))
        batch_op.drop_index(batch_op.f('ix_tradingorder_created_at'))

    with op.batch_alter_table('trade_action_result', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trade_action_result_expert_recommendation_id'))
        batch_op.drop_index(batch_op.f('ix_trade_action_result_created_at'))

    with op.batch_alter_table('marketanalysis', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_marketanalysis_symbol'))
        batch_op.drop_index(batch_op.f('ix_marketanalysis_status'))
        batch_op.drop_index(batch_op.f('ix_marketanalysis_expert_instance_id'))
        batch_op.drop_index(batch_op.f('ix_marketanalysis_created_at'))

    with op.batch_alter_table('expertrecommendation', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_expertrecommendation_symbol'))
        batch_op.drop_index(batch_op.f('ix_expertrecommendation_market_analysis_id'))
        batch_op.drop_index(batch_op.f('ix_expertrecommendation_instance_id'))
        batch_op.drop_index(batch_op.f('ix_expertrecommendation_created_at'))

    with op.batch_alter_table('activitylog', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_activitylog_type'))
        batch_op.drop_index(batch_op.f('ix_activitylog_source_expert_id'))
        batch_op.drop_index(batch_op.f('ix_activitylog_source_account_id'))
        batch_op.drop_index(batch_op.f('ix_activitylog_created_at'))
