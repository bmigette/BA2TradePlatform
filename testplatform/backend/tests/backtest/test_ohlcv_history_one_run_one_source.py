"""One run, one OHLCV history: every read of a trial resolves to the SAME source, or the run refuses.

Cross-task check of the Alpaca-history change (plan Task 5/7/8): a trial of an optimization stamped S reads its
OHLCV provider (registry key), the seam's thread-local source, the screener PANEL, the METRIC STORE and the
MARKET-CONDITION snapshot, and each of them is derived from the trial config's stamp. A derived store built from
the other history refuses at its own read; nothing falls back to a default that happens to agree.

Fakes only: ``get_provider`` is recorded, the panel / store / snapshot manifests are written (or faked) with the
stamp under test.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from ba2_common.core.market_condition_source import source_profile_for_history
from ba2_common.core.ohlcv_history import (
    OHLCV_HISTORY_SOURCE_KEY, history_registry_key, resolve_job_history_source)
from ba2_common.core.ohlcv_history_stamp import HistorySourceMismatch

SOURCES = ("fmp", "alpaca")
OTHER = {"fmp": "alpaca", "alpaca": "fmp"}


def _trial_config(source):
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    block = {
        "backtest_id": "e2e", "name": "e2e-trial",
        "start_date": datetime(2024, 3, 4).isoformat(), "end_date": datetime(2024, 3, 8).isoformat(),
        "enabled_instruments": ["AAA", "BBB"],
        "experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
        "initial_capital": 100_000.0,
        "account_settings": {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
                             "slippage_bps": 0.0, "fill_model": "next_bar_open"},
        "warmup_days": 30, "seed": 7, "subtype": "daily_expert", "execution_interval": "1d",
        OHLCV_HISTORY_SOURCE_KEY: source,
    }
    decoded = decode_params(Strategy(name="e2e", entry_rules=[], exit_rules=[]), {"model:top_n": 5})
    return _build_daily_trial_config(block, decoded, None, option_trade_records=False)


def _store(tmp_path, built_from):
    d = tmp_path / f"store_{built_from}"
    d.mkdir()
    (d / "build_manifest.json").write_text(json.dumps({"builds": [{OHLCV_HISTORY_SOURCE_KEY: built_from}]}))
    return str(d)


class _FakeMapped:
    """Stands in for ``MappedMarketConditionReader``: only the manifest matters to the source check."""

    def __init__(self, root, digest, profile, memo_size=0):
        self.manifest = {"source_profile": _FakeMapped.built_profile}
        self.manifest_digest = digest
        self.calc_version = "x"
        self.profile = profile
        self.fields = ()


def _resolve_everything(tmp_path, monkeypatch, trial, *, store_from, panel_from, snapshot_from):
    """Resolve every read of ``trial`` the way a run does; return the sources each one used."""
    import ba2_providers
    from ba2_common.core import market_condition_reader as mcr
    from app.services.backtest import screener_gate as sg
    from app.services.backtest import seam_wiring as sw
    from app.services.backtest.history_source import job_history_provider, load_job_store
    from app.services.backtest.market_condition_bt import BacktestMarketConditionReader
    from ba2_common.core import TradeConditions
    from ba2_providers.screener import live_sim as ls
    from ba2_providers.screener import metric_store as ms

    job_source = resolve_job_history_source(trial)
    asked = []
    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda category, name, **kw: asked.append((category, name)) or object())

    # 1) the raw OHLCV provider of the run, 2) the seam's thread-local source (what an expert's bundle gets)
    job_history_provider(trial)
    original = TradeConditions.get_provider_resolver()
    try:
        sw._wire_provider_resolver()
        sw.set_backtest_ohlcv_override(None)
        sw.set_backtest_history_source(trial[OHLCV_HISTORY_SOURCE_KEY])
        TradeConditions._get_provider("ohlcv", "fmp")
        sw.make_indicator_provider()
    finally:
        sw.set_backtest_history_source(None)
        if original is not None:
            TradeConditions.set_provider_resolver(original)

    # 3) the panel
    sg.clear_panel_memo()
    monkeypatch.setattr(ls, "load_panel", lambda path: SimpleNamespace(
        manifest={OHLCV_HISTORY_SOURCE_KEY: panel_from, "panel_fingerprint": "fp"}))
    sg.get_panel("panel-dir", "fp", history_source=job_source)

    # 4) the metric store
    monkeypatch.setattr(ms, "load_store", lambda d: "STORE")
    assert load_job_store(_store(tmp_path, store_from), trial) == "STORE"

    # 5) the market-condition snapshot
    _FakeMapped.built_profile = source_profile_for_history(snapshot_from)
    monkeypatch.setattr(mcr, "MappedMarketConditionReader", _FakeMapped)
    reader = BacktestMarketConditionReader(SimpleNamespace(is_intraday=False), "ohlcv-v1", manifest_digest="d",
                                           source_profile=source_profile_for_history(job_source))
    return asked, reader.source_profile


@pytest.mark.parametrize("source", SOURCES)
def test_every_read_of_a_trial_resolves_to_the_jobs_source(tmp_path, monkeypatch, source):
    trial = _trial_config(source)
    assert trial[OHLCV_HISTORY_SOURCE_KEY] == source          # the whitelist kept the stamp
    asked, profile = _resolve_everything(tmp_path, monkeypatch, trial, store_from=source, panel_from=source,
                                         snapshot_from=source)
    keys = {name for category, name in asked if category == "ohlcv"}
    assert keys == {history_registry_key(source)}              # provider, seam resolver, indicator calc: one key
    assert profile == source_profile_for_history(source)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("which", ["store_from", "panel_from", "snapshot_from"])
def test_a_derived_store_of_the_other_history_refuses(tmp_path, monkeypatch, source, which):
    from app.services.backtest.screener_gate import ScreenerGateRefusal

    built = {"store_from": source, "panel_from": source, "snapshot_from": source}
    built[which] = OTHER[source]
    with pytest.raises((HistorySourceMismatch, ScreenerGateRefusal)):
        _resolve_everything(tmp_path, monkeypatch, _trial_config(source), **built)


def test_an_unstamped_store_is_fmp_so_an_alpaca_job_refuses_it(tmp_path, monkeypatch):
    from app.services.backtest.history_source import load_job_store

    d = tmp_path / "legacy"
    d.mkdir()
    (d / "build_manifest.json").write_text(json.dumps({"builds": [{}]}))
    with pytest.raises(HistorySourceMismatch):
        load_job_store(str(d), {OHLCV_HISTORY_SOURCE_KEY: "alpaca"})
    import ba2_providers.screener.metric_store as ms
    monkeypatch.setattr(ms, "load_store", lambda s: "STORE")
    assert load_job_store(str(d), {}) == "STORE"               # an unstamped job is fmp as well


# ------------------------------------------------ NEW jobs created through the API are stamped


def test_new_job_source_rule():
    from app.services.backtest.history_source import OptionHistorySourceUnsupported, new_job_history_source
    from ba2_common.core.ohlcv_history import UnknownHistorySource

    assert new_job_history_source(None, uses_options=False, where="t") == "alpaca"
    assert new_job_history_source(None, uses_options=True, where="t") == "fmp"
    assert new_job_history_source("fmp", uses_options=False, where="t") == "fmp"
    with pytest.raises(OptionHistorySourceUnsupported):
        new_job_history_source("alpaca", uses_options=True, where="t")
    with pytest.raises(UnknownHistorySource):
        new_job_history_source("yahoo", uses_options=False, where="t")


def test_api_stamps_new_backtests_and_refuses_bad_ones():
    from fastapi import HTTPException
    from app.api.backtests import _new_job_history_source

    assert _new_job_history_source(None, {}, "t") == "alpaca"
    assert _new_job_history_source("fmp", {}, "t") == "fmp"
    option_probe = {"exit_rules": [{"action_type": "buy_call"}]}
    assert _new_job_history_source(None, option_probe, "t") == "fmp"
    for stated, probe in (("yahoo", {}), ("alpaca", option_probe)):
        with pytest.raises(HTTPException) as e:
            _new_job_history_source(stated, probe, "t")
        assert e.value.status_code == 400


def test_api_stamps_new_optimizations(monkeypatch):
    from fastapi import HTTPException
    from app.api import strategies as S

    class _Q:
        def __init__(self, strategy): self.s = strategy
        def filter(self, *a): return self
        def first(self): return self.s

    class _Db:
        def __init__(self, strategy): self.s = strategy
        def query(self, *a): return _Q(self.s)

    equity = SimpleNamespace(exit_rules=[], entry_rules=[])
    option = SimpleNamespace(exit_rules=[{"action_type": "buy_call"}], entry_rules=[])
    cfg = {"backtest": {"experts": []}}
    out = S._stamp_new_optimization_source(cfg, 1, _Db(equity))
    assert out["backtest"][OHLCV_HISTORY_SOURCE_KEY] == "alpaca" and OHLCV_HISTORY_SOURCE_KEY not in cfg["backtest"]
    assert S._stamp_new_optimization_source(cfg, 1, _Db(option))["backtest"][OHLCV_HISTORY_SOURCE_KEY] == "fmp"
    explicit = {"backtest": {OHLCV_HISTORY_SOURCE_KEY: "fmp"}}
    assert S._stamp_new_optimization_source(explicit, 1, _Db(equity))["backtest"][OHLCV_HISTORY_SOURCE_KEY] == "fmp"
    for bad, strategy in (("yahoo", equity), ("alpaca", option)):
        with pytest.raises(HTTPException):
            S._stamp_new_optimization_source({"backtest": {OHLCV_HISTORY_SOURCE_KEY: bad}}, 1, _Db(strategy))
    assert S._stamp_new_optimization_source({}, 1, _Db(equity)) == {}


def test_manifest_of_the_other_source_profile_refuses_with_a_mapped_reader(monkeypatch):
    from ba2_common.core import market_condition_reader as mcr
    from app.services.backtest.market_condition_bt import BacktestMarketConditionReader

    _FakeMapped.built_profile = source_profile_for_history("fmp")
    monkeypatch.setattr(mcr, "MappedMarketConditionReader", _FakeMapped)
    with pytest.raises(HistorySourceMismatch, match="source profile"):
        BacktestMarketConditionReader(SimpleNamespace(is_intraday=False), "ohlcv-v1", manifest_digest="d",
                                      source_profile=source_profile_for_history("alpaca"))


# ------------------------------------------------ an option optimization on alpaca fails at job start


def test_option_optimization_stamped_alpaca_fails_before_any_trial():
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.strategy_optimization_handler import REQUIRED_GA_KEYS, handle_strategy_optimization

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        strategy = Strategy(name="opt-alpaca", entry_rules=[], exit_rules=[{"action_type": "buy_call"}])
        db.add(strategy)
        db.commit()
        cfg = {k: 1 for k in REQUIRED_GA_KEYS}
        cfg["backtest"] = {OHLCV_HISTORY_SOURCE_KEY: "alpaca", "start_date": "2024-03-04"}
        row = StrategyOptimization(strategy_id=strategy.id, name="x", fitness_metric="sharpe",
                                   optimization_type="genetic", optimization_config=cfg, status="pending")
        db.add(row)
        db.commit()
        opt_id = row.id
    finally:
        db.close()
    handle_strategy_optimization("t", {"optimization_id": opt_id})
    db = SessionLocal()
    try:
        row = db.get(StrategyOptimization, opt_id)
        assert row.status == "failed" and "option job cannot run on OHLCV history source 'alpaca'" in row.error_message
    finally:
        db.close()
