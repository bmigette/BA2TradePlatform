"""add account_symbol_facts — cached broker per-symbol facts, PER ACCOUNT

Revision ID: a4e91c76b2d8
Revises: b7f3d21c98ae
Create Date: 2026-09-05

Fractionability and the margin rate are answers a specific broker gives a specific
account, not properties of the ticker: the same name is fractionable at one broker and
not another, and a marginable symbol costs 1x buying power in a 2:1 account but 2x in a
cash one. Storing either on ``instrument`` would state the fact about the wrong subject,
which is why this is keyed on ``(account_id, symbol)``.

The table is a CACHE of ``AccountInterface.get_symbol_margin_info()`` so the allocator
can show the facts without a REST round-trip per page load. It is refreshed by an
explicit user action (the allocator's Refresh button), never lazily on read — a stale
row that silently repairs itself is indistinguishable from a fresh one, so ``fetched_at``
is stored and the reader can see the age.

EVERY FLAG IS NULLABLE AND TRI-STATE, mirroring ``MarginInfo``: NULL means "the broker
did not say", which is also what a missing row means, and neither may be read as a
refusal. That is why no column carries a server default — a DEFAULT FALSE here would
turn "unknown" into "the broker said no" for every pre-existing account on upgrade.

Index names are the ones SQLAlchemy itself emits (``ix_<table>_<column>``) so
``init_db()``'s create_all on a fresh database and Alembic on an existing one produce
the same schema. The foreign key is declarative only — the live DB runs with
``PRAGMA foreign_keys = 0``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a4e91c76b2d8'
down_revision: Union[str, Sequence[str], None] = 'b7f3d21c98ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'account_symbol_facts'


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        # init_db()'s create_all may already have built it on a fresh database.
        return
    op.create_table(
        _TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('fractionable', sa.Boolean(), nullable=True),
        sa.Column('marginable', sa.Boolean(), nullable=True),
        sa.Column('tradable', sa.Boolean(), nullable=True),
        sa.Column('bp_factor', sa.Float(), nullable=True),
        sa.Column('initial_margin_rate', sa.Float(), nullable=True),
        sa.Column('maintenance_margin_rate', sa.Float(), nullable=True),
        sa.Column('min_order_size', sa.Float(), nullable=True),
        sa.Column('min_trade_increment', sa.Float(), nullable=True),
        sa.Column('min_fractional_notional', sa.Float(), nullable=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accountdefinition.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'symbol', name='uix_account_symbol_facts'),
    )
    op.create_index(f'ix_{_TABLE}_account_id', _TABLE, ['account_id'])
    op.create_index(f'ix_{_TABLE}_symbol', _TABLE, ['symbol'])
    op.create_index(f'ix_{_TABLE}_fetched_at', _TABLE, ['fetched_at'])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    op.drop_index(f'ix_{_TABLE}_fetched_at', table_name=_TABLE)
    op.drop_index(f'ix_{_TABLE}_symbol', table_name=_TABLE)
    op.drop_index(f'ix_{_TABLE}_account_id', table_name=_TABLE)
    op.drop_table(_TABLE)
