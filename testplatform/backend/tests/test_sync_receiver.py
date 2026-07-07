"""Worker-side upsert-by-(name, created_at) logic — no HTTP involved, a temp sqlite file only."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization
from app.models.backtest import Backtest
from app.services.sync_receiver import upsert_by_natural_key


@pytest.fixture()
def session(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'worker_mirror.sqlite'}")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


def _iso(dt):
    return dt.isoformat()


def test_insert_when_absent(session):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {"id": 999, "name": "my-strategy", "created_at": _iso(created), "description": "d"}

    upsert_by_natural_key(session, Strategy, payload)

    rows = session.query(Strategy).all()
    assert len(rows) == 1
    assert rows[0].id != 999  # never trust the master's id
    assert rows[0].name == "my-strategy"
    assert rows[0].description == "d"


def test_update_in_place_when_name_and_created_at_match(session):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(created), "description": "v1"},
    )
    local_id = session.query(Strategy).one().id

    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(created), "description": "v2"},
    )

    rows = session.query(Strategy).all()
    assert len(rows) == 1  # no duplicate row
    assert rows[0].id == local_id  # same local row, updated in place
    assert rows[0].description == "v2"


def test_different_created_at_is_a_different_row(session):
    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))},
    )
    upsert_by_natural_key(
        session, Strategy,
        {"id": 1, "name": "s1", "created_at": _iso(datetime(2026, 1, 2, tzinfo=timezone.utc))},
    )
    assert session.query(Strategy).count() == 2


def test_parent_fk_resolves_to_local_id_not_master_id(session):
    strat_created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, Strategy,
        {"id": 42, "name": "parent-strat", "created_at": _iso(strat_created)},
    )
    local_strategy_id = session.query(Strategy).one().id
    assert local_strategy_id != 42  # prove the local id really differs from master's

    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, StrategyOptimization,
        {
            "id": 7, "name": "opt-1", "created_at": _iso(opt_created),
            "strategy_id": 42,  # the MASTER's id — must NOT end up on the local row verbatim
            "strategy_name": "parent-strat", "strategy_created_at": _iso(strat_created),
            "status": "running", "fitness_metric": "sharpe", "optimization_type": "genetic",
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )

    opt = session.query(StrategyOptimization).one()
    assert opt.strategy_id == local_strategy_id
    assert opt.strategy_id != 42


def test_required_parent_fk_missing_locally_skips_the_write(session):
    """StrategyOptimization.strategy_id is NOT NULL in the schema — unlike Backtest's FKs,
    which are nullable. If the referenced Strategy hasn't synced to this worker yet (the
    strategy-first push ordering is meant to prevent this, but a single push is fire-and-forget
    per worker, so a narrow race is possible), inserting with strategy_id=None would violate
    the column's constraint. The upsert must skip the write entirely rather than raise — the
    next full-state push (e.g. next generation) retries the parent first and self-heals."""
    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    result = upsert_by_natural_key(
        session, StrategyOptimization,
        {
            "id": 7, "name": "opt-1", "created_at": _iso(opt_created),
            "strategy_id": 42,
            "strategy_name": "never-synced", "strategy_created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            "status": "running", "fitness_metric": "sharpe", "optimization_type": "genetic",
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )
    assert result is None  # signals "skipped", not an error
    assert session.query(StrategyOptimization).count() == 0  # no partial/invalid row persisted


def test_nullable_parent_fk_missing_locally_resolves_to_none(session):
    """Contrast with the required-FK case above: Backtest.strategy_id IS nullable (a standalone
    backtest legitimately has no optimization/strategy row), so a missing parent resolves to
    None and the row is still written — it is not skipped."""
    bt_created = datetime(2026, 1, 3, tzinfo=timezone.utc)
    bt = upsert_by_natural_key(
        session, Backtest,
        {
            "id": 1, "name": "standalone-run", "created_at": _iso(bt_created),
            "engine_type": "daily_expert",
            "strategy_id": 42, "strategy_name": "never-synced",
            "strategy_created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            "start_date": _iso(datetime(2024, 1, 1)), "end_date": _iso(datetime(2024, 6, 1)),
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )
    assert bt is not None
    assert bt.strategy_id is None
    assert session.query(Backtest).count() == 1


def test_self_healing_sequence_skip_then_resolve_after_parent_syncs(session):
    """The design's whole self-healing story: a StrategyOptimization pushed before its
    Strategy has synced gets skipped (no row persisted); once the Strategy arrives, a re-push
    of the SAME optimization payload succeeds and the FK resolves correctly — proving a
    temporarily-unresolved required parent doesn't permanently lose the row, it just waits for
    the next full-state push."""
    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    opt_payload = {
        "id": 7, "name": "opt-1", "created_at": _iso(opt_created),
        "strategy_id": 42,
        "strategy_name": "parent-strat", "strategy_created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
        "status": "running", "fitness_metric": "sharpe", "optimization_type": "genetic",
    }
    parent_fk = {"strategy_id": (Strategy, "strategy_name", "strategy_created_at")}

    # First push: Strategy hasn't synced yet -> skipped, no row.
    # (No dict() copy needed here: upsert_by_natural_key's very first line is
    # `data = dict(payload)`, so it only ever mutates its own internal copy — the caller's
    # opt_payload dict is never touched, and reusing it below for the re-push is safe.)
    result = upsert_by_natural_key(session, StrategyOptimization, opt_payload, parent_fk=parent_fk)
    assert result is None
    assert session.query(StrategyOptimization).count() == 0

    # Strategy syncs (e.g. push_strategy succeeded on a subsequent push_optimization call).
    upsert_by_natural_key(
        session, Strategy,
        {"id": 42, "name": "parent-strat", "created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc))},
    )
    local_strategy_id = session.query(Strategy).one().id

    # Re-push the SAME optimization payload -> now resolves and persists correctly.
    opt = upsert_by_natural_key(session, StrategyOptimization, opt_payload, parent_fk=parent_fk)
    assert opt is not None
    assert opt.strategy_id == local_strategy_id
    assert session.query(StrategyOptimization).count() == 1


def test_backtest_dual_parent_fk_resolution(session):
    strat_created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upsert_by_natural_key(session, Strategy, {"id": 1, "name": "s", "created_at": _iso(strat_created)})
    local_strategy_id = session.query(Strategy).one().id

    opt_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, StrategyOptimization,
        {
            "id": 1, "name": "opt", "created_at": _iso(opt_created),
            "strategy_id": 1, "strategy_name": "s", "strategy_created_at": _iso(strat_created),
            "fitness_metric": "sharpe", "optimization_type": "genetic",
        },
        parent_fk={"strategy_id": (Strategy, "strategy_name", "strategy_created_at")},
    )
    local_opt_id = session.query(StrategyOptimization).one().id

    bt_created = datetime(2026, 1, 3, tzinfo=timezone.utc)
    upsert_by_natural_key(
        session, Backtest,
        {
            "id": 1, "name": "TOP1-opt", "created_at": _iso(bt_created),
            "engine_type": "daily_expert",
            "strategy_id": 1, "strategy_name": "s", "strategy_created_at": _iso(strat_created),
            "optimization_id": 1, "optimization_name": "opt", "optimization_created_at": _iso(opt_created),
            "start_date": _iso(datetime(2024, 1, 1)), "end_date": _iso(datetime(2024, 6, 1)),
        },
        parent_fk={
            "strategy_id": (Strategy, "strategy_name", "strategy_created_at"),
            "optimization_id": (StrategyOptimization, "optimization_name", "optimization_created_at"),
        },
    )

    bt = session.query(Backtest).one()
    assert bt.strategy_id == local_strategy_id
    assert bt.optimization_id == local_opt_id
