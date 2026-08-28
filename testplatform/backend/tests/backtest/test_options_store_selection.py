"""WHICH option store a run reads — the selection seam, and the fact that it defaults to sqlite.

Constraint under test: every backtest number on record was produced against the Alpaca sqlite
store, so a run that does not explicitly ask for anything else must build exactly the reader it
built before, and the vendor whose history floor is enforced must be the vendor of the store
actually being read.

Run:
    ./venv/bin/python -m pytest tests/backtest/test_options_store_selection.py -q
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.backtest.daily_backtest_handler import (
    backtest_options_provider,
    validate_options_window,
)
from app.services.backtest.options_store import (
    OPTIONS_STORES,
    PARQUET,
    SQLITE,
    STORE_VENDOR,
    build_options_provider,
    default_options_parquet_root,
    default_options_risk_free_rate,
    price_source_spot,
    resolve_options_store,
)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_default_is_sqlite(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert resolve_options_store(None) == SQLITE
    assert resolve_options_store({}) == SQLITE
    assert resolve_options_store({"options_store": None}) == SQLITE


def test_explicit_config_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    assert resolve_options_store({"options_store": "sqlite"}) == SQLITE
    assert resolve_options_store({}) == PARQUET


def test_env_selects_the_store(monkeypatch):
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "PARQUET")   # case/space tolerant
    assert resolve_options_store({}) == PARQUET


def test_unknown_store_raises_rather_than_falling_back(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    with pytest.raises(ValueError, match="Unknown options store"):
        resolve_options_store({"options_store": "sqlight"})


def test_every_store_maps_to_a_vendor_with_a_known_floor():
    from ba2_providers.options import options_history_floor

    assert set(STORE_VENDOR) == set(OPTIONS_STORES)
    for store, vendor in STORE_VENDOR.items():
        assert isinstance(options_history_floor(vendor), date)


# --------------------------------------------------------------------------- #
# Vendor / history floor follows the store
# --------------------------------------------------------------------------- #
def test_vendor_defaults_to_alpaca(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert backtest_options_provider() == "alpaca"
    assert backtest_options_provider({}) == "alpaca"


def test_vendor_is_tastytrade_for_the_parquet_store():
    assert backtest_options_provider({"options_store": "parquet"}) == "tastytrade"


def test_2023_is_refused_on_sqlite_and_allowed_on_parquet(monkeypatch):
    """THE reason this selection is not a private detail of run_daily_backtest: the Alpaca
    store holds no 2023 at all, the dxfeed one holds only 2023."""
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    with pytest.raises(ValueError, match="alpaca"):
        validate_options_window("2023-01-03", True, backtest_options_provider({}))
    validate_options_window("2023-01-03", True,
                            backtest_options_provider({"options_store": "parquet"}))


def test_parquet_floor_still_refuses_something(monkeypatch):
    """A second store must not become a way to wave any window through."""
    with pytest.raises(ValueError, match="tastytrade"):
        validate_options_window("2019-01-02", True,
                                backtest_options_provider({"options_store": "parquet"}))


# --------------------------------------------------------------------------- #
# The factory
# --------------------------------------------------------------------------- #
def _sqlite_cache(tmp_path):
    from app.services.backtest.options_cache import OptionsHistoryCache

    db = str(tmp_path / "opt.sqlite")
    OptionsHistoryCache(db)
    return db


def test_equity_only_run_builds_no_reader():
    assert build_options_provider({}, price_source=None) is None
    assert build_options_provider({"options_cache_db": None}, price_source=None) is None


def test_default_builds_the_sqlite_reader(monkeypatch, tmp_path):
    from app.services.backtest.options_provider import HistoricalOptionsProvider

    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    db = _sqlite_cache(tmp_path)
    p = build_options_provider({"options_cache_db": db}, price_source=None)
    assert isinstance(p, HistoricalOptionsProvider)
    assert p.db_path == db


def test_parquet_selection_builds_the_parquet_reader(monkeypatch, tmp_path):
    from app.services.backtest.parquet_options_provider import ParquetOptionsProvider

    root = tmp_path / "TastyTradeOptionsProvider"
    root.mkdir()

    class _PS:
        def close_asof(self, symbol, as_of):
            return 100.0

    p = build_options_provider(
        {"options_cache_db": _sqlite_cache(tmp_path), "options_store": "parquet",
         "options_parquet_root": str(root)},
        price_source=_PS())
    assert isinstance(p, ParquetOptionsProvider)
    assert p.root == str(root)
    assert p.risk_free_rate == pytest.approx(default_options_risk_free_rate())


def test_parquet_rate_is_overridable_and_defaults_to_the_cache_builders_own(monkeypatch, tmp_path):
    """The read-time inversion must assume the same rate the sqlite store's build-time
    inversion assumed, or the two backends' greeks differ for a reason nobody chose."""
    from app.services.backtest.fetch_options import _FALLBACK_RISK_FREE_RATE

    monkeypatch.delenv("BACKTEST_OPTIONS_RISK_FREE_RATE", raising=False)
    assert default_options_risk_free_rate() == pytest.approx(_FALLBACK_RISK_FREE_RATE)
    monkeypatch.setenv("BACKTEST_OPTIONS_RISK_FREE_RATE", "0.02")
    assert default_options_risk_free_rate() == pytest.approx(0.02)


