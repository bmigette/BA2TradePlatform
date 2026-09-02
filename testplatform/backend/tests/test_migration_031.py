"""Migration 031: covering indexes for the backtests / strategy_optimizations summary queries.

WHY. In the live DB the JSON blobs (results/trades/equity_curve/drawdown_curve; ~10 MB per recent
row) sit physically BEFORE created_at and before every column added by a later migration
(expert_name, labels, ga_fitness, ...). SQLite reads a row's overflow chain sequentially, so any
query that touches a post-blob column pays ~10 MB per row visited: ORDER BY created_at over 1,035
rows measured 18 s warm / ~2 min cold and froze the API event loop. A covering index holding
exactly the summary columns lets SQLite answer those queries from the index without touching rows.

The model declares the same indexes (__table_args__) so a FRESH DB gets them from create_all; this
migration builds them on an EXISTING DB. The parity test below pins the two column lists together.
"""
import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = Path(__file__).resolve().parent.parent / "db_migrate" / "031_add_covering_summary_indexes.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_031", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_columns(cursor, index_name):
    cursor.execute(f"PRAGMA index_info({index_name})")
    return [row[2] for row in sorted(cursor.fetchall())]  # (seqno, cid, name) ordered by seqno


def _fresh_schema_db(tmp_path):
    """A sqlite file with the FULL model schema (what a fresh install gets from create_all)."""
    from sqlalchemy import create_engine
    from app.models.database import Base
    import app.models  # noqa: F401 — registers every model on Base.metadata

    path = tmp_path / "m031.sqlite"
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    eng.dispose()
    return path


def test_migration_column_lists_match_the_model_indexes():
    """The migration must build EXACTLY what the model declares, or fresh and upgraded DBs diverge."""
    from app.models.backtest import Backtest
    from app.models.strategy_optimization import StrategyOptimization

    m = _load_migration()
    declared = {ix.name: [c.name for c in ix.columns]
                for model in (Backtest, StrategyOptimization)
                for ix in model.__table__.indexes}
    for table, name, cols in m._INDEXES:
        assert name in declared, f"{name} is not declared on the {table} model"
        assert declared[name] == cols, f"{name}: migration columns != model columns"


def test_summary_index_starts_with_created_at_and_covers_the_list_columns():
    m = _load_migration()
    by_name = {name: cols for _t, name, cols in m._INDEXES}
    bt = by_name["ix_backtests_summary"]
    assert bt[0] == "created_at"
    # every column app/api/backtests.py:list_backtests selects from `b` must be covered
    for col in ("id", "name", "model_id", "prediction_dataset_id", "execution_dataset_id",
                "strategy_id", "start_date", "end_date", "initial_capital", "fitness_metric",
                "status", "total_return", "sharpe_ratio", "max_drawdown", "win_rate",
                "profit_factor", "total_trades", "winning_trades", "losing_trades",
                "avg_trade_duration", "final_equity", "best_trade", "worst_trade",
                "error_message", "is_saved", "created_at", "completed_at", "expert_name",
                "optimization_id", "engine_type", "description", "labels", "started_at"):
        assert col in bt, f"{col} missing from ix_backtests_summary"
    for blob in ("results", "trades", "equity_curve", "drawdown_curve"):
        assert blob not in bt
    so = by_name["ix_strategy_optimizations_activity"]
    assert so[0] == "created_at"
    assert "all_results" not in so


def test_upgrade_builds_the_indexes_on_an_existing_db_without_them(tmp_path):
    path = _fresh_schema_db(tmp_path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    # simulate a populated pre-031 DB: tables exist, indexes do not
    cur.execute("DROP INDEX ix_backtests_summary")
    cur.execute("DROP INDEX ix_strategy_optimizations_activity")
    conn.commit()

    m = _load_migration()
    assert m.upgrade(cur, conn) is True
    for _table, name, cols in m._INDEXES:
        assert _index_columns(cur, name) == cols
    conn.close()


def test_upgrade_is_a_noop_on_a_fresh_db_that_already_has_them(tmp_path):
    path = _fresh_schema_db(tmp_path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    m = _load_migration()
    assert m.upgrade(cur, conn) is False
    assert m.upgrade(cur, conn) is False  # idempotent
    conn.close()


def test_upgrade_skips_an_index_whose_columns_are_not_all_present_yet(tmp_path):
    """A DB whose backtests table predates the summary columns must be skipped, not half-built.

    scripts/migrate_db.py runs migrations in order, so this should not happen — but a partially
    migrated DB must not raise (that would block every later migration) and must not leave a
    truncated index behind. The strategy_optimizations index is unaffected and still gets built,
    so upgrade() must still report True.
    """
    path = _fresh_schema_db(tmp_path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("DROP INDEX ix_backtests_summary")
    cur.execute("DROP INDEX ix_strategy_optimizations_activity")
    # Replace backtests with a pre-021-style table missing almost every summary column.
    cur.execute("DROP TABLE backtests")
    cur.execute("CREATE TABLE backtests (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                "created_at DATETIME)")
    conn.commit()

    m = _load_migration()
    assert m.upgrade(cur, conn) is True  # strategy_optimizations index still built
    cur.execute("PRAGMA index_list(backtests)")
    assert "ix_backtests_summary" not in [row[1] for row in cur.fetchall()]
    by_name = {name: cols for _t, name, cols in m._INDEXES}
    assert _index_columns(cur, "ix_strategy_optimizations_activity") == \
        by_name["ix_strategy_optimizations_activity"]
    conn.close()


def test_upgrade_skips_when_the_table_is_missing(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.sqlite")
    cur = conn.cursor()
    m = _load_migration()
    assert m.upgrade(cur, conn) is False
    conn.close()
