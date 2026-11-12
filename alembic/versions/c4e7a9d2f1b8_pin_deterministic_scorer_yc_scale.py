"""pin yc_scale = 0.005 on every existing DeterministicScorer instance

Revision ID: c4e7a9d2f1b8
Revises: b8d1f4a29c63
Create Date: 2026-11-09

The DeterministicScorer setting definition of ``yc_scale`` changes its default from 0.005 to
0.5 (``macro.DEF_YC_SCALE``; FRED's 10y-3m spread is in PERCENT, and 0.005 saturated the tanh on
~96% of days). No existing instance stores the setting, so each one resolves the DEFINITION
default -- and would silently switch to 0.5 with the new code. The owner's decision: existing
instances keep behaving exactly as before, new instances get 0.5.

Data-only (ExpertSetting is a key/value table). For every ``expertinstance`` whose ``expert`` is
``DeterministicScorer``:

* no ``yc_scale`` row -> insert one with ``value_float = 0.005`` (a float setting lives in
  ``value_float``; ``value_json`` gets the '{}' the model's default factory writes, exactly as
  ``ExtendableSettingsInterface.save_settings`` does for a new float row);
* a ``yc_scale`` row whose ``value_float`` is NULL -> set it to 0.005 (such a row resolves to the
  definition default today, i.e. 0.005);
* a ``yc_scale`` row with a value -> untouched.

Idempotent: a second run finds nothing to do. Other experts are never touched.

APPLY IT BEFORE THE NEW CODE RUNS ANY ANALYSIS. The app does not run alembic at startup: run
``python migrate.py upgrade`` against each live DB (``BA2_DB_FILE=<db.sqlite>``) before the
instances restart on the code that carries the new default. The pinned value equals what the old
code resolves, so applying it while the old code is still running changes nothing.

Downgrade is a documented NO-OP: a pinned row cannot be told apart from one a user saved with the
same value afterwards, and leaving it is behaviour-neutral -- the code before this revision
resolves 0.005 from its own definition anyway.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e7a9d2f1b8'
down_revision: Union[str, Sequence[str], None] = 'b8d1f4a29c63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EXPERT = "DeterministicScorer"
KEY = "yc_scale"
LEGACY_VALUE = 0.005   # the definition default every existing instance has resolved until now


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not (inspector.has_table("expertinstance") and inspector.has_table("expertsetting")):
        print("[ds-yc-scale] no expertinstance/expertsetting table -- nothing to pin")
        return
    params = {"expert": EXPERT, "key": KEY, "val": LEGACY_VALUE}
    filled = conn.execute(
        sa.text(
            "UPDATE expertsetting SET value_float = :val "
            "WHERE key = :key AND value_float IS NULL "
            "AND instance_id IN (SELECT id FROM expertinstance WHERE expert = :expert)"
        ),
        params,
    ).rowcount
    inserted = conn.execute(
        sa.text(
            "INSERT INTO expertsetting (instance_id, key, value_float, value_json) "
            "SELECT ei.id, :key, :val, '{}' FROM expertinstance ei "
            "WHERE ei.expert = :expert AND NOT EXISTS ("
            "  SELECT 1 FROM expertsetting es WHERE es.instance_id = ei.id AND es.key = :key)"
        ),
        params,
    ).rowcount
    print(f"[ds-yc-scale] pinned {KEY}={LEGACY_VALUE} on {inserted} {EXPERT} instance(s) "
          f"without the setting, filled {filled} empty row(s)")


def downgrade() -> None:
    # Documented no-op (see the module docstring): the rows stay, and the pre-revision code
    # resolves the same 0.005 from its own definition.
    pass
