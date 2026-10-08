"""Launch- and job-start preflight: an intraday-reading job REFUSES a universe holding a symbol whose
intraday cache is on a different price level than its daily cache.

THE DEFECT (measured 2026-10-08): 7 of 250 random symbols had a 5-minute history a constant multiple (0.10 ..
6.25) of the daily one for a whole year; an intraday-clock backtest reads its decision price from the 5-minute
bars and its history (indicators, ATR, highs, TP/SL anchors) from the daily bars, so such a symbol was
silently wrong, and no launch check compared the two intervals' price levels.

WHO IS CHECKED, and what is not:
  * an intraday-clock job (``execution_interval`` sub-daily)                 -> checked, ``execution_interval``
  * a daily-clock job that prices OPTIONS (``options_cache_db``)             -> checked, 5m (the post-hoc
    intraday drawdown refinement re-prices flagged trades on the underlying's 5-minute bars)
  * a daily-clock EQUITY job                                                 -> reads no intraday bar: untouched
"""
from __future__ import annotations

import inspect
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from app.services.backtest import intraday_basis_preflight as pf
from app.services.job_fatal import JOB_FATAL_ERROR_TYPES, job_fatal
from ba2_common.core import native_cache, split_basis
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.split_basis import IntradayBasisMismatch, IntradayBasisStale
from ba2_providers.ohlcv import cross_interval_basis as cib

PROV = "FMPOHLCVProvider"


def _pair(first=date(2023, 1, 3), last=date(2023, 12, 29), seed=5):
    rng = np.random.default_rng(seed)
    price = 40.0
    d, i = [], []
    for _o, c in nyse_regular_sessions(first, last):
        local = c.astimezone(NY_TZ)
        n = (local.hour * 60 + local.minute - 570) // 5
        path = price * np.exp(np.cumsum(rng.normal(0.0001, 0.0006, n)))
        price = float(path[-1])
        stamps = [pd.Timestamp(local.date()) + pd.Timedelta(minutes=570 + 5 * k) for k in range(n)]
        i.append(pd.DataFrame({"Date": stamps, "Open": path, "High": path * 1.0003, "Low": path * 0.9997,
                               "Close": path, "Volume": 1000.0}))
        d.append({"Date": pd.Timestamp(local.date()), "Open": float(path[0]), "High": float(path.max() * 1.0003),
                  "Low": float(path.min() * 0.9997), "Close": float(path[-1]), "Volume": 1e6})
    return pd.DataFrame(d), pd.concat(i, ignore_index=True)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    (root / PROV).mkdir(parents=True)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(root))
    pf.reset_job_memo()
    cib._STORES.clear()
    yield root / PROV
    pf.reset_job_memo()
    cib._STORES.clear()


