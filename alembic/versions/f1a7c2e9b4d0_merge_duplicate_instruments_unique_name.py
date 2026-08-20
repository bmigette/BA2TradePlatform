"""merge duplicate instrument rows and make instrument.name unique

Revision ID: f1a7c2e9b4d0
Revises: 0a3e0bd24598
Create Date: 2026-08-20 12:00:00.000000

`instrument.name` had no unique constraint and no index, and production holds
duplicate names. `add_label_to_instruments` resolves a symbol with `.first()`
while `get_labels_by_symbol` keys by name, so on those symbols a label write
lands on an arbitrary row and can be invisible to the next read. Portfolio
allocation cannot be built on that.

The merge is unusually safe: NO table has a foreign key to `instrument` (verified
by grepping for `foreign_key="instrument` and by iterating pragma_foreign_key_list
over the live schema), so rows can be merged without repointing anything.

The merge itself lives in `ba2_common.core.instrument_merge` and is imported here
through the in-tree alias shim -- exactly how alembic/env.py imports models -- so
this migration and its tests execute the SAME code. It is idempotent: the plan is
recomputed from the current table state, so re-running writes nothing.

The import is INSIDE upgrade(), not at module scope. `alembic heads` / `history` /
`branches` load every revision module but never run env.py, and env.py is what puts
packages/* on sys.path (the venv's editable installs point at a path that does not
exist here). A module-scope import of the shim therefore breaks those commands with
ModuleNotFoundError: ba2_common, for every user, forever. Deferring it costs nothing:
upgrade() only ever runs under env.py.

INDEX NAME: `ix_instrument_name`, NOT `uix_instrument_name`. `Instrument.name` is
declared `Field(unique=True, index=True)`, which makes SQLModel's create_all emit
`CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)` on a fresh database
(verified by probe). Any other name here and a migrated database would disagree
with a freshly created one forever.

The merge is NOT reversible: downgrade only drops the index.

PRODUCTION RUNBOOK
==================
For ~/Documents/ba2/trade/db.sqlite. The order is load-bearing; do not skip ahead.

0. SHIP THIS TOGETHER WITH TASK 6, NEVER BEFORE IT. Once the unique index exists,
   write paths that today insert a silent duplicate start raising IntegrityError.
   Task 6 is what normalises and guards them. Same deployment, or not at all.

1. STOP THE APP FIRST. InstrumentAutoAdder and JobManager add instruments from
   background threads. One insert landing between the merge and the CREATE UNIQUE
   INDEX re-introduces a duplicate and fails the index creation.

2. BACK UP. THIS IS THE ONLY WAY BACK. `downgrade` drops the index but CANNOT
   restore the deleted rows -- no downgrade, no undo, only this copy:

       cp ~/Documents/ba2/trade/db.sqlite ~/Documents/ba2/trade/db.sqlite.bak-YYYYMMDD

3. CATCH UP TO THIS REVISION'S PARENT FIRST, ON ITS OWN. Production was last seen
   at d5e1b9a3c842, which is behind 0a3e0bd24598. Step 4 is only read-only if
   there is nothing left in front of it to apply:

       venv/bin/python -m alembic upgrade 0a3e0bd24598

4. DRY RUN. Read-only ONLY from 0a3e0bd24598 -- from any earlier revision alembic
   commits the intervening migrations before reaching this one (observed, not
   theorised), so step 3 is not optional:

       BA2_INSTRUMENT_MERGE_DRY_RUN=1 venv/bin/python -m alembic upgrade f1a7c2e9b4d0

   EXIT CODE 1 WITH A RuntimeError TRACEBACK IS SUCCESS, NOT FAILURE. Raising is
   how the dry run refuses to write. Now read the printed group count: 124 was the
   verified figure on 2026-08-20. Materially different means the table moved under
   you -- STOP and re-check instead of proceeding.

5. REAL RUN. UNSET the variable -- do not set it to 0. Any value this flag does not
   recognise as explicitly false means DRY RUN, so an unset variable is the only
   unambiguous way to ask for the real, irreversible merge:

       unset BA2_INSTRUMENT_MERGE_DRY_RUN
       venv/bin/python -m alembic upgrade f1a7c2e9b4d0

6. VERIFY, before restarting the app:

       sqlite3 db.sqlite "SELECT count(*), count(DISTINCT name) FROM instrument;"
         -> 2353|2353   (2477 rows before; 124 deleted)
       sqlite3 db.sqlite "SELECT sql FROM sqlite_master WHERE name='ix_instrument_name';"
         -> CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)
       venv/bin/python -m alembic current
         -> f1a7c2e9b4d0

7. IF IT FAILS, JUST RE-RUN IT. The merge and the index creation share one alembic
   transaction -- verified by injecting a failure at create_index against a copy of
   the real database, where all 124 merged groups rolled back intact. The table is
   either fully merged or untouched. Do NOT hand-repair rows.
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a7c2e9b4d0'
down_revision: Union[str, Sequence[str], None] = '0a3e0bd24598'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'ix_instrument_name'

# Only these disarm the dry run. See _dry_run_requested.
_EXPLICITLY_NOT_A_DRY_RUN = ('', '0', 'false', 'no')


def _dry_run_requested() -> bool:
    """Whether BA2_INSTRUMENT_MERGE_DRY_RUN asks for a report-only run.

    FAIL-SAFE, NOT FAIL-DANGEROUS: anything that is not explicitly false means
    yes. A whitelist of truthy spellings would silently perform the real,
    irreversible deletion of ~124 production rows for an operator who typed
    ``on``, ``Y``, ``enabled`` or ``2`` and believed they had asked for a report.
    On this flag, an unrecognised value must never mean "go ahead"; the cost of
    guessing wrong in this direction is one wasted, harmless run.
    """
    raw = os.environ.get('BA2_INSTRUMENT_MERGE_DRY_RUN', '').strip().lower()
    return raw not in _EXPLICITLY_NOT_A_DRY_RUN


def _find_index(connection):
    """The reflected definition of INDEX_NAME on `instrument`, or None."""
    for index in sa.inspect(connection).get_indexes('instrument'):
        if index['name'] == INDEX_NAME:
            return index
    return None


def _is_unique_on_name(index) -> bool:
    """Whether a reflected index really is UNIQUE over exactly (name)."""
    return bool(index.get('unique')) and list(index.get('column_names') or []) == ['name']


def upgrade() -> None:
    """Upgrade schema."""
    # Deferred on purpose -- see the module docstring: `alembic heads`/`history`
    # import this file without ever running env.py, which is what makes ba2_common
    # importable.
    from ba2_trade_platform.core.instrument_merge import (
        merge_duplicate_instruments,
        report_duplicate_instruments,
    )

    connection = op.get_bind()

    if _dry_run_requested():
        # print(), not logger: alembic's fileConfig can disable the app loggers,
        # and this report is the whole point of the dry run.
        plan = report_duplicate_instruments(connection)
        print(f"[instrument-merge dry-run] {len(plan)} instrument group(s) would be rewritten")
        for group in plan:
            print(
                f"[instrument-merge dry-run] {group['name']}: keep id={group['keep_id']} "
                f"delete ids={group['delete_ids']} type={group['instrument_type']} "
                f"labels={group['labels']} categories={group['categories']}"
            )
        raise RuntimeError(
            f"BA2_INSTRUMENT_MERGE_DRY_RUN is set: reported {len(plan)} group(s) and aborted "
            "before writing anything. Unset the variable to run the merge for real."
        )

    stats = merge_duplicate_instruments(connection)
    print(
        f"[instrument-merge] merged {stats['duplicate_groups']} duplicate group(s), "
        f"deleted {stats['rows_deleted']} row(s), normalised {stats['rows_renamed']} name(s)"
    )

    # Checked by DEFINITION, not just by name. Skipping on the name alone lets an
    # index that merely happens to be called ix_instrument_name -- non-unique, or
    # over the wrong column -- satisfy the guard: the migration would report
    # success, stamp the revision, and leave uniqueness unenforced, which is the
    # single invariant every downstream task is built on. Fail loudly instead.
    existing = _find_index(connection)
    if existing is None:
        op.create_index(INDEX_NAME, 'instrument', ['name'], unique=True)
    elif not _is_unique_on_name(existing):
        raise RuntimeError(
            f"{INDEX_NAME} already exists but is not UNIQUE(name): {existing!r}. "
            "Uniqueness is NOT enforced. Drop that index and re-run this migration."
        )


def downgrade() -> None:
    """Downgrade schema. The row merge cannot be undone; only the index is dropped."""
    connection = op.get_bind()

    # Same care as upgrade(): drop only the index this revision created. Dropping
    # whatever else happens to carry the name would destroy someone else's index
    # and report success.
    existing = _find_index(connection)
    if existing is None:
        return
    if not _is_unique_on_name(existing):
        raise RuntimeError(
            f"{INDEX_NAME} exists but is not the UNIQUE(name) index this revision "
            f"created: {existing!r}. Refusing to drop an index this migration does "
            "not own; inspect it and drop it by hand if that is really what you want."
        )
    op.drop_index(INDEX_NAME, table_name='instrument')
