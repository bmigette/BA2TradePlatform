"""FactorRanker ``weighting`` is a GA gene, end to end: gene -> trial -> book -> deploy.

7c270156 gave the expert setting ``weighting`` a third mode, ``rank`` (linear in rank, still
capped by ``max_virtual_equity_per_instrument_percent``, the platform-wide per-instrument cap that
replaced FactorRanker's own ``max_weight_per_name`` on 2026-09-29). This file pins that the
optimizer can SEARCH it and that the
searched value is the value that trades, in the backtest and in the deployed instance:

  1. GENE SPACE   the launcher's FactorRanker space (``_bypass_gene_space``) run through the
                  handler's ``collect_param_space(bypass=True)`` carries ``model:weighting`` as a
                  categorical whose choices ARE the expert's declared ``valid_values``, and the
                  run-level ``fixed_settings`` no longer pins a value for it.
  2. DECODE       a GA chromosome index -> ``GeneticOptimizer.decode_individual`` ->
                  ``decode_params`` -> ``_build_daily_trial_config`` -> the settings
                  ``daily_backtest_handler._build_experts`` hands the engine (the whitelist trap:
                  a knob missing from any of those rebuilds is inert while every log claims it
                  works).
  3. BACKTEST     the REAL FactorRanker through ``run_daily_backtest`` on a small hermetic
                  fixture: ``rank`` builds a different book from ``equal`` -- the top name holds
                  several times the bottom name's notional instead of the same -- with whole
                  shares (the grid's case: its ``risk_per_trade_pct`` floor keeps the protective
                  stop on) AND with fractional shares (honoured only while the stop is off).
  4. DEPLOY       the genome persisted as ``_persist_top_backtests`` persists a TOP-N row, exported
                  by ``_derive_export_payload`` (tools/export_deploy_payload.py) and written the way
                  tools/import_deploy_payload.py writes it (``save_settings`` with no type hint),
                  reads back as ``rank`` through the expert's LIVE settings resolver.

Run from the backend dir:
    python -m pytest tests/backtest/test_factorranker_weighting_gene.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

import pandas as pd
import pytest

# tests/backtest/ -> tests/ -> backend/ -> testplatform/, then the launcher beside backend/.
_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_frweight", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_frweight"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()
GENE = "model:weighting"


# ==================================================================================================
# helpers: the launcher's own space, a genome over it, the run block
# ==================================================================================================
def _space():
    """The FactorRanker gene space exactly as a FactorRanker optimize job builds it."""
    from app.services.strategy_param_space import collect_param_space

    spec = _M._EXPERT_OPT["FactorRanker"]
    expert_cfg = _M._bypass_gene_space(spec)
    model_cfg = {k: v for k, v in expert_cfg.items()
                 if not k.startswith(("screener:", "schedule:"))}
    strat = _M._build_strategy_minimal("frweight")
    return strat, collect_param_space(strat, expert_cfg=model_cfg, bypass=True)


def _genome(space, weighting: str, **pins) -> Dict[str, Any]:
    """Decoded flat params through the REAL GA decoder: the categorical is set by its chromosome
    INDEX (what the GA evolves), every other gene sits at an in-band value. ``pins`` overrides
    decoded values (only the fractional case uses it, to switch the stop off -- see there)."""
    from app.services.genetic import GeneticOptimizer

    values = {
        "model:factor_weight_momentum": 1.0,   # momentum alone ranks the fixture
        "model:factor_weight_value": 0.0,
        "model:factor_weight_quality": 0.0,
        "model:factor_weight_pead": 0.0,
        "model:top_n": 10,
        "model:max_virtual_equity_per_instrument_percent": 20.0,  # non-binding for top_n=10
        "model:risk_per_trade_pct": 10.0,
    }
    ga = GeneticOptimizer(param_ranges=space, population_size=2, n_generations=1)
    chromosome = []
    for name, cfg in space.items():
        if name == GENE:
            chromosome.append(cfg["choices"].index(weighting))
        else:
            chromosome.append(values[name])
    decoded = ga.decode_individual(chromosome)
    decoded.update(pins)
    return decoded


SYMBOLS = [f"FRW{c}" for c in "ABCDEFGHIJKL"]   # 12 names; unique so no other test's memo hits
RUN_START = datetime(2024, 3, 4)                # a Monday
RUN_END = datetime(2024, 3, 8)                 # ONE rebalance (see _book)


def _bt_block(expert_settings=None, end: datetime = RUN_END) -> Dict[str, Any]:
    """The run-level ``optimization_config['backtest']`` block (the launcher's shape)."""
    return {
        "backtest_id": "frweight", "name": "frweight-trial",
        "start_date": RUN_START.isoformat(), "end_date": end.isoformat(),
        "enabled_instruments": list(SYMBOLS),
        "experts": [{"class": "FactorRanker", "settings": dict(
            _M._expert_run_settings(_M._EXPERT_OPT["FactorRanker"], SYMBOLS),
            **(expert_settings or {}))}],
        "initial_capital": 100_000.0,
        "account_settings": {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
                             "slippage_bps": 0.0, "fill_model": "next_bar_open"},
        # momentum_12_1 needs 252 closes before the first rebalance.
        "warmup_days": 450, "seed": 7, "subtype": "daily_expert",
        "run_schedule_override": {"days": {d: d == "monday" for d in
                                           ("monday", "tuesday", "wednesday", "thursday",
                                            "friday", "saturday", "sunday")},
                                  "times": ["09:30"]},
        "execution_interval": "1d",
    }


def _trial(decoded_flat, expert_settings=None, end: datetime = RUN_END):
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    strat, _ = _space()
    decoded = decode_params(strat, decoded_flat)
    return decoded, _build_daily_trial_config(_bt_block(expert_settings, end), decoded, None,
                                              option_trade_records=False)


# ==================================================================================================
# 1. GENE SPACE
# ==================================================================================================
def test_weighting_is_a_categorical_gene_with_the_experts_own_choices():
    from ba2_experts.FactorRanker import FactorRanker
    from ba2_experts.FactorRanker.construction import WEIGHTINGS

    _, space = _space()
    declared = FactorRanker.get_settings_definitions()["weighting"]

    assert GENE in space
    assert space[GENE]["type"] == "choice"
    # EXACTLY the expert's declaration, in its order (the GA mutates the index by +/-1, so the
    # order is part of the search).
    assert space[GENE]["choices"] == declared["valid_values"] == declared["choices"]
    assert space[GENE]["choices"] == list(WEIGHTINGS)
    assert (space[GENE]["min"], space[GENE]["max"], space[GENE]["step"]) == (
        0, len(WEIGHTINGS) - 1, 1)
    # The default the gene replaces stays reachable.
    assert declared["default"] in space[GENE]["choices"]


def test_the_run_level_settings_no_longer_pin_weighting():
    """A fixed value would sit UNDER the gene (overrides win) and read as if it applied."""
    spec = _M._EXPERT_OPT["FactorRanker"]
    assert "weighting" not in spec["fixed_settings"]
    assert "weighting" not in _M._expert_run_settings(spec, SYMBOLS)


def test_the_rest_of_the_factorranker_space_is_unchanged():
    """Adding the gene appends ONE locus; every pre-existing gene keeps its spec."""
    _, space = _space()
    assert set(space) == {
        "model:factor_weight_momentum", "model:factor_weight_value",
        "model:factor_weight_quality", "model:factor_weight_pead", "model:top_n",
        "model:max_virtual_equity_per_instrument_percent", GENE, "model:risk_per_trade_pct",
    }


# ==================================================================================================
# 2. DECODE -> TRIAL -> ENGINE SETTINGS
# ==================================================================================================
@pytest.mark.parametrize("weighting", ["equal", "score", "rank"])
def test_the_decoded_gene_reaches_the_settings_the_engine_runs(weighting):
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    from ba2_experts.FactorRanker import FactorRanker

    _, space = _space()
    flat = _genome(space, weighting)
    assert flat[GENE] == weighting                     # index -> VALUE, not the index

    decoded, trial = _trial(flat)
    assert decoded["expert_overrides"]["weighting"] == weighting
    trial_settings = trial["experts"][0]["settings"]
    assert trial_settings["weighting"] == weighting
    # What _build_experts hands the engine (-> BacktestContext.settings -> _process).
    engine_settings = _expert_decision_settings(FactorRanker, trial_settings)
    assert engine_settings["weighting"] == weighting


# ==================================================================================================
# 3. BACKTEST: rank builds a different book from equal
# ==================================================================================================
def _price_rows(i: int) -> List[Dict[str, Any]]:
    """Business-day bars 2022-09-01..2024-03-29, a steady climb whose slope grows with ``i`` so
    12-1 momentum ranks FRWL first and FRWA last, and no bar ever touches a protective stop."""
    rows, d, close = [], date(2022, 9, 1), 20.0
    slope = 0.01 * (i + 1)
    while d <= date(2024, 3, 29):
        if d.weekday() < 5:
            open_ = close
            close = round(open_ + slope, 4)
            rows.append({"Date": d, "Open": open_, "High": close + 0.05,
                         "Low": open_ - 0.05, "Close": close, "Volume": 5_000_000})
        d += timedelta(days=1)
    return rows


_ROWS = {s: _price_rows(i) for i, s in enumerate(SYMBOLS)}


class _FixtureOHLCV:
    """In-memory OHLCV (no ``get_provider_name`` -> the memo serves it directly, no disk cache)."""

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d",
                       use_cache=True, max_cache_age_hours=24, lookback_days=30):
        def _d(v):
            if v is None:
                return None
            return v.date() if isinstance(v, datetime) else (
                v if isinstance(v, date) else pd.Timestamp(v).date())
        lo = _d(start_date)
        hi = _d(end_date)
        if lo is None and hi is not None:
            lo = hi - timedelta(days=int(lookback_days))
        return pd.DataFrame([r for r in _ROWS.get(symbol, [])
                             if (lo is None or r["Date"] >= lo) and (hi is None or r["Date"] <= hi)])