def test_parquet_root_default_follows_the_writer(monkeypatch):
    from ba2_providers.options.parquet_store import PROVIDER_DIR
    import ba2_common.config as cfg

    monkeypatch.delenv("BACKTEST_OPTIONS_PARQUET_ROOT", raising=False)
    assert default_options_parquet_root().endswith(PROVIDER_DIR)
    assert default_options_parquet_root().startswith(cfg.CACHE_FOLDER)
    monkeypatch.setenv("BACKTEST_OPTIONS_PARQUET_ROOT", "/tmp/elsewhere")
    assert default_options_parquet_root() == "/tmp/elsewhere"


def test_spot_source_reads_the_price_source_as_of_that_date():
    seen = []

    class _PS:
        def close_asof(self, symbol, as_of):
            seen.append((symbol, as_of))
            return 123.5

    spot = price_source_spot(_PS())
    assert spot("AAPL", date(2023, 1, 5)) == 123.5
    assert seen == [("AAPL", date(2023, 1, 5))]


def test_spot_source_over_a_real_daily_price_source_returns_that_days_close():
    """Pins the semantics the parquet reader's greeks depend on, against the REAL
    AsOfPriceSource rather than a stub: on a daily source a bar's own date resolves to that
    bar's close (not the previous session's), a gap forward-fills the last KNOWN close, and a
    date before the first bar is None — never a future one."""
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", [
        {"Date": date(2023, 1, 3), "Open": 1, "High": 1, "Low": 1, "Close": 101.0, "Volume": 1},
        {"Date": date(2023, 1, 5), "Open": 1, "High": 1, "Low": 1, "Close": 105.0, "Volume": 1},
    ])
    spot = price_source_spot(ps)
    assert spot("AAPL", date(2023, 1, 3)) == pytest.approx(101.0)
    assert spot("AAPL", date(2023, 1, 4)) == pytest.approx(101.0)   # gap -> last KNOWN
    assert spot("AAPL", date(2023, 1, 5)) == pytest.approx(105.0)
    assert spot("AAPL", date(2023, 1, 2)) is None                   # never a future close


# --------------------------------------------------------------------------- #
# Config plumbing (single-run + optimizer trial)
# --------------------------------------------------------------------------- #
def _payload(**over):
    """A minimal, equity-only ``_build_config`` payload (no option rule -> no window check)."""
    p = {
        "backtest_id": 1, "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
        "start_date": "2024-03-01", "end_date": "2024-03-10", "initial_capital": 100000,
        "commission": 0.0, "slippage": 0.0, "fill_model": "next_bar_open", "seed": 1,
        "enabled_instruments": ["AAPL"], "warmup_days": 0,
    }
    p.update(over)
    return p


def _backtest_cfg(**over):
    """A minimal run-level optimize block for ``_build_daily_trial_config``."""
    c = {
        "backtest_id": 7, "start_date": "2024-02-01", "end_date": "2024-02-29",
        "enabled_instruments": ["AAPL"], "warmup_days": 30, "seed": 42,
        "experts": [{"class": "FMPEarningsDrift", "settings": {}}],
        "initial_capital": 100000.0, "account_settings": {"starting_cash": 100000.0},
        "options_cache_db": "/tmp/whatever.sqlite",
    }
    c.update(over)
    return c


