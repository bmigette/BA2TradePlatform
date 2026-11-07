"""``--option-history-source {thetadata,alphavantage}``: the Alpha Vantage option store beside ThetaData's.

Pinned: the store resolves and refuses contradictions; the legacy thetadata value adds NOTHING to any
identity (checkpoint fingerprint, job digest, discovery name, job name) while alphavantage joins all of them;
the launcher records the key; the real ParquetOptionsProvider reads an AV-built store; the downloader's runner
resumes, reports failures with their reasons and never records one as done.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from app.services.backtest import options_store as OS

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
_TOOLS = os.path.join(_REPO, "tools")
_FIXTURE = os.path.join(_REPO, "packages", "providers", "tests", "data", "av_historical_options_nvda_2024-05-31.json")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)


def _rows():
    with open(_FIXTURE, encoding="utf-8") as f:
        return json.load(f)["data"]


# ------------------------------------------------------------------ resolution
def test_alphavantage_is_a_store_with_its_own_vendor_and_tree(monkeypatch, tmp_path):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.delenv("BACKTEST_OPTIONS_PARQUET_ROOT", raising=False)
    assert OS.resolve_options_store({"options_store": "alphavantage"}) == "alphavantage"
    assert OS.STORE_VENDOR["alphavantage"] == "alphavantage"
    av_root = OS.default_options_parquet_root("alphavantage")
    assert av_root == str(tmp_path / "AlphaVantageOptionsProvider")
    assert av_root != OS.default_options_parquet_root("thetadata")          # never one directory


def test_the_source_key_selects_the_store_and_contradictions_refuse(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert OS.resolve_options_store({OS.OPTION_HISTORY_SOURCE_KEY: "alphavantage"}) == "alphavantage"
    assert OS.resolve_options_store({OS.OPTION_HISTORY_SOURCE_KEY: "thetadata"}) == "thetadata"
    assert OS.resolve_options_store({OS.OPTION_HISTORY_SOURCE_KEY: "alphavantage",
                                     "options_store": "alphavantage"}) == "alphavantage"
    with pytest.raises(ValueError, match="conflicting option history"):
        OS.resolve_options_store({OS.OPTION_HISTORY_SOURCE_KEY: "alphavantage", "options_store": "thetadata"})
    with pytest.raises(OS.UnknownOptionHistorySource):
        OS.resolve_options_store({OS.OPTION_HISTORY_SOURCE_KEY: "polygon"})


def test_a_config_without_the_key_resolves_exactly_as_before(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    assert OS.resolve_options_store({}) == "sqlite"
    assert OS.resolve_options_store({"options_store": "parquet"}) == "tastytrade"
    assert OS.resolve_options_store({"options_store": "thetadata"}) == "thetadata"


def test_the_history_floor_of_the_serving_vendor_is_the_av_stores():
    from app.services.backtest.daily_backtest_handler import backtest_options_provider, validate_options_window
    prov = backtest_options_provider({"options_store": "alphavantage"})
    assert prov == "alphavantage"
    validate_options_window("2020-01-02", True, prov)
    with pytest.raises(Exception):
        validate_options_window("2019-06-03", True, prov)


# ------------------------------------------------------------------ identity
def test_legacy_thetadata_is_out_of_every_identity_and_alphavantage_joins_it():
    from matrix_flags import job_name_with_digest, option_store_tokens, with_option_history_source_name
    from app.services.strategy_optimization_handler import checkpoint_fingerprint
    space = {"g": {"min": 0, "max": 1}}
    ga = {"populationSize": 4, "generations": 2}
    base = checkpoint_fingerprint(space, ga)
    assert checkpoint_fingerprint(space, ga, option_history_source="thetadata") == base
    assert checkpoint_fingerprint(space, ga, option_history_source=None) == base
    assert checkpoint_fingerprint(space, ga, option_history_source="alphavantage") != base
    assert OS.non_legacy_option_history_source({"options_store": "thetadata"}) is None
    assert OS.non_legacy_option_history_source({"options_store": "sqlite"}) is None
    assert OS.non_legacy_option_history_source({OS.OPTION_HISTORY_SOURCE_KEY: "alphavantage"}) == "alphavantage"

    theta = SimpleNamespace(options_store="thetadata", option_history_source="thetadata")
    av = SimpleNamespace(options_store="thetadata", option_history_source="alphavantage")
    assert option_store_tokens(theta) == ["--options-store", "thetadata"]        # argv byte-identical to before
    assert option_store_tokens(av) == ["--options-store", "alphavantage", "--option-history-source", "alphavantage"]
    cmd = ["x", "optimize", "--expert", "E"]
    assert job_name_with_digest("n", cmd + option_store_tokens(theta)) == job_name_with_digest("n", cmd + ["--options-store", "thetadata"])
    assert job_name_with_digest("n", cmd + option_store_tokens(theta)) != job_name_with_digest("n", cmd + option_store_tokens(av))
    # an explicit legacy token never moves a digest either
    assert (job_name_with_digest("n", cmd + ["--option-history-source", "thetadata"])
            == job_name_with_digest("n", cmd))
    assert with_option_history_source_name("job", theta) == "job"
    assert with_option_history_source_name("job", av) == "job-optalphavantage"
    assert with_option_history_source_name("job-optalphavantage", av) == "job-optalphavantage"
    with pytest.raises(SystemExit):
        option_store_tokens(SimpleNamespace(options_store="tastytrade", option_history_source="alphavantage"))


def test_the_option_drivers_keep_their_legacy_argv_and_names():
    import importlib.util
    for name in ("run_options_matrix", "run_options2_matrix", "run_convex_matrix"):
        spec = importlib.util.spec_from_file_location(name, os.path.join(_TOOLS, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        ap = m.build_parser()
        from matrix_flags import add_estimates_source_flag, add_option_history_source_flag
        add_estimates_source_flag(ap)
        add_option_history_source_flag(ap)
        legacy = ap.parse_args(["--estimates-source", "fmp"])
        assert legacy.option_history_source == "thetadata"        # the implicit vendor every driver already used
        assert legacy.options_store == "thetadata"


# ------------------------------------------------------------------ launcher
def test_launcher_records_the_source_and_refuses_a_contradiction(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    import ba2test_launcher as L
    block = {}
    L._apply_options_store(SimpleNamespace(options_store=None, option_history_source="alphavantage"), block)
    assert block == {"options_store": "alphavantage", "option_history_source": "alphavantage"}
    block = {}
    L._apply_options_store(SimpleNamespace(options_store="thetadata", option_history_source=None), block)
    assert block == {"options_store": "thetadata"}                 # unstated: nothing new on the block
    with pytest.raises(SystemExit):
        L._apply_options_store(SimpleNamespace(options_store="thetadata", option_history_source="alphavantage"), {})


def test_the_cli_offers_the_flag_on_optimize_batch_and_prewarm():
    import ba2test_launcher as L
    text = open(L.__file__, encoding="utf-8").read()
    assert text.count("_add_option_history_source_arg(") == 4          # def + prewarm + optimize + optimize-batch


# ------------------------------------------------------------------ the reader reads an AV store
def _build_av_store(root, rows, days):
    from ba2_providers.options.alphavantage import AvOptionStore, map_chain
    store = AvOptionStore(str(root))
    for d in days:
        df, st = map_chain("NVDA", d, [dict(r, date=d.isoformat()) for r in rows])
        store.record_day("NVDA", d, df, st)
    store.compact("NVDA")
    return store


def test_parquet_reader_serves_an_alphavantage_store(tmp_path):
    from app.services.backtest.parquet_options_provider import (
        ParquetOptionsProvider, clear_worker_parquet_options_cache)
    clear_worker_parquet_options_cache()
    days = [date(2024, 5, 30), date(2024, 5, 31)]
    _build_av_store(tmp_path, _rows(), days)
    p = ParquetOptionsProvider(str(tmp_path), spot_source=lambda u, d: 1100.0, risk_free_rate=0.05,
                               spot_scope="av-test")
    chain = p.get_chain("NVDA", days[1], expiry_min=date(2024, 6, 1), expiry_max=date(2024, 6, 30),
                        data_session=days[1], strike_min=1090, strike_max=1115)
    by = {c.symbol: c for c in chain}
    c = by["NVDA240607C01100000"]
    assert c.strike == 1100.0 and c.bid == 27.60 and c.ask == 28.80 and c.open_interest == 6164
    bar = p.get_bar("NVDA240607C01100000", days[1])
    assert bar["close"] == 27.50 and bar["open"] == 27.50 and bar["bid"] == 27.60 and bar["vendor_iv"] == 0.48316
    clear_worker_parquet_options_cache()


# ------------------------------------------------------------------ the downloader's runner
class _Budget:
    def __init__(self):
        self.n = 0

    def acquire(self):
        self.n += 1


def _runner(tmp_path, fetch, **kw):
    import download_av_option_chains as D
    from ba2_providers.options.alphavantage import AvOptionStore
    store = AvOptionStore(str(tmp_path))
    return D, store, D.Runner(store, fetch, _Budget(), workers=2, compact_every=2, max_attempts=2,
                              sleep=lambda s: None, log=lambda m: None, **kw)


def test_runner_downloads_resumes_and_reports_failures_with_reasons(tmp_path):
    from ba2_providers.options.alphavantage import AlphaVantageOptionsError, AlphaVantageThrottled
    rows = _rows()
    days = [date(2024, 5, 28), date(2024, 5, 29), date(2024, 5, 30), date(2024, 5, 31)]
    calls = []

    def fetch(sym, day):
        calls.append((sym, day))
        if day == date(2024, 5, 29):
            return []                                              # genuinely empty chain
        if day == date(2024, 5, 30):
            raise AlphaVantageOptionsError("boom for apikey=***")
        if day == date(2024, 5, 28):
            raise AlphaVantageThrottled("rate limit")
        return [dict(r, date=day.isoformat(), contractID=r["contractID"]) for r in rows]

    D, store, r = _runner(tmp_path, fetch)
    rep = r.run(["NVDA"], days)
    s = rep["symbols"]["NVDA"]
    assert (s["ok"], s["empty"]) == (1, 1) and set(s["failed"]) == {"2024-05-28", "2024-05-30"}
    assert s["failed"]["2024-05-28"].startswith("throttled") and "boom" in s["failed"]["2024-05-30"]
    assert rep["totals"]["failure_reasons"] == {"throttled": 1, "AlphaVantageOptionsError": 1}
    # failures are NOT recorded; the ok and the empty day are
    assert store.pending_days("NVDA", days) == [date(2024, 5, 28), date(2024, 5, 30)]
    # a second run asks only for what is owed (the recorded empty is not re-requested)
    calls.clear()
    r2 = _runner(tmp_path, fetch)[2]
    r2.run(["NVDA"], days)
    assert sorted(d for _s, d in calls) == [date(2024, 5, 28), date(2024, 5, 28), date(2024, 5, 30), date(2024, 5, 30)]
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    assert OptionHistoryParquetStore(str(tmp_path)).read_underlying("NVDA")["bar_date"].nunique() == 1


def test_runner_aborts_after_consecutive_failures(tmp_path):
    def fetch(sym, day):
        raise RuntimeError("down")
    D, store, r = _runner(tmp_path, fetch, abort_after=3)
    rep = r.run(["AAA", "BBB"], [date(2024, 5, d) for d in range(20, 31)])
    assert rep["aborted"] and "consecutive failures" in rep["aborted"]
    assert "BBB" not in rep["symbols"]


def test_trading_days_use_the_nyse_calendar():
    import download_av_option_chains as D
    got = D.trading_days(date(2024, 5, 24), date(2024, 5, 31))
    assert date(2024, 5, 27) not in got          # Memorial Day
    assert got == [date(2024, 5, 24), date(2024, 5, 28), date(2024, 5, 29), date(2024, 5, 30), date(2024, 5, 31)]
    assert D.latest_session(date(2024, 5, 28)) == date(2024, 5, 24)


def test_read_symbols_unions_files_and_dedupes(tmp_path):
    import download_av_option_chains as D
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("AAPL\nMSFT\n")
    b.write_text("msft\nNVDA\n")
    assert D.read_symbols([str(a), str(b)]) == ["AAPL", "MSFT", "NVDA"]


def test_the_real_universe_files_union_to_856_names():
    import download_av_option_chains as D
    syms = D.read_symbols([os.path.join(_TOOLS, "options_universe_large_cap.txt"),
                           os.path.join(_TOOLS, "options_universe_top100.txt")])
    assert len(syms) == 856


def test_prewarm_reports_av_store_coverage_without_network(monkeypatch, tmp_path, capsys):
    import ba2_common.config as cfg
    import ba2test_launcher as L
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    _build_av_store(tmp_path / "AlphaVantageOptionsProvider", _rows(), [date(2024, 5, 30)])
    L._report_option_history_coverage(
        SimpleNamespace(option_history_source="alphavantage", start="2024-05-30", end="2024-05-31"), ["NVDA", "AAPL"])
    out = capsys.readouterr().out
    assert "0/2 symbols fully recorded" in out and "2 symbol(s) incomplete" in out and "AAPL:2" in out
    L._report_option_history_coverage(SimpleNamespace(option_history_source=None, start=None, end=None), ["X"])
    assert capsys.readouterr().out == ""