@contextmanager
def _hermetic(fractionable: bool):
    """Both provider seams on the fixture (as tests/backtest/fixtures/e2e_support does), and the
    backtest account's fractionability answer pinned."""
    import ba2_providers
    from ba2_common.core import TradeConditions, fractionable_store

    ohlcv = _FixtureOHLCV()
    real_get = ba2_providers.get_provider

    def fixture_get_provider(category, name="", **kwargs):
        if category == "ohlcv":
            return ohlcv
        if category == "indicators":
            return real_get("indicators", "pandas", ohlcv_provider=kwargs.get("ohlcv_provider") or ohlcv)
        raise KeyError(f"frweight fixture: no provider for {category!r}/{name!r}")

    orig_resolver = TradeConditions.get_provider_resolver()
    orig_map = fractionable_store.load_fractionable_map
    ba2_providers.get_provider = fixture_get_provider
    TradeConditions.set_provider_resolver(fixture_get_provider)
    fractionable_store.load_fractionable_map = lambda *a, **k: (
        {s: True for s in SYMBOLS} if fractionable else {})
    try:
        yield
    finally:
        ba2_providers.get_provider = real_get
        if orig_resolver is not None:
            TradeConditions.set_provider_resolver(orig_resolver)
        fractionable_store.load_fractionable_map = orig_map


