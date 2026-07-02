"""Task 5: fitness-metrics catalog (UI single source of truth) + endpoint.

Two things under test:

1. ``strategy_fitness.METRICS_CATALOG`` — a list of metadata dicts, ONE per
   selectable fitness metric, built so that a NEW metric added to
   ``_FITNESS_KEYS`` (or the specials) WITHOUT a matching catalog entry FAILS
   here (drift guard). The guard is exercised directly via the completeness
   helper ``assert_catalog_complete``.

2. ``GET /api/optimization/fitness-options`` — returns the catalog + the four
   cap/scale knob definitions with defaults, so the UI never hardcodes them.

Also asserts the optimize route accepts ``fitness_metric`` + the 4 knobs
(profit_cap_pct / profit_share_cap_pct / fitness_trade_scale /
fitness_trade_scale_cap): they ride inside ``optimization_config.backtest`` (a
free dict) and are threaded per-trial by the handler — this test proves the
request validates and the knobs survive onto the persisted job config.

Self-contained: a throwaway sqlite DATABASE_URL is set before any app import
(mirrors tests/test_optimize_route.py).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/test_fitness_catalog.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_fitness_catalog.db")
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

import pytest


# --------------------------------------------------------------------------- #
# 1. Catalog structure + drift guard (no app/db needed)                        #
# --------------------------------------------------------------------------- #
def test_catalog_covers_every_fitness_key_plus_specials():
    from app.services import strategy_fitness as sf

    # Catalog is keyed by CANONICAL metric name (aliases collapsed) but must ACCEPT every
    # _FITNESS_KEYS entry + max_drawdown + every consistent_annual_return alias.
    accepted = sf.catalog_accepted_metrics()
    expected = set(sf._FITNESS_KEYS) | {"max_drawdown"} | set(sf._CAR_ALIASES)
    missing = expected - accepted
    assert not missing, f"METRICS_CATALOG does not cover: {sorted(missing)}"


def test_catalog_entries_have_required_metadata():
    from app.services import strategy_fitness as sf

    required = {"key", "label", "description",
                "supports_trade_scale", "uses_adjusted_under_caps"}
    for m in sf.METRICS_CATALOG:
        assert required <= set(m), f"entry {m.get('key')} missing {required - set(m)}"
        assert isinstance(m["label"], str) and m["label"]
        assert isinstance(m["description"], str) and m["description"]
        assert isinstance(m["supports_trade_scale"], bool)
        assert isinstance(m["uses_adjusted_under_caps"], bool)


def test_completeness_helper_catches_a_hypothetical_new_metric():
    """Drift guard: a metric added to _FITNESS_KEYS without a catalog entry fails."""
    from app.services import strategy_fitness as sf

    # Real map must pass the completeness check.
    sf.assert_catalog_complete()

    # Simulate someone adding a new metric to _FITNESS_KEYS but forgetting the
    # catalog: the helper must raise.
    original = dict(sf._FITNESS_KEYS)
    try:
        sf._FITNESS_KEYS["brand_new_metric"] = "brand_new_metric"
        with pytest.raises(Exception):
            sf.assert_catalog_complete()
    finally:
        sf._FITNESS_KEYS.clear()
        sf._FITNESS_KEYS.update(original)


def test_car_entry_does_not_support_trade_scale():
    from app.services import strategy_fitness as sf

    by_key = {m["key"]: m for m in sf.METRICS_CATALOG}
    car = by_key["consistent_annual_return"]
    assert car["supports_trade_scale"] is False


def test_max_drawdown_entry_present_and_no_trade_scale():
    from app.services import strategy_fitness as sf

    by_key = {m["key"]: m for m in sf.METRICS_CATALOG}
    assert "max_drawdown" in by_key
    # max_drawdown is negated, not return-based; trade-scale doesn't apply.
    assert by_key["max_drawdown"]["supports_trade_scale"] is False


# --------------------------------------------------------------------------- #
# 2. Endpoint + optimize-route knob wiring (needs the app)                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def test_db():
    from app.models.database import engine, Base, SessionLocal

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(scope="module")
def client(test_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seed_strategy(test_db):
    from app.models import Strategy

    s = Strategy(name="fitness-catalog-test", description="strat for catalog test")
    test_db.add(s)
    test_db.commit()
    test_db.refresh(s)
    return s


def test_fitness_options_endpoint_returns_catalog_and_knobs(client):
    from app.services import strategy_fitness as sf

    r = client.get("/api/optimization/fitness-options")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "metrics" in body and isinstance(body["metrics"], list)
    accepted = set()
    for m in body["metrics"]:
        accepted.add(m["key"])
        accepted.update(m.get("aliases", []))
    expected = set(sf._FITNESS_KEYS) | {"max_drawdown"} | set(sf._CAR_ALIASES)
    assert expected <= accepted

    knobs = body["knobs"]
    assert knobs["profit_cap_pct"]["default"] == 2000
    assert knobs["profit_share_cap_pct"]["default"] == 25
    assert knobs["fitness_trade_scale"]["default"] is False
    assert knobs["fitness_trade_scale_cap"]["default"] == 100


def test_optimize_route_accepts_fitness_metric_and_knobs(client, seed_strategy, test_db):
    """The 4 knobs ride in optimization_config.backtest and survive onto the job config."""
    from app.models import StrategyOptimization

    payload = {
        "fitness_metric": "consistent_annual_return",
        "optimization_type": "genetic",
        "optimization_config": {
            "populationSize": 8,
            "generations": 3,
            "crossoverProb": 0.7,
            "mutationProb": 0.2,
            "earlyStoppingGenerations": 10,
            "elitismPercent": 10.0,
            "seed": 42,
            "backtest": {
                "engine": "daily",
                "start_date": "2020-01-01",
                "end_date": "2021-01-01",
                "initial_capital": 10000.0,
                "profit_cap_pct": 2000.0,
                "profit_share_cap_pct": 25.0,
                "fitness_trade_scale": True,
                "fitness_trade_scale_cap": 100.0,
            },
        },
    }
    r = client.post(f"/api/strategies/{seed_strategy.id}/optimize", json=payload)
    assert r.status_code == 200, r.text
    opt_id = r.json()["optimizationId"]
    assert r.json()["fitnessMetric"] == "consistent_annual_return"

    test_db.expire_all()
    row = (
        test_db.query(StrategyOptimization)
        .filter(StrategyOptimization.id == opt_id)
        .first()
    )
    assert row is not None
    assert row.fitness_metric == "consistent_annual_return"
    bt = row.optimization_config["backtest"]
    assert bt["profit_cap_pct"] == 2000.0
    assert bt["profit_share_cap_pct"] == 25.0
    assert bt["fitness_trade_scale"] is True
    assert bt["fitness_trade_scale_cap"] == 100.0
