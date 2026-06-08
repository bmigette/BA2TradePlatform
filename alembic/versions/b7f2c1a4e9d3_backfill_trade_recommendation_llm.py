"""backfill trade_recommendation_llm from deep_think_llm

Revision ID: b7f2c1a4e9d3
Revises: a3c9e7d1b250
Create Date: 2026-06-08

Adds the new TradingAgents per-instance setting ``trade_recommendation_llm`` (the
model used by the final risk-management judge that issues the trade recommendation).
For backward compatibility, every existing ExpertInstance that already has a
``deep_think_llm`` setting gets a ``trade_recommendation_llm`` row seeded with the
same value, so behaviour is unchanged until the user customizes it.

This is a data-only migration (no schema change); ExpertSetting is a key/value table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7f2c1a4e9d3'
down_revision: Union[str, None] = 'a3c9e7d1b250'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    conn = op.get_bind()

    deep_rows = conn.execute(
        sa.text("SELECT instance_id, value_str FROM expertsetting WHERE key = 'deep_think_llm'")
    ).fetchall()

    for instance_id, value_str in deep_rows:
        already = conn.execute(
            sa.text(
                "SELECT 1 FROM expertsetting "
                "WHERE instance_id = :iid AND key = 'trade_recommendation_llm'"
            ),
            {"iid": instance_id},
        ).first()
        if already:
            continue

        conn.execute(
            sa.text(
                "INSERT INTO expertsetting (instance_id, key, value_str, value_json) "
                "VALUES (:iid, 'trade_recommendation_llm', :val, '{}')"
            ),
            {"iid": instance_id, "val": value_str},
        )


def downgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM expertsetting WHERE key = 'trade_recommendation_llm'")
    )