def _book(weighting: str, fractional: bool) -> Dict[str, Dict[str, float]]:
    """Run the trial and return the end-of-run book {symbol: {qty, value}}."""
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    _, space = _space()
    pins = {}
    expert_settings = None
    if fractional:
        # allow_fractional_shares is honoured ONLY while the protective stop is off
        # (risk_per_trade_pct == 0, 7c270156). The grid's own band never reaches 0 (floor 0.5),
        # so this is the non-grid configuration, set outside the gene band on purpose.
        pins = {"model:risk_per_trade_pct": 0.0}
        expert_settings = {"allow_fractional_shares": True}
    flat = _genome(space, weighting, **pins)
    _, trial = _trial(flat, expert_settings)
    with _hermetic(fractionable=fractional):
        res = run_daily_backtest(trial)
    book = {p["symbol"]: {"qty": p["qty"], "value": p["qty"] * p["current_price"]}
            for p in res["open_positions"] if p["qty"]}
    assert set(book) == set(SYMBOLS[2:]), (   # top_n=10 of 12: the two weakest are never bought
        f"{weighting}/{'frac' if fractional else 'whole'}: unexpected book {sorted(book)}")
    return book


@pytest.mark.parametrize("fractional", [False, True], ids=["whole-shares", "fractional"])
def test_rank_builds_a_different_book_from_equal(fractional):
    equal = _book("equal", fractional)
    rank = _book("rank", fractional)

    best, worst = SYMBOLS[-1], SYMBOLS[2]
    # equal: every name carries ~1/10 of the book.
    assert equal[best]["value"] / equal[worst]["value"] == pytest.approx(1.0, rel=0.1)
    # rank: 10 parts for the best, 1 for the 10th -> ~10x (whole-share flooring blurs it a bit).
    ratio = rank[best]["value"] / rank[worst]["value"]
    assert 8.0 < ratio < 12.0, f"rank best/worst notional ratio {ratio:.2f}"
    # Monotone in rank.
    values = [rank[s]["value"] for s in SYMBOLS[2:]]
    assert values == sorted(values)
    # The share grid really is the one each case claims.
    qtys = [p["qty"] for p in rank.values()]
    if fractional:
        assert any(q != int(q) for q in qtys), f"fractional run held only whole shares: {qtys}"
    else:
        assert all(q == int(q) for q in qtys), f"whole-share run held a fraction: {qtys}"


