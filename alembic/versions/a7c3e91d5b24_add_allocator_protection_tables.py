"""add the allocator TP/SL protection, exclusion and weight-audit tables

Revision ID: a7c3e91d5b24
Revises: d9e3b72a10fc
Create Date: 2026-10-04

Creates ``allocator_protection`` (one row per account+symbol: the operator's stop price, the
take-profit targets, the last protective fill and the alert state), ``allocator_protection_order``
(one row per order the allocator placed at the broker: an OCO complex order per take-profit
target, plus a plain stop-only order for the part no target covers), ``allocator_exclusion`` (the
operator's manual 'disable' of a symbol for allocation) and ``allocator_weight_change`` (the
audit trail of the automatic weight reductions a protective fill makes).

IDEMPOTENT ON PURPOSE, the same pattern as f1c8a24b7e05: ``main.initialize_system`` imports the
in-tree models BEFORE ``init_db()``, so ``create_all`` materialises all four tables on any database
the first time the app starts on this branch -- outside alembic. An unguarded ``create_table``
would then die with "table already exists" and the revision could never be applied. Every create
is therefore guarded by ``has_table`` / ``has_index``; if create_all already built them,
``alembic stamp a7c3e91d5b24`` is also legitimate.

Index names are the ones SQLAlchemy itself emits for these models (``ix_<table>_<column>``) so
create_all and this revision agree. Foreign keys are declarative (the live DB runs with
PRAGMA foreign_keys = 0).

Nothing here touches an existing row. Downgrade drops the four tables (and with them every
protection configuration -- they are NOT recoverable; orders already placed at the broker are
not cancelled by a downgrade, so cancel them on the broker first).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c3e91d5b24'
down_revision: Union[str, Sequence[str], None] = 'd9e3b72a10fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _create_index_if_absent(index_name: str, table_name: str, columns, *, unique: bool = False) -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_index(table_name, index_name):
        print(f"[alloc-protection] index {index_name} already exists -- skipped")
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    if _has_table('allocator_protection'):
        print("[alloc-protection] allocator_protection already exists -- skipped")
    else:
        op.create_table(
            'allocator_protection',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('account_id', sa.Integer(),
                      sa.ForeignKey('accountdefinition.id', ondelete='CASCADE'), nullable=False),
            sa.Column('symbol', sa.String(), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('sl_price', sa.Float(), nullable=False),
            sa.Column('tp_targets', sa.JSON(), nullable=True),
            sa.Column('pending_replace', sa.Boolean(), nullable=False),
            sa.Column('pending_replace_since', sa.DateTime(), nullable=True),
            sa.Column('last_fill_at', sa.DateTime(), nullable=True),
            sa.Column('auto_failures', sa.Integer(), nullable=False),
            sa.Column('last_auto_action_at', sa.DateTime(), nullable=True),
            sa.Column('disarmed_at', sa.DateTime(), nullable=True),
            sa.Column('disarmed_note', sa.String(), nullable=True),
            sa.Column('last_fill_note', sa.String(), nullable=True),
            sa.Column('alert_code', sa.String(), nullable=True),
            sa.Column('alert_message', sa.String(), nullable=True),
            sa.Column('alerted_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.String(), nullable=True),
            sa.Column('protected_quantity', sa.Float(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('account_id', 'symbol', name='uix_alloc_protection_account_symbol'),
        )
    _create_index_if_absent('ix_allocator_protection_account_id', 'allocator_protection', ['account_id'])
    _create_index_if_absent('ix_allocator_protection_symbol', 'allocator_protection', ['symbol'])

    if _has_table('allocator_protection_order'):
        print("[alloc-protection] allocator_protection_order already exists -- skipped")
    else:
        op.create_table(
            'allocator_protection_order',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('protection_id', sa.Integer(),
                      sa.ForeignKey('allocator_protection.id', ondelete='CASCADE'), nullable=False),
            sa.Column('slice_index', sa.Integer(), nullable=False),
            sa.Column('target_index', sa.Integer(), nullable=False),
            sa.Column('quantity', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(), nullable=False),
            sa.Column('tp_price', sa.Float(), nullable=True),
            sa.Column('sl_price', sa.Float(), nullable=False),
            sa.Column('complex_order_id', sa.Integer(), nullable=True),
            sa.Column('tp_order_id', sa.Integer(), nullable=True),
            sa.Column('sl_order_id', sa.Integer(), nullable=True),
            sa.Column('external_tag', sa.String(), nullable=True),
            sa.Column('state', sa.String(), nullable=False),
            sa.Column('filled_qty', sa.Float(), nullable=False),
            sa.Column('weight_applied_qty', sa.Float(), nullable=False),
            sa.Column('fill_price', sa.Float(), nullable=True),
            sa.Column('gtc_date', sa.DateTime(), nullable=True),
            sa.Column('gtc_date_assumed', sa.Boolean(), nullable=False),
            sa.Column('gtc_warned', sa.Boolean(), nullable=False),
            sa.Column('cancel_requested', sa.Boolean(), nullable=False),
            sa.Column('detail', sa.String(), nullable=True),
            sa.Column('placed_at', sa.DateTime(), nullable=False),
            sa.Column('closed_at', sa.DateTime(), nullable=True),
        )
    _create_index_if_absent('ix_allocator_protection_order_protection_id',
                            'allocator_protection_order', ['protection_id'])
    _create_index_if_absent('ix_allocator_protection_order_complex_order_id',
                            'allocator_protection_order', ['complex_order_id'])
    _create_index_if_absent('ix_allocator_protection_order_state',
                            'allocator_protection_order', ['state'])
    _create_exclusion_and_audit_tables()


def _create_exclusion_and_audit_tables() -> None:
    if _has_table('allocator_exclusion'):
        print("[alloc-protection] allocator_exclusion already exists -- skipped")
    else:
        op.create_table(
            'allocator_exclusion',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('account_id', sa.Integer(),
                      sa.ForeignKey('accountdefinition.id', ondelete='CASCADE'), nullable=False),
            sa.Column('symbol', sa.String(), nullable=False),
            sa.Column('excluded_reason', sa.String(), nullable=False),
            sa.Column('since', sa.DateTime(), nullable=False),
            sa.Column('note', sa.String(), nullable=True),
            sa.UniqueConstraint('account_id', 'symbol', name='uix_alloc_exclusion_account_symbol'),
        )
    _create_index_if_absent('ix_allocator_exclusion_account_id', 'allocator_exclusion', ['account_id'])
    _create_index_if_absent('ix_allocator_exclusion_symbol', 'allocator_exclusion', ['symbol'])

    if _has_table('allocator_weight_change'):
        print("[alloc-protection] allocator_weight_change already exists -- skipped")
    else:
        op.create_table(
            'allocator_weight_change',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('account_id', sa.Integer(),
                      sa.ForeignKey('accountdefinition.id', ondelete='CASCADE'), nullable=False),
            sa.Column('label', sa.String(), nullable=False),
            sa.Column('symbol', sa.String(), nullable=False),
            sa.Column('reason', sa.String(), nullable=False),
            sa.Column('before_pct', sa.Float(), nullable=False),
            sa.Column('after_pct', sa.Float(), nullable=False),
            sa.Column('detail', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
    for column in ('account_id', 'label', 'symbol', 'created_at'):
        _create_index_if_absent(f'ix_allocator_weight_change_{column}', 'allocator_weight_change', [column])


def downgrade() -> None:
    for name in ('allocator_weight_change', 'allocator_exclusion',
                 'allocator_protection_order', 'allocator_protection'):
        if _has_table(name):
            op.drop_table(name)
