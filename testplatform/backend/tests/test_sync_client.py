"""Master-side push client — mocks httpx, no real network or worker process involved."""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models import Worker
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest
from app.services import sync_client


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'master.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def _seed_worker(db, sync_enabled=True, is_enabled=True):
    w = Worker(
        name="w1", url="http://w1:8100", password="secret", worker_type="remote",
        is_local=False, is_enabled=is_enabled, sync_results_enabled=sync_enabled,
    )
    db.add(w)
    db.commit()
    return w


def test_push_strategy_posts_to_every_enabled_sync_worker(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value.raise_for_status.return_value = None

        sync_client.push_strategy(strat, db)

        assert mock_client.post.call_count == 1
        args, kwargs = mock_client.post.call_args
        assert args[0] == "http://w1:8100/sync/strategy"
        assert kwargs["json"]["name"] == "s1"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_push_skips_workers_with_sync_disabled(db):
    _seed_worker(db, sync_enabled=False)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        sync_client.push_strategy(strat, db)
        mock_client_cls.assert_not_called()


def test_push_optimization_syncs_parent_strategy_first(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()
    opt = StrategyOptimization(
        name="opt1", strategy_id=strat.id, created_at=datetime.now(timezone.utc),
        status="running", fitness_metric="sharpe", optimization_type="genetic",
    )
    db.add(opt)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value.raise_for_status.return_value = None

        sync_client.push_optimization(opt, db)

        paths = [c.args[0] for c in mock_client.post.call_args_list]
        assert paths == ["http://w1:8100/sync/strategy", "http://w1:8100/sync/optimization"]
        opt_call = mock_client.post.call_args_list[1]
        assert opt_call.kwargs["json"]["strategy_name"] == "s1"


def test_push_backtest_syncs_both_parents(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()
    opt = StrategyOptimization(
        name="opt1", strategy_id=strat.id, created_at=datetime.now(timezone.utc), status="completed",
        fitness_metric="sharpe", optimization_type="genetic",
    )
    db.add(opt)
    db.commit()
    bt = Backtest(
        name="TOP1-opt1", engine_type="daily_expert", strategy_id=strat.id, optimization_id=opt.id,
        created_at=datetime.now(timezone.utc),
        start_date=datetime.now(timezone.utc), end_date=datetime.now(timezone.utc),
    )
    db.add(bt)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value.raise_for_status.return_value = None

        sync_client.push_backtest(bt, db)

        paths = [c.args[0] for c in mock_client.post.call_args_list]
        assert paths == [
            "http://w1:8100/sync/strategy",
            "http://w1:8100/sync/optimization",
            "http://w1:8100/sync/backtest",
        ]
        bt_call = mock_client.post.call_args_list[-1]
        assert bt_call.kwargs["json"]["strategy_name"] == "s1"
        assert bt_call.kwargs["json"]["optimization_name"] == "opt1"


def test_push_failure_is_logged_and_swallowed_not_raised(db):
    _seed_worker(db)
    strat = Strategy(name="s1", created_at=datetime.now(timezone.utc))
    db.add(strat)
    db.commit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.side_effect = ConnectionError("offline")
        sync_client.push_strategy(strat, db)  # must not raise
