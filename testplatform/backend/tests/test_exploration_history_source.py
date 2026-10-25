"""The exploration driver's OHLCV history source: stamp, identity, cache folder, certificate, store."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research.exploration import market_conditions as MC
from tools.strategy_research.exploration import profiles as P
from tools.strategy_research.exploration import run_exploration as D
from tools.strategy_research.exploration import runtime as R

ALPACA_STORE = "C:/stores/metric_store_alpaca_20261022"


def jobs(source, **kw):
    kw.setdefault("families", ["etf_trend"])
    if source == "alpaca" and "store" not in kw:
        kw["store"] = ALPACA_STORE
    return P.build_manifest(history_source=source, **kw)["jobs"]


@pytest.mark.parametrize("source", ["fmp", "alpaca"])
def test_every_job_is_stamped_with_the_source(source):
    for job in jobs(source, families=["large_ds", "etf_trend"]):
        assert job["optimization_config"]["backtest"]["ohlcv_history_source"] == source


def test_sources_never_share_a_fingerprint_name_or_backtest_label():
    fmp, alp = jobs("fmp")[0], jobs("alpaca")[0]
    assert fmp["fingerprint"] != alp["fingerprint"]
    assert fmp["name"] != alp["name"]
    assert "hsalpaca" in alp["name"] and "hsalpaca" not in fmp["name"]
    assert alp["optimization_config"]["backtest"]["name"] == alp["name"]


def test_unknown_source_refused():
    with pytest.raises(ValueError):
        P.build_manifest(history_source="yahoo")


def test_alpaca_requires_an_explicit_store_for_screened_families():
    with pytest.raises(ValueError, match="--store"):
        P.build_manifest(history_source="alpaca", families=["large_ds"])
    job = P.build_manifest(history_source="alpaca", families=["large_ds"], store=ALPACA_STORE)["jobs"][0]
    assert job["optimization_config"]["backtest"]["screener_opt"]["store"].endswith("metric_store_alpaca_20261022")


def test_fmp_keeps_the_snapshot_store_by_default():
    job = P.build_manifest(history_source="fmp", families=["large_ds"])["jobs"][0]
    assert job["optimization_config"]["backtest"]["screener_opt"]["store"].endswith("metric_store")


def test_cli_defaults_to_alpaca_and_derives_the_cache_dir():
    args = D.parser().parse_args([])
    assert args.ohlcv_history_source == "alpaca"
    assert D.resolve_cache_dir(args).name == "AlpacaHistoryOHLCVProvider"
    args = D.parser().parse_args(["--ohlcv-history-source", "fmp"])
    assert D.resolve_cache_dir(args).name == "FMPOHLCVProvider"


def test_cache_dir_of_the_other_source_is_refused(tmp_path):
    args = D.parser().parse_args(["--cache-dir", str(tmp_path / "FMPOHLCVProvider")])
    with pytest.raises(ValueError, match="AlpacaHistoryOHLCVProvider"):
        D.resolve_cache_dir(args)
    args = D.parser().parse_args(["--ohlcv-history-source", "fmp", "--cache-dir", str(tmp_path / "AlpacaHistoryOHLCVProvider")])
    with pytest.raises(ValueError, match="FMPOHLCVProvider"):
        D.resolve_cache_dir(args)


def test_main_refuses_mismatched_cache_dir_before_writing(tmp_path, capsys):
    rc = D.main(["--cache-dir", str(tmp_path / "FMPOHLCVProvider"), "--output-dir", str(tmp_path / "out"),
                 "--store", ALPACA_STORE, "--dry-run"])
    assert rc == 1 and not (tmp_path / "out").exists()


def test_preflight_refuses_a_job_whose_stamp_disagrees_with_the_folder(tmp_path):
    folder = tmp_path / "FMPOHLCVProvider"
    folder.mkdir()
    with pytest.raises(ValueError, match="AlpacaHistoryOHLCVProvider"):
        R.preflight(jobs("alpaca")[0], folder)


def test_missing_certificate_refused(tmp_path):
    folder = tmp_path / "AlpacaHistoryOHLCVProvider"
    folder.mkdir()
    with pytest.raises(RuntimeError, match="not certified"):
        R.preflight(jobs("alpaca")[0], folder)


def test_failed_certificate_refused(tmp_path):
    folder = tmp_path / "AlpacaHistoryOHLCVProvider"
    (folder / "_meta").mkdir(parents=True)
    (folder / "_meta" / "gate_certificate.json").write_text(
        json.dumps({"status": "FAIL", "failed": ["x"], "build_run_id": "r"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not certified"):
        R.preflight(jobs("alpaca")[0], folder)


def _write(folder, symbol, start="2018-01-01", end="2025-12-31"):
    for interval in ("1d", "5min"):
        pd.DataFrame({"Date": pd.to_datetime([start, "2020-01-02", end], utc=True)}).to_parquet(
            folder / f"{symbol}_{interval}.parquet")


@pytest.mark.parametrize("source,dirname", [("fmp", "FMPOHLCVProvider"), ("alpaca", "AlpacaHistoryOHLCVProvider")])
def test_preflight_reads_the_chosen_folder(monkeypatch, tmp_path, source, dirname):
    monkeypatch.setattr(R, "certify_history_cache", lambda s, d: None)
    monkeypatch.setattr(R, "code_signature", lambda: "v")
    folder = tmp_path / dirname
    folder.mkdir()
    job = jobs(source, etf_symbols=["AAA"])[0]
    with pytest.raises(ValueError, match="missing OHLCV"):
        R.preflight(job, folder)
    _write(folder, "AAA")
    for ref in ("SPY",):
        _write(folder, ref)
    ready = R.preflight(job, folder)
    assert ready["optimization_config"]["backtest"]["ohlcv_history_source"] == source
    assert ready["preflight"]["symbols"] == 1


def test_market_condition_files_come_from_the_source_folder(tmp_path):
    bt = {"market_condition_profiles": ["ohlcv-v1"], "ohlcv_history_source": "alpaca", "start_date": "2024-03-26",
          "end_date": "2024-03-28", "enabled_instruments": ["AAA"]}
    with pytest.raises(FileNotFoundError, match="AlpacaHistoryOHLCVProvider"):
        MC.preflight(bt, tmp_path)


def test_market_condition_provenance_uses_the_source_profile():
    from ba2_common.core.market_condition_source import source_profile_for_history
    pins = {"ohlcv-v1": "0" * 64}
    for source in ("fmp", "alpaca"):
        job = {"family": "t", "strategy": {"entry_rules": [{"conditions": {"type": "AND", "conditions": []},
                                                             "actions": [{"action_type": "buy"}]}], "exit_rules": []}}
        bt = {"experts": [{"settings": {}}]}
        MC.attach(job, bt, ("ohlcv-v1",), pins, "search", history_source=source)
        assert bt["market_condition"]["source_profile"] == source_profile_for_history(source)
