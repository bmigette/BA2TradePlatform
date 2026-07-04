"""
Migration 026: replace the dead `Strategy.initial_tp_*`/`initial_sl_*` columns with a
single `entry_actions` JSON column.

Entry TP/SL is now expressed as RULES, not bespoke fields: `entry_actions` carries a list of
rule dicts in the EXACT SAME SHAPE `exit_conditions` rows already use ({id, action_type,
reference_value, action_value, action_value_optimize, action_value_min, action_value_max,
action_value_step, enabled, toggle_optimize}), minus a nested `conditions` sub-tree (an entry
action fires on the SAME gate as the buy/sell action — it has no condition of its own). The
seeder (`default_rulesets.seed_ruleset_from_tree`) converts each rule via the SAME shared
`action_from_rule` conversion exit rules already go through, so an entry-time TP/SL bracket is
just another rule, not a parallel mechanism. See
docs/plans/2026-07-03-entry-tp-sl-bracket-actions.md (REVISION 2026-07-04) for the full
rationale — the user rejected the earlier `initial_tp_percent`/`initial_sl_percent`/
`initial_tp_reference` dedicated-field design as leftover/confusing (the "5.0/2.0 dead
defaults" load-bearing on nothing since `_apply_initial_brackets` was removed).

sqlite cannot reliably DROP COLUMN/ADD COLUMN together across versions, so we rebuild the
table — following migration 022's exact pattern. The rebuild is SCHEMA-PRESERVING: we recreate
`strategies_new` with explicit DDL mirroring the current SQLAlchemy `Strategy` model — keeping
`id INTEGER PRIMARY KEY AUTOINCREMENT`, `name VARCHAR(255) NOT NULL`, the JSON columns, the NEW
`entry_actions JSON` column, and the timestamps — then copy the kept columns' data over, drop
the old table, rename the new one into place, and recreate the model's index(es).

Idempotent: a no-op (returns False) when there are no `initial_tp_*`/`initial_sl_*` columns AND
`entry_actions` already exists, so it is safe on a FRESH DB (SQLAlchemy create_all already
builds `strategies` with `entry_actions` and without the dead columns) and on a re-run.
Follows the 017-025 house pattern: `upgrade(cursor, conn)` returning truthy when changes were
applied.
"""

# Explicit DDL for the rebuilt table, mirroring app/models/strategy.py.
# Keeping this in sync with the model is what preserves the PK/AUTOINCREMENT,
# NOT NULL constraints, and column defaults that a CTAS would silently drop.
STRATEGIES_DDL = """
CREATE TABLE strategies_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    required_fields JSON,
    entry_conditions JSON,
    buy_entry_conditions JSON,
    sell_entry_conditions JSON,
    exit_conditions JSON,
    entry_actions JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
)
"""

# Indexes declared on the model (Strategy.id has index=True).
STRATEGIES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_strategies_id ON strategies (id)",
]


def get_table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [col[1] for col in cursor.fetchall()]


def _table_exists(cursor, name):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Rebuild `strategies` dropping `initial_tp_*`/`initial_sl_*` and adding `entry_actions`.

    Schema-preserving: the new table is created from explicit DDL mirroring the ORM model, so
    the PRIMARY KEY/AUTOINCREMENT, NOT NULL, and defaults survive.
    """
    if not _table_exists(cursor, "strategies"):
        print("  - strategies table does not exist yet; nothing to migrate")
        return False

    columns = get_table_columns(cursor, "strategies")
    dead_columns = [
        c for c in columns if c.startswith("initial_tp_") or c.startswith("initial_sl_")
    ]
    has_entry_actions = "entry_actions" in columns
    if not dead_columns and has_entry_actions:
        print("  - no initial_tp_*/initial_sl_* columns and entry_actions already present; "
              "nothing to migrate")
        return False

    kept = [c for c in columns if c not in dead_columns]
    kept_csv = ", ".join(kept)
    action_desc = f"dropping {len(dead_columns)} initial_tp_*/initial_sl_* columns"
    if not has_entry_actions:
        action_desc += " + adding entry_actions"
    print(f"  - {action_desc} on strategies")

    # SQLite-safe, schema-preserving table rebuild:
    #   1. create strategies_new with the real ORM schema (PK/AUTOINCREMENT etc.)
    #   2. copy the kept columns' data (incl. id) into it (entry_actions defaults to NULL if it
    #      didn't already exist on the old table)
    #   3. drop the old table and rename the new one into place
    #   4. recreate the model's index(es)
    cursor.execute("DROP TABLE IF EXISTS strategies_new")
    cursor.execute(STRATEGIES_DDL)
    cursor.execute(
        f"INSERT INTO strategies_new ({kept_csv}) "
        f"SELECT {kept_csv} FROM strategies"
    )
    cursor.execute("DROP TABLE strategies")
    cursor.execute("ALTER TABLE strategies_new RENAME TO strategies")
    for index_sql in STRATEGIES_INDEXES:
        cursor.execute(index_sql)

    conn.commit()
    print(f"  - rebuilt strategies with {len(kept) + (0 if has_entry_actions else 1)} columns "
          f"(PK/AUTOINCREMENT preserved)")
    return True


def downgrade(cursor, conn):
    """SQLite has no simple DROP COLUMN; downgrade is a no-op (data is gone)."""
    print("  - Downgrade not supported for this migration")
    return False