def _put(folder, symbol, factor=1.0):
    daily, intra = _pair()
    for c in ("Open", "High", "Low", "Close"):
        intra[c] = intra[c] * factor
    daily.assign(effective_date=daily["Date"]).to_parquet(folder / f"{symbol}_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(folder / f"{symbol}_5min.parquet", index=False)


def cfg(symbols, interval="5min", options=False):
    c = {"enabled_instruments": list(symbols), "execution_interval": interval,
         "start_date": datetime(2023, 3, 1), "end_date": datetime(2023, 12, 29), "warmup_days": 30}
    if options:
        c["options_cache_db"] = "/tmp/x.db"
    return c


# ------------------------------------------------------------------------------------------ who is checked
def test_a_daily_clock_equity_job_reads_no_intraday_bar_and_is_untouched(cache):
    _put(cache, "BAD", 0.5)
    assert pf.interval_to_check(cfg(["BAD"], "1d")) is None
    assert pf.require_for_config(cfg(["BAD"], "1d")) is None          # even with a wrong-basis 5-minute file


def test_an_intraday_clock_job_and_a_daily_clock_options_job_are_checked(cache):
    assert pf.interval_to_check(cfg(["A"], "5min")) == "5min"
    assert pf.interval_to_check(cfg(["A"], "15min")) == "15min"
    assert pf.interval_to_check(cfg(["A"], "1d", options=True)) == "5m"
    _put(cache, "OPT", 0.5)
    with pytest.raises(IntradayBasisMismatch, match="OPT"):
        pf.require_for_config(cfg(["OPT"], "1d", options=True))


# ------------------------------------------------------------------------------------------ the refusal
def test_a_healthy_universe_passes_and_the_report_is_recorded_on_the_result(cache):
    _put(cache, "GOOD1")
    _put(cache, "GOOD2")
    rep = pf.require_for_config(cfg(["GOOD1", "GOOD2"]))
    assert rep is not None and not rep.refused and rep.counts["ok"] == 2
    d = rep.to_dict()
    assert d["symbols"] == 2 and d["interval"] == "5min" and d["unjudged"] == [] and d["unjudged_count"] == 0


def test_a_mismatched_symbol_refuses_with_symbol_factor_and_class(cache):
    _put(cache, "GOOD")
    _put(cache, "DDLIKE", 1 / 3)
    _put(cache, "SIRILIKE", 0.1)
    with pytest.raises(IntradayBasisMismatch) as ei:
        pf.require_for_config(cfg(["GOOD", "DDLIKE", "SIRILIKE"]))
    msg = str(ei.value)
    assert "2 of 3 symbols" in msg
    assert "DDLIKE: constant_factor x0.333" in msg and "SIRILIKE: constant_factor x0.1" in msg
    assert "GOOD:" not in msg
    assert "--exclude-symbols" in msg            # the recorded way to run without them: the only escape


def test_the_warmup_range_is_part_of_the_window(cache):
    # bad ONLY in January 2023; the job window starts 2023-03-01 with 30 warm-up days -> 2023-01-30..
    daily, intra = _pair()
    jan = intra["Date"] < "2023-02-15"
    for c in ("Open", "High", "Low", "Close"):
        intra.loc[jan, c] = intra.loc[jan, c] * 4.0
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "EARLY_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(cache / "EARLY_5min.parquet", index=False)
    pf.reset_job_memo()
    with pytest.raises(IntradayBasisMismatch, match="EARLY"):
        pf.require_for_config({**cfg(["EARLY"]), "warmup_days": 45})   # reads back into the defect
    pf.reset_job_memo()
    assert pf.require_for_config({**cfg(["EARLY"]), "warmup_days": 0}) is not None  # starts after it


def test_a_stale_marker_refuses_even_when_the_prices_agree(cache):
    _put(cache, "MARKED")
    split_basis.write_intraday_stale(str(cache / "MARKED_5min.parquet"), reason="daily history replaced")
    with pytest.raises(IntradayBasisStale, match="MARKED"):
        pf.require_for_config(cfg(["MARKED"]))


def test_symbols_that_cannot_be_judged_are_reported_never_ok_and_not_refused(cache):
    _put(cache, "GOOD")
    rep = pf.require_for_config(cfg(["GOOD", "NOFILES"]))
    assert rep.counts["ok"] == 1 and rep.counts["no_daily"] == 1
    assert [r.symbol for r in rep.unjudged] == ["NOFILES"]
    assert rep.to_dict()["unjudged"][0]["class"] == "no_daily"


# ------------------------------------------------------------------------------------------ cost
def test_the_verdict_is_computed_once_per_process_per_job_not_per_trial(cache):
    _put(cache, "AAA")
    _put(cache, "BBB")
    store = cib.store_for(PROV)
    pf.require_for_config(cfg(["AAA", "BBB"]))
    reads = store.reads
    assert reads == 2
    for _ in range(50):                                            # 50 GA trials of the same job
        pf.require_for_config(cfg(["AAA", "BBB"]))
    assert store.reads == reads and store.mem_hits == 0            # not even re-statted: the job memo answered
    # a job on the same universe with a different window needs no parquet read either (per-file memo)
    pf.require_for_config({**cfg(["AAA", "BBB"]), "end_date": datetime(2023, 11, 30)})
    assert store.reads == reads and store.mem_hits == 2


def test_a_rewritten_file_is_rescanned_never_answered_from_a_stale_verdict(cache):
    _put(cache, "AAA")
    pf.require_for_config(cfg(["AAA"]))
    _put(cache, "AAA", 0.5)                                        # the 5-minute file is replaced
    pf.reset_job_memo()                                            # a new job
    with pytest.raises(IntradayBasisMismatch):
        pf.require_for_config(cfg(["AAA"]))


# ------------------------------------------------------------------------------------------ job-fatal path
def test_both_refusals_are_job_fatal_by_name():
    for exc in (IntradayBasisMismatch("x"), IntradayBasisStale("x")):
        assert job_fatal(exc) and job_fatal(type(exc).__name__)
    assert {"IntradayBasisMismatch", "IntradayBasisStale"} <= JOB_FATAL_ERROR_TYPES


def test_the_job_start_runs_the_preflight_before_any_bar_is_loaded():
    from app.services.backtest import daily_backtest_handler as h
    src = inspect.getsource(h.run_daily_backtest)
    assert "require_for_config(config)" in src
    assert src.index("require_for_config(config)") < src.index("ps.preload(")
    assert 'results["intraday_basis_preflight"]' in src                  # recorded on the result


def test_a_trial_that_hits_the_refusal_is_reported_fatal_to_the_master(cache):
    """The worker's trial wrapper classifies by the exception's NAME (the object does not cross the process
    boundary): ``fatal`` + ``error_type`` ride the result, and the master aborts the job on the first one."""
    _put(cache, "BADONE", 0.5)
    try:
        pf.require_for_config(cfg(["BADONE"]))
    except Exception as e:  # noqa: BLE001 -- reproducing the wrapper's classification
        out = {"ok": False, "fatal": job_fatal(e), "error_type": type(e).__name__, "error": str(e)}
    assert out["fatal"] and out["error_type"] == "IntradayBasisMismatch"
    from app.services.strategy_optimization_handler import _FatalTrialError, _abort_on_fatal_trial
    with pytest.raises(_FatalTrialError, match="BADONE"):
        _abort_on_fatal_trial(out, {"msg": None}, key=0, flat={})


# ------------------------------------------------------------------------------------------ launch refusal
def test_the_launcher_refuses_early_with_the_list_and_has_no_skip_flag(cache):
    import ba2test_launcher as L
    _put(cache, "GOOD")
    _put(cache, "SPLIT", 0.5)
    block = {**cfg(["GOOD", "SPLIT"]), "start_date": "2023-03-01", "end_date": "2023-12-29"}
    with pytest.raises(SystemExit) as ei:
        L._refuse_intraday_basis_mismatch("optimize", block)
    assert "REFUSED" in str(ei.value) and "SPLIT: constant_factor x0.5" in str(ei.value)
    # the recorded way out: exclude the symbol; the check then passes
    pf.reset_job_memo()
    ok = {**block, "enabled_instruments": ["GOOD"]}
    L._refuse_intraday_basis_mismatch("optimize", ok)
    # a daily-clock equity job is not asked at all
    L._refuse_intraday_basis_mismatch("optimize", {**block, "execution_interval": "1d"})
    src = inspect.getsource(L)
    assert "BA2_SKIP_BASIS" not in src and "skip-basis-check" not in src.lower()
