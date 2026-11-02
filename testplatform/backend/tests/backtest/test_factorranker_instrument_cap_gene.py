"""FactorRanker's per-instrument cap is a GA gene, end to end: gene -> trial -> book.

2026-09-29: FactorRanker's own ``max_weight_per_name`` (fraction 0-1) was retired in favour
of the platform-wide ``max_virtual_equity_per_instrument_percent`` (percent, shared with
every other expert). This file pins that the optimizer's gene for the REPLACEMENT setting
reaches the settings the engine actually runs with, and that a real backtest book is capped
at the searched value -- 5.0 -> every held name at (at most, share-grid-rounded) 5% of
equity, 20.0 -> 20%.

  1. GENE SPACE   the launcher's FactorRanker space (``_bypass_gene_space``) run through the
                  handler's ``collect_param_space(bypass=True)`` carries
                  ``model:max_virtual_equity_per_instrument_percent`` as a 5.0-20.0/step 5.0
                  float, and NOT ``model:max_weight_per_name`` (retired).
  2. DECODE       a GA chromosome value -> ``GeneticOptimizer.decode_individual`` ->
                  ``decode_params`` -> ``_build_daily_trial_config`` -> the settings
                  ``daily_backtest_handler._build_experts`` hands the engine.
  3. BACKTEST     the REAL FactorRanker through ``run_daily_backtest`` on a small hermetic
                  fixture: with more candidates than top_n and equal weighting, every held
                  name's uncapped share would be 1/N > both 5% and 20%, so the cap binds and
                  the book's per-name notional tracks the gene.

Run from the backend dir:
    python -m pytest tests/backtest/test_factorranker_instrument_cap_gene.py -v
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
    spec = importlib.util.spec_from_file_location("lch_frcap", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_frcap"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()
GENE = "model:max_virtual_equity_per_instrument_percent"


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
    strat = _M._build_strategy_minimal("frcap")
    return strat, collect_param_space(strat, expert_cfg=model_cfg, bypass=True)


def _genome(space, cap_percent: float, **pins) -> Dict[str, Any]:
    """Decoded flat params through the REAL GA decoder: the cap gene is set by its
    chromosome VALUE (a float gene, not a categorical index), every other gene sits at an
    in-band value. ``pins`` overrides decoded values after decoding."""
    from app.services.genetic import GeneticOptimizer

    values = {
        "model:factor_weight_momentum": 1.0,   # momentum alone ranks the fixture
        "model:factor_weight_value": 0.0,
        "model:factor_weight_quality": 0.0,
        "model:factor_weight_pead": 0.0,
        "model:top_n": 10,                     # >= len(SYMBOLS): every name is held
        "model:weighting": 0,                  # index 0 -> "equal" (1/N each, uncapped)
        "model:risk_per_trade_pct": 10.0,
        GENE: cap_percent,
    }
    ga = GeneticOptimizer(param_ranges=space, population_size=2, n_generations=1)
    chromosome = [values[name] for name in space]
    decoded = ga.decode_individual(chromosome)
    decoded.update(pins)
    return decoded


SYMBOLS = [f"FRC{c}" for c in "ABCD"]           # 4 names; unique prefix, no other test's memo
RUN_START = datetime(2024, 3, 4)                # a Monday
RUN_END = datetime(2024, 3, 8)                  # ONE rebalance


def _bt_block(end: datetime = RUN_END) -> Dict[str, Any]:
    """The run-level ``optimization_config['backtest']`` block (the launcher's shape)."""
    return {
        "backtest_id": "frcap", "name": "frcap-trial",
        "start_date": RUN_START.isoformat(), "end_date": end.isoformat(),
        "enabled_instruments": list(SYMBOLS),
        "experts": [{"class": "FactorRanker", "settings": dict(
            _M._expert_run_settings(_M._EXPERT_OPT["FactorRanker"], SYMBOLS))}],
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


def _trial(decoded_flat, end: datetime = RUN_END):
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    strat, _ = _space()
    decoded = decode_params(strat, decoded_flat)
    return decoded, _build_daily_trial_config(_bt_block(end), decoded, None,
                                              option_trade_records=False)


# ==================================================================================================
# 1. GENE SPACE
# ==================================================================================================
def test_the_cap_gene_replaces_max_weight_per_name():
    from ba2_experts.FactorRanker import FactorRanker

    _, space = _space()

    assert GENE in space
    assert space[GENE] == {"type": "float", "min": 5.0, "max": 20.0, "step": 5.0}
    assert "model:max_weight_per_name" not in space
    # The class no longer declares the retired setting at all.
    assert "max_weight_per_name" not in FactorRanker.get_settings_definitions()
    assert "max_virtual_equity_per_instrument_percent" not in FactorRanker.get_settings_definitions()  # noqa: E501 -- it's a BUILTIN setting, not FactorRanker's own


def test_the_rest_of_the_factorranker_space_is_unchanged():
    """Adding this gene did not touch the neighbouring genes' specs."""
    _, space = _space()
    assert set(space) == {
        "model:factor_weight_momentum", "model:factor_weight_value",
        "model:factor_weight_quality", "model:factor_weight_pead", "model:top_n",
        GENE, "model:weighting", "model:risk_per_trade_pct",
    }


# ==================================================================================================
# 2. DECODE -> TRIAL -> ENGINE SETTINGS
# ==================================================================================================
@pytest.mark.parametrize("cap_percent", [5.0, 10.0, 20.0])
def test_the_decoded_gene_reaches_the_settings_the_engine_runs(cap_percent):
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    from ba2_experts.FactorRanker import FactorRanker

    _, space = _space()
    flat = _genome(space, cap_percent)
    assert flat[GENE] == cap_percent

    decoded, trial = _trial(flat)
    assert decoded["expert_overrides"]["max_virtual_equity_per_instrument_percent"] == cap_percent
    trial_settings = trial["experts"][0]["settings"]
    assert trial_settings["max_virtual_equity_per_instrument_percent"] == cap_percent
    # What _build_experts hands the engine (-> BacktestContext.settings -> _process).
    engine_settings = _expert_decision_settings(FactorRanker, trial_settings)
    assert engine_settings["max_virtual_equity_per_instrument_percent"] == cap_percent


# ==================================================================================================
# 3. BACKTEST: the cap binds and tracks the gene
# ==================================================================================================
def _price_rows(i: int) -> List[Dict[str, Any]]:
    """Business-day bars 2022-09-01..2024-03-29, a steady climb whose slope grows with ``i`` so
    12-1 momentum ranks FRCD first and FRCA last -- irrelevant to weighting here (equal), but
    keeps the fixture identical in spirit to the weighting-gene test."""
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
def _hermetic():
    """Both provider seams on the fixture (as tests/backtest/fixtures/e2e_support does)."""
    import ba2_providers
    from ba2_common.core import TradeConditions

    ohlcv = _FixtureOHLCV()
    real_get = ba2_providers.get_provider

    def fixture_get_provider(category, name="", **kwargs):
        if category == "ohlcv":
            return ohlcv
        if category == "indicators":
            return real_get("indicators", "pandas", ohlcv_provider=kwargs.get("ohlcv_provider") or ohlcv)
        raise KeyError(f"frcap fixture: no provider for {category!r}/{name!r}")

    orig_resolver = TradeConditions.get_provider_resolver()
    ba2_providers.get_provider = fixture_get_provider
    TradeConditions.set_provider_resolver(fixture_get_provider)
    try:
        yield
    finally:
        ba2_providers.get_provider = real_get
        if orig_resolver is not None:
            TradeConditions.set_provider_resolver(orig_resolver)


def _book(cap_percent: float) -> Dict[str, Dict[str, float]]:
    """Run the trial and return the end-of-run book {symbol: {qty, value}}."""
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    _, space = _space()
    flat = _genome(space, cap_percent)
    _, trial = _trial(flat)
    with _hermetic():
        res = run_daily_backtest(trial)
    book = {p["symbol"]: {"qty": p["qty"], "value": p["qty"] * p["current_price"]}
            for p in res["open_positions"] if p["qty"]}
    # top_n=10 >= 4 symbols: every name is picked, each wanting an uncapped 25% share.
    assert set(book) == set(SYMBOLS), (
        f"cap={cap_percent}: unexpected book {sorted(book)}")
    return book


@pytest.mark.parametrize("cap_percent", [5.0, 20.0])
def test_every_held_name_is_capped_at_the_genes_value(cap_percent):
    """Equal weighting wants 25% each (4 names); both 5% and 20% are below that, so the
    cap binds for every name and the book's per-name notional tracks the gene -- not just
    its RELATIVE shape (that's what the weighting gene proves), but its ABSOLUTE size."""
    book = _book(cap_percent)
    expected_value = 100_000.0 * (cap_percent / 100.0)
    for symbol, pos in book.items():
        # Whole-share flooring can only round DOWN from the target, and by less than one
        # share's worth (~$20-25 on this fixture) -- a tight relative tolerance still
        # catches the gene not reaching the engine (which would leave every name at the
        # OTHER cap, or at the 10% declared default, or uncapped at 25%).
        assert pos["value"] == pytest.approx(expected_value, rel=0.02), (cap_percent, book)


def test_the_5_percent_book_deploys_a_quarter_of_the_20_percent_book():
    """Cross-check independent of the absolute-value tolerance above: the SAME 4-name,
    equal-weighted, both-capped book scales linearly with the cap."""
    book_5 = _book(5.0)
    book_20 = _book(20.0)
    for symbol in SYMBOLS:
        ratio = book_20[symbol]["value"] / book_5[symbol]["value"]
        assert ratio == pytest.approx(4.0, rel=0.02), (symbol, book_5, book_20)