_DECODED = {"tp": 8.0, "sl": 3.0, "expert_overrides": {}, "buy_tree": None,
            "sell_tree": None, "exit_rules": []}


def test_build_config_forwards_the_store_keys():
    """A payload key must survive into the config run_daily_backtest reads."""
    import app.services.backtest.daily_backtest_handler as H

    cfg = H._build_config(_payload(options_store="sqlite", options_parquet_root="/tmp/x",
                                   options_risk_free_rate=0.01))
    assert cfg["options_store"] == "sqlite"
    assert cfg["options_parquet_root"] == "/tmp/x"
    assert cfg["options_risk_free_rate"] == 0.01


def test_optimizer_forwards_the_store_keys_per_trial():
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    cfg = _build_daily_trial_config(
        _backtest_cfg(options_store="parquet", options_parquet_root="/tmp/root",
                      options_risk_free_rate=0.03),
        _DECODED)
    assert cfg["options_store"] == "parquet"
    assert cfg["options_parquet_root"] == "/tmp/root"
    assert cfg["options_risk_free_rate"] == 0.03


# --------------------------------------------------------------------------- #
# The config must be SELF-DESCRIBING — the store has to survive a process hop
# --------------------------------------------------------------------------- #
# A distributed trial ships {config, fitness_metric, cache_root, inmem_trades} and NOTHING else
# (worker_client.run_trial -> POST /submit-trial). No environment goes with it. So a store
# chosen via BACKTEST_OPTIONS_STORE is a decision the master made and the worker cannot see:
# the worker re-resolves, finds no env var, and falls back to sqlite — a VALID store, which is
# why the whole grid then reads Alpaca history while every log on the master says parquet.
#
# The property these tests pin is therefore not "the key is forwarded" but "the config states
# the ANSWER": resolve it once with the env set, then resolve it again from the config alone
# with the env gone, and it must still say parquet.
def _across_the_wire(config):
    """What the worker actually resolves against: the config as JSON over HTTP, nothing else."""
    import json

    return json.loads(json.dumps(config, default=str))


def test_build_config_records_the_resolved_store_not_the_raw_key(monkeypatch):
    import app.services.backtest.daily_backtest_handler as H

    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    cfg = H._build_config(_payload())               # payload says nothing; env does
    assert cfg["options_store"] == PARQUET, "the DECISION must be in the config, not None"

    on_the_worker = _across_the_wire(cfg)
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)   # the env does not travel
    assert resolve_options_store(on_the_worker) == PARQUET


def test_trial_config_records_the_resolved_store_not_the_raw_key(monkeypatch):
    """Same property on the GA path — the one that actually posts configs to remote workers."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    cfg = _build_daily_trial_config(_backtest_cfg(), _DECODED)
    assert cfg["options_store"] == PARQUET

    on_the_worker = _across_the_wire(cfg)
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert resolve_options_store(on_the_worker) == PARQUET


def test_the_worker_would_have_read_sqlite_from_the_old_verbatim_forward(monkeypatch):
    """The bug, stated as the thing that must not be true again: a config carrying the RAW key
    (None, because the store came from the env) resolves to the sqlite DEFAULT off-box — no
    error, no warning, just the other vendor's history."""
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert resolve_options_store({"options_store": None}) == SQLITE


def test_an_unflagged_run_still_says_sqlite_explicitly(monkeypatch):
    """Materialising the decision must not change WHICH store an existing run reads — only
    whether the config admits to it. ``sqlite`` is what these resolved to before."""
    import app.services.backtest.daily_backtest_handler as H
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert H._build_config(_payload())["options_store"] == SQLITE
    assert _build_daily_trial_config(_backtest_cfg(), _DECODED)["options_store"] == SQLITE


def test_a_typo_in_the_store_is_refused_at_config_build_time(monkeypatch):
    """Fail on the master, where a human is watching, rather than on N workers at once."""
    import app.services.backtest.daily_backtest_handler as H
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    with pytest.raises(ValueError, match="Unknown options store"):
        H._build_config(_payload(options_store="parqet"))
    with pytest.raises(ValueError, match="Unknown options store"):
        _build_daily_trial_config(_backtest_cfg(options_store="parqet"), _DECODED)
