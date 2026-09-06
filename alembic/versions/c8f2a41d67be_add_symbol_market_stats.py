"""add symbol_market_stats — cached yield / 1Y / 3Y per symbol, GLOBAL

Revision ID: c8f2a41d67be
Revises: a4e91c76b2d8
Create Date: 2026-09-05

The allocator's ⓘ tooltip shows a symbol's dividend yield and 1Y/3Y total return.
Those come from ``ba2_providers.symbol_info``, whose own cache is an in-memory 24h
TTL — so every process restart re-fetches several FMP calls per symbol, and a
35-symbol allocation page would pay that on its first render. This table makes the
answer survive a restart and keeps the render path free of REST entirely.

GLOBAL, keyed on symbol alone, unlike ``account_symbol_facts`` next door: a yield and
a total return are properties of the INSTRUMENT (every account holding GDXY sees the
same 69.73%), whereas fractionability and the margin rate are answers a specific
broker gives a specific account.

Every figure is nullable with NO server default, and null means UNKNOWN. A fund that
pays nothing has 0.0; a symbol whose fetch failed has NULL and an ``error``. A
DEFAULT 0 here would render every unfetched symbol as a non-payer.

Index names are the ones SQLAlchemy itself emits (``ix_<table>_<column>``) so
``init_db()``'s create_all on a fresh database and Alembic on an existing one produce
the same schema.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c8f2a41d67be'
down_revision: Union[str, Sequence[str], None] = 'a4e91c76b2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'symbol_market_stats'


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return          # init_db()'s create_all may already have built it
    op.create_table(
        _TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('dividend_yield_pct', sa.Float(), nullable=True),
        sa.Column('total_return_1y_pct', sa.Float(), nullable=True),
        sa.Column('total_return_3y_pct', sa.Float(), nullable=True),
        sa.Column('company_name', sa.String(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', name='uix_symbol_market_stats_symbol'),
    )
    op.create_index(f'ix_{_TABLE}_symbol', _TABLE, ['symbol'])
    op.create_index(f'ix_{_TABLE}_fetched_at', _TABLE, ['fetched_at'])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index(f'ix_{_TABLE}_fetched_at', table_name=_TABLE)
    op.drop_index(f'ix_{_TABLE}_symbol', table_name=_TABLE)
    op.drop_table(_TABLE)
