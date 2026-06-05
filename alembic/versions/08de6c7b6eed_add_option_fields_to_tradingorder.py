"""add option fields to tradingorder

Revision ID: 08de6c7b6eed
Revises: a1f7e9c4b023
Create Date: 2026-06-05

Adds nullable option-contract metadata columns to TradingOrder so option
holdings reuse the existing TradingOrder/Transaction lifecycle. Equity orders
leave these unset (asset_class defaults to 'equity'). Multi-leg spreads are
stored later as a parent option order + leg children via the existing
parent_order_id field, so no new relationship is introduced here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '08de6c7b6eed'
down_revision: Union[str, None] = 'a1f7e9c4b023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column("tradingorder", sa.Column("asset_class", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("contract_symbol", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("option_type", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("strike", sa.Float(), nullable=True))
    op.add_column("tradingorder", sa.Column("expiry", sa.Date(), nullable=True))
    op.add_column("tradingorder", sa.Column("underlying_symbol", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("multiplier", sa.Integer(), nullable=True))
    op.add_column("tradingorder", sa.Column("position_intent", sa.String(), nullable=True))
    op.add_column("tradingorder", sa.Column("option_strategy", sa.String(), nullable=True))
    op.execute("UPDATE tradingorder SET asset_class = 'equity' WHERE asset_class IS NULL")
    with op.batch_alter_table("tradingorder") as batch_op:
        batch_op.alter_column("asset_class", existing_type=sa.String(),
                              nullable=False, server_default="equity")
    op.create_index("ix_tradingorder_contract_symbol", "tradingorder", ["contract_symbol"])
    op.create_index("ix_tradingorder_underlying_symbol", "tradingorder", ["underlying_symbol"])
    op.create_index("ix_tradingorder_asset_class", "tradingorder", ["asset_class"])


def downgrade():
    op.drop_index("ix_tradingorder_asset_class", table_name="tradingorder")
    op.drop_index("ix_tradingorder_underlying_symbol", table_name="tradingorder")
    op.drop_index("ix_tradingorder_contract_symbol", table_name="tradingorder")
    for col in ("option_strategy", "position_intent", "multiplier", "underlying_symbol",
                "expiry", "strike", "option_type", "contract_symbol", "asset_class"):
        op.drop_column("tradingorder", col)
