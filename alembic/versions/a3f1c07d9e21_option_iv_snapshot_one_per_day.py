"""one option_iv_snapshot per (account, underlying, day)

Revision ID: a3f1c07d9e21
Revises: f1c8a24b7e05
Create Date: 2026-08-24

``get_iv_rank`` reads ``option_iv_snapshot`` as an UNWEIGHTED percentile over a
252-day window, so every extra row for the same day buys that day another vote.
Nothing in the original schema prevented that, and the recorder now runs off a
scheduler. ``OptionsAccountInterface.record_atm_iv`` enforces one sample per
(account, underlying, UTC day) in Python; this index enforces the same invariant in
the database so a stray writer (a manual script, a second process racing the daily
job) cannot quietly turn a "1-year IV percentile" into a "last-few-days percentile"
underneath nine live option trading rules.

Duplicates are collapsed before the index is created, keeping the FIRST row of each
day (the one ``record_atm_iv`` would have returned). ``date()`` is deterministic on a
column argument, so SQLite accepts it in an index expression.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a3f1c07d9e21'
down_revision: Union[str, None] = 'f1c8a24b7e05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uix_option_iv_snapshot_account_underlying_day"


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        # Expression indexes are dialect-specific; the platform ships on SQLite only.
        # On any other backend the Python-side guard in record_atm_iv still applies.
        return
    # Collapse any pre-existing same-day duplicates, keeping the lowest id per day.
    op.execute(
        "DELETE FROM option_iv_snapshot WHERE id NOT IN ("
        "  SELECT MIN(id) FROM option_iv_snapshot"
        "  GROUP BY account_id, underlying, date(recorded_at))"
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
        "ON option_iv_snapshot(account_id, underlying, date(recorded_at))"
    )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
