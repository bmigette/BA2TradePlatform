"""
Migration 032: add the `strategy_optimization_populations` table (walk-forward chain seeding).

A completed GA job stores its FINAL evaluated population (genes + fitnesses + the gene-space
signature they were bred in) here so the next walk-forward fold can start from it. It is a table of
its own, NOT a key of strategy_optimizations.optimization_config: that column is read by the list
endpoint, the clone paths and the re-run handler, and a population is ~100-300 KB.

`StrategyOptimizationPopulation` is a SQLAlchemy model, so a FRESH DB gets the table from
`Base.metadata.create_all` and this migration is a no-op there; an EXISTING DB gets it from here.
No existing table or row is touched. Idempotent. House pattern: 025.
"""

_DDL = """
CREATE TABLE IF NOT EXISTS strategy_optimization_populations (
    optimization_id INTEGER PRIMARY KEY REFERENCES strategy_optimizations(id),
    gene_space_signature JSON NOT NULL,
    population JSON NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _table_exists(cursor, name):
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cursor.fetchone() is not None


def upgrade(cursor, conn):
    """Create the table if absent. Returns True when something was created."""
    if _table_exists(cursor, "strategy_optimization_populations"):
        print("  - strategy_optimization_populations already exists; nothing to do")
        return False
    cursor.executescript(_DDL)
    conn.commit()
    print("  - Created strategy_optimization_populations")
    return True


def downgrade(cursor, conn):
    if not _table_exists(cursor, "strategy_optimization_populations"):
        return False
    cursor.execute("DROP TABLE strategy_optimization_populations")
    conn.commit()
    return True
