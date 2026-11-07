"""The analyst-estimates source (fmp | alphavantage) reaches every backtest switch point.

A backtest must NAME its estimates source (no default): the trial config, the engine's provider bundle, the
launcher, the single-run CLI, the API, the checkpoint fingerprint, the job name/digest and prewarm.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from ba2_common.core.estimates_source import (
    ESTIMATES_SOURCE_KEY, EstimatesSourceMissing, UnknownEstimatesSource, resolve_job_estimates_source,
)

pytestmark = pytest.mark.strict_estimates_source      # the refusals are the point here

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TOOLS = os.path.normpath(os.path.join(_BACKEND, "..", "..", "tools"))


def _block(**extra):
    from datetime import datetime
    block = {
        "backtest_id": "es", "name": "es-trial",
        "start_date": datetime(2024, 3, 4).isoformat(), "end_date": datetime(2024, 3, 8).isoformat(),
        "enabled_instruments": ["AAA", "BBB"],
        "experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
        "initial_capital": 100_000.0,
        "account_settings": {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
                             "slippage_bps": 0.0, "fill_model": "next_bar_open"},
        "warmup_days": 30, "seed": 7, "subtype": "daily_expert", "execution_interval": "1d",
    }
    block.update(extra)
    return block


def _trial(block):
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    decoded = decode_params(Strategy(name="es", entry_rules=[], exit_rules=[]), {"model:top_n": 5})
    return _build_daily_trial_config(block, decoded, None, option_trade_records=False)


@pytest.mark.parametrize("source", ["fmp", "alphavantage"])
def test_stamp_survives_the_trial_config_whitelist(source):
    assert _trial(_block(**{ESTIMATES_SOURCE_KEY: source}))[ESTIMATES_SOURCE_KEY] == source


def test_a_trial_without_the_stamp_refuses_instead_of_defaulting():
    with pytest.raises(EstimatesSourceMissing):
        _trial(_block())


def test_unknown_stamp_refuses():
    with pytest.raises(UnknownEstimatesSource):
        _trial(_block(**{ESTIMATES_SOURCE_KEY: "yahoo"}))


def test_engine_bundle_is_built_from_the_runs_source():
    from app.services.backtest.daily_engine import _BacktestProviderBundle

    made = []

    def get_provider(category, name, **kw):
        made.append((category, name))
        return SimpleNamespace()

    bundle = _BacktestProviderBundle(get_provider, SimpleNamespace(is_intraday=False), "alphavantage")
    assert bundle.fundamentals_details().estimates_source == "alphavantage"
    assert ("estimates", "alphavantage") in made and ("fundamentals_details", "fmp") in made
    with pytest.raises(TypeError):                  # no default: the source is a required argument
        _BacktestProviderBundle(get_provider, SimpleNamespace(is_intraday=False))


def test_run_config_resolution_refuses_a_missing_source():
    with pytest.raises(EstimatesSourceMissing):
        resolve_job_estimates_source({"ohlcv_history_source": "alpaca"})


# ---------------------------------------------------------------- launcher / CLI / API


@pytest.fixture(scope="module")
def launcher():
    import ba2test_launcher
    return ba2test_launcher


def test_launcher_refuses_a_job_that_does_not_state_the_source(launcher):
    with pytest.raises(SystemExit) as exc:
        launcher._resolve_estimates_source(SimpleNamespace(estimates_source=None), "optimize")
    assert "--estimates-source" in str(exc.value) and "no default" in str(exc.value).lower()
    assert launcher._resolve_estimates_source(SimpleNamespace(estimates_source="fmp"), "optimize") == "fmp"


@pytest.mark.parametrize("command", ["optimize", "optimize-batch", "prewarm"])
def test_every_job_creating_subcommand_has_the_flag(launcher, capsys, command, _seed_backtest_credentials):
    from ba2_common.core import db as common_db

    cwd = os.getcwd()
    try:
        with pytest.raises(SystemExit):
            launcher.main([command, "--help"])
    finally:
        os.chdir(cwd)
        common_db.configure_db(str(_seed_backtest_credentials))
    assert "--estimates-source" in capsys.readouterr().out


def test_single_backtest_cli_requires_and_stamps_the_source():
    from scripts.run_daily_backtest import _build_real_config, _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--start", "2024-03-04", "--end", "2024-03-08"])
    args = _parse_args(["--start", "2024-03-04", "--end", "2024-03-08", "--estimates-source", "alphavantage"])
    assert _build_real_config(args)[ESTIMATES_SOURCE_KEY] == "alphavantage"


def test_api_create_refuses_a_missing_source():
    from fastapi import HTTPException
    from app.api.backtests import _new_job_estimates_source

    assert _new_job_estimates_source("alphavantage", "x") == "alphavantage"
    with pytest.raises(HTTPException) as exc:
        _new_job_estimates_source(None, "POST /backtests")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------- job identity


def test_job_name_digest_and_fingerprint_differ_between_the_sources():
    if _TOOLS not in sys.path:
        sys.path.insert(0, _TOOLS)
    from matrix_flags import estimates_source_tokens, job_name_with_digest, with_estimates_source_name
    from app.services.strategy_optimization_handler import checkpoint_fingerprint

    assert with_estimates_source_name("job", "fmp") == "job"
    assert with_estimates_source_name("job", "alphavantage") == "job-estalphavantage"
    cmd = ["python", "optimize", "--expert", "X"]
    assert (job_name_with_digest("n", cmd + estimates_source_tokens("fmp"))
            != job_name_with_digest("n", cmd + estimates_source_tokens("alphavantage")))
    space = {"a": {"optimize": True, "min": 0, "max": 1}}
    ga = {"populationSize": 4, "generations": 2}
    base = checkpoint_fingerprint(space, ga)
    assert checkpoint_fingerprint(space, ga, estimates_source="fmp") != base          # stated -> part of identity
    assert (checkpoint_fingerprint(space, ga, estimates_source="fmp")
            != checkpoint_fingerprint(space, ga, estimates_source="alphavantage"))


# ---------------------------------------------------------------- prewarm


def _fetchers(source):
    from datetime import datetime
    from app.services.prewarm_fetchers import PrewarmFetchers

    f = PrewarmFetchers(fmp_key="k", end_date=datetime(2024, 3, 8), finnhub_key=None, estimates_source=source)
    warmed = []
    f._warm = lambda namespace, sym, lookback_days=None: warmed.append((namespace, sym))
    return f, warmed


def test_prewarm_warms_the_av_namespace_only_for_an_alphavantage_run():
    f, warmed = _fetchers("alphavantage")
    f.do_earnings_drift("AAPL")
    assert ("av_eps_consensus", "AAPL") in warmed and ("past_earnings_quarterly", "AAPL") in warmed
    f, warmed = _fetchers("fmp")
    f.do_earnings_drift("AAPL")
    assert not any(ns == "av_eps_consensus" for ns, _ in warmed)


def test_prewarm_requires_the_source():
    from datetime import datetime
    from app.services.prewarm_fetchers import PrewarmFetchers

    with pytest.raises(TypeError):
        PrewarmFetchers(fmp_key="k", end_date=datetime(2024, 3, 8), finnhub_key=None)
    with pytest.raises(UnknownEstimatesSource):
        PrewarmFetchers(fmp_key="k", end_date=datetime(2024, 3, 8), finnhub_key=None, estimates_source="x")


def test_warm_table_fetches_the_payload_and_the_split_calendar(monkeypatch):
    from datetime import datetime
    from ba2_experts import warm_fetchers as wf
    import ba2_providers.fundamentals.estimates.AlphaVantageEstimatesProvider  # noqa: F401
    av = sys.modules["ba2_providers.fundamentals.estimates.AlphaVantageEstimatesProvider"]

    seen = []
    monkeypatch.setattr(av, "load_av_consensus_payload", lambda s: seen.append(("payload", s)) or {"ok": 1})
    monkeypatch.setattr(av, "load_split_calendar", lambda s: seen.append(("splits", s)) or [])
    out = wf.NamespaceFetchers().fetch(wf.NamespaceRequest(
        namespace=wf.AV_CONSENSUS_NAMESPACE, symbol="AAPL", end_date=datetime(2024, 3, 8), fmp_key="k"))
    assert out == {"ok": 1} and seen == [("payload", "AAPL"), ("splits", "AAPL")]