# ==================================================================================================
# 4. DEPLOY: the persisted TOP-N row exports and imports as the value that ran
# ==================================================================================================
@pytest.fixture(scope="module")
def host_db(tmp_path_factory):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 -- registers every model class on Base.metadata
    from app.models.database import Base

    eng = create_engine(f"sqlite:///{tmp_path_factory.mktemp('frw') / 'host.sqlite'}",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _persisted_top_row(db, flat):
    """A Backtest row written the way ``_persist_top_backtests`` writes one: the raw genes, plus
    the run's expert settings stashed as ``expertFixedSettings``, linked to its optimization."""
    from app.models.backtest import Backtest
    from app.models.strategy import Strategy as StrategyModel
    from app.models.strategy_optimization import StrategyOptimization

    block = _bt_block()
    srow = StrategyModel(name="frweight", entry_rules=[], exit_rules=[])
    db.add(srow)
    db.commit()
    db.refresh(srow)
    opt = StrategyOptimization(strategy_id=srow.id, name="opt-frweight",
                               fitness_metric="calmar_ratio", optimization_type="genetic",
                               status="completed",
                               optimization_config={"populationSize": 2, "generations": 1,
                                                    "seed": 7, "backtest": block})
    db.add(opt)
    db.commit()
    db.refresh(opt)
    strategy_params = dict(flat)
    strategy_params["expertFixedSettings"] = dict(block["experts"][0]["settings"])
    bt = Backtest(name="TOP1-frweight", expert_name="FactorRanker", engine_type="daily_expert",
                  status="completed", start_date=RUN_START, end_date=RUN_END,
                  initial_capital=100_000.0, optimization_id=opt.id,
                  strategy_params=strategy_params)
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


@pytest.mark.parametrize("weighting", ["rank", "score", "equal"])
def test_the_deployed_instance_reads_the_weighting_the_backtest_ran(host_db, weighting):
    from app.api.backtests import _derive_export_payload
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance,
    )
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.deploy_parity import explicit_default_settings, live_settings_from_universe
    from ba2_experts.FactorRanker import FactorRanker

    _, space = _space()
    flat = _genome(space, weighting)
    _, trial = _trial(flat)
    bt = _persisted_top_row(host_db, flat)

    payload = _derive_export_payload(bt, "expert_settings", host_db)
    expert_params = payload["settings"]["expert_params"]
    assert expert_params["weighting"] == weighting == trial["experts"][0]["settings"]["weighting"]

    # tools/import_deploy_payload.py: expert_params + universe settings + explicit defaults,
    # saved with NO type hint (the declaration decides the type).
    to_write = {**expert_params, **live_settings_from_universe(payload.get("universe"))}
    to_write.update(explicit_default_settings(FactorRanker, to_write))

    wire_backtest_seams()
    with backtest_trading_db(f"frweight-deploy-{weighting}"):
        seed_account_definition(1, {"starting_cash": 100_000.0})
        expert_id = seed_expert_instance(
            account_id=1, expert_class_name="FactorRanker",
            enter_market_ruleset_id=seed_enter_long_ruleset(name=f"frw-{weighting}"),
            instance_id=1)
        FactorRanker(expert_id).save_settings({k: (v, None) for k, v in to_write.items()})

        live = FactorRanker(expert_id)                 # a fresh load, as the live process does
        assert live.settings["weighting"] == weighting
        # The resolver the live run_analysis feeds to _process.
        assert live._resolve_factor_settings()["weighting"] == weighting
