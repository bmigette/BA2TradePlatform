"""Manual backfill script: pushes pre-existing is_saved backtests and terminal-status
optimizations to synced workers. Run on demand, never automatically."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'backfill.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def test_backfill_pushes_saved_backtests_and_terminal_optimizations(db):
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    # fitness_metric/optimization_type are NOT NULL columns on StrategyOptimization (no
    # default) — must be supplied or the insert raises sqlalchemy.exc.IntegrityError before
    # the test ever reaches `from scripts import sync_backfill`.
    completed_opt = StrategyOptimization(
        name="done-opt", strategy_id=strat.id, status="completed",
        fitness_metric="sharpe", optimization_type="genetic", created_at=datetime.now(timezone.utc)
    )
    running_opt = StrategyOptimization(  # must NOT be pushed — not terminal
        name="still-running-opt", strategy_id=strat.id, status="running",
        fitness_metric="sharpe", optimization_type="genetic", created_at=datetime.now(timezone.utc)
    )
    db.add_all([completed_opt, running_opt])
    db.commit()

    # start_date/end_date are also NOT NULL on Backtest (no default) — same reasoning as above.
    saved_bt = Backtest(
        name="saved-run", engine_type="daily_expert", strategy_id=strat.id,
        start_date=datetime(2024, 1, 1), end_date=datetime(2024, 1, 31),
        is_saved=True, created_at=datetime.now(timezone.utc),
    )
    unsaved_bt = Backtest(  # must NOT be pushed
        name="scratch-run", engine_type="daily_expert", strategy_id=strat.id,
        start_date=datetime(2024, 1, 1), end_date=datetime(2024, 1, 31),
        is_saved=False, created_at=datetime.now(timezone.utc),
    )
    db.add_all([saved_bt, unsaved_bt])
    db.commit()

    from scripts import sync_backfill

    with patch.object(sync_backfill, "push_optimization") as mock_push_opt, \
         patch.object(sync_backfill, "push_backtest") as mock_push_bt:
        sync_backfill.run(db, dry_run=False)

        pushed_opt_names = [c.args[0].name for c in mock_push_opt.call_args_list]
        assert pushed_opt_names == ["done-opt"]

        pushed_bt_names = [c.args[0].name for c in mock_push_bt.call_args_list]
        assert pushed_bt_names == ["saved-run"]


def test_dry_run_does_not_push(db):
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()
    opt = StrategyOptimization(
        name="done-opt", strategy_id=strat.id, status="completed",
        fitness_metric="sharpe", optimization_type="genetic", created_at=datetime.now(timezone.utc),
    )
    db.add(opt)
    db.commit()

    from scripts import sync_backfill
    with patch.object(sync_backfill, "push_optimization") as mock_push_opt, \
         patch.object(sync_backfill, "push_backtest") as mock_push_bt:
        sync_backfill.run(db, dry_run=True)
        mock_push_opt.assert_not_called()
        mock_push_bt.assert_not_called()
